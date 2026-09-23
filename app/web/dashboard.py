"""The staff dashboard: a thin, server-rendered operational view.

DELIBERATELY NOT A SPA, NOT A PRODUCT UI. Three things a staff member
would otherwise need `curl`/Postman and a JWT to see or act on: upcoming
appointments, the escalation queue, and calendar connection health --
plus the one real write action worth a button, reopening a terminal
conversation (Phase 3.5).

AUTHORIZATION SHAPE: every route requires a valid staff session cookie
(get_current_staff_from_cookie, api/deps.py) -- same underlying check as
the JSON API's bearer-token dependency, different only in where the
token comes from. The appointments list is doctor-scoped exactly the
way the JSON API already is: FRONT_DESK sees everyone's, DOCTOR sees
only their own (assert_doctor_scope is not reusable here as-is, since
it checks a single path `doctor_id` -- scoping a LIST is filtering the
query, not rejecting the request, so app/services/appointment_service.
list_upcoming takes an optional doctor_id and this router decides what
to pass).

REOPEN, WIRED FOR REAL: the reopen form on this page calls
conversation_service.reopen directly -- the exact same function the
JSON API's PATCH /conversations/{id}/reopen route calls, not a
copy of its logic. Same guarantee applies here as there: this router
never reads or sets patient_id, identity_verified_at or
identity_attempts, and never will -- see reopen()'s own docstring for
why that must stay true regardless of which transport calls it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DASHBOARD_COOKIE_NAME, get_current_staff_from_cookie, get_db
from app.models.enums import StaffRole
from app.models.staff_account import StaffAccount
from app.services import appointment_service, auth_service, conversation_service, doctor_service
from app.services.exceptions import (
    ConversationNotFoundError,
    ConversationNotTerminalError,
    InvalidCredentialsError,
    StaffAccountInactiveError,
)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    try:
        account = await auth_service.authenticate(db, email=email, password=password)
    except (InvalidCredentialsError, StaffAccountInactiveError):
        # SAME body for both, same reasoning as the JSON login endpoint:
        # distinguishing "wrong password" from "account disabled" tells a
        # caller which emails are real staff accounts.
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password"}, status_code=401
        )

    token, expires_at = auth_service.create_access_token(account)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        DASHBOARD_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        # Secure iff THIS request actually arrived over https -- not tied
        # to the `debug` flag. That first version was wrong twice over:
        # it made the cookie's security property depend on a general-
        # purpose config flag instead of the one fact that actually
        # matters (was this connection encrypted), and it broke silently
        # in exactly the environment meant to catch that kind of thing --
        # CI, where `debug` defaults to False with no .env to override
        # it, produced a Secure cookie that a plain http://testserver
        # client correctly refused to resend, failing 10 tests. Local
        # Docker runs never caught it because a leaked .env (see
        # .dockerignore -- fixed alongside this) baked DEBUG=true into
        # the image, masking the bug the same way the real .env has now
        # masked two earlier bugs in this project (the packaging fix,
        # the JWT ordering fix) before CI caught them clean.
        # FLAGGED: request.url.scheme reads the connection FastAPI itself
        # terminated. Behind a TLS-terminating reverse proxy, that will
        # read "http" even for a real https request unless the proxy's
        # X-Forwarded-Proto is honored -- not implemented here, same
        # class of gap as TWILIO_WEBHOOK_BASE_URL's note in core/config.py
        # about request.url not reflecting what the client actually saw.
        secure=request.url.scheme == "https",
        max_age=int((expires_at - datetime.now(UTC)).total_seconds()),
    )
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/dashboard/login", status_code=303)
    response.delete_cookie(DASHBOARD_COOKIE_NAME)
    return response


@router.get("", response_class=HTMLResponse)
async def home(
    request: Request,
    staff: StaffAccount = Depends(get_current_staff_from_cookie),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    # DOCTOR-scoped exactly like the JSON API: front desk sees every
    # doctor's appointments, a doctor account sees only their own -- see
    # the module docstring for why this is a query filter, not a
    # rejected request, and so not `assert_doctor_scope`.
    scope_doctor_id = staff.doctor_id if staff.role is StaffRole.DOCTOR else None

    appointments = await appointment_service.list_upcoming(db, doctor_id=scope_doctor_id)
    escalated = await conversation_service.list_escalated(db)
    doctors = await doctor_service.list_doctors(db)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "staff": staff,
            "appointments": appointments,
            "escalated": escalated,
            "doctors": doctors,
        },
    )


@router.post("/conversations/{conversation_id}/reopen")
async def reopen_conversation(
    conversation_id: UUID,
    reason: str = Form(...),
    staff: StaffAccount = Depends(get_current_staff_from_cookie),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    try:
        await conversation_service.reopen(
            db, conversation_id=conversation_id, staff_id=staff.id, reason=reason
        )
    except (ConversationNotFoundError, ConversationNotTerminalError):
        # No flash-message infrastructure in this first cut -- the queue
        # simply reflects reality on redirect: a conversation that failed
        # to reopen is still sitting in the escalated list, which is a
        # correct (if terse) signal. Not swallowed silently: still logged
        # server-side by reopen()/get_or_create_conversation's own callers.
        pass
    return RedirectResponse(url="/dashboard", status_code=303)
