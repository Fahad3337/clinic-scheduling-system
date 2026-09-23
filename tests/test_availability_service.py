"""Availability service: local-date -> UTC range translation and filtering."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services import availability_service
from app.services import appointment_service
from app.services.availability_service import DoctorNotFoundError
from app.models.time_slot import TimeSlot


@pytest.mark.asyncio
async def test_availability_excludes_booked_slots(db, doctor, patient, time_slot):
    on_date = time_slot.starts_at.astimezone(UTC).date()

    _, before = await availability_service.get_availability(
        db, doctor_id=doctor.id, on_date=on_date
    )
    assert any(s.id == time_slot.id for s in before)

    await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )

    _, after = await availability_service.get_availability(
        db, doctor_id=doctor.id, on_date=on_date
    )
    assert all(s.id != time_slot.id for s in after)


@pytest.mark.asyncio
async def test_availability_excludes_blocked_slots(db, doctor, time_slot):
    time_slot.is_blocked = True
    db.add(time_slot)
    await db.commit()

    on_date = time_slot.starts_at.astimezone(UTC).date()
    _, slots = await availability_service.get_availability(
        db, doctor_id=doctor.id, on_date=on_date
    )
    assert all(s.id != time_slot.id for s in slots)


@pytest.mark.asyncio
async def test_availability_unknown_doctor_raises(db):
    with pytest.raises(DoctorNotFoundError):
        await availability_service.get_availability(
            db, doctor_id=uuid.uuid4(), on_date=datetime.now(UTC).date()
        )
