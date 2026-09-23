"""Google Calendar API v3 client: pure HTTP, no database.

Deliberately knows nothing about our models. It takes an access token, makes
a call, and either returns data or raises a CLASSIFIED error. Deciding what
to do about a classified error -- refresh, back off, mark the connection dead
-- is policy, and policy lives in the service layer where the database is.

WHY NOT google-api-python-client: it is synchronous. Dropping a blocking
HTTP call into an async FastAPI worker stalls the whole event loop, and the
usual workaround (run_in_executor) reintroduces a thread pool we otherwise
do not need. The REST surface we use is four endpoints.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# Marks events WE created on the doctor's calendar. See the feedback-loop
# warning in calendar_sync_service -- without this, our own pushed
# appointments come back on the next pull as "busy" and conflict with
# themselves.
CLINIC_APPOINTMENT_PROPERTY = "clinic_appointment_id"


# --------------------------------------------------------------------- #
# Classified errors
# --------------------------------------------------------------------- #


class GoogleApiError(RuntimeError):
    """Base for anything the Calendar API threw at us."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class GoogleAuthRevokedError(GoogleApiError):
    """TERMINAL. The refresh token is dead and no retry can fix it.

    Causes: the user revoked access, changed their password, the grant
    expired (7 days for an app still in Google's "Testing" publishing
    status), or the account was deleted.

    Treating this as retryable is a classic and expensive bug: the job
    retries forever, burns quota, floods logs, and nobody is ever told that
    a human must re-authorize. It must stop the connection dead and raise a
    flag.
    """


class GoogleRateLimitError(GoogleApiError):
    """TRANSIENT. 429, or 403 with a rate-limit reason. Back off and retry."""

    def __init__(self, message: str, *, status_code: int | None = None, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message, status_code=status_code)


class GoogleServerError(GoogleApiError):
    """TRANSIENT. 5xx or a network failure."""


class GoogleAccessTokenExpiredError(GoogleApiError):
    """401. Refresh the access token once and retry the call once."""


class SyncTokenExpiredError(GoogleApiError):
    """410 GONE.

    NOT an error condition -- it is Google's documented way of saying "your
    incremental cursor is too old, start over". The caller drops the stored
    sync token and performs a full resync.
    """


class GoogleNotFoundError(GoogleApiError):
    """404. For a delete this means "already gone", which is success."""


@dataclass(frozen=True)
class RefreshedToken:
    access_token: str
    expires_at: datetime
    scopes: tuple[str, ...]
    # Google usually omits this on refresh -- verified against the live API.
    # The caller must NOT overwrite a stored refresh token with None.
    refresh_token: str | None


@dataclass(frozen=True)
class EventPage:
    events: list[dict[str, Any]]
    next_page_token: str | None
    next_sync_token: str | None


@dataclass(frozen=True)
class RetryPolicy:
    """Explicit rather than a decorator so tests can set delays to zero.

    WHY jitter: without it, every doctor's sync that failed during the same
    Google blip retries at exactly the same moment, re-creating the spike
    that caused the failure. Full jitter spreads them out.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def delay_for(self, attempt: int) -> float:
        capped = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** attempt))
        return random.uniform(0, capped)  # full jitter


NO_RETRY = RetryPolicy(max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=0.0)


class GoogleCalendarClient:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._http = http_client
        self.retry = retry_policy or RetryPolicy()

    # ----------------------------------------------------------------- #
    # Plumbing
    # ----------------------------------------------------------------- #

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        owns = self._http is None
        http = self._http or httpx.AsyncClient(timeout=30.0)
        try:
            return await http.request(method, url, **kwargs)
        finally:
            if owns:
                await http.aclose()

    @staticmethod
    def _classify(response: httpx.Response) -> None:
        """Turn an HTTP status into one of our typed errors, or return."""
        if response.status_code < 400:
            return

        try:
            body = response.json()
            err = body.get("error", {})
            message = err.get("message") if isinstance(err, dict) else str(err)
            reasons = {
                d.get("reason", "")
                for d in (err.get("errors") or [])
                if isinstance(d, dict)
            } if isinstance(err, dict) else set()
        except ValueError:
            message = response.text[:200]
            reasons = set()

        code = response.status_code
        detail = f"{code}: {message}"

        if code == 401:
            raise GoogleAccessTokenExpiredError(detail, status_code=code)
        if code == 410:
            raise SyncTokenExpiredError(detail, status_code=code)
        if code == 404:
            raise GoogleNotFoundError(detail, status_code=code)
        if code == 429 or (code == 403 and reasons & {
            "rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"
        }):
            retry_after = response.headers.get("Retry-After")
            raise GoogleRateLimitError(
                detail,
                status_code=code,
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if code >= 500:
            raise GoogleServerError(detail, status_code=code)
        raise GoogleApiError(detail, status_code=code)

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Retry ONLY transient classes. 401/410/404/4xx return immediately.

        WHY 401 is not retried here: retrying the identical request with the
        same dead token cannot succeed. It needs a refresh first, which is
        the caller's job because only the caller can persist the new token.
        """
        last_exc: Exception | None = None
        for attempt in range(self.retry.max_attempts):
            try:
                response = await self._send(method, url, **kwargs)
                self._classify(response)
                return response
            except (GoogleRateLimitError, GoogleServerError) as exc:
                last_exc = exc
                if attempt == self.retry.max_attempts - 1:
                    raise
                delay = getattr(exc, "retry_after", None) or self.retry.delay_for(attempt)
                await asyncio.sleep(delay)
            except httpx.HTTPError as exc:
                # Connection reset / DNS / timeout -- transient by nature.
                last_exc = GoogleServerError(f"network error: {exc}")
                if attempt == self.retry.max_attempts - 1:
                    raise last_exc from exc
                await asyncio.sleep(self.retry.delay_for(attempt))
        raise last_exc  # pragma: no cover - loop always returns or raises

    # ----------------------------------------------------------------- #
    # OAuth token operations
    # ----------------------------------------------------------------- #

    async def refresh_access_token(
        self, *, refresh_token: str, client_id: str, client_secret: str
    ) -> RefreshedToken:
        """Exchange a refresh token for a new access token."""
        response = await self._send(
            "POST",
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code == 200:
            payload = response.json()
            return RefreshedToken(
                access_token=payload["access_token"],
                expires_at=datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600))),
                scopes=tuple(payload.get("scope", "").split()),
                refresh_token=payload.get("refresh_token"),
            )

        try:
            body = response.json()
            error = body.get("error", "")
            description = body.get("error_description", "")
        except ValueError:
            error, description = "", response.text[:200]

        # invalid_grant on a REFRESH means the grant itself is gone. This is
        # the one error that must never be retried.
        if error == "invalid_grant":
            raise GoogleAuthRevokedError(
                f"refresh token rejected: {description or error}", status_code=response.status_code
            )
        if response.status_code == 429:
            raise GoogleRateLimitError(f"{error}: {description}", status_code=429)
        if response.status_code >= 500:
            raise GoogleServerError(f"{error}: {description}", status_code=response.status_code)
        raise GoogleApiError(f"token refresh failed: {error}: {description}", status_code=response.status_code)

    async def revoke(self, *, token: str) -> None:
        """Ask Google to invalidate a token.

        Google answers 200 for a successfully revoked token and 400 for one
        that is already invalid. Both mean "this token is not usable", which
        is what we wanted, so neither is an error to us -- making revocation
        idempotent and safe to retry.
        """
        response = await self._send("POST", GOOGLE_REVOKE_ENDPOINT, data={"token": token})
        if response.status_code not in (200, 400):
            self._classify(response)

    # ----------------------------------------------------------------- #
    # Calendar operations
    # ----------------------------------------------------------------- #

    async def list_events(
        self,
        *,
        access_token: str,
        calendar_id: str,
        sync_token: str | None = None,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        page_token: str | None = None,
        max_results: int = 250,
    ) -> EventPage:
        """One page of events.

        `singleEvents=true` expands recurring events into individual
        instances. Without it a weekly 09:00 meeting arrives as ONE event
        with a recurrence rule, and we would have to implement RRULE
        expansion (including exceptions and timezone-sensitive DST shifts)
        ourselves. That is a notorious source of subtle bugs; let Google do it.

        `showDeleted=true` is REQUIRED for incremental sync to be correct.
        Deletions arrive as events with status="cancelled"; without this flag
        they simply vanish from the response and we would never learn that a
        block should be removed -- the doctor's cancelled holiday would keep
        blocking slots forever.

        NOTE the parameter rules: when `syncToken` is supplied, Google
        forbids timeMin/timeMax/q/orderBy. The window is therefore fixed at
        whatever the last FULL sync used, which is why the sync service
        re-baselines periodically.
        """
        params: dict[str, Any] = {
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": max_results,
        }
        if sync_token:
            params["syncToken"] = sync_token
        else:
            if time_min:
                params["timeMin"] = time_min.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if time_max:
                params["timeMax"] = time_max.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if page_token:
            params["pageToken"] = page_token

        response = await self._request_with_retry(
            "GET",
            f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        payload = response.json()
        return EventPage(
            events=payload.get("items", []),
            next_page_token=payload.get("nextPageToken"),
            next_sync_token=payload.get("nextSyncToken"),
        )

    async def insert_event(
        self, *, access_token: str, calendar_id: str, event: dict[str, Any]
    ) -> dict[str, Any]:
        response = await self._request_with_retry(
            "POST",
            f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events",
            json=event,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return response.json()

    async def patch_event(
        self, *, access_token: str, calendar_id: str, event_id: str, event: dict[str, Any]
    ) -> dict[str, Any]:
        response = await self._request_with_retry(
            "PATCH",
            f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}",
            json=event,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return response.json()

    async def delete_event(self, *, access_token: str, calendar_id: str, event_id: str) -> None:
        """Delete, treating 404/410 as success.

        WHY: the operation is "ensure this event is not on the calendar". If
        the doctor already deleted it by hand, we are done. Raising would
        park the outbox row in FAILED forever over an outcome we wanted.
        """
        try:
            await self._request_with_retry(
                "DELETE",
                f"{CALENDAR_API_BASE}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except (GoogleNotFoundError, SyncTokenExpiredError):
            # 410 on a DELETE means "already deleted", not "bad sync token".
            return
