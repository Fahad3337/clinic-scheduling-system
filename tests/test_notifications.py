"""Notification tests: idempotency, at-most-once ordering, cold sessions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.integrations.messaging import MessageSendError
from app.models.enums import (
    AppointmentStatus,
    NotificationChannel,
    NotificationKind,
    NotificationStatus,
)
from app.models.notification import Notification, build_dedupe_key
from app.services import appointment_service, notification_service


class FakeSender:
    """Records every send. Optionally fails."""

    def __init__(self, *, fail_with: Exception | None = None, message_id: str = "msg-1"):
        self.fail_with = fail_with
        self.message_id = message_id
        self.sends: list[dict] = []

    async def send(self, *, recipient: str, body: str, subject: str | None = None) -> str:
        self.sends.append({"recipient": recipient, "body": body, "subject": subject})
        if self.fail_with:
            raise self.fail_with
        return self.message_id


def senders(sms=None, email=None):
    return {
        NotificationChannel.SMS: sms or FakeSender(),
        NotificationChannel.EMAIL: email or FakeSender(),
    }


# ===================================================================== #
# Enqueue
# ===================================================================== #


@pytest.mark.asyncio
async def test_booking_enqueues_confirmation_and_reminder(db, patient, time_slot):
    appt = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    rows = (await db.scalars(select(Notification).where(Notification.appointment_id == appt.id))).all()
    kinds = {(n.kind, n.channel) for n in rows}

    assert (NotificationKind.BOOKING_CONFIRMATION, NotificationChannel.SMS) in kinds
    assert (NotificationKind.REMINDER_24H, NotificationChannel.SMS) in kinds
    # No email queued -- this patient fixture has no email address.
    assert (NotificationKind.BOOKING_CONFIRMATION, NotificationChannel.EMAIL) not in kinds
    assert all(n.status is NotificationStatus.PENDING for n in rows)


@pytest.mark.asyncio
async def test_reminder_scheduled_24h_before_slot(db, patient, time_slot):
    appt = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    reminder = await db.scalar(
        select(Notification).where(
            Notification.appointment_id == appt.id,
            Notification.kind == NotificationKind.REMINDER_24H,
        )
    )
    assert reminder.scheduled_for == time_slot.starts_at - timedelta(hours=24)


@pytest.mark.asyncio
async def test_short_notice_booking_queues_no_reminder(db, patient, doctor):
    """A '24h reminder' 20 minutes after booking is noise, not a reminder."""
    from app.models.time_slot import TimeSlot

    soon = datetime.now(UTC) + timedelta(hours=2)
    slot = TimeSlot(doctor_id=doctor.id, starts_at=soon, ends_at=soon + timedelta(minutes=30))
    db.add(slot)
    await db.commit()

    appt = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=slot.id
    )
    rows = (await db.scalars(select(Notification).where(Notification.appointment_id == appt.id))).all()
    assert all(n.kind is not NotificationKind.REMINDER_24H for n in rows)
    assert any(n.kind is NotificationKind.BOOKING_CONFIRMATION for n in rows)


@pytest.mark.asyncio
async def test_notifications_roll_back_with_a_failed_booking(db, patient, second_patient, time_slot):
    """The outbox must be atomic with the booking it describes."""
    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    before = len((await db.scalars(select(Notification))).all())

    from app.services.exceptions import SlotAlreadyBookedError

    with pytest.raises(SlotAlreadyBookedError):
        await appointment_service.book_appointment(
            db, patient_id=second_patient.id, time_slot_id=time_slot.id
        )

    after = len((await db.scalars(select(Notification))).all())
    assert after == before  # no orphan notifications for a booking that never happened


# ===================================================================== #
# Idempotency -- the headline requirement
# ===================================================================== #


@pytest.mark.asyncio
async def test_duplicate_enqueue_is_suppressed_by_dedupe_key(db, patient, time_slot):
    appt = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    # Simulate a retried/duplicated enqueue (double-fired scheduler, retry).
    for _ in range(5):
        await notification_service.enqueue_for_booking(db, appointment=appt)
    await db.commit()

    key = build_dedupe_key(
        kind=NotificationKind.REMINDER_24H, channel=NotificationChannel.SMS,
        appointment_id=appt.id, time_slot_id=time_slot.id,
    )
    rows = (await db.scalars(select(Notification).where(Notification.dedupe_key == key))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_reminder_sent_only_once_across_repeated_drains(db, patient, time_slot):
    """THE requirement: a retried send must not deliver twice."""
    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    sms = FakeSender()
    due = time_slot.starts_at - timedelta(hours=23)  # reminder is due

    for _ in range(4):
        await notification_service.send_due_notifications(
            db, senders=senders(sms=sms), now=due
        )

    reminders = [s for s in sms.sends if "reminder" in s["body"].lower()]
    assert len(reminders) == 1


@pytest.mark.asyncio
async def test_concurrent_workers_do_not_double_send(db, session_factory, patient, time_slot):
    """Three workers draining at once: SKIP LOCKED must partition the work.

    The confirmation is drained first at its natural time. We then jump to
    the reminder's due moment, at which point exactly ONE notification is
    deliverable -- so "three workers, one message, one send" is an
    unambiguous assertion. (Leaving the confirmation undrained would not
    work: by the reminder's due time it is ~49 hours overdue and the
    staleness cutoff correctly skips it, which would muddy the count.)
    """
    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    await notification_service.send_due_notifications(db, senders=senders())  # confirmation

    due = time_slot.starts_at - timedelta(hours=23)
    shared = FakeSender()

    async def worker():
        async with session_factory() as s:
            return await notification_service.send_due_notifications(
                s, senders=senders(sms=shared), now=due
            )

    results = await asyncio.gather(worker(), worker(), worker())

    total_sent = sum(r.sent for r in results)
    assert total_sent == 1, f"expected exactly 1 send across all workers, got {total_sent}"
    assert len(shared.sends) == 1, f"provider was called {len(shared.sends)} times"
    assert "reminder" in shared.sends[0]["body"].lower()

    # The database must agree, and nothing may be left claimable.
    async with session_factory() as s:
        reminder = await s.scalar(
            select(Notification).where(Notification.kind == NotificationKind.REMINDER_24H)
        )
        assert reminder.status is NotificationStatus.SENT
        leftover = (await s.scalars(
            select(Notification).where(
                Notification.status.in_([NotificationStatus.PENDING, NotificationStatus.CLAIMED])
            )
        )).all()
        assert leftover == []


# ===================================================================== #
# Sending
# ===================================================================== #


@pytest.mark.asyncio
async def test_successful_send_records_provider_id(db, patient, time_slot):
    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    sms = FakeSender(message_id="SM123")
    result = await notification_service.send_due_notifications(db, senders=senders(sms=sms))

    assert result.sent == 1  # only the confirmation is due yet
    row = await db.scalar(
        select(Notification).where(Notification.status == NotificationStatus.SENT)
    )
    assert row.provider == "twilio"
    assert row.provider_message_id == "SM123"
    assert row.sent_at is not None


@pytest.mark.asyncio
async def test_transient_failure_is_retried_then_abandoned(db, patient, time_slot):
    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    failing = FakeSender(fail_with=MessageSendError("503 upstream", permanent=False))

    for _ in range(5):
        await notification_service.send_due_notifications(db, senders=senders(sms=failing))

    row = await db.scalar(
        select(Notification).where(Notification.kind == NotificationKind.BOOKING_CONFIRMATION)
    )
    assert row.status is NotificationStatus.ABANDONED
    assert row.attempts == row.max_attempts  # stops trying; does not loop forever


@pytest.mark.asyncio
async def test_permanent_failure_abandons_immediately(db, patient, time_slot):
    """An unreachable number should not consume three attempts."""
    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    failing = FakeSender(fail_with=MessageSendError("400 invalid number", permanent=True))

    await notification_service.send_due_notifications(db, senders=senders(sms=failing))

    row = await db.scalar(
        select(Notification).where(Notification.kind == NotificationKind.BOOKING_CONFIRMATION)
    )
    assert row.status is NotificationStatus.ABANDONED
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_cancelling_skips_the_pending_reminder(db, patient, time_slot):
    appt = await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    await appointment_service.cancel_appointment(db, appointment_id=appt.id)

    reminder = await db.scalar(
        select(Notification).where(Notification.kind == NotificationKind.REMINDER_24H)
    )
    assert reminder.status is NotificationStatus.SKIPPED

    sms = FakeSender()
    await notification_service.send_due_notifications(
        db, senders=senders(sms=sms), now=time_slot.starts_at - timedelta(hours=23)
    )
    assert not any("reminder" in s["body"].lower() for s in sms.sends)


@pytest.mark.asyncio
async def test_sender_revalidates_cancellation_at_send_time(db, patient, time_slot):
    """Braces to the belt: even a reminder that slipped through is re-checked."""
    appt = await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    reminder = await db.scalar(
        select(Notification).where(Notification.kind == NotificationKind.REMINDER_24H)
    )
    # Cancel the appointment WITHOUT going through the service, so the
    # pending reminder is deliberately left untouched.
    appt.status = AppointmentStatus.CANCELLED
    db.add(appt)
    await db.commit()
    assert reminder.status is NotificationStatus.PENDING

    sms = FakeSender()
    await notification_service.send_due_notifications(
        db, senders=senders(sms=sms), now=time_slot.starts_at - timedelta(hours=23)
    )
    await db.refresh(reminder)
    assert reminder.status is NotificationStatus.SKIPPED
    assert not any("reminder" in s["body"].lower() for s in sms.sends)


@pytest.mark.asyncio
async def test_badly_overdue_reminder_is_skipped(db, patient, time_slot):
    """Do not send 'see you tomorrow' after the appointment already happened."""
    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    sms = FakeSender()
    way_late = time_slot.starts_at + timedelta(days=2)

    await notification_service.send_due_notifications(db, senders=senders(sms=sms), now=way_late)

    reminder = await db.scalar(
        select(Notification).where(Notification.kind == NotificationKind.REMINDER_24H)
    )
    assert reminder.status is NotificationStatus.SKIPPED


@pytest.mark.asyncio
async def test_stuck_claim_becomes_unresolved_not_resent(db, patient, time_slot):
    """A crash mid-send is ambiguous, and we refuse to guess."""
    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    row = await db.scalar(
        select(Notification).where(Notification.kind == NotificationKind.BOOKING_CONFIRMATION)
    )
    row.status = NotificationStatus.CLAIMED
    row.claimed_at = datetime.now(UTC) - timedelta(hours=1)
    db.add(row)
    await db.commit()

    reaped = await notification_service.reap_stuck_claims(db)
    assert reaped == 1
    await db.refresh(row)
    assert row.status is NotificationStatus.UNRESOLVED

    # Crucially it is NOT picked up again -- that would risk the duplicate.
    sms = FakeSender()
    await notification_service.send_due_notifications(db, senders=senders(sms=sms))
    assert sms.sends == []


# ===================================================================== #
# Cold identity map -- the new convention
# ===================================================================== #


@pytest.mark.asyncio
async def test_send_works_with_a_cold_identity_map(db, patient, time_slot):
    """No lazy relationship access anywhere in the send path.

    The worker always runs with a fresh session. `expunge_all()` empties
    the identity map so any bare `appointment.patient.phone` style access
    raises MissingGreenlet instead of silently resolving from memory.
    """
    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    db.expunge_all()

    sms = FakeSender()
    result = await notification_service.send_due_notifications(db, senders=senders(sms=sms))
    assert result.sent == 1
    assert sms.sends[0]["recipient"] == patient.phone


@pytest.mark.asyncio
async def test_render_body_works_with_a_cold_identity_map(db, patient, time_slot, doctor):
    """render_body touches appointment, slot, doctor AND patient."""
    appt = await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    row = await db.scalar(
        select(Notification).where(Notification.kind == NotificationKind.BOOKING_CONFIRMATION)
    )
    row_id = row.id
    db.expunge_all()

    row = await db.get(Notification, row_id)
    subject, body = await notification_service.render_body(db, row)
    assert doctor.full_name in body
    assert subject


@pytest.mark.asyncio
async def test_enqueue_works_with_a_cold_identity_map(db, patient, time_slot):
    from app.models.appointment import Appointment
    from app.models.enums import BookingChannel

    appt = Appointment(
        patient_id=patient.id, doctor_id=time_slot.doctor_id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()
    appt_id = appt.id
    db.expunge_all()

    appt = await db.get(Appointment, appt_id)
    queued = await notification_service.enqueue_for_booking(db, appointment=appt)
    await db.commit()
    assert queued["confirmation_sms"] is True


@pytest.mark.asyncio
async def test_message_body_uses_doctor_local_time(db, patient, time_slot, doctor):
    """A patient told a UTC time will miss their appointment."""
    from zoneinfo import ZoneInfo

    doctor.timezone = "America/New_York"
    db.add(doctor)
    await db.commit()

    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    row = await db.scalar(
        select(Notification).where(Notification.kind == NotificationKind.BOOKING_CONFIRMATION)
    )
    _, body = await notification_service.render_body(db, row)

    local = time_slot.starts_at.astimezone(ZoneInfo("America/New_York"))
    assert local.strftime("%H:%M") in body


# ===================================================================== #
# The 24h scheduling boundary
# ===================================================================== #


@pytest.mark.asyncio
async def test_reminder_boundary_just_over_lead_time_is_queued(db, patient, doctor):
    from app.models.time_slot import TimeSlot

    starts = datetime.now(UTC) + timedelta(hours=25)
    slot = TimeSlot(doctor_id=doctor.id, starts_at=starts, ends_at=starts + timedelta(minutes=30))
    db.add(slot)
    await db.commit()

    appt = await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=slot.id)
    reminder = await db.scalar(
        select(Notification).where(
            Notification.appointment_id == appt.id,
            Notification.kind == NotificationKind.REMINDER_24H,
        )
    )
    assert reminder is not None
    assert reminder.scheduled_for > datetime.now(UTC)


@pytest.mark.asyncio
async def test_reminder_boundary_just_under_lead_time_is_skipped(db, patient, doctor):
    """Documents the policy at the edge: booked 23h ahead -> no reminder.

    FLAGGED as a clinic decision, not a technical one. The confirmation has
    just gone out, so a "reminder" minutes later is noise -- but a clinic
    worried about no-shows might prefer one anyway, sent immediately.
    """
    from app.models.time_slot import TimeSlot

    starts = datetime.now(UTC) + timedelta(hours=23)
    slot = TimeSlot(doctor_id=doctor.id, starts_at=starts, ends_at=starts + timedelta(minutes=30))
    db.add(slot)
    await db.commit()

    appt = await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=slot.id)
    reminder = await db.scalar(
        select(Notification).where(
            Notification.appointment_id == appt.id,
            Notification.kind == NotificationKind.REMINDER_24H,
        )
    )
    assert reminder is None
