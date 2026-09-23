"""Calendar connection routes.

Thin, per the Phase 1 rule: parse, call a service, map domain exceptions to
HTTP. No OAuth logic lives here -- see services/calendar_oauth_service.py,
which the Phase 3 chatbot will call directly without going through HTTP.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_doctor_scope, get_current_staff, get_db
from app.integrations.google_oauth import GoogleOAuthConfigError
from app.models.staff_account import StaffAccount
from app.schemas.calendar import AuthorizationStartResponse, CalendarConnectionRead
from app.services import calendar_oauth_service
from app.services.exceptions import (
    CalendarAuthorizationError,
    CalendarNotConnectedError,
    DoctorNotFoundError,
    InsufficientCalendarScopeError,
    InvalidOAuthStateError,
)

router = APIRouter(tags=["calendar"])


@router.post(
    "/doctors/{doctor_id}/calendar/authorize",
    response_model=AuthorizationStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_calendar_authorization(
    doctor_id: UUID,
    db: AsyncSession = Depends(get_db),
    staff: StaffAccount = Depends(get_current_staff),
) -> AuthorizationStartResponse:
    """Mint a consent URL for this doctor.

    WHY POST RETURNING JSON rather than a 302 redirect: the caller is not
    always a browser. The Phase 3 chatbot needs the URL as a string to text
    to a doctor, and a voice flow needs to shorten it. A redirect would force
    every non-browser client to follow-but-not-follow it and scrape the
    Location header. A browser-facing wrapper that 302s to this URL is a
    two-line addition on top; the reverse is not.
    is not idempotent (it creates a state row), so POST is also the honest verb.
    """
    assert_doctor_scope(staff, doctor_id)
    try:
        start = await calendar_oauth_service.start_authorization(db, doctor_id=doctor_id)
    except DoctorNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except GoogleOAuthConfigError as exc:
        # 503, not 500: the service is correctly built but not configured for
        # this environment. Distinguishing them matters at 3am.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return AuthorizationStartResponse(
        authorization_url=start.authorization_url,
        state=start.state,
        expires_at=start.expires_at,
    )


@router.get("/calendar/oauth/callback", response_class=HTMLResponse)
async def calendar_oauth_callback(
    response: Response,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Where Google sends the doctor's browser after the consent screen.

    Returns HTML rather than JSON because a human is looking at it -- this is
    the one endpoint in the system whose client is definitively a browser.

    NOTE the `noindex` header and the absence of any echoed query parameter
    in the body: the URL in the address bar contains the authorization code
    and state, and reflecting them into the page would put secrets into
    screenshots, bug reports and browser history previews.
    """
    # Tell crawlers and browser features not to retain this page.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"

    def page(title: str, message: str, http_status: int) -> HTMLResponse:
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='robots' content='noindex'>"
            "<meta name='referrer' content='no-referrer'>"
            f"<title>{title}</title></head>"
            "<body style=\"font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto\">"
            f"<h1>{title}</h1><p>{message}</p></body></html>"
        )
        return HTMLResponse(content=html, status_code=http_status, headers=dict(response.headers))

    # Google reports user refusal via ?error=access_denied, not an HTTP error.
    if error:
        return page(
            "Calendar not connected",
            "You declined access, so nothing was changed. You can close this tab.",
            status.HTTP_400_BAD_REQUEST,
        )

    if not code or not state:
        return page(
            "Invalid request",
            "This link is missing required information. Please start again from the clinic app.",
            status.HTTP_400_BAD_REQUEST,
        )

    try:
        connection = await calendar_oauth_service.complete_authorization(
            db, state_token=state, code=code
        )
    except InvalidOAuthStateError:
        # Intentionally vague to the browser; the specifics are logged.
        # Wording deliberately covers used/expired/unknown without saying
        # which: distinguishing them would hand an attacker an oracle for
        # probing CSRF tokens. But "expired" alone was actively misleading --
        # the common real cause is a link that was already used, e.g. after
        # a first attempt failed because a permission was left unticked.
        return page(
            "This link can no longer be used",
            "Authorization links work only once and expire after a few minutes. "
            "This one has already been used or has expired. "
            "Please request a new link from the clinic app and try again.",
            status.HTTP_400_BAD_REQUEST,
        )
    except InsufficientCalendarScopeError as exc:
        return page(
            "Missing permissions",
            f"{exc} Please authorize again and leave all permissions ticked.",
            status.HTTP_400_BAD_REQUEST,
        )
    except CalendarAuthorizationError as exc:
        return page("Could not connect calendar", str(exc), status.HTTP_400_BAD_REQUEST)
    except GoogleOAuthConfigError as exc:
        return page("Not configured", str(exc), status.HTTP_503_SERVICE_UNAVAILABLE)

    return page(
        "Calendar connected",
        f"{connection.account_email} is now linked. You can close this tab.",
        status.HTTP_200_OK,
    )


@router.get("/doctors/{doctor_id}/calendar", response_model=CalendarConnectionRead)
async def get_calendar_connection(
    doctor_id: UUID,
    db: AsyncSession = Depends(get_db),
    staff: StaffAccount = Depends(get_current_staff),
) -> CalendarConnectionRead:
    assert_doctor_scope(staff, doctor_id)
    try:
        connection = await calendar_oauth_service.get_connection(db, doctor_id=doctor_id)
    except CalendarNotConnectedError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return CalendarConnectionRead.model_validate(connection)


@router.delete("/doctors/{doctor_id}/calendar", response_model=CalendarConnectionRead)
async def disconnect_calendar(
    doctor_id: UUID,
    db: AsyncSession = Depends(get_db),
    staff: StaffAccount = Depends(get_current_staff),
) -> CalendarConnectionRead:
    """Destroy stored credentials and stop syncing.

    Returns the connection rather than 204 so the caller can see the
    resulting state -- the row deliberately survives as an audit record
    (see the service docstring), and a bare 204 would wrongly imply it is gone.
    """
    assert_doctor_scope(staff, doctor_id)
    try:
        connection = await calendar_oauth_service.disconnect(db, doctor_id=doctor_id)
    except CalendarNotConnectedError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return CalendarConnectionRead.model_validate(connection)
