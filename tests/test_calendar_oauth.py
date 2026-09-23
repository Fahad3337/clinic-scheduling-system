"""OAuth authorization flow tests.

WHY A FAKE GOOGLE RATHER THAN MOCKING httpx: the interesting behaviour is
ours, not Google's -- state single-use, the refresh-token-preservation rule,
scope checking, account-change handling. A fake client that returns
controlled GoogleTokens lets each of those be provoked deliberately. Mocking
at the HTTP layer would mostly test that we can build a form body.

Nothing here touches the network.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.integrations.google_oauth import (
    REQUIRED_SCOPES,
    GoogleOAuthError,
    GoogleTokens,
    PKCEChallenge,
)
from app.models.calendar_connection import CalendarConnection
from app.models.enums import CalendarConnectionState, CalendarProvider
from app.models.external_busy_block import ExternalBusyBlock
from app.models.oauth_state import OAuthState
from app.services import calendar_oauth_service
from app.services.exceptions import (
    CalendarAuthorizationError,
    CalendarNotConnectedError,
    DoctorNotFoundError,
    InsufficientCalendarScopeError,
    InvalidOAuthStateError,
)

GRANTED = ("openid", "email", *REQUIRED_SCOPES)


class FakeGoogleOAuthClient:
    """Stands in for Google. Records what it was asked, returns what we tell it."""

    redirect_uri = "http://testserver/api/v1/calendar/oauth/callback"

    def __init__(self, tokens: GoogleTokens | None = None, raises: Exception | None = None):
        self._tokens = tokens
        self._raises = raises
        self.exchange_calls: list[dict] = []
        self.authorize_calls: list[dict] = []

    def build_authorization_url(self, *, state: str, code_challenge: str) -> str:
        self.authorize_calls.append({"state": state, "code_challenge": code_challenge})
        return f"https://accounts.google.com/fake?state={state}&code_challenge={code_challenge}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> GoogleTokens:
        self.exchange_calls.append(
            {"code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri}
        )
        if self._raises:
            raise self._raises
        assert self._tokens is not None
        return self._tokens


def make_tokens(
    *, refresh_token: str | None = "refresh-abc", email: str = "rao@example.com", scopes=GRANTED
) -> GoogleTokens:
    return GoogleTokens(
        access_token="access-xyz",
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=tuple(scopes),
        account_email=email,
    )


# ---------------------------------------------------------------------- #
# PKCE
# ---------------------------------------------------------------------- #


def test_pkce_challenge_is_correct_s256():
    """The challenge must be base64url(sha256(verifier)) with padding stripped."""
    import base64
    import hashlib

    pkce = PKCEChallenge.generate()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pkce.verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert pkce.challenge == expected
    assert "=" not in pkce.challenge  # padding would be rejected by Google
    assert 43 <= len(pkce.verifier) <= 128  # RFC 7636 bounds


def test_pkce_verifiers_are_unique():
    assert len({PKCEChallenge.generate().verifier for _ in range(100)}) == 100


# ---------------------------------------------------------------------- #
# start_authorization
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_authorization_persists_state_and_returns_url(db, doctor):
    fake = FakeGoogleOAuthClient()
    start = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)

    assert start.authorization_url.startswith("https://accounts.google.com/fake")
    assert start.state in start.authorization_url

    row = await db.scalar(select(OAuthState).where(OAuthState.state_token == start.state))
    assert row is not None
    assert row.doctor_id == doctor.id
    assert row.consumed_at is None
    # The verifier must be stored, and must NOT be the challenge we sent out.
    assert row.code_verifier
    assert row.code_verifier != fake.authorize_calls[0]["code_challenge"]


@pytest.mark.asyncio
async def test_code_verifier_is_encrypted_at_rest(db, doctor):
    """The PKCE secret must not be readable in the raw column."""
    fake = FakeGoogleOAuthClient()
    start = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)
    row = await db.scalar(select(OAuthState).where(OAuthState.state_token == start.state))
    plaintext = row.code_verifier

    stored = (
        await db.execute(
            text("SELECT code_verifier FROM oauth_states WHERE state_token = :s"),
            {"s": start.state},
        )
    ).scalar_one()
    assert stored != plaintext
    assert plaintext not in stored


@pytest.mark.asyncio
async def test_start_authorization_unknown_doctor_raises(db):
    with pytest.raises(DoctorNotFoundError):
        await calendar_oauth_service.start_authorization(
            db, doctor_id=uuid.uuid4(), client=FakeGoogleOAuthClient()
        )


# ---------------------------------------------------------------------- #
# complete_authorization -- state handling
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_complete_authorization_creates_connection(db, doctor):
    fake = FakeGoogleOAuthClient(tokens=make_tokens())
    start = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)

    conn = await calendar_oauth_service.complete_authorization(
        db, state_token=start.state, code="auth-code-1", client=fake
    )

    assert conn.doctor_id == doctor.id
    assert conn.provider is CalendarProvider.GOOGLE
    assert conn.account_email == "rao@example.com"
    assert conn.refresh_token == "refresh-abc"
    assert conn.state is CalendarConnectionState.ACTIVE
    # PKCE verifier from the stored state must have been sent, not the challenge
    assert fake.exchange_calls[0]["code_verifier"]
    assert fake.exchange_calls[0]["redirect_uri"] == fake.redirect_uri


@pytest.mark.asyncio
async def test_state_is_single_use(db, doctor):
    """Replaying a state token must fail -- this is the CSRF guarantee."""
    fake = FakeGoogleOAuthClient(tokens=make_tokens())
    start = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)

    await calendar_oauth_service.complete_authorization(
        db, state_token=start.state, code="code-1", client=fake
    )
    with pytest.raises(InvalidOAuthStateError):
        await calendar_oauth_service.complete_authorization(
            db, state_token=start.state, code="code-2", client=fake
        )
    # The replay must not have reached Google at all.
    assert len(fake.exchange_calls) == 1


@pytest.mark.asyncio
async def test_unknown_state_rejected(db):
    with pytest.raises(InvalidOAuthStateError):
        await calendar_oauth_service.complete_authorization(
            db, state_token="never-issued", code="c", client=FakeGoogleOAuthClient()
        )


@pytest.mark.asyncio
async def test_expired_state_rejected(db, doctor):
    fake = FakeGoogleOAuthClient(tokens=make_tokens())
    start = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)

    row = await db.scalar(select(OAuthState).where(OAuthState.state_token == start.state))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.add(row)
    await db.commit()

    with pytest.raises(InvalidOAuthStateError):
        await calendar_oauth_service.complete_authorization(
            db, state_token=start.state, code="c", client=fake
        )


@pytest.mark.asyncio
async def test_state_is_burned_even_when_google_fails(db, doctor):
    """A failed exchange must not leave the state replayable."""
    good = FakeGoogleOAuthClient(tokens=make_tokens())
    start = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=good)

    failing = FakeGoogleOAuthClient(raises=GoogleOAuthError("invalid_grant"))
    with pytest.raises(CalendarAuthorizationError):
        await calendar_oauth_service.complete_authorization(
            db, state_token=start.state, code="bad", client=failing
        )

    with pytest.raises(InvalidOAuthStateError):
        await calendar_oauth_service.complete_authorization(
            db, state_token=start.state, code="retry", client=good
        )


# ---------------------------------------------------------------------- #
# Scope and refresh-token rules
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_calendar_scope_rejected(db, doctor):
    """A doctor who unticks calendar access must not get a broken connection."""
    fake = FakeGoogleOAuthClient(tokens=make_tokens(scopes=("openid", "email")))
    start = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)

    with pytest.raises(InsufficientCalendarScopeError):
        await calendar_oauth_service.complete_authorization(
            db, state_token=start.state, code="c", client=fake
        )
    assert await db.scalar(select(CalendarConnection)) is None


@pytest.mark.asyncio
async def test_first_connection_without_refresh_token_rejected(db, doctor):
    fake = FakeGoogleOAuthClient(tokens=make_tokens(refresh_token=None))
    start = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)

    with pytest.raises(CalendarAuthorizationError, match="refresh token"):
        await calendar_oauth_service.complete_authorization(
            db, state_token=start.state, code="c", client=fake
        )


@pytest.mark.asyncio
async def test_reauth_without_refresh_token_preserves_existing(db, doctor):
    """THE classic Google bug: re-auth omits the refresh token.

    If we blindly stored the response we would null the only credential that
    can keep this doctor syncing.
    """
    fake = FakeGoogleOAuthClient(tokens=make_tokens(refresh_token="original-refresh"))
    s1 = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)
    await calendar_oauth_service.complete_authorization(
        db, state_token=s1.state, code="c1", client=fake
    )

    fake2 = FakeGoogleOAuthClient(tokens=make_tokens(refresh_token=None))
    s2 = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake2)
    conn = await calendar_oauth_service.complete_authorization(
        db, state_token=s2.state, code="c2", client=fake2
    )

    assert conn.refresh_token == "original-refresh"  # preserved, not clobbered
    assert conn.access_token == "access-xyz"  # but the access token IS refreshed


@pytest.mark.asyncio
async def test_switching_google_account_resets_sync_state(db, doctor):
    """Mirrored data from the OLD calendar must stop blocking availability."""
    fake = FakeGoogleOAuthClient(tokens=make_tokens(email="old@example.com"))
    s1 = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)
    conn = await calendar_oauth_service.complete_authorization(
        db, state_token=s1.state, code="c1", client=fake
    )

    conn.sync_token = "old-sync-cursor"
    db.add(conn)
    start = datetime.now(UTC) + timedelta(days=1)
    db.add(
        ExternalBusyBlock(
            connection_id=conn.id,
            doctor_id=doctor.id,
            external_event_id="from-old-account",
            starts_at=start,
            ends_at=start + timedelta(minutes=30),
            synced_at=datetime.now(UTC),
        )
    )
    await db.commit()

    fake2 = FakeGoogleOAuthClient(
        tokens=make_tokens(email="new@example.com", refresh_token="new-refresh")
    )
    s2 = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake2)
    conn2 = await calendar_oauth_service.complete_authorization(
        db, state_token=s2.state, code="c2", client=fake2
    )

    assert conn2.account_email == "new@example.com"
    assert conn2.sync_token is None  # cursor from the old account is meaningless
    blocks = (await db.scalars(select(ExternalBusyBlock))).all()
    assert len(blocks) == 1
    assert blocks[0].deleted_at is not None  # soft-deleted, not blocking anymore


# ---------------------------------------------------------------------- #
# Disconnect
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disconnect_destroys_credentials_but_keeps_row(db, doctor):
    fake = FakeGoogleOAuthClient(tokens=make_tokens())
    s1 = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)
    conn = await calendar_oauth_service.complete_authorization(
        db, state_token=s1.state, code="c1", client=fake
    )
    start = datetime.now(UTC) + timedelta(days=1)
    db.add(
        ExternalBusyBlock(
            connection_id=conn.id, doctor_id=doctor.id, external_event_id="e1",
            starts_at=start, ends_at=start + timedelta(minutes=30), synced_at=datetime.now(UTC),
        )
    )
    await db.commit()

    result = await calendar_oauth_service.disconnect(db, doctor_id=doctor.id)

    assert result.state is CalendarConnectionState.DISABLED
    assert result.refresh_token is None
    assert result.access_token is None
    assert result.sync_token is None
    # Row survives so conflict history stays interpretable.
    assert await db.scalar(select(CalendarConnection)) is not None
    blocks = (await db.scalars(select(ExternalBusyBlock))).all()
    assert all(b.deleted_at is not None for b in blocks)


@pytest.mark.asyncio
async def test_disconnect_when_not_connected_raises(db, doctor):
    with pytest.raises(CalendarNotConnectedError):
        await calendar_oauth_service.disconnect(db, doctor_id=doctor.id)


@pytest.mark.asyncio
async def test_reconnect_after_disconnect_requires_new_refresh_token(db, doctor):
    """Disconnect nulls the credential, so a token-less re-auth cannot revive it."""
    fake = FakeGoogleOAuthClient(tokens=make_tokens())
    s1 = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=fake)
    await calendar_oauth_service.complete_authorization(db, state_token=s1.state, code="c1", client=fake)
    await calendar_oauth_service.disconnect(db, doctor_id=doctor.id)

    tokenless = FakeGoogleOAuthClient(tokens=make_tokens(refresh_token=None))
    s2 = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=tokenless)
    with pytest.raises(CalendarAuthorizationError, match="refresh token"):
        await calendar_oauth_service.complete_authorization(
            db, state_token=s2.state, code="c2", client=tokenless
        )

    # ...and a proper re-auth works.
    good = FakeGoogleOAuthClient(tokens=make_tokens(refresh_token="fresh-refresh"))
    s3 = await calendar_oauth_service.start_authorization(db, doctor_id=doctor.id, client=good)
    conn = await calendar_oauth_service.complete_authorization(
        db, state_token=s3.state, code="c3", client=good
    )
    assert conn.state is CalendarConnectionState.ACTIVE
    assert conn.refresh_token == "fresh-refresh"
