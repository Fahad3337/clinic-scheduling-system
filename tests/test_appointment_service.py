"""Basic service-layer behavior, independent of the concurrency scenario."""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import AppointmentStatus
from app.services import appointment_service
from app.services.exceptions import (
    AppointmentNotFoundError,
    InvalidStatusTransitionError,
    PatientNotFoundError,
    SlotAlreadyBookedError,
    SlotBlockedError,
    TimeSlotNotFoundError,
)


@pytest.mark.asyncio
async def test_book_appointment_happy_path(db, patient, time_slot, doctor):
    appt = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id, reason="Annual checkup"
    )
    assert appt.status is AppointmentStatus.BOOKED
    assert appt.patient_id == patient.id
    assert appt.doctor_id == doctor.id  # copied from the slot, not passed in
    assert appt.reason == "Annual checkup"


@pytest.mark.asyncio
async def test_book_appointment_unknown_patient_raises(db, time_slot):
    with pytest.raises(PatientNotFoundError):
        await appointment_service.book_appointment(
            db, patient_id=uuid.uuid4(), time_slot_id=time_slot.id
        )


@pytest.mark.asyncio
async def test_book_appointment_unknown_slot_raises(db, patient):
    with pytest.raises(TimeSlotNotFoundError):
        await appointment_service.book_appointment(
            db, patient_id=patient.id, time_slot_id=uuid.uuid4()
        )


@pytest.mark.asyncio
async def test_book_already_booked_slot_raises(db, patient, second_patient, time_slot):
    await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    with pytest.raises(SlotAlreadyBookedError):
        await appointment_service.book_appointment(
            db, patient_id=second_patient.id, time_slot_id=time_slot.id
        )


@pytest.mark.asyncio
async def test_book_blocked_slot_raises(db, time_slot, patient):
    time_slot.is_blocked = True
    db.add(time_slot)
    await db.commit()

    with pytest.raises(SlotBlockedError):
        await appointment_service.book_appointment(
            db, patient_id=patient.id, time_slot_id=time_slot.id
        )


@pytest.mark.asyncio
async def test_cancel_appointment_happy_path(db, patient, time_slot):
    appt = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    cancelled = await appointment_service.cancel_appointment(
        db, appointment_id=appt.id, cancellation_reason="Patient request"
    )
    assert cancelled.status is AppointmentStatus.CANCELLED
    assert cancelled.cancelled_at is not None
    assert cancelled.cancellation_reason == "Patient request"


@pytest.mark.asyncio
async def test_cancel_unknown_appointment_raises(db):
    with pytest.raises(AppointmentNotFoundError):
        await appointment_service.cancel_appointment(db, appointment_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_cancel_already_cancelled_appointment_raises(db, patient, time_slot):
    appt = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    await appointment_service.cancel_appointment(db, appointment_id=appt.id)

    with pytest.raises(InvalidStatusTransitionError):
        await appointment_service.cancel_appointment(db, appointment_id=appt.id)


@pytest.mark.asyncio
async def test_cancel_completed_appointment_raises(db, patient, time_slot):
    appt = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    appt.status = AppointmentStatus.COMPLETED
    db.add(appt)
    await db.commit()

    with pytest.raises(InvalidStatusTransitionError):
        await appointment_service.cancel_appointment(db, appointment_id=appt.id)


@pytest.mark.asyncio
async def test_get_appointment_not_found_raises(db):
    with pytest.raises(AppointmentNotFoundError):
        await appointment_service.get_appointment(db, uuid.uuid4())


# ===================================================================== #
# Rescheduling
# ===================================================================== #


@pytest.mark.asyncio
async def test_reschedule_happy_path(db, patient, doctor, time_slot):
    from datetime import timedelta

    from app.models.time_slot import TimeSlot

    new_slot = TimeSlot(
        doctor_id=doctor.id,
        starts_at=time_slot.starts_at + timedelta(days=1),
        ends_at=time_slot.ends_at + timedelta(days=1),
    )
    db.add(new_slot)
    await db.commit()

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id, reason="Checkup"
    )
    moved = await appointment_service.reschedule_appointment(
        db, appointment_id=original.id, new_time_slot_id=new_slot.id
    )

    assert moved.id != original.id
    assert moved.time_slot_id == new_slot.id
    assert moved.status is AppointmentStatus.BOOKED
    assert moved.patient_id == original.patient_id
    assert moved.doctor_id == original.doctor_id
    assert moved.rescheduled_from_id == original.id
    assert moved.reason == "Checkup"  # carried forward since none was given

    await db.refresh(original)
    assert original.status is AppointmentStatus.CANCELLED
    assert original.cancelled_at is not None


@pytest.mark.asyncio
async def test_reschedule_old_slot_becomes_bookable_again(db, patient, second_patient, doctor, time_slot):
    from datetime import timedelta

    from app.models.time_slot import TimeSlot

    new_slot = TimeSlot(
        doctor_id=doctor.id,
        starts_at=time_slot.starts_at + timedelta(days=1),
        ends_at=time_slot.ends_at + timedelta(days=1),
    )
    db.add(new_slot)
    await db.commit()

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    await appointment_service.reschedule_appointment(
        db, appointment_id=original.id, new_time_slot_id=new_slot.id
    )

    # The vacated old slot must be bookable by someone else.
    rebooked = await appointment_service.book_appointment(
        db, patient_id=second_patient.id, time_slot_id=time_slot.id
    )
    assert rebooked.status is AppointmentStatus.BOOKED


@pytest.mark.asyncio
async def test_reschedule_to_an_already_booked_slot_raises(
    db, patient, second_patient, doctor, time_slot
):
    from datetime import timedelta

    from app.models.time_slot import TimeSlot

    taken_slot = TimeSlot(
        doctor_id=doctor.id,
        starts_at=time_slot.starts_at + timedelta(days=1),
        ends_at=time_slot.ends_at + timedelta(days=1),
    )
    db.add(taken_slot)
    await db.commit()
    await appointment_service.book_appointment(
        db, patient_id=second_patient.id, time_slot_id=taken_slot.id
    )

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    with pytest.raises(SlotAlreadyBookedError):
        await appointment_service.reschedule_appointment(
            db, appointment_id=original.id, new_time_slot_id=taken_slot.id
        )

    # ATOMICITY: the failed attempt must not have cancelled the original.
    await db.refresh(original)
    assert original.status is AppointmentStatus.BOOKED
    assert original.time_slot_id == time_slot.id


@pytest.mark.asyncio
async def test_reschedule_to_same_slot_raises(db, patient, time_slot):
    from app.services.exceptions import RescheduleToSameSlotError

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    with pytest.raises(RescheduleToSameSlotError):
        await appointment_service.reschedule_appointment(
            db, appointment_id=original.id, new_time_slot_id=time_slot.id
        )


@pytest.mark.asyncio
async def test_reschedule_across_doctors_raises(db, patient, time_slot):
    from datetime import timedelta

    from app.models.doctor import Doctor
    from app.models.time_slot import TimeSlot
    from app.services.exceptions import RescheduleAcrossDoctorsError

    other_doctor = Doctor(full_name="Dr. Someone Else", timezone="UTC")
    db.add(other_doctor)
    await db.commit()
    other_slot = TimeSlot(
        doctor_id=other_doctor.id,
        starts_at=time_slot.starts_at + timedelta(days=1),
        ends_at=time_slot.ends_at + timedelta(days=1),
    )
    db.add(other_slot)
    await db.commit()

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    with pytest.raises(RescheduleAcrossDoctorsError):
        await appointment_service.reschedule_appointment(
            db, appointment_id=original.id, new_time_slot_id=other_slot.id
        )


@pytest.mark.asyncio
async def test_reschedule_a_cancelled_appointment_raises(db, patient, doctor, time_slot):
    from datetime import timedelta

    from app.models.time_slot import TimeSlot

    new_slot = TimeSlot(
        doctor_id=doctor.id,
        starts_at=time_slot.starts_at + timedelta(days=1),
        ends_at=time_slot.ends_at + timedelta(days=1),
    )
    db.add(new_slot)
    await db.commit()

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    await appointment_service.cancel_appointment(db, appointment_id=original.id)

    with pytest.raises(InvalidStatusTransitionError):
        await appointment_service.reschedule_appointment(
            db, appointment_id=original.id, new_time_slot_id=new_slot.id
        )


@pytest.mark.asyncio
async def test_reschedule_unknown_appointment_raises(db, doctor, time_slot):
    with pytest.raises(AppointmentNotFoundError):
        await appointment_service.reschedule_appointment(
            db, appointment_id=uuid.uuid4(), new_time_slot_id=time_slot.id
        )


@pytest.mark.asyncio
async def test_reschedule_unknown_new_slot_raises(db, patient, time_slot):
    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    with pytest.raises(TimeSlotNotFoundError):
        await appointment_service.reschedule_appointment(
            db, appointment_id=original.id, new_time_slot_id=uuid.uuid4()
        )


@pytest.mark.asyncio
async def test_reschedule_sends_exactly_one_notification_not_two(db, patient, doctor, time_slot):
    """A patient who moves their appointment gets ONE message, not a
    cancellation notice followed by a separate confirmation."""
    from datetime import timedelta

    from app.models.enums import NotificationChannel, NotificationKind
    from app.models.notification import Notification
    from app.models.time_slot import TimeSlot
    from sqlalchemy import select

    new_slot = TimeSlot(
        doctor_id=doctor.id,
        starts_at=time_slot.starts_at + timedelta(days=1),
        ends_at=time_slot.ends_at + timedelta(days=1),
    )
    db.add(new_slot)
    await db.commit()

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    moved = await appointment_service.reschedule_appointment(
        db, appointment_id=original.id, new_time_slot_id=new_slot.id
    )

    reschedule_notices = (
        await db.scalars(
            select(Notification).where(
                Notification.appointment_id == moved.id,
                Notification.kind == NotificationKind.RESCHEDULE_CONFIRMATION,
            )
        )
    ).all()
    assert len(reschedule_notices) == 1

    cancellation_notices = (
        await db.scalars(
            select(Notification).where(
                Notification.kind == NotificationKind.CANCELLATION_CONFIRMATION,
            )
        )
    ).all()
    assert cancellation_notices == []  # no separate cancellation message


@pytest.mark.asyncio
async def test_reschedule_skips_old_pending_reminder(db, patient, doctor, time_slot):
    from datetime import timedelta

    from app.models.enums import NotificationKind, NotificationStatus
    from app.models.notification import Notification
    from app.models.time_slot import TimeSlot
    from sqlalchemy import select

    new_slot = TimeSlot(
        doctor_id=doctor.id,
        starts_at=time_slot.starts_at + timedelta(days=1),
        ends_at=time_slot.ends_at + timedelta(days=1),
    )
    db.add(new_slot)
    await db.commit()

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    old_reminder = await db.scalar(
        select(Notification).where(
            Notification.appointment_id == original.id,
            Notification.kind == NotificationKind.REMINDER_24H,
        )
    )
    assert old_reminder is not None

    await appointment_service.reschedule_appointment(
        db, appointment_id=original.id, new_time_slot_id=new_slot.id
    )
    await db.refresh(old_reminder)
    assert old_reminder.status is NotificationStatus.SKIPPED


@pytest.mark.asyncio
async def test_reschedule_queues_calendar_push_for_new_and_delete_for_old(
    db, patient, doctor, time_slot
):
    from datetime import timedelta

    from app.models.appointment_external_event import AppointmentExternalEvent
    from app.models.calendar_connection import CalendarConnection
    from app.models.enums import CalendarProvider, CalendarPushState
    from app.models.time_slot import TimeSlot
    from sqlalchemy import select

    conn = CalendarConnection(
        doctor_id=doctor.id, provider=CalendarProvider.GOOGLE,
        account_email="rao@example.com", refresh_token="rt",
    )
    db.add(conn)
    await db.commit()

    new_slot = TimeSlot(
        doctor_id=doctor.id,
        starts_at=time_slot.starts_at + timedelta(days=1),
        ends_at=time_slot.ends_at + timedelta(days=1),
    )
    db.add(new_slot)
    await db.commit()

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    moved = await appointment_service.reschedule_appointment(
        db, appointment_id=original.id, new_time_slot_id=new_slot.id
    )

    pushes = {
        p.appointment_id: p
        for p in (await db.scalars(select(AppointmentExternalEvent))).all()
    }
    assert pushes[original.id].push_state is CalendarPushState.DELETED  # never pushed -> straight to DELETED
    assert pushes[moved.id].push_state is CalendarPushState.PENDING


@pytest.mark.asyncio
async def test_reschedule_works_with_a_cold_identity_map(db, patient, doctor, time_slot):
    from datetime import timedelta

    from app.models.time_slot import TimeSlot

    new_slot = TimeSlot(
        doctor_id=doctor.id,
        starts_at=time_slot.starts_at + timedelta(days=1),
        ends_at=time_slot.ends_at + timedelta(days=1),
    )
    db.add(new_slot)
    await db.commit()

    original = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    original_id, new_slot_id = original.id, new_slot.id
    db.expunge_all()

    moved = await appointment_service.reschedule_appointment(
        db, appointment_id=original_id, new_time_slot_id=new_slot_id
    )
    assert moved.time_slot_id == new_slot_id
