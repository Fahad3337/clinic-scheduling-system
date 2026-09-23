"""Producing a usable access token for a calendar connection.

Everything that touches Google needs a live access token. Access tokens last
an hour; refresh tokens last until revoked. This module owns that
transition, including what to do when it fails permanently.

THE REFRESH STRATEGY, and why each part is the way it is
---------------------------------------------------------
1. PROACTIVE, not reactive. We refresh when the token is within
   `TOKEN_EXPIRY_SKEW` of expiring rather than waiting for a 401.

   WHY: a purely reactive scheme fails one call every hour, per connection,
   by design -- and each of those failures costs a wasted API call plus a
   retry. It also breaks whenever our clock and Google's disagree, which
   they always do slightly. A skew buffer makes the boundary a non-event.

2. REACTIVE ANYWAY, as a fallback. A token can die early (revoked mid-hour,
   password change). Callers catch GoogleAccessTokenExpiredError, call
   `force_refresh`, and retry the operation ONCE. Not in a loop: if a
   freshly minted token is also rejected, the problem is not staleness and
   retrying will not discover that.

3. SERIALIZED WITH SELECT ... FOR UPDATE. Two jobs refreshing the same
   connection simultaneously both POST to Google, and we then race to write
   two different access tokens into one row -- the loser's token is live but
   unrecorded, and if Google ever rotates the refresh token the loser's
   write can persist a superseded one and lock the doctor out.

   The lock is the SAME primitive Phase 1 used for double-booking, for the
   same reason: a read-then-write on shared state is not atomic. Contention
   here is per-connection, so doctors never block each other.

4. TERMINAL FAILURES STOP THE CONNECTION. invalid_grant is not retryable.
   The connection goes to NEEDS_REAUTH, which the sync job skips, and a
   human is told. Retrying it forever is the classic version of this bug.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.google_calendar import (
    GoogleApiError,
    GoogleAuthRevokedError,
    GoogleCalendarClient,
)
from app.models.calendar_connection import CalendarConnection
from app.models.enums import CalendarConnectionState
from app.services.exceptions import CalendarNotConnectedError, DomainError

# Refresh this far before actual expiry. Covers clock skew between us and
# Google plus the duration of whatever call we are about to make -- a token
# with 40 seconds left is not much use for a paginated sync.
TOKEN_EXPIRY_SKEW = timedelta(minutes=5)


class CalendarNeedsReauthorizationError(DomainError):
    """The doctor must personally re-authorize; no retry will help."""

    def __init__(self, doctor_id, detail: str = "") -> None:
        self.doctor_id = doctor_id
        self.detail = detail
        super().__init__(
            f"Calendar for doctor {doctor_id} needs re-authorization. {detail}".strip()
        )


class CalendarConnectionDisabledError(DomainError):
    def __init__(self, doctor_id) -> None:
        self.doctor_id = doctor_id
        super().__init__(f"Calendar for doctor {doctor_id} is disabled")


def _is_fresh(connection: CalendarConnection, *, now: datetime) -> bool:
    return (
        connection.access_token is not None
        and connection.access_token_expires_at is not None
        and connection.access_token_expires_at - TOKEN_EXPIRY_SKEW > now
    )


async def get_valid_access_token(
    session: AsyncSession,
    *,
    connection_id: UUID,
    client: GoogleCalendarClient | None = None,
    force: bool = False,
) -> str:
    """Return a token good for at least TOKEN_EXPIRY_SKEW, refreshing if needed.

    Commits when it refreshes, so the new token is durable before any caller
    uses it. If we handed back a token that only existed in an uncommitted
    transaction and then rolled back, the next call would refresh again --
    harmless but wasteful, and it would mask how often we are refreshing.
    """
    settings = get_settings()
    client = client or GoogleCalendarClient()
    now = datetime.now(UTC)

    # Cheap path first: a read with no lock. The overwhelming majority of
    # calls land here, and taking a row lock every time would serialize
    # every operation on a connection for no reason.
    connection = await session.get(CalendarConnection, connection_id)
    if connection is None:
        raise CalendarNotConnectedError(connection_id)
    _assert_usable(connection)
    if not force and _is_fresh(connection, now=now):
        return connection.access_token  # type: ignore[return-value]

    # Slow path: lock the row, then RE-CHECK. Another worker may have
    # refreshed while we waited for the lock, in which case we do nothing --
    # the classic double-checked pattern.
    #
    # CORRECTION (2026-09-21): this comment previously claimed the pattern
    # was "correct because the second check happens under the lock". That
    # was not true as written. The cheap path above loads this connection
    # into the session's identity map, so without populate_existing the
    # locking SELECT below returned that SAME stale object -- the re-check
    # re-read a pre-lock copy and concluded the token was still stale, and
    # two workers would both refresh. Taking the lock is necessary but not
    # sufficient; re-reading the locked row is the other half, and it is
    # not automatic. See conversation_service.load_active_conversation for
    # the case where this cost a real lost update.
    locked = await session.scalar(
        select(CalendarConnection)
        .where(CalendarConnection.id == connection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise CalendarNotConnectedError(connection_id)
    _assert_usable(locked)

    now = datetime.now(UTC)
    if not force and _is_fresh(locked, now=now):
        await session.commit()  # release the lock promptly
        return locked.access_token  # type: ignore[return-value]

    if locked.refresh_token is None:
        # Should be impossible: the active_requires_refresh_token CHECK
        # forbids it. Belt and braces, because being wrong here means
        # handing back None and failing with an AttributeError deep inside
        # an HTTP call.
        await session.commit()
        raise CalendarNeedsReauthorizationError(locked.doctor_id, "no refresh token stored")

    try:
        refreshed = await client.refresh_access_token(
            refresh_token=locked.refresh_token,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret.get_secret_value(),
        )
    except GoogleAuthRevokedError as exc:
        # TERMINAL. Park the connection and stop. Note we clear the access
        # token but KEEP the refresh token: it is useless for minting
        # tokens, but retaining it lets a human see that a grant once
        # existed, and re-authorization overwrites it anyway.
        locked.state = CalendarConnectionState.NEEDS_REAUTH
        locked.access_token = None
        locked.access_token_expires_at = None
        locked.last_error = f"refresh rejected: {exc}"
        locked.consecutive_failures += 1
        await session.commit()
        raise CalendarNeedsReauthorizationError(locked.doctor_id, str(exc)) from exc
    except GoogleApiError as exc:
        # TRANSIENT. Count it, but leave the connection ACTIVE so the next
        # tick tries again. Do not blank the existing token -- it may still
        # have minutes left on it and be perfectly usable.
        locked.consecutive_failures += 1
        locked.last_error = f"refresh failed: {exc}"
        await session.commit()
        raise

    locked.access_token = refreshed.access_token
    locked.access_token_expires_at = refreshed.expires_at
    # Google normally omits refresh_token on a refresh -- confirmed against
    # the live API. Only overwrite when it actually sends a new one, which
    # happens when it rotates the grant.
    if refreshed.refresh_token is not None:
        locked.refresh_token = refreshed.refresh_token
    if refreshed.scopes:
        locked.granted_scopes = " ".join(refreshed.scopes)
    locked.consecutive_failures = 0
    locked.last_error = None

    await session.commit()
    return refreshed.access_token


def _assert_usable(connection: CalendarConnection) -> None:
    if connection.state is CalendarConnectionState.NEEDS_REAUTH:
        raise CalendarNeedsReauthorizationError(connection.doctor_id, connection.last_error or "")
    if connection.state is CalendarConnectionState.DISABLED:
        raise CalendarConnectionDisabledError(connection.doctor_id)
