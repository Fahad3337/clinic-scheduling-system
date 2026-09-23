"""Booking, cancellation, and lifecycle logic for appointments.

This module is the ONE place double-booking is prevented at the application
level. It works together with two schema-level guarantees documented in
models/appointment.py:
  1. A partial unique index (`uq_appointments_active_slot`) -- the invariant
     that makes double-booking impossible, no matter what writes it.
  2. `SELECT ... FOR UPDATE` on the time_slot row -- taken here, before the
     INSERT, so the two concurrent bookers queue instead of racing to hit the
     index and one getting an ugly IntegrityError.

Read the appointment_service tests (tests/test_booking_race.py) alongside
this file -- the test drives two real concurrent transactions at the same
slot and asserts exactly one wins.

INTERNAL STRUCTURE: `book_appointment`, `cancel_appointment` and
`reschedule_appointment` are all thin, COMMITTING wrappers around a shared
set of non-committing `_lock_*` / `_insert_*` / `_mark_cancelled` helpers.
Reschedule needs to do "cancel one appointment, book another" as ONE atomic
transaction -- if it called the public `book_appointment`/`cancel_appointment`
functions directly, each would commit separately, and a crash between the
two commits would leave a patient with a cancelled appointment and no new
one. Factoring the no-commit core out and having reschedule call it twice
under one commit is what keeps that impossible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.appointment_external_event import AppointmentExternalEvent
from app.models.calendar_connection import CalendarConnection
from app.models.enums import (
    AppointmentStatus,
    BookingChannel,
    CalendarConnectionState,
    CalendarPushState,
)
from app.models.patient import Patient
from app.models.time_slot import TimeSlot
from app.services.exceptions import (
    AppointmentNotFoundError,
    InvalidStatusTransitionError,
    PatientNotFoundError,
    RescheduleAcrossDoctorsError,
    RescheduleToSameSlotError,
    SlotAlreadyBookedError,
    SlotBlockedError,
    TimeSlotNotFoundError,
)

# Terminal statuses that a cancellation cannot touch. Kept as a module-level
# constant (rather than inline in cancel_appointment) so the state machine is
# visible in one glance without reading the method body.
_TERMINAL_STATUSES = frozenset(
    {AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW}
)


# =========================================================================
# Internal, non-committing core. Every function below assumes the caller
# owns the transaction and will commit (or roll back) it.
# =========================================================================


async def _lock_and_validate_slot(session: AsyncSession, time_slot_id: UUID) -> TimeSlot:
    """Lock a time_slot and confirm it is currently bookable.

    See book_appointment's docstring for the full concurrency walkthrough --
    this is step 1-2 of it, factored out so reschedule can lock the NEW slot
    with the identical guarantee without duplicating the logic.
    """
    slot = await session.scalar(
        select(TimeSlot)
        .where(TimeSlot.id == time_slot_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if slot is None:
        raise TimeSlotNotFoundError(time_slot_id)
    if slot.is_blocked:
        raise SlotBlockedError(time_slot_id)

    existing = await session.scalar(
        select(Appointment).where(
            Appointment.time_slot_id == time_slot_id,
            Appointment.status != AppointmentStatus.CANCELLED,
        )
    )
    if existing is not None:
        raise SlotAlreadyBookedError(time_slot_id)

    return slot


async def _insert_appointment(
    session: AsyncSession,
    *,
    patient_id: UUID,
    doctor_id: UUID,
    time_slot_id: UUID,
    booking_channel: BookingChannel,
    reason: str | None,
    rescheduled_from_id: UUID | None = None,
) -> Appointment:
    """Create the row and queue its calendar push. No notification, no commit.

    Notification enqueueing is deliberately NOT here: booking wants a
    booking-confirmation message, rescheduling wants a single reschedule
    message, and folding both callers' notification needs into one helper
    would just reintroduce a branch here. Each public function enqueues its
    own.
    """
    appointment = Appointment(
        patient_id=patient_id,
        doctor_id=doctor_id,
        time_slot_id=time_slot_id,
        status=AppointmentStatus.BOOKED,
        booking_channel=booking_channel,
        reason=reason,
        rescheduled_from_id=rescheduled_from_id,
    )
    session.add(appointment)

    # EXPLICIT flush, not incidental. `appointment.id` has a client-side
    # Python default (uuid.uuid4, see db/base.py) rather than a database
    # default -- but a column default, client-side or not, is only
    # EVALUATED when SQLAlchemy compiles the INSERT, which happens at
    # flush. Until then `appointment.id` is None.
    #
    # THIS WAS A REAL, LATENT BUG: earlier code assumed the id was already
    # readable here and relied on it implicitly working -- and it DID work,
    # by accident, because the query inside _enqueue_calendar_push right
    # below (a SELECT on CalendarConnection) triggers SQLAlchemy's
    # autoflush before running, which flushed the pending INSERT as a side
    # effect. That masked the bug in every environment where autoflush is
    # on -- including the entire test suite, whose session_factory fixture
    # never set autoflush=False. Production's session factory (db/session.py)
    # sets autoflush=False DELIBERATELY ("explicit flushes only; surprise
    # flushes mid-lock are hard to reason about") -- which is exactly the
    # setting that stopped the accidental flush from happening, and exactly
    # why this surfaced in production, live, and nowhere in 137 passing
    # tests. Do not remove this flush and go back to relying on an
    # incidental side effect of the next query -- if that query is ever
    # reordered, deleted, or short-circuited, this bug returns silently.
    await session.flush()

    # Transactional outbox: queue the push to the doctor's calendar in the
    # SAME transaction as the appointment. See the Phase 2 module docstring
    # in calendar_sync_service.py for why Google is never called inline
    # here. `appointment.id` is safe to read now.
    await _enqueue_calendar_push(session, appointment)
    return appointment


async def _mark_cancelled(
    session: AsyncSession, *, appointment: Appointment, cancellation_reason: str | None
) -> None:
    """Flip an already-locked, already-validated appointment to CANCELLED.

    Caller is responsible for the terminal-status check -- this function
    assumes it has already been done, because reschedule's check ("cancel"
    vs "reschedule" as the attempted action in the error) needs to happen
    with its own wording, not this one's.
    """
    appointment.status = AppointmentStatus.CANCELLED
    appointment.cancelled_at = datetime.now(UTC)
    appointment.cancellation_reason = cancellation_reason

    # Ask the worker to remove the event from the doctor's calendar. A row
    # still in PENDING (never pushed) goes straight to DELETED -- there is
    # nothing out there to remove, and asking Google to delete an event that
    # was never created would just 404.
    pushes = (
        await session.scalars(
            select(AppointmentExternalEvent).where(
                AppointmentExternalEvent.appointment_id == appointment.id
            )
        )
    ).all()
    for push in pushes:
        if push.external_event_id is None:
            push.push_state = CalendarPushState.DELETED
        elif push.push_state is not CalendarPushState.DELETED:
            push.push_state = CalendarPushState.DELETE_PENDING
            push.attempts = 0  # a fresh operation deserves a fresh budget


async def _enqueue_calendar_push(session: AsyncSession, appointment: Appointment) -> None:
    """Queue an outbox row if the doctor has a live calendar connection.

    Silently does nothing when no calendar is connected -- that is the
    normal state for a clinic that has not set up sync, not an error, and
    booking must never depend on an optional integration being configured.
    """
    connection = await session.scalar(
        select(CalendarConnection).where(
            CalendarConnection.doctor_id == appointment.doctor_id,
            CalendarConnection.state == CalendarConnectionState.ACTIVE,
        )
    )
    if connection is None:
        return

    session.add(
        AppointmentExternalEvent(
            appointment_id=appointment.id,
            connection_id=connection.id,
            push_state=CalendarPushState.PENDING,
        )
    )


# =========================================================================
# Public, committing API
# =========================================================================


async def book_appointment(
    session: AsyncSession,
    *,
    patient_id: UUID,
    time_slot_id: UUID,
    booking_channel: BookingChannel = BookingChannel.WEB,
    reason: str | None = None,
) -> Appointment:
    """Book `time_slot_id` for `patient_id`, or raise if it can't be booked.

    Concurrency walkthrough (this is the important part):

      1. We SELECT the time_slot FOR UPDATE. Postgres blocks any other
         transaction that also tries to lock this same row (via FOR UPDATE,
         directly or through this same function) until we COMMIT or
         ROLLBACK. Transactions locking *different* slots never block each
         other -- contention is per-row, not global.
      2. While holding the lock, we check for an existing active appointment
         on this slot. Because we hold the lock, no other transaction could
         have inserted one since we started (any competitor is stuck at step
         1). This check is now safe to trust.
      3. We INSERT the new appointment and COMMIT, releasing the lock. The
         next waiting transaction acquires it, re-runs step 2, and now sees
         our row -- so it raises SlotAlreadyBookedError cleanly instead of
         attempting the INSERT.
      4. As defense in depth, if the INSERT is somehow still attempted for an
         already-booked slot (e.g. a future code path that forgets to lock
         first), the partial unique index rejects it and we translate that
         IntegrityError into the same SlotAlreadyBookedError. The lock is what
         makes this the rare path instead of the common one.

    WHY the lock is taken on time_slots and not appointments: at the moment
    we need to serialize, the competing appointment row may not exist yet --
    there is nothing to lock. The time_slot is the resource being contended
    for, so it is the natural thing to lock, and it exists before any
    appointment does.
    """
    slot = await _lock_and_validate_slot(session, time_slot_id)

    # Fail fast on an unknown patient before we do any writing. Not strictly
    # required for the concurrency guarantee, but a 404 here is a better
    # error than a foreign-key violation surfacing from the INSERT below.
    patient = await session.get(Patient, patient_id)
    if patient is None:
        raise PatientNotFoundError(patient_id)

    # doctor_id is copied from the slot, never trusted from the caller --
    # see the denormalization note in models/appointment.py.
    appointment = await _insert_appointment(
        session,
        patient_id=patient_id,
        doctor_id=slot.doctor_id,
        time_slot_id=time_slot_id,
        booking_channel=booking_channel,
        reason=reason,
    )

    # Same transaction, same outbox reasoning: the confirmation and the
    # 24h reminder become durable at the instant the booking does. Calling
    # SendGrid inline here would mean a SendGrid outage fails bookings --
    # an optional integration must never be able to stop a patient getting
    # an appointment.
    #
    # Imported here rather than at module scope to avoid a circular import:
    # notification_service imports appointment models, and this module
    # would otherwise import it back at load time.
    from app.services import notification_service

    await notification_service.enqueue_for_booking(session, appointment=appointment)

    try:
        await session.commit()
    except IntegrityError as exc:
        # --- 4. Defense in depth: see docstring point 4 above. Reaching this
        # branch under normal operation means the lock step was bypassed
        # somewhere -- it should not happen via this function, but the
        # constraint exists precisely so we never depend on that assumption.
        await session.rollback()
        raise SlotAlreadyBookedError(time_slot_id) from exc

    await session.refresh(appointment)
    return appointment


async def get_appointment(session: AsyncSession, appointment_id: UUID) -> Appointment:
    appointment = await session.get(Appointment, appointment_id)
    if appointment is None:
        raise AppointmentNotFoundError(appointment_id)
    return appointment


async def cancel_appointment(
    session: AsyncSession,
    *,
    appointment_id: UUID,
    cancellation_reason: str | None = None,
) -> Appointment:
    """Cancel a booked appointment, freeing its slot for rebooking.

    WHY this also needs to lock: two simultaneous cancel + reschedule-style
    calls on the same appointment are a much smaller risk than the booking
    race, but SELECT ... FOR UPDATE here costs nothing and keeps the
    read-modify-write on `status` from being a second unguarded check-then-act.
    """
    appointment = await session.scalar(
        select(Appointment)
        .where(Appointment.id == appointment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if appointment is None:
        raise AppointmentNotFoundError(appointment_id)

    if appointment.status in _TERMINAL_STATUSES:
        raise InvalidStatusTransitionError(appointment.status, "cancel")

    await _mark_cancelled(session, appointment=appointment, cancellation_reason=cancellation_reason)

    # Queue the cancellation notice and skip any reminder not yet sent.
    from app.services import notification_service

    await notification_service.enqueue_for_cancellation(session, appointment=appointment)

    # No IntegrityError handling needed here: cancelling never conflicts with
    # the partial unique index (a cancelled row is explicitly excluded from
    # it), so this write cannot lose a race the way booking can.
    await session.commit()
    await session.refresh(appointment)
    return appointment


async def reschedule_appointment(
    session: AsyncSession,
    *,
    appointment_id: UUID,
    new_time_slot_id: UUID,
    reason: str | None = None,
) -> Appointment:
    """Move a BOOKED appointment to a new time slot.

    Implemented as cancel-old + book-new, ONE new Appointment row linked to
    the old one via `rescheduled_from_id`, committed atomically -- NOT as an
    in-place mutation of `time_slot_id`. See the module docstring in
    models/appointment.py (the rescheduled_from_id field) for the full
    reasoning: it is the same audit-trail argument that made Phase 1
    cancel-and-rebook instead of un-cancelling, and it means the
    notification dedupe key never has to special-case a slot change -- a
    reschedule always produces a new appointment_id, which is automatically
    a new dedupe key.

    LOCK ORDER: old appointment, then new slot, always in that order. Two
    concurrent reschedules can only deadlock if they lock the same two
    resources in opposite orders; a fixed order makes that impossible
    regardless of which reschedule reads which row first.
    """
    old = await session.scalar(
        select(Appointment)
        .where(Appointment.id == appointment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if old is None:
        raise AppointmentNotFoundError(appointment_id)
    if old.status in _TERMINAL_STATUSES:
        raise InvalidStatusTransitionError(old.status, "reschedule")
    if old.time_slot_id == new_time_slot_id:
        raise RescheduleToSameSlotError(new_time_slot_id)

    new_slot = await _lock_and_validate_slot(session, new_time_slot_id)
    if new_slot.doctor_id != old.doctor_id:
        raise RescheduleAcrossDoctorsError(old.doctor_id, new_slot.doctor_id)

    new_appointment = await _insert_appointment(
        session,
        patient_id=old.patient_id,
        doctor_id=new_slot.doctor_id,
        time_slot_id=new_time_slot_id,
        booking_channel=old.booking_channel,
        reason=reason if reason is not None else old.reason,
        rescheduled_from_id=old.id,
    )

    await _mark_cancelled(
        session,
        appointment=old,
        cancellation_reason=f"Rescheduled to time slot {new_time_slot_id}",
    )

    # ONE reschedule notification, not a cancellation notice plus a separate
    # booking confirmation -- two near-identical messages for one patient
    # action reads as a system glitch. See notification_service.
    from app.services import notification_service

    await notification_service.enqueue_for_reschedule(
        session, old_appointment=old, new_appointment=new_appointment
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        # Same defense-in-depth as book_appointment: the lock above should
        # make this unreachable, but the partial unique index is what
        # actually guarantees no double-booking, not our discipline in
        # calling the lock first.
        await session.rollback()
        raise SlotAlreadyBookedError(new_time_slot_id) from exc

    await session.refresh(new_appointment)
    return new_appointment
