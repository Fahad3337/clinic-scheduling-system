"""Pydantic v2 schemas for Patient."""

from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

_E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


class PatientCreate(BaseModel):
    """Inbound payload for creating (or identifying) a patient.

    WHY this lives separately from the appointment-booking request: the
    chatbot/voice flow will eventually look a patient up by phone before
    asking any of this, and reuse this schema unchanged.
    """

    full_name: str
    phone: str
    email: EmailStr | None = None

    @field_validator("phone")
    @classmethod
    def _validate_e164(cls, v: str) -> str:
        # Mirrors the DB CHECK constraint in models/patient.py. Validating
        # here gives a 422 with a clear message instead of a 500 from a
        # constraint violation surfacing through the service layer.
        if not _E164_RE.match(v):
            raise ValueError("phone must be E.164 format, e.g. +14155550123")
        return v


class PatientRead(BaseModel):
    # WHY from_attributes: lets this schema be built directly from the ORM
    # object (`PatientRead.model_validate(patient)`) instead of hand-mapping
    # every field in the route handler.
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    phone: str
    email: str | None = None
