"""Pydantic v2 schemas for Appointment."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import AppointmentStatus, BookingChannel


class AppointmentCreate(BaseModel):
    """Inbound booking request.

    ASSUMPTION: the caller already has a `patient_id` (looked up or created
    beforehand) and a `time_slot_id` (chosen from a prior availability
    query). Combining "create patient" and "book appointment" into one
    endpoint was tempting but would make the service do two different jobs
    with two different failure modes; kept separate on purpose.
    """

    patient_id: UUID
    time_slot_id: UUID
    booking_channel: BookingChannel = BookingChannel.WEB
    reason: str | None = Field(default=None, max_length=2000)


class AppointmentCancel(BaseModel):
    cancellation_reason: str | None = Field(default=None, max_length=500)


class AppointmentReschedule(BaseModel):
    """Inbound reschedule request.

    Deliberately just a new slot id, not a new doctor_id or patient_id --
    see RescheduleAcrossDoctorsError. `reason` is optional and, if omitted,
    carries the original appointment's reason forward (see
    appointment_service.reschedule_appointment).
    """

    new_time_slot_id: UUID
    reason: str | None = Field(default=None, max_length=2000)


class AppointmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_id: UUID
    doctor_id: UUID
    time_slot_id: UUID
    status: AppointmentStatus
    booking_channel: BookingChannel
    reason: str | None = None
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None
    # Phase 3. Non-null when this row exists because an earlier appointment
    # was rescheduled into it -- see models/appointment.py.
    rescheduled_from_id: UUID | None = None
    created_at: datetime
