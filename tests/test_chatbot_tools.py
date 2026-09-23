"""Tool layer: identity, guards, two-phase commit, and injection resistance.

The tests that matter most here are not "does the happy path work" but
"can a model that is confused, hallucinating, or actively steered by an
attacker cause something it should not". Those are the ones the whole
design exists for.

No LLM is involved anywhere in this file -- the tool layer is a plain
async function surface, dispatched by name with a dict of arguments. That
is deliberate: every safety property below is testable without spending a
token or depending on a model's behaviour.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.chatbot import tools
from app.chatbot.tools import ToolContext
from app.models.appointment import Appointment
from app.models.booking_proposal import BookingProposal
from app.models.conversation import MAX_IDENTITY_ATTEMPTS, Conversation
from app.models.enums import (
    AppointmentStatus,
    BookingChannel,
    ConversationChannel,
    ConversationStatus,
    ProposalKind,
)
from app.models.patient import Patient
from app.services import conversation_service

DOB = date(1985, 3, 14)


@pytest.fixture
async def dob_patient(db):
    """A patient who CAN be verified -- phone on file plus a date of birth."""
    p = Patient(full_name="Alex Chen", phone="+14155550123", date_of_birth=DOB)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


@pytest.fixture
async def conversation(db, dob_patient):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=dob_patient.phone
    )
    await db.commit()
    return convo


@pytest.fixture
def ctx(db, conversation):
    return ToolContext(session=db, conversation_id=conversation.id)


async def verify(ctx) -> dict:
    return await tools.dispatch(ctx, "verify_identity", {"date_of_birth": DOB.isoformat()})


# ===================================================================== #
# Identity
# ===================================================================== #


@pytest.mark.asyncio
async def test_correct_dob_verifies_and_binds_the_patient(ctx, db, conversation, dob_patient):
    result = await verify(ctx)
    assert result["status"] == "verified"
    assert result["patient_first_name"] == "Alex"

    await db.refresh(conversation)
    assert conversation.patient_id == dob_patient.id
    assert conversation.identity_verified_at is not None


@pytest.mark.asyncio
async def test_wrong_dob_does_not_bind_and_consumes_an_attempt(ctx, db, conversation):
    result = await tools.dispatch(ctx, "verify_identity", {"date_of_birth": "1999-01-01"})
    assert result["status"] == "wrong_details"
    assert result["attempts_remaining"] == MAX_IDENTITY_ATTEMPTS - 1

    await db.refresh(conversation)
    assert conversation.patient_id is None


@pytest.mark.asyncio
async def test_three_wrong_attempts_escalate_and_end_the_conversation(ctx, db, conversation):
    for _ in range(MAX_IDENTITY_ATTEMPTS - 1):
        await tools.dispatch(ctx, "verify_identity", {"date_of_birth": "1999-01-01"})

    final = await tools.dispatch(ctx, "verify_identity", {"date_of_birth": "1999-01-01"})
    assert final["status"] == "escalated"
    assert final["conversation_ended"] is True

    await db.refresh(conversation)
    assert conversation.status is ConversationStatus.ESCALATED
    assert conversation.escalation_reason


@pytest.mark.asyncio
async def test_escalation_is_terminal_for_every_tool(ctx, db, conversation):
    """The property that makes escalation real rather than advisory."""
    for _ in range(MAX_IDENTITY_ATTEMPTS):
        await tools.dispatch(ctx, "verify_identity", {"date_of_birth": "1999-01-01"})

    # Even the CORRECT date of birth must now fail -- the lockout is not
    # "keep guessing until you get it right".
    after = await verify(ctx)
    assert after["error"] == "conversation_ended"

    for tool_name, args in [
        ("check_availability", {"on_date": "2026-10-01"}),
        ("list_my_appointments", {}),
        ("propose_booking", {"time_slot_id": str(uuid.uuid4())}),
        ("request_human", {"reason": "x"}),
    ]:
        result = await tools.dispatch(ctx, tool_name, args)
        assert result["error"] == "conversation_ended", tool_name


@pytest.mark.asyncio
async def test_unregistered_number_reports_not_registered_and_costs_an_attempt(db):
    """An unknown caller must not get unlimited DOB guesses either."""
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref="+14155559999"
    )
    await db.commit()
    ctx = ToolContext(session=db, conversation_id=convo.id)

    result = await tools.dispatch(ctx, "verify_identity", {"date_of_birth": DOB.isoformat()})
    assert result["status"] == "not_registered"
    assert result["attempts_remaining"] == MAX_IDENTITY_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_patient_without_dob_escalates_immediately_without_burning_attempts(db):
    """No number of retries can ever succeed, so do not pretend otherwise."""
    p = Patient(full_name="No Dob", phone="+14155550777", date_of_birth=None)
    db.add(p)
    await db.commit()
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=p.phone
    )
    await db.commit()
    ctx = ToolContext(session=db, conversation_id=convo.id)

    result = await tools.dispatch(ctx, "verify_identity", {"date_of_birth": DOB.isoformat()})
    assert result["status"] == "cannot_verify"
    assert result["conversation_ended"] is True

    await db.refresh(convo)
    assert convo.status is ConversationStatus.ESCALATED
    assert convo.identity_attempts == 0  # not the caller's fault


@pytest.mark.asyncio
async def test_verification_expires(ctx, db, conversation):
    """A day-old verification must not still authorize a cancellation."""
    await verify(ctx)
    await db.refresh(conversation)

    from app.core.config import get_settings

    ttl = get_settings().conversation_identity_ttl_minutes
    later = datetime.now(UTC) + timedelta(minutes=ttl + 1)
    stale_ctx = ToolContext(session=db, conversation_id=conversation.id, now=later)

    result = await tools.dispatch(stale_ctx, "list_my_appointments", {})
    assert result["error"] == "identity_required"


@pytest.mark.asyncio
async def test_identity_result_never_leaks_the_stored_date_of_birth(ctx):
    """The tool answers yes/no. It must not echo the record back."""
    ok = await verify(ctx)
    wrong = await tools.dispatch(ctx, "verify_identity", {"date_of_birth": "1999-01-01"})
    for payload in (ok, wrong):
        assert DOB.isoformat() not in str(payload)
        assert "date_of_birth" not in payload


# ===================================================================== #
# The guard: patient-scoped tools refuse without fresh identity
# ===================================================================== #


@pytest.mark.asyncio
async def test_patient_scoped_tools_refuse_before_verification(ctx):
    """The model is not trusted to call verify_identity first."""
    for tool_name, args in [
        ("list_my_appointments", {}),
        ("propose_booking", {"time_slot_id": str(uuid.uuid4())}),
        ("confirm_booking", {"proposal_id": str(uuid.uuid4())}),
        ("propose_cancellation", {"appointment_id": str(uuid.uuid4())}),
        ("confirm_cancellation", {"proposal_id": str(uuid.uuid4())}),
    ]:
        result = await tools.dispatch(ctx, tool_name, args)
        assert result["error"] == "identity_required", tool_name


@pytest.mark.asyncio
async def test_availability_does_not_require_verification(ctx, doctor, time_slot):
    """Consistent with the public HTTP availability endpoint -- asking
    whether Tuesday is free is not patient data."""
    on_date = time_slot.starts_at.date()
    result = await tools.dispatch(ctx, "check_availability", {"on_date": on_date.isoformat()})
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_every_registered_tool_declares_its_identity_requirement(ctx):
    """Guard-by-declaration only works if every tool declares. A new tool
    added without thinking about this fails here rather than silently
    defaulting to unprotected."""
    patient_scoped = {
        "list_my_appointments",
        "propose_booking",
        "confirm_booking",
        "propose_cancellation",
        "confirm_cancellation",
    }
    for name, spec in tools.TOOLS.items():
        assert isinstance(spec.requires_identity, bool)
        if name in patient_scoped:
            assert spec.requires_identity is True, name


# ===================================================================== #
# Injection resistance -- the reason the design looks like this
# ===================================================================== #


@pytest.mark.asyncio
async def test_no_tool_accepts_a_patient_identifier(ctx):
    """Structural, not behavioural: there is no field to inject into.

    If a future tool grows a `patient_id` argument, this fails -- which is
    the point. The defence is the absence of the parameter, not a check
    inside a handler that someone could forget.
    """
    for name, spec in tools.TOOLS.items():
        fields = set(spec.input_model.model_fields)
        assert "patient_id" not in fields, name
        assert "phone" not in fields, name
        assert "conversation_id" not in fields, name


@pytest.mark.asyncio
async def test_cannot_cancel_another_patients_appointment(ctx, db, dob_patient, doctor, time_slot):
    """The injection scenario, end to end: the model passes a real
    appointment id belonging to somebody else."""
    other = Patient(full_name="Jordan Lee", phone="+14155550124", date_of_birth=date(1970, 1, 1))
    db.add(other)
    await db.commit()
    victim = Appointment(
        patient_id=other.id, doctor_id=doctor.id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(victim)
    await db.commit()

    await verify(ctx)
    result = await tools.dispatch(
        ctx, "propose_cancellation", {"appointment_id": str(victim.id)}
    )
    # Indistinguishable from "no such appointment" -- see the note in
    # proposal_service about not confirming another patient's records exist.
    assert result["error"] == "appointment_not_found"

    await db.refresh(victim)
    assert victim.status is AppointmentStatus.BOOKED


@pytest.mark.asyncio
async def test_list_my_appointments_only_returns_the_verified_patients(
    ctx, db, dob_patient, doctor, time_slot
):
    other = Patient(full_name="Jordan Lee", phone="+14155550124", date_of_birth=date(1970, 1, 1))
    db.add(other)
    await db.commit()
    db.add(
        Appointment(
            patient_id=other.id, doctor_id=doctor.id, time_slot_id=time_slot.id,
            status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
        )
    )
    await db.commit()

    await verify(ctx)
    result = await tools.dispatch(ctx, "list_my_appointments", {})
    assert result["appointments"] == []


@pytest.mark.asyncio
async def test_hallucinated_tool_name_is_reported_not_raised(ctx):
    result = await tools.dispatch(ctx, "delete_all_appointments", {})
    assert result["error"] == "unknown_tool"


@pytest.mark.asyncio
async def test_hallucinated_arguments_are_reported_not_raised(ctx):
    await verify(ctx)
    result = await tools.dispatch(ctx, "propose_booking", {"time_slot_id": "not-a-uuid"})
    assert result["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_hallucinated_slot_id_cannot_book(ctx):
    await verify(ctx)
    result = await tools.dispatch(ctx, "propose_booking", {"time_slot_id": str(uuid.uuid4())})
    assert result["error"] == "slot_not_found"


# ===================================================================== #
# Two-phase commit
# ===================================================================== #


@pytest.mark.asyncio
async def test_propose_does_not_book(ctx, db, time_slot):
    await verify(ctx)
    result = await tools.dispatch(ctx, "propose_booking", {"time_slot_id": str(time_slot.id)})
    assert result["status"] == "proposed"
    assert (await db.scalars(select(Appointment))).all() == []


@pytest.mark.asyncio
async def test_confirm_books_through_the_phase_1_service(ctx, db, time_slot, dob_patient):
    await verify(ctx)
    proposed = await tools.dispatch(ctx, "propose_booking", {"time_slot_id": str(time_slot.id)})
    confirmed = await tools.dispatch(ctx, "confirm_booking", {"proposal_id": proposed["proposal_id"]})

    assert confirmed["status"] == "booked"
    appointment = await db.scalar(select(Appointment))
    assert appointment.patient_id == dob_patient.id
    assert appointment.time_slot_id == time_slot.id
    # Booked through appointment_service, so it is tagged as a chat booking
    # rather than looking like a front-desk one.
    assert appointment.booking_channel is BookingChannel.CHAT


@pytest.mark.asyncio
async def test_confirm_is_single_use(ctx, db, time_slot):
    """A patient texting "yes" twice must not book twice."""
    await verify(ctx)
    proposed = await tools.dispatch(ctx, "propose_booking", {"time_slot_id": str(time_slot.id)})
    first = await tools.dispatch(ctx, "confirm_booking", {"proposal_id": proposed["proposal_id"]})
    second = await tools.dispatch(ctx, "confirm_booking", {"proposal_id": proposed["proposal_id"]})

    assert first["status"] == "booked"
    assert second["error"] == "proposal_unavailable"
    assert len((await db.scalars(select(Appointment))).all()) == 1


@pytest.mark.asyncio
async def test_confirming_an_invented_proposal_id_does_nothing(ctx, db, time_slot):
    """The core claim of the two-phase design: a hallucinated confirm has
    nothing to act on, because the service wrote no such proposal."""
    await verify(ctx)
    result = await tools.dispatch(ctx, "confirm_booking", {"proposal_id": str(uuid.uuid4())})
    assert result["error"] == "proposal_unavailable"
    assert (await db.scalars(select(Appointment))).all() == []


@pytest.mark.asyncio
async def test_expired_proposal_cannot_be_confirmed(ctx, db, time_slot):
    await verify(ctx)
    proposed = await tools.dispatch(ctx, "propose_booking", {"time_slot_id": str(time_slot.id)})

    from app.core.config import get_settings

    later = datetime.now(UTC) + timedelta(minutes=get_settings().proposal_ttl_minutes + 1)
    late_ctx = ToolContext(session=db, conversation_id=ctx.conversation_id, now=later)
    result = await tools.dispatch(late_ctx, "confirm_booking", {"proposal_id": proposed["proposal_id"]})

    assert result["error"] == "proposal_unavailable"
    assert (await db.scalars(select(Appointment))).all() == []


@pytest.mark.asyncio
async def test_proposal_cannot_be_confirmed_from_a_different_conversation(
    db, dob_patient, time_slot
):
    """A leaked or guessed proposal id is useless in another conversation."""
    convo_a = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=dob_patient.phone
    )
    await db.commit()
    ctx_a = ToolContext(session=db, conversation_id=convo_a.id)
    await verify(ctx_a)
    proposed = await tools.dispatch(ctx_a, "propose_booking", {"time_slot_id": str(time_slot.id)})

    attacker_patient = Patient(
        full_name="Mallory", phone="+14155550911", date_of_birth=date(1990, 5, 5)
    )
    db.add(attacker_patient)
    await db.commit()
    convo_b = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=attacker_patient.phone
    )
    await db.commit()
    ctx_b = ToolContext(session=db, conversation_id=convo_b.id)
    await tools.dispatch(ctx_b, "verify_identity", {"date_of_birth": "1990-05-05"})

    result = await tools.dispatch(ctx_b, "confirm_booking", {"proposal_id": proposed["proposal_id"]})
    assert result["error"] == "proposal_unavailable"
    assert (await db.scalars(select(Appointment))).all() == []


@pytest.mark.asyncio
async def test_confirm_booking_rejects_a_cancellation_proposal(
    ctx, db, dob_patient, doctor, time_slot
):
    """Cross-check: the model grabbed the wrong proposal id from context."""
    appointment = Appointment(
        patient_id=dob_patient.id, doctor_id=doctor.id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appointment)
    await db.commit()
    await verify(ctx)

    proposed = await tools.dispatch(
        ctx, "propose_cancellation", {"appointment_id": str(appointment.id)}
    )
    result = await tools.dispatch(ctx, "confirm_booking", {"proposal_id": proposed["proposal_id"]})
    assert result["error"] == "proposal_wrong_kind"

    # And the mismatched attempt must NOT have burned the proposal -- the
    # patient should still be able to confirm the cancellation they asked for.
    await db.refresh(appointment)
    assert appointment.status is AppointmentStatus.BOOKED
    ok = await tools.dispatch(ctx, "confirm_cancellation", {"proposal_id": proposed["proposal_id"]})
    assert ok["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_round_trip(ctx, db, dob_patient, doctor, time_slot):
    await verify(ctx)
    booked = await tools.dispatch(ctx, "propose_booking", {"time_slot_id": str(time_slot.id)})
    confirmed = await tools.dispatch(ctx, "confirm_booking", {"proposal_id": booked["proposal_id"]})

    listed = await tools.dispatch(ctx, "list_my_appointments", {})
    assert len(listed["appointments"]) == 1
    assert listed["appointments"][0]["appointment_id"] == confirmed["appointment_id"]

    proposed = await tools.dispatch(
        ctx, "propose_cancellation", {"appointment_id": confirmed["appointment_id"]}
    )
    cancelled = await tools.dispatch(
        ctx, "confirm_cancellation", {"proposal_id": proposed["proposal_id"]}
    )
    assert cancelled["status"] == "cancelled"

    appointment = await db.get(Appointment, uuid.UUID(confirmed["appointment_id"]))
    await db.refresh(appointment)
    assert appointment.status is AppointmentStatus.CANCELLED


# ===================================================================== #
# Times crossing the service/model boundary
# ===================================================================== #


@pytest.mark.asyncio
async def test_slots_are_returned_pre_rendered_in_the_doctors_timezone(
    ctx, db, doctor, time_slot
):
    """The model quotes a time string; it never converts an instant."""
    doctor.timezone = "America/New_York"
    db.add(doctor)
    await db.commit()

    result = await tools.dispatch(
        ctx, "check_availability", {"on_date": time_slot.starts_at.date().isoformat()}
    )
    assert result["slots"], "expected the fixture slot to be free"
    rendered = result["slots"][0]["starts_at_local"]

    from zoneinfo import ZoneInfo

    expected = time_slot.starts_at.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M")
    assert expected in rendered
    # And no raw UTC instant is handed over for the model to mangle.
    assert "+00:00" not in rendered


@pytest.mark.asyncio
async def test_function_declarations_are_wellformed(ctx):
    decls = tools.function_declarations()
    assert {d["name"] for d in decls} == set(tools.TOOLS)
    for decl in decls:
        assert decl["description"]
        assert decl["parameters"]["type"] == "object"


# ===================================================================== #
# Cold identity map -- standing convention
# ===================================================================== #


@pytest.mark.asyncio
async def test_tools_work_with_a_cold_identity_map(ctx, db, time_slot):
    """The real transport opens a fresh session per inbound message."""
    await verify(ctx)
    slot_id = time_slot.id
    db.expunge_all()

    proposed = await tools.dispatch(ctx, "propose_booking", {"time_slot_id": str(slot_id)})
    assert proposed["status"] == "proposed"
    db.expunge_all()

    confirmed = await tools.dispatch(ctx, "confirm_booking", {"proposal_id": proposed["proposal_id"]})
    assert confirmed["status"] == "booked"


# ===================================================================== #
# Concurrency: the attempt counter must not lose updates
# ===================================================================== #


@pytest.mark.asyncio
async def test_concurrent_wrong_guesses_each_consume_an_attempt(
    db, session_factory, dob_patient
):
    """Two wrong guesses arriving together must cost TWO attempts, not one.

    PREMISE, verified independently below rather than assumed: both
    verifications actually ran and both actually failed. Without that
    check this test would pass just as happily if one call silently did
    nothing -- the failure mode the standing convention exists to catch.

    The real hazard is a lost update: both transactions read
    identity_attempts = 0, both write 1, and a caller gets unlimited
    guesses two at a time. The FOR UPDATE in verify_identity is what makes
    that impossible.
    """
    import asyncio

    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=dob_patient.phone
    )
    await db.commit()
    convo_id = convo.id

    async def wrong_guess():
        async with session_factory() as session:
            ctx = ToolContext(session=session, conversation_id=convo_id)
            return await tools.dispatch(ctx, "verify_identity", {"date_of_birth": "1999-01-01"})

    results = await asyncio.gather(wrong_guess(), wrong_guess())

    # PREMISE: both calls ran to completion and both were rejected. If
    # either had errored out or short-circuited, the count below would be
    # meaningless.
    assert len(results) == 2
    assert all(r["status"] == "wrong_details" for r in results), results

    async with session_factory() as session:
        refreshed = await session.get(Conversation, convo_id)
        assert refreshed.identity_attempts == 2, (
            f"expected 2 attempts consumed, got {refreshed.identity_attempts} -- "
            "a lost update means the lockout can be bypassed by sending guesses in pairs"
        )
