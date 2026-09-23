"""Doctor / availability routes."""

from __future__ import annotations

from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.schemas.doctor import AvailableSlot, DoctorAvailability
from app.services import availability_service
from app.services.availability_service import DoctorNotFoundError

router = APIRouter(prefix="/doctors", tags=["doctors"])


@router.get("/{doctor_id}/availability", response_model=DoctorAvailability)
async def get_doctor_availability(
    doctor_id: UUID,
    on_date: date = Query(..., alias="date"),
    db: AsyncSession = Depends(get_db),
) -> DoctorAvailability:
    try:
        doctor, slots = await availability_service.get_availability(
            db, doctor_id=doctor_id, on_date=on_date
        )
    except DoctorNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    tz = ZoneInfo(doctor.timezone)
    return DoctorAvailability(
        doctor_id=doctor.id,
        date=on_date,
        timezone=doctor.timezone,
        slots=[
            AvailableSlot(
                time_slot_id=slot.id,
                # Convert the stored UTC instant to the doctor's local wall
                # clock for display -- see AvailableSlot's docstring.
                starts_at_local=slot.starts_at.astimezone(tz).time(),
                ends_at_local=slot.ends_at.astimezone(tz).time(),
            )
            for slot in slots
        ],
    )
