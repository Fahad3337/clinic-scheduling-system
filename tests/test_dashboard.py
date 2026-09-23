"""The staff dashboard: login, session cookie, scoping, and the reopen action.

Real HTTP throughout (the `client` fixture, ASGITransport) -- same
convention as every other transport test in this project. httpx's
AsyncClient carries cookies across requests on the same client instance
automatically, the same way a browser does, so a login POST followed by
a GET /dashboard on the SAME `client` is a genuine end-to-end check of
the cookie round-trip, not a shortcut around it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.deps import DASHBOARD_COOKIE_NAME
from app.models.appointment import Appointment
from app.models.conversation import Conversation
from app.models.enums import AppointmentStatus, BookingChannel, ConversationChannel, StaffRole
from app.models.staff_account import StaffAccount
from app.services import auth_service, conversation_service

LOGIN_PATH = "/dashboard/login"
HOME_PATH = "/dashboard"


@pytest.fixture
async def front_desk_account(db):
    account = StaffAccount(
        email="desk-dashboard@clinic.example.com",
        password_hash=auth_service.hash_password("dash-front-desk-pw"),
        role=StaffRole.FRONT_DESK,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


@pytest.fixture
async def doctor_account(db, doctor):
    account = StaffAccount(
        email="doc-dashboard@clinic.example.com",
        password_hash=auth_service.hash_password("dash-doctor-pw"),
        role=StaffRole.DOCTOR,
        doctor_id=doctor.id,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


@pytest.fixture
async def other_doctor(db):
    from app.models.doctor import Doctor

    doc = Doctor(full_name="Dr. Second Opinion", timezone="UTC")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def login(client, email: str, password: str):
    return await client.post(LOGIN_PATH, data={"email": email, "password": password})


# ===================================================================== #
# Login flow
# ===================================================================== #


@pytest.mark.asyncio
async def test_login_page_loads(client):
    resp = await client.get(LOGIN_PATH)
    assert resp.status_code == 200
    assert "Sign in" in resp.text


@pytest.mark.asyncio
async def test_successful_login_sets_cookie_and_redirects(client, front_desk_account):
    resp = await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    assert resp.status_code == 303
    assert resp.headers["location"] == HOME_PATH
    assert DASHBOARD_COOKIE_NAME in client.cookies
    # HttpOnly is a header attribute, not something httpx exposes on the
    # jar -- checked directly on the Set-Cookie header instead.
    assert "HttpOnly" in resp.headers.get("set-cookie", "")
    assert "SameSite=lax" in resp.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_wrong_password_shows_error_and_sets_no_cookie(client, front_desk_account):
    resp = await login(client, "desk-dashboard@clinic.example.com", "not-the-password")
    assert resp.status_code == 401
    assert "Invalid email or password" in resp.text
    assert DASHBOARD_COOKIE_NAME not in client.cookies


@pytest.mark.asyncio
async def test_unknown_email_gives_the_same_error_as_wrong_password(client):
    """Same reasoning as the JSON login endpoint: distinguishing 'no such
    account' from 'wrong password' hands an attacker an email oracle."""
    resp = await login(client, "nobody-here@clinic.example.com", "whatever")
    assert resp.status_code == 401
    assert "Invalid email or password" in resp.text


@pytest.mark.asyncio
async def test_deactivated_account_cannot_log_in(client, front_desk_account, session_factory):
    async with session_factory() as s:
        row = await s.get(StaffAccount, front_desk_account.id)
        row.is_active = False
        await s.commit()

    resp = await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    assert resp.status_code == 401
    assert DASHBOARD_COOKIE_NAME not in client.cookies


@pytest.mark.asyncio
async def test_logout_clears_the_cookie(client, front_desk_account):
    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    assert DASHBOARD_COOKIE_NAME in client.cookies

    resp = await client.post("/dashboard/logout")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login"
    assert DASHBOARD_COOKIE_NAME not in client.cookies

    # And the page itself is no longer reachable.
    home = await client.get(HOME_PATH)
    assert home.status_code == 303
    assert home.headers["location"] == "/dashboard/login"


# ===================================================================== #
# Access control -- the cookie-based dependency fails exactly like the
# bearer one, just with a redirect instead of a bare 401
# ===================================================================== #


@pytest.mark.asyncio
async def test_dashboard_without_a_cookie_redirects_to_login(client):
    resp = await client.get(HOME_PATH)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login"


@pytest.mark.asyncio
async def test_dashboard_with_a_garbage_cookie_redirects_to_login(client):
    client.cookies.set(DASHBOARD_COOKIE_NAME, "not-a-real-jwt")
    resp = await client.get(HOME_PATH)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login"


@pytest.mark.asyncio
async def test_authenticated_dashboard_shows_the_signed_in_staff_email(client, front_desk_account):
    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    resp = await client.get(HOME_PATH)
    assert resp.status_code == 200
    assert "desk-dashboard@clinic.example.com" in resp.text
    assert "front_desk" in resp.text


# ===================================================================== #
# Appointments: doctor-scoped exactly like the JSON API
# ===================================================================== #


@pytest.fixture
async def future_appointment(db, doctor, patient, time_slot):
    appt = Appointment(
        patient_id=patient.id,
        doctor_id=doctor.id,
        time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED,
        booking_channel=BookingChannel.WEB,
        reason="checkup",
    )
    db.add(appt)
    await db.commit()
    return appt


@pytest.mark.asyncio
async def test_front_desk_sees_every_doctors_appointments(
    client, front_desk_account, future_appointment, doctor, patient
):
    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    resp = await client.get(HOME_PATH)
    assert patient.full_name in resp.text
    assert doctor.full_name in resp.text


@pytest.mark.asyncio
async def test_doctor_sees_only_their_own_appointments(
    client, doctor_account, future_appointment, patient, other_doctor, session_factory
):
    """The scoping property that actually matters: a SECOND doctor's
    appointment must NOT appear on this doctor's dashboard."""
    from app.models.time_slot import TimeSlot

    async with session_factory() as s:
        other_slot = TimeSlot(
            doctor_id=other_doctor.id,
            starts_at=datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=5),
            ends_at=datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=5, minutes=30),
        )
        s.add(other_slot)
        await s.flush()
        other_appt = Appointment(
            patient_id=patient.id,
            doctor_id=other_doctor.id,
            time_slot_id=other_slot.id,
            status=AppointmentStatus.BOOKED,
            booking_channel=BookingChannel.WEB,
            reason="a different doctor's appointment",
        )
        s.add(other_appt)
        await s.commit()

    await login(client, "doc-dashboard@clinic.example.com", "dash-doctor-pw")
    resp = await client.get(HOME_PATH)
    assert resp.status_code == 200
    assert "checkup" in resp.text  # this doctor's own appointment
    assert "a different doctor's appointment" not in resp.text


@pytest.mark.asyncio
async def test_no_upcoming_appointments_renders_the_empty_state(client, front_desk_account):
    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    resp = await client.get(HOME_PATH)
    assert "Nothing booked ahead of now." in resp.text


# ===================================================================== #
# Escalation queue + reopen action
# ===================================================================== #


@pytest.fixture
async def escalated_conversation(db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref="+14155550199"
    )
    await db.commit()
    await conversation_service.escalate(db, conversation_id=convo.id, reason="lockout")
    return convo


@pytest.mark.asyncio
async def test_escalated_conversation_appears_with_a_reopen_form(
    client, front_desk_account, escalated_conversation
):
    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    resp = await client.get(HOME_PATH)
    assert escalated_conversation.external_ref in resp.text
    assert f"/dashboard/conversations/{escalated_conversation.id}/reopen" in resp.text


@pytest.mark.asyncio
async def test_reopen_action_genuinely_reopens_state_not_text(
    client, front_desk_account, escalated_conversation, db
):
    """Assert what CHANGED in the database, not the redirect's status
    code -- a 303 proves the route ran, not that reopen() succeeded."""
    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")

    resp = await client.post(
        f"/dashboard/conversations/{escalated_conversation.id}/reopen",
        data={"reason": "caller phoned the front desk"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == HOME_PATH

    row = await db.scalar(select(Conversation).where(Conversation.id == escalated_conversation.id))
    await db.refresh(row)
    assert row.status.value == "expired"
    assert row.reopened_by_staff_id == front_desk_account.id
    assert row.reopen_reason == "caller phoned the front desk"
    # The load-bearing guarantee, unaffected by which transport called
    # reopen(): never resurrects an identity.
    assert row.patient_id is None
    assert row.identity_attempts == 0


@pytest.mark.asyncio
async def test_reopened_conversation_leaves_the_queue(
    client, front_desk_account, escalated_conversation
):
    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    await client.post(
        f"/dashboard/conversations/{escalated_conversation.id}/reopen",
        data={"reason": "resolved"},
    )
    resp = await client.get(HOME_PATH)
    assert escalated_conversation.external_ref not in resp.text
    assert "Nothing waiting on a human right now." in resp.text


@pytest.mark.asyncio
async def test_reopen_requires_a_staff_session(client, escalated_conversation, db):
    resp = await client.post(
        f"/dashboard/conversations/{escalated_conversation.id}/reopen",
        data={"reason": "no session"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login"

    # Per the standing "assert state, not text" convention: an
    # unauthenticated request must not have reopened anything, regardless
    # of what the response looked like.
    row = await db.get(Conversation, escalated_conversation.id)
    await db.refresh(row)
    assert row.status.value == "escalated"
    assert row.reopened_by_staff_id is None


@pytest.mark.asyncio
async def test_reopen_on_a_non_terminal_conversation_does_not_crash(client, front_desk_account, db):
    """A conversation that was never escalated (e.g. already reopened by
    someone else a moment earlier) must not 500 -- it just stays as it is."""
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref="+14155550299"
    )
    await db.commit()

    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    resp = await client.post(
        f"/dashboard/conversations/{convo.id}/reopen",
        data={"reason": "nothing to reopen"},
    )
    assert resp.status_code == 303  # redirected back, not a 500

    row = await db.get(Conversation, convo.id)
    await db.refresh(row)
    assert row.status.value == "active"  # untouched


# ===================================================================== #
# Calendar connection health
# ===================================================================== #


@pytest.mark.asyncio
async def test_doctor_without_a_calendar_connection_shows_not_connected(
    client, front_desk_account, doctor
):
    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    resp = await client.get(HOME_PATH)
    assert doctor.full_name in resp.text
    assert "not connected" in resp.text


@pytest.mark.asyncio
async def test_doctor_with_a_calendar_connection_shows_its_state(
    client, front_desk_account, doctor, session_factory
):
    from app.models.calendar_connection import CalendarConnection
    from app.models.enums import CalendarConnectionState, CalendarProvider

    async with session_factory() as s:
        conn = CalendarConnection(
            doctor_id=doctor.id,
            provider=CalendarProvider.GOOGLE,
            account_email="rao@example-clinic.test",
            state=CalendarConnectionState.NEEDS_REAUTH,
            last_error="refresh rejected: invalid_grant",
        )
        s.add(conn)
        await s.commit()

    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    resp = await client.get(HOME_PATH)
    assert "needs_reauth" in resp.text
    assert "invalid_grant" in resp.text


# ===================================================================== #
# Cold identity map (standing project convention)
# ===================================================================== #


@pytest.mark.asyncio
async def test_dashboard_survives_a_cold_identity_map(
    client, front_desk_account, future_appointment, escalated_conversation, db
):
    await login(client, "desk-dashboard@clinic.example.com", "dash-front-desk-pw")
    db.expunge_all()
    resp = await client.get(HOME_PATH)
    assert resp.status_code == 200
