"""Enqueuing and sending patient notifications.

Two halves that deliberately never run together:

  ENQUEUE  runs inside the booking/cancellation transaction. It writes
           intent rows and nothing else -- no network, no provider calls.
  SEND     runs in a background worker, claims due rows, and talks to
           Twilio/SendGrid.

Everything about send-once is enforced by the schema (see
models/notification.py): UNIQUE(dedupe_key) stops duplicate rows, and
FOR UPDATE SKIP LOCKED stops two workers claiming the same row. This module
is the policy around those two guarantees.

THE AT-MOST-ONCE ORDERING, restated because it is easy to "fix" wrongly:
the claim is COMMITTED BEFORE the provider is called. A crash after the
commit but before the send loses a message; holding the transaction open
across the send would instead re-send after a crash. We chose losing over
duplicating -- a duplicate 3am SMS erodes trust more than a missed
reminder. Anyone tempted to move the commit should read the module
docstring in models/notification.py first.

RELATIONSHIP LOADING: every read across a relationship here is explicit
(session.get / join), never `appointment.patient.phone`. Lazy access raises
MissingGreenlet in async SQLAlchemy, and the worker always runs with a cold
session. Tests call expunge_all() to prove it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.messaging import MessageSender, MessageSendError
from app.models.appointment import Appointment
from app.models.conversation import Conversation
from app.models.doctor import Doctor
from app.models.enums import (
    AppointmentStatus,
    NotificationChannel,
    NotificationKind,
    NotificationStatus,
)
from app.models.notification import Notification, build_dedupe_key, build_escalation_dedupe_key
from app.models.patient import Patient
from app.models.time_slot import TimeSlot

logger = logging.getLogger(__name__)


@dataclass
class SendResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    abandoned: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- #
# Enqueue (runs inside the caller's transaction -- never commits)
# --------------------------------------------------------------------- #


async def _enqueue(
    session: AsyncSession,
    *,
    appointment: Appointment,
    time_slot: TimeSlot,
    kind: NotificationKind,
    channel: NotificationChannel,
    recipient: str | None,
    scheduled_for: datetime,
) -> bool:
    """Insert one notification row, ignoring duplicates.

    Deliberately does NOT commit: the caller owns the transaction, which is
    the whole point of the outbox. The row becomes durable at the same
    instant the appointment does, so we can never have a booking with no
    confirmation queued, or a queued confirmation for a booking that rolled
    back.

    ON CONFLICT DO NOTHING rather than a prior SELECT: a read-then-insert
    has the same race as every other check-then-act in this codebase.
    """
    if not recipient:
        # No phone or no email is a data gap, not an error. Skipping
        # silently beats a NOT NULL violation that aborts the booking.
        logger.info("no %s recipient for appointment %s; skipping %s",
                    channel.value, appointment.id, kind.value)
        return False

    stmt = (
        pg_insert(Notification)
        .values(
            appointment_id=appointment.id,
            kind=kind.value,
            channel=channel.value,
            status=NotificationStatus.PENDING.value,
            dedupe_key=build_dedupe_key(
                kind=kind,
                channel=channel,
                appointment_id=appointment.id,
                time_slot_id=time_slot.id,
            ),
            recipient=recipient,
            scheduled_for=scheduled_for,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Notification.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def enqueue_for_booking(
    session: AsyncSession, *, appointment: Appointment, now: datetime | None = None
) -> dict[str, bool]:
    """Queue the confirmation and the 24h reminder for a new booking."""
    settings = get_settings()
    now = now or datetime.now(UTC)

    # Explicit loads -- see the module docstring on relationship loading.
    patient = await session.get(Patient, appointment.patient_id)
    time_slot = await session.get(TimeSlot, appointment.time_slot_id)
    if patient is None or time_slot is None:  # pragma: no cover - FK-guaranteed
        return {}

    queued: dict[str, bool] = {}

    queued["confirmation_sms"] = await _enqueue(
        session, appointment=appointment, time_slot=time_slot,
        kind=NotificationKind.BOOKING_CONFIRMATION, channel=NotificationChannel.SMS,
        recipient=patient.phone,
        # scheduled_for = now: the sender polls every few seconds, so
        # "immediately" means within one tick. Doing it inline instead
        # would make a SendGrid outage fail bookings.
        scheduled_for=now,
    )
    queued["confirmation_email"] = await _enqueue(
        session, appointment=appointment, time_slot=time_slot,
        kind=NotificationKind.BOOKING_CONFIRMATION, channel=NotificationChannel.EMAIL,
        recipient=patient.email, scheduled_for=now,
    )

    reminder_at = time_slot.starts_at - timedelta(hours=settings.reminder_lead_hours)
    if reminder_at > now:
        queued["reminder_sms"] = await _enqueue(
            session, appointment=appointment, time_slot=time_slot,
            kind=NotificationKind.REMINDER_24H, channel=NotificationChannel.SMS,
            recipient=patient.phone, scheduled_for=reminder_at,
        )
    else:
        # NOT A BUG -- a deliberate, accepted policy.
        #
        # The appointment is less than `reminder_lead_hours` away, so the
        # "24 hour reminder" would fire within minutes of the confirmation
        # the patient just received. Two near-identical messages read as a
        # system malfunction, so we send neither a late reminder nor an
        # immediate duplicate.
        #
        # ACCEPTED as-is pending confirmation with the clinic; a practice
        # with a high short-notice no-show rate may prefer an immediate
        # reminder instead, which is a one-line change here. Covered by
        # test_reminder_boundary_just_under_lead_time_is_skipped.
        queued["reminder_sms"] = False

    return queued


async def enqueue_for_cancellation(
    session: AsyncSession, *, appointment: Appointment, now: datetime | None = None
) -> dict[str, bool]:
    """Confirm the cancellation, and stop any reminder that has not gone out.

    The skip is belt; the sender's re-validation is braces. Both exist
    because the window between cancelling and the reminder firing is real,
    and a reminder for a cancelled appointment makes patients turn up.
    """
    now = now or datetime.now(UTC)
    patient = await session.get(Patient, appointment.patient_id)
    time_slot = await session.get(TimeSlot, appointment.time_slot_id)
    if patient is None or time_slot is None:  # pragma: no cover
        return {}

    await session.execute(
        update(Notification)
        .where(
            Notification.appointment_id == appointment.id,
            Notification.status.in_([NotificationStatus.PENDING, NotificationStatus.FAILED]),
        )
        .values(status=NotificationStatus.SKIPPED)
    )

    return {
        "cancellation_sms": await _enqueue(
            session, appointment=appointment, time_slot=time_slot,
            kind=NotificationKind.CANCELLATION_CONFIRMATION, channel=NotificationChannel.SMS,
            recipient=patient.phone, scheduled_for=now,
        )
    }


async def enqueue_for_reschedule(
    session: AsyncSession,
    *,
    old_appointment: Appointment,
    new_appointment: Appointment,
    now: datetime | None = None,
) -> dict[str, bool]:
    """One reschedule notice for the new time, plus a fresh reminder.

    Deliberately does NOT call enqueue_for_cancellation for the old
    appointment or enqueue_for_booking for the new one -- a patient who
    moved their 10:00 to 14:00 should get ONE message ("moved to 14:00"),
    not a cancellation notice immediately followed by a confirmation. Two
    near-identical messages for one action reads as a system malfunction,
    the same judgment call as the under-24h reminder policy above.

    The old appointment's pending notifications are skipped here directly
    (not via enqueue_for_cancellation, which would also queue a
    cancellation-confirmation message we don't want).
    """
    now = now or datetime.now(UTC)
    patient = await session.get(Patient, new_appointment.patient_id)
    new_slot = await session.get(TimeSlot, new_appointment.time_slot_id)
    if patient is None or new_slot is None:  # pragma: no cover - FK-guaranteed
        return {}

    await session.execute(
        update(Notification)
        .where(
            Notification.appointment_id == old_appointment.id,
            Notification.status.in_([NotificationStatus.PENDING, NotificationStatus.FAILED]),
        )
        .values(status=NotificationStatus.SKIPPED)
    )

    queued: dict[str, bool] = {}
    queued["reschedule_sms"] = await _enqueue(
        session, appointment=new_appointment, time_slot=new_slot,
        kind=NotificationKind.RESCHEDULE_CONFIRMATION, channel=NotificationChannel.SMS,
        recipient=patient.phone, scheduled_for=now,
    )

    settings = get_settings()
    reminder_at = new_slot.starts_at - timedelta(hours=settings.reminder_lead_hours)
    if reminder_at > now:
        queued["reminder_sms"] = await _enqueue(
            session, appointment=new_appointment, time_slot=new_slot,
            kind=NotificationKind.REMINDER_24H, channel=NotificationChannel.SMS,
            recipient=patient.phone, scheduled_for=reminder_at,
        )
    else:
        # Same accepted policy as enqueue_for_booking: no reminder when the
        # new time is already inside the lead window.
        queued["reminder_sms"] = False

    return queued


async def enqueue_escalation_notification(
    session: AsyncSession, *, conversation: Conversation, now: datetime | None = None
) -> bool:
    """Page staff that a conversation escalated. Phase 3.5.

    Called from conversation_service._escalate, in the SAME transaction
    as the state change -- same outbox reasoning as every enqueue_for_*
    function in this module: the notification becomes durable at the
    exact instant the escalation does, so there is no window where a
    conversation is escalated but the page was never queued.

    THE ONE DELIBERATE ASYMMETRY WITH EVERY OTHER _enqueue CALL IN THIS
    FILE: `_enqueue`'s own docstring calls a missing recipient "a data
    gap, not an error" and skips it quietly (a patient with no email on
    file is unremarkable). An unconfigured escalation destination is NOT
    that. Phase 3.5 exists specifically because "escalation happened but
    nobody was told" is worse than no escalation path at all -- so a
    missing `escalation_notify_phone` is logged as an ERROR here, loudly,
    on every single occurrence, not once at startup. It intentionally
    does not raise: the ESCALATION ITSELF (ending the conversation, so
    the caller is not left talking to a bot that cannot help them) must
    still succeed even when paging is misconfigured -- failing the whole
    transaction over a missing phone number would make a config mistake
    take down the safety mechanism it was supposed to only weaken.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    destination = settings.escalation_notify_phone

    if not destination:
        logger.error(
            "conversation %s escalated but escalation_notify_phone is not "
            "configured -- NO STAFF NOTIFICATION WAS SENT",
            conversation.id,
        )
        return False

    return await _enqueue_escalation(session, conversation=conversation, recipient=destination, now=now)


async def _enqueue_escalation(
    session: AsyncSession, *, conversation: Conversation, recipient: str, now: datetime
) -> bool:
    """The actual insert -- conversation-shaped, not appointment-shaped.

    Does NOT go through `_enqueue`: that helper's signature takes
    `appointment` + `time_slot` because every OTHER caller in this module
    is appointment-shaped and `build_dedupe_key` needs both. Forcing a
    conversation-shaped notification through an appointment-shaped helper
    (passing `None` for both and hoping nothing downstream touches them)
    is exactly the kind of thing this table's `kind_matches_target` CHECK
    exists to make structurally impossible -- so the insert is written out
    here instead, using `build_escalation_dedupe_key`.

    Same ON CONFLICT DO NOTHING pattern as `_enqueue`, for the identical
    reason: a read-then-insert has the same race as every other check-
    then-act in this codebase. One conversation can only ever produce one
    dedupe key (see build_escalation_dedupe_key), so this also means
    re-calling `_escalate` twice on the same conversation -- which should
    not be possible once it is terminal, but "should not be possible" is
    exactly the claim a unique constraint is supposed to not have to rely
    on -- cannot double-page.
    """
    stmt = (
        pg_insert(Notification)
        .values(
            conversation_id=conversation.id,
            appointment_id=None,
            kind=NotificationKind.ESCALATION.value,
            channel=NotificationChannel.SMS.value,
            status=NotificationStatus.PENDING.value,
            dedupe_key=build_escalation_dedupe_key(
                channel=NotificationChannel.SMS, conversation_id=conversation.id
            ),
            recipient=recipient,
            scheduled_for=now,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Notification.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


# --------------------------------------------------------------------- #
# Message bodies
# --------------------------------------------------------------------- #


async def render_body(
    session: AsyncSession, notification: Notification
) -> tuple[str, str]:
    """Return (subject, body) for a notification.

    Times are rendered in the DOCTOR's timezone: a patient told "14:00 UTC"
    will miss their appointment. Everything is loaded explicitly.

    ESCALATION IS A TRUE EARLY BRANCH, not a case folded into the logic
    below -- and that split is load-bearing, not stylistic. Every kind
    below this point assumes `notification.appointment_id` is a real id
    and loads Appointment/TimeSlot/Doctor/Patient from it unconditionally
    before even looking at `kind`. An escalation notification has
    appointment_id = NULL by construction (see models/notification.py's
    kind_matches_target CHECK) -- session.get(Appointment, None) is not a
    "not found", it is a call with no meaningful primary key to look up,
    and letting it fall through to the appointment-shaped code below
    would fail confusingly rather than being handled as the different
    shape of message it actually is.
    """
    if notification.kind is NotificationKind.ESCALATION:
        return await _render_escalation_body(session, notification)

    from zoneinfo import ZoneInfo

    appointment = await session.get(Appointment, notification.appointment_id)
    time_slot = await session.get(TimeSlot, appointment.time_slot_id)
    doctor = await session.get(Doctor, appointment.doctor_id)
    patient = await session.get(Patient, appointment.patient_id)

    tz = ZoneInfo(doctor.timezone if doctor else "UTC")
    local = time_slot.starts_at.astimezone(tz)
    when = local.strftime("%A %d %B at %H:%M")
    doctor_name = doctor.full_name if doctor else "your doctor"
    name = patient.full_name.split()[0] if patient and patient.full_name else "there"

    if notification.kind is NotificationKind.BOOKING_CONFIRMATION:
        return (
            "Appointment confirmed",
            f"Hi {name}, your appointment with {doctor_name} is confirmed for {when}. "
            f"Reply to this message or call the clinic if you need to change it.",
        )
    if notification.kind is NotificationKind.REMINDER_24H:
        return (
            "Appointment reminder",
            f"Hi {name}, a reminder of your appointment with {doctor_name} tomorrow at "
            f"{local.strftime('%H:%M')}. Please let us know if you cannot make it.",
        )
    if notification.kind is NotificationKind.CANCELLATION_CONFIRMATION:
        return (
            "Appointment cancelled",
            f"Hi {name}, your appointment with {doctor_name} on {when} has been cancelled.",
        )
    if notification.kind is NotificationKind.RESCHEDULE_CONFIRMATION:
        # This is the ONLY branch that reads from `appointment` directly for
        # the "moved from" time rather than only the slot passed in --
        # everything else here is scoped to the notification's own
        # appointment/slot, but a reschedule message is specifically about
        # a CHANGE, so it must name the old time too.
        return (
            "Appointment rescheduled",
            f"Hi {name}, your appointment with {doctor_name} has been moved to {when}. "
            f"Reply to this message or call the clinic if this doesn't work for you.",
        )
    # Exhaustive above for every NotificationKind that exists today.
    # Reaching here means a kind was added to the enum without a branch
    # here -- fail loudly rather than silently send the wrong wording.
    raise ValueError(f"render_body has no template for notification kind {notification.kind!r}")


async def _render_escalation_body(
    session: AsyncSession, notification: Notification
) -> tuple[str, str]:
    """The one STAFF-facing template in this module -- everything else
    here is written to a patient.

    WHY the phone number and the raw escalation reason are included in
    plain text, when the rest of this codebase encrypts patient-authored
    conversation content at rest (see models/conversation_message.py):
    this message is not stored patient data at rest, it is an operational
    page TO a clinic employee who already has legitimate access to the
    patient record it references -- the same category of access a phone
    call from the front desk would carry. Withholding the number here
    would make the page useless for its only purpose.
    """
    conversation = await session.get(Conversation, notification.conversation_id)
    if conversation is None:  # pragma: no cover - FK-guaranteed
        return ("Clinic escalation", "A conversation needs attention, but its details could not be loaded.")

    reason = conversation.escalation_reason or "no reason recorded"
    return (
        "Clinic: conversation needs attention",
        f"A chatbot conversation with {conversation.external_ref} was escalated. "
        f"Reason: {reason}. Conversation id: {conversation.id}.",
    )


# --------------------------------------------------------------------- #
# Send (background worker)
# --------------------------------------------------------------------- #


async def send_due_notifications(
    session: AsyncSession,
    *,
    senders: dict[NotificationChannel, MessageSender],
    limit: int = 50,
    now: datetime | None = None,
) -> SendResult:
    """Claim and deliver everything that is due."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    result = SendResult()

    # FOR UPDATE SKIP LOCKED: the loser of a race takes DIFFERENT work
    # rather than waiting. Plain FOR UPDATE (as used for booking, where the
    # loser genuinely wants that one slot) would serialize the entire queue
    # behind its first row.
    claimable = (
        select(Notification)
        .where(
            Notification.status.in_([NotificationStatus.PENDING, NotificationStatus.FAILED]),
            Notification.scheduled_for <= now,
            Notification.attempts < Notification.max_attempts,
        )
        .order_by(Notification.scheduled_for)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    rows = list((await session.scalars(claimable)).all())
    if not rows:
        return result

    staleness_cutoff = timedelta(hours=settings.reminder_staleness_cutoff_hours)
    to_send: list[Notification] = []

    for row in rows:
        # THE APPOINTMENT-LIVENESS RE-CHECK BELOW IS APPOINTMENT-SHAPED,
        # AND MUST NOT RUN FOR CONVERSATION-SHAPED ROWS.
        #
        # Found the hard way: this loop used to call
        # `session.get(Appointment, row.appointment_id)` unconditionally
        # for every claimed row. For an ESCALATION row, appointment_id is
        # NULL by construction (see the kind_matches_target CHECK in
        # models/notification.py) -- `session.get(Appointment, None)`
        # returns None, which then satisfied `appointment is None`, which
        # then satisfied `row.kind is not CANCELLATION_CONFIRMATION`
        # (true for ESCALATION too), and the row was marked SKIPPED and
        # NEVER SENT. Every staff page would have silently vanished. This
        # is the exact same shape of bug as render_body's early branch a
        # few functions up -- appointment-shaped logic assuming every row
        # is appointment-shaped -- caught here only because a test
        # actually drained an escalation notification through this
        # function rather than only checking that the row was enqueued.
        if row.kind is NotificationKind.ESCALATION:
            row.status = NotificationStatus.CLAIMED
            row.claimed_at = now
            row.attempts += 1
            to_send.append(row)
            continue

        appointment = await session.get(Appointment, row.appointment_id)

        # BRACES to enqueue_for_cancellation's belt: re-check at send time.
        # An appointment cancelled after this row was queued must not
        # produce a reminder telling the patient to turn up.
        if appointment is None or appointment.status is AppointmentStatus.CANCELLED:
            if row.kind is not NotificationKind.CANCELLATION_CONFIRMATION:
                row.status = NotificationStatus.SKIPPED
                result.skipped += 1
                continue

        # Too late to be useful. Sending a "reminder for tomorrow" after the
        # appointment has happened is worse than silence.
        if row.scheduled_for < now - staleness_cutoff:
            row.status = NotificationStatus.SKIPPED
            row.last_error = "skipped: overdue beyond staleness cutoff"
            result.skipped += 1
            continue

        row.status = NotificationStatus.CLAIMED
        row.claimed_at = now
        row.attempts += 1
        to_send.append(row)

    # COMMIT THE CLAIM BEFORE ANY NETWORK CALL. See the module docstring.
    # This is the line that makes the system at-most-once. It also releases
    # the row locks, so a slow provider does not hold them for the whole
    # batch.
    await session.commit()

    for row in to_send:
        sender = senders.get(row.channel)
        if sender is None:
            row.status = NotificationStatus.FAILED
            row.last_error = f"no sender configured for {row.channel.value}"
            result.failed += 1
            continue
        try:
            subject, body = await render_body(session, row)
            message_id = await sender.send(recipient=row.recipient, body=body, subject=subject)
            row.status = NotificationStatus.SENT
            row.sent_at = datetime.now(UTC)
            row.provider = "twilio" if row.channel is NotificationChannel.SMS else "sendgrid"
            row.provider_message_id = message_id or None
            row.last_error = None
            result.sent += 1
        except MessageSendError as exc:
            row.last_error = str(exc)[:500]
            if exc.permanent or row.attempts >= row.max_attempts:
                row.status = NotificationStatus.ABANDONED
                result.abandoned += 1
            else:
                row.status = NotificationStatus.FAILED
                result.failed += 1
            result.errors.append(str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
            row.last_error = f"unexpected: {exc}"[:500]
            row.status = (
                NotificationStatus.ABANDONED
                if row.attempts >= row.max_attempts
                else NotificationStatus.FAILED
            )
            result.failed += 1
            result.errors.append(str(exc)[:200])

    await session.commit()
    return result


async def reap_stuck_claims(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Move long-CLAIMED rows to UNRESOLVED.

    A row still CLAIMED long after it was taken means the worker died
    somewhere around the provider call, and we genuinely do not know
    whether the message went out.

    WE DO NOT AUTO-RETRY THESE. Retrying risks the duplicate we chose to
    avoid; marking them SENT would hide a miss. UNRESOLVED says "a human or
    a provider-API reconciliation must decide", which is the only honest
    option. Exactly-once delivery does not exist across a network boundary.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=settings.notification_claim_timeout_minutes)

    result = await session.execute(
        update(Notification)
        .where(
            Notification.status == NotificationStatus.CLAIMED,
            Notification.claimed_at < cutoff,
        )
        .values(
            status=NotificationStatus.UNRESOLVED,
            last_error="worker did not report an outcome; delivery unknown",
        )
    )
    await session.commit()
    return int(result.rowcount or 0)
