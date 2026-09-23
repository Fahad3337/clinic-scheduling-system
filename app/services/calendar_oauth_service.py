"""Connecting and disconnecting a doctor's Google Calendar.

Transport-agnostic, like every other service here: it takes a session and
plain values, raises domain exceptions, and knows nothing about HTTP. The
Phase 3 chatbot will call `start_authorization` to produce a link it can text
to a doctor, using this same code path.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.google_oauth import (
    REQUIRED_SCOPES,
    GoogleOAuthClient,
    GoogleOAuthError,
    GoogleTokens,
    PKCEChallenge,
)
from app.models.calendar_connection import CalendarConnection
from app.models.doctor import Doctor
from app.models.enums import CalendarConnectionState, CalendarProvider
from app.models.external_busy_block import ExternalBusyBlock
from app.models.oauth_state import OAuthState
from app.services.exceptions import (
    CalendarAuthorizationError,
    CalendarNotConnectedError,
    DoctorNotFoundError,
    InsufficientCalendarScopeError,
    InvalidOAuthStateError,
)


@dataclass(frozen=True)
class AuthorizationStart:
    authorization_url: str
    state: str
    expires_at: datetime


async def start_authorization(
    session: AsyncSession,
    *,
    doctor_id: UUID,
    client: GoogleOAuthClient | None = None,
) -> AuthorizationStart:
    """Begin the OAuth dance: mint state + PKCE, return the consent URL.

    SECURITY ASSUMPTION, AND IT IS THE BIG ONE: this function does not check
    WHO is asking. Phase 1 shipped with no authentication and Phase 2 has not
    added any, so any caller who knows a doctor_id can generate a consent
    link for that doctor.

    That is less catastrophic than it sounds -- the link only does anything
    if a human completes Google's consent screen with their own credentials,
    and the resulting tokens are bound to the doctor_id baked into the state
    at THIS moment, not to anything the callback supplies. So an attacker
    cannot attach their own calendar to a doctor without that doctor
    personally consenting.

    What it DOES enable: an attacker can spam consent links, and can phish a
    doctor with a link that genuinely originates from this server. Before
    this goes anywhere near production, these endpoints need authentication
    and an authorization check that the caller IS that doctor or is clinic
    staff. FLAGGED as the top security item for Phase 3.
    """
    settings = get_settings()
    client = client or GoogleOAuthClient()

    doctor = await session.get(Doctor, doctor_id)
    if doctor is None:
        raise DoctorNotFoundError(doctor_id)

    pkce = PKCEChallenge.generate()
    # token_urlsafe(32) -> 256 bits of CSPRNG entropy. Not uuid4(): UUIDs are
    # for identifying things, not for being unguessable, and v4 gives 122
    # bits with a recognizable shape.
    state_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.oauth_state_ttl_minutes)

    session.add(
        OAuthState(
            state_token=state_token,
            doctor_id=doctor_id,
            code_verifier=pkce.verifier,
            redirect_uri=client.redirect_uri,
            expires_at=expires_at,
        )
    )
    await session.commit()

    url = client.build_authorization_url(state=state_token, code_challenge=pkce.challenge)
    return AuthorizationStart(authorization_url=url, state=state_token, expires_at=expires_at)


async def _consume_state(session: AsyncSession, state_token: str) -> OAuthState:
    """Atomically claim a state row, or raise.

    The claim is a single conditional UPDATE rather than SELECT-then-UPDATE,
    for the same reason booking uses a constraint and notifications use
    ON CONFLICT: a read followed by a write is not atomic, and two callbacks
    arriving together would both pass a `consumed_at IS NULL` check. Here the
    database decides the winner, and the loser gets zero rows back.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        update(OAuthState)
        .where(
            OAuthState.state_token == state_token,
            OAuthState.consumed_at.is_(None),
            OAuthState.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(OAuthState)
    )
    row = result.scalar_one_or_none()
    if row is None:
        # Unknown / expired / replayed all land here on purpose -- see
        # InvalidOAuthStateError's docstring.
        raise InvalidOAuthStateError()
    return row


async def complete_authorization(
    session: AsyncSession,
    *,
    state_token: str,
    code: str,
    client: GoogleOAuthClient | None = None,
) -> CalendarConnection:
    """Finish the dance: validate state, exchange the code, store the tokens."""
    client = client or GoogleOAuthClient()

    oauth_state = await _consume_state(session, state_token)
    # Committing the consumption immediately, BEFORE the network call to
    # Google, is deliberate: if the exchange fails or the process dies, the
    # state must stay burned. Leaving it reusable would turn a failed
    # exchange into a replay window.
    await session.commit()

    try:
        tokens: GoogleTokens = await client.exchange_code(
            code=code,
            code_verifier=oauth_state.code_verifier,
            redirect_uri=oauth_state.redirect_uri,
        )
    except GoogleOAuthError as exc:
        raise CalendarAuthorizationError(str(exc)) from exc

    missing = tuple(s for s in REQUIRED_SCOPES if s not in tokens.scopes)
    if missing:
        # The doctor unticked a permission on the consent screen. Storing the
        # connection anyway would mean every sync fails with a 403 forever
        # and nobody knows why; better to refuse now with a message that
        # says what to do.
        raise InsufficientCalendarScopeError(missing)

    existing = await session.scalar(
        select(CalendarConnection).where(CalendarConnection.doctor_id == oauth_state.doctor_id)
    )

    if existing is None:
        if tokens.refresh_token is None:
            # Nothing to fall back on. Refusing beats storing a connection
            # that can never refresh and will die in an hour.
            raise CalendarAuthorizationError(
                "Google did not return a refresh token; re-authorize and accept the consent screen"
            )
        connection = CalendarConnection(
            doctor_id=oauth_state.doctor_id,
            provider=CalendarProvider.GOOGLE,
            account_email=tokens.account_email,
            refresh_token=tokens.refresh_token,
            access_token=tokens.access_token,
            access_token_expires_at=tokens.expires_at,
            granted_scopes=" ".join(tokens.scopes),
            state=CalendarConnectionState.ACTIVE,
        )
        session.add(connection)
        await session.commit()
        await session.refresh(connection)
        return connection

    # --- Re-authorization of an existing connection -------------------- #
    account_changed = existing.account_email != tokens.account_email

    # THE REFRESH TOKEN RULE: never overwrite a good refresh token with None.
    # Google omits it from the response when it decides the client already
    # has one. We force prompt=consent to make that unlikely, but "unlikely"
    # is not "never", and blanking this column costs the doctor a re-auth
    # and silently stops their sync.
    if tokens.refresh_token is not None:
        existing.refresh_token = tokens.refresh_token
    elif account_changed:
        # A different Google account with no new refresh token is the one
        # case we cannot paper over: the stored token belongs to the OLD
        # account and is useless for the new one.
        raise CalendarAuthorizationError(
            "Connected a different Google account but received no refresh token; "
            "revoke this app's access in that account's settings and try again"
        )
    elif existing.refresh_token is None:
        # Reconnecting a previously DISCONNECTED calendar: disconnect nulls
        # the credential, so there is nothing to preserve and Google gave us
        # nothing new. Reactivating here would violate the
        # active_requires_refresh_token CHECK -- better to say why.
        raise CalendarAuthorizationError(
            "Google did not return a refresh token; revoke this app's access "
            "in your Google account settings, then authorize again"
        )

    existing.account_email = tokens.account_email
    existing.access_token = tokens.access_token
    existing.access_token_expires_at = tokens.expires_at
    existing.granted_scopes = " ".join(tokens.scopes)
    existing.state = CalendarConnectionState.ACTIVE
    existing.consecutive_failures = 0
    existing.last_error = None

    if account_changed:
        # Everything we mirrored belongs to a DIFFERENT calendar now. Keeping
        # it would block slots based on a stranger's schedule, and the stored
        # sync_token is meaningless against the new account.
        #
        # Soft-delete rather than DELETE: schedule_conflicts references these
        # rows with RESTRICT (see migration 0003 / the busy block model).
        existing.sync_token = None
        existing.last_full_sync_at = None
        await session.execute(
            update(ExternalBusyBlock)
            .where(
                ExternalBusyBlock.connection_id == existing.id,
                ExternalBusyBlock.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(UTC))
        )

    await session.commit()
    await session.refresh(existing)
    return existing


async def get_connection(session: AsyncSession, *, doctor_id: UUID) -> CalendarConnection:
    connection = await session.scalar(
        select(CalendarConnection).where(CalendarConnection.doctor_id == doctor_id)
    )
    if connection is None:
        raise CalendarNotConnectedError(doctor_id)
    return connection


async def disconnect(session: AsyncSession, *, doctor_id: UUID) -> CalendarConnection:
    """Destroy a doctor's stored credentials and stop syncing them.

    IMPLEMENTED AS A SOFT DISABLE, NOT A DELETE. The row survives with its
    secrets nulled and state=DISABLED.

    WHY NOT DELETE THE ROW: calendar_connections CASCADEs to
    external_busy_blocks, and schedule_conflicts references those with
    RESTRICT (migration 0003). A hard delete therefore raises a
    ForeignKeyViolation for precisely the doctors who have had a booking
    collide with their personal calendar -- the ones whose audit trail
    matters most. Nulling the credential achieves the actual security goal
    (we no longer hold a usable token) without destroying history.

    Busy blocks are soft-deleted so they immediately stop blocking
    availability. They are not hard-deleted, for the same RESTRICT reason.

    ASSUMPTION / KNOWN GAP: this does NOT call Google's token revocation
    endpoint, so the grant stays listed in the doctor's Google account until
    they remove it by hand. Revoking is one more HTTPS call and belongs
    here; it is deferred to the Google client wrapper step where the
    authenticated-request plumbing will live. FLAGGED, because calling this
    "disconnect" while leaving a live grant on Google's side is generous
    wording and a privacy reviewer would say so.
    """
    connection = await get_connection(session, doctor_id=doctor_id)

    await session.execute(
        update(ExternalBusyBlock)
        .where(
            ExternalBusyBlock.connection_id == connection.id,
            ExternalBusyBlock.deleted_at.is_(None),
        )
        .values(deleted_at=datetime.now(UTC))
    )

    # Order matters for the CHECK: state must leave 'active' in the same
    # UPDATE that clears the token, which SQLAlchemy flushes atomically.
    connection.state = CalendarConnectionState.DISABLED
    connection.refresh_token = None
    connection.access_token = None
    connection.access_token_expires_at = None
    connection.sync_token = None
    connection.last_full_sync_at = None

    await session.commit()
    await session.refresh(connection)
    return connection
