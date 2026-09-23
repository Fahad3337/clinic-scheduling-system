"""Creating and confirming proposed mutations.

See models/booking_proposal.py for why this indirection exists at all.
This module is the enforcement: a confirm can only ever act on a row this
service wrote, for this conversation, for this patient, within its TTL,
exactly once.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.appointment import Appointment
from app.models.booking_proposal import BookingProposal
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.enums import (
    AppointmentStatus,
    BookingChannel,
    ConversationChannel,
    ProposalKind,
)
from app.models.time_slot import TimeSlot
from app.services import appointment_service
from app.services.exceptions import (
    AppointmentNotFoundError,
    ProposalKindMismatchError,
    ProposalNotFoundError,
    SlotAlreadyBookedError,
    SlotBlockedError,
    TimeSlotNotFoundError,
)

logger = logging.getLogger(__name__)

# A conversation's transport maps to how the resulting appointment was
# booked. One place, so the two enums never drift into meaning the same
# thing badly -- see the note on ConversationChannel.
_CHANNEL_TO_BOOKING_CHANNEL = {
    ConversationChannel.SMS: BookingChannel.CHAT,
    ConversationChannel.WEB: BookingChannel.CHAT,
    ConversationChannel.VOICE: BookingChannel.VOICE,
}


def describe_slot_local(slot: TimeSlot, doctor: Doctor) -> str:
    """Render a slot in the DOCTOR'S timezone, as a human would say it.

    THIS CROSSES THE SERVICE/MODEL BOUNDARY DELIBERATELY, and it is worth
    naming why rather than leaving it as a convenience: the doctor's
    timezone is a fact only the service has, and converting a UTC instant
    into "Tuesday 22 September at 10:00" is arithmetic language models do
    badly and confidently. Handing back a pre-rendered local string means
    the model QUOTES a time rather than COMPUTING one. A patient told the
    wrong hour turns up on the wrong day; this is the cheapest possible
    guard against that.
    """
    local = slot.starts_at.astimezone(ZoneInfo(doctor.timezone))
    return local.strftime("%A %d %B at %H:%M")


async def propose_booking(
    session: AsyncSession,
    *,
    conversation: Conversation,
    time_slot_id: UUID,
    now: datetime | None = None,
) -> tuple[BookingProposal, str]:
    """Offer a specific slot. Books nothing.

    Validates the slot as far as a read can: exists, not blocked, not
    already taken. That validation is deliberately NOT a guarantee -- the
    slot can still be gone by confirm time, which is why confirm goes
    through the full Phase 1 booking path with its lock and its unique
    index rather than trusting this check.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)

    slot = await session.get(TimeSlot, time_slot_id)
    if slot is None:
        raise TimeSlotNotFoundError(time_slot_id)
    if slot.is_blocked:
        raise SlotBlockedError(time_slot_id)

    taken = await session.scalar(
        select(Appointment).where(
            Appointment.time_slot_id == time_slot_id,
            Appointment.status != AppointmentStatus.CANCELLED,
        )
    )
    if taken is not None:
        raise SlotAlreadyBookedError(time_slot_id)

    doctor = await session.get(Doctor, slot.doctor_id)

    proposal = BookingProposal(
        conversation_id=conversation.id,
        patient_id=conversation.patient_id,
        kind=ProposalKind.BOOK,
        time_slot_id=time_slot_id,
        expires_at=now + timedelta(minutes=settings.proposal_ttl_minutes),
    )
    session.add(proposal)
    await session.flush()
    await session.commit()
    return proposal, describe_slot_local(slot, doctor)


async def propose_cancellation(
    session: AsyncSession,
    *,
    conversation: Conversation,
    appointment_id: UUID,
    now: datetime | None = None,
) -> tuple[BookingProposal, str]:
    """Offer to cancel a specific appointment. Cancels nothing.

    THE AUTHORIZATION CHECK THAT MATTERS: the appointment must belong to
    the conversation's verified patient. The model can pass any UUID it
    likes -- including one a caller typed, or one it invented -- and an
    appointment belonging to someone else is rejected here, before a
    proposal for it can ever exist.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)

    appointment = await session.get(Appointment, appointment_id)
    # Not-found and not-yours are ONE outcome on purpose: telling a caller
    # "that appointment exists but is not yours" confirms the existence of
    # another patient's record.
    if appointment is None or appointment.patient_id != conversation.patient_id:
        raise AppointmentNotFoundError(appointment_id)
    if appointment.status is not AppointmentStatus.BOOKED:
        raise AppointmentNotFoundError(appointment_id)

    slot = await session.get(TimeSlot, appointment.time_slot_id)
    doctor = await session.get(Doctor, appointment.doctor_id)

    proposal = BookingProposal(
        conversation_id=conversation.id,
        patient_id=conversation.patient_id,
        kind=ProposalKind.CANCEL,
        appointment_id=appointment_id,
        expires_at=now + timedelta(minutes=settings.proposal_ttl_minutes),
    )
    session.add(proposal)
    await session.flush()
    await session.commit()
    return proposal, describe_slot_local(slot, doctor)


async def _claim_proposal(
    session: AsyncSession,
    *,
    proposal_id: UUID,
    conversation: Conversation,
    expected_kind: ProposalKind,
    now: datetime,
) -> BookingProposal:
    """Atomically take ownership of a proposal, or refuse.

    ONE conditional UPDATE, not SELECT-then-UPDATE: two confirms arriving
    together (a patient texting "yes" twice) must not both succeed and
    book twice. Same primitive as the OAuth state claim, for the same
    reason.

    The WHERE clause is the whole authorization check, and every term in
    it earns its place:
      - id            : the proposal named
      - conversation  : proposals do not cross conversations
      - patient       : nor re-bound patients (see the model's note)
      - consumed_at   : single use
      - expires_at    : still fresh
    """
    result = await session.execute(
        update(BookingProposal)
        .where(
            BookingProposal.id == proposal_id,
            BookingProposal.conversation_id == conversation.id,
            BookingProposal.patient_id == conversation.patient_id,
            BookingProposal.consumed_at.is_(None),
            BookingProposal.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(BookingProposal)
    )
    proposal = result.scalar_one_or_none()
    if proposal is None:
        # Unknown / wrong conversation / already used / expired all land
        # here -- see ProposalNotFoundError.
        raise ProposalNotFoundError()

    if proposal.kind is not expected_kind:
        # Consumed-but-wrong-kind: the claim already happened, so roll it
        # back rather than burning a legitimate proposal because the model
        # called the wrong confirm tool.
        #
        # READ THE ATTRIBUTE FIRST. `session.rollback()` EXPIRES every
        # attribute on every instance in the session, so touching
        # `proposal.kind` afterwards is a lazy re-load -- which in async
        # SQLAlchemy is an IO attempt in the wrong place, not a quiet
        # refetch. Note that `expire_on_commit=False` does NOT help here:
        # it governs commit, and rollback expires regardless. Same family
        # as every other "do not read ORM state after changing session
        # state" bug in this codebase.
        actual_kind = proposal.kind
        await session.rollback()
        raise ProposalKindMismatchError(expected_kind, actual_kind)

    return proposal


async def confirm_booking(
    session: AsyncSession,
    *,
    conversation: Conversation,
    proposal_id: UUID,
    now: datetime | None = None,
) -> tuple[Appointment, str]:
    """Execute a BOOK proposal through the ordinary Phase 1 booking path."""
    now = now or datetime.now(UTC)
    proposal = await _claim_proposal(
        session,
        proposal_id=proposal_id,
        conversation=conversation,
        expected_kind=ProposalKind.BOOK,
        now=now,
    )
    await session.commit()

    # Straight through appointment_service -- NOT a parallel booking
    # implementation. Every Phase 1 guarantee (the FOR UPDATE lock, the
    # partial unique index, the calendar outbox, the notification outbox)
    # applies to a chatbot booking exactly as it does to a front-desk one,
    # because it is the same function. If this module ever grows its own
    # INSERT, that is the bug.
    appointment = await appointment_service.book_appointment(
        session,
        patient_id=proposal.patient_id,
        time_slot_id=proposal.time_slot_id,
        booking_channel=_CHANNEL_TO_BOOKING_CHANNEL[conversation.channel],
    )

    slot = await session.get(TimeSlot, appointment.time_slot_id)
    doctor = await session.get(Doctor, appointment.doctor_id)
    logger.info("conversation %s booked appointment %s", conversation.id, appointment.id)
    return appointment, describe_slot_local(slot, doctor)


async def confirm_cancellation(
    session: AsyncSession,
    *,
    conversation: Conversation,
    proposal_id: UUID,
    now: datetime | None = None,
) -> tuple[Appointment, str]:
    """Execute a CANCEL proposal through the ordinary cancellation path."""
    now = now or datetime.now(UTC)
    proposal = await _claim_proposal(
        session,
        proposal_id=proposal_id,
        conversation=conversation,
        expected_kind=ProposalKind.CANCEL,
        now=now,
    )
    await session.commit()

    slot_description = ""
    appointment = await session.get(Appointment, proposal.appointment_id)
    if appointment is not None:
        slot = await session.get(TimeSlot, appointment.time_slot_id)
        doctor = await session.get(Doctor, appointment.doctor_id)
        slot_description = describe_slot_local(slot, doctor)

    cancelled = await appointment_service.cancel_appointment(
        session,
        appointment_id=proposal.appointment_id,
        cancellation_reason="Cancelled by patient via chatbot",
    )
    logger.info("conversation %s cancelled appointment %s", conversation.id, cancelled.id)
    return cancelled, slot_description
