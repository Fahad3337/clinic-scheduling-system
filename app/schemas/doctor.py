"""Pydantic v2 schemas for Doctor and availability."""

from __future__ import annotations

from datetime import date, time
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DoctorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    specialty: str | None = None
    timezone: str
    slot_duration_minutes: int


class AvailableSlot(BaseModel):
    """One bookable window, returned in the doctor's own local timezone.

    WHY return local time here even though the DB stores UTC: the caller
    asked "what's free on 2026-09-22" -- a question about a local calendar
    day. Echoing UTC instants back would force every client (web, chatbot,
    voice) to re-derive the clinic's timezone to display something sensible.
    """

    time_slot_id: UUID
    starts_at_local: time
    ends_at_local: time


class DoctorAvailability(BaseModel):
    doctor_id: UUID
    date: date
    timezone: str
    slots: list[AvailableSlot]
