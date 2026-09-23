"""Doctor availability queries.

Converts a *local calendar date* (what a patient/chatbot asks about: "do you
have anything Tuesday?") into a UTC instant range (what the database stores)
and returns the free time_slots in that range.
"""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.doctor import Doctor
from app.models.enums import AppointmentStatus
from app.models.external_busy_block import ExternalBusyBlock
from app.models.time_slot import TimeSlot
from app.services.exceptions import DomainError


class DoctorNotFoundError(DomainError):
    def __init__(self, doctor_id: UUID) -> None:
        self.doctor_id = doctor_id
        super().__init__(f"Doctor {doctor_id} not found")


async def get_availability(
    session: AsyncSession, *, doctor_id: UUID, on_date: date
) -> tuple[Doctor, list[TimeSlot]]:
    """Return the doctor's bookable slots on `on_date`.

    A slot is bookable when ALL of these hold:
      * staff have not blocked it        (time_slots.is_blocked)
      * it has no active appointment     (Phase 1 partial unique index)
      * no live external busy block overlaps it   (Phase 2 calendar sync)

    WHY the local-day -> UTC conversion matters: "2026-09-22" in the doctor's
    timezone is NOT [2026-09-22 00:00 UTC, 2026-09-23 00:00 UTC) unless the
    doctor happens to be in UTC. Using ZoneInfo (stdlib, tz-database backed)
    instead of a fixed offset means this stays correct across DST transitions
    automatically -- a fixed-offset approach would be off by an hour twice a
    year for any doctor in a zone that observes DST.

    This is a read query, so it does NOT take a row lock -- only booking needs
    that. Reading a slightly stale "available" list and then losing the race
    at booking time is fine and expected; that's exactly the case
    SlotAlreadyBookedError exists to handle at the write.
    """
    doctor = await session.get(Doctor, doctor_id)
    if doctor is None:
        raise DoctorNotFoundError(doctor_id)

    tz = ZoneInfo(doctor.timezone)
    local_start = datetime.combine(on_date, time.min, tzinfo=tz)
    local_end = datetime.combine(on_date, time.max, tzinfo=tz)

    # Subquery-free approach: LEFT JOIN slots to active appointments, keep
    # rows where no active appointment matched. Reads clearly and lets
    # Postgres use the ix_time_slots_doctor_id_starts_at index for the range
    # scan and uq_appointments_active_slot's underlying index for the join.
    active_appt = select(Appointment.time_slot_id).where(
        Appointment.status != AppointmentStatus.CANCELLED
    )

    # Phase 2: the doctor's own calendar also removes capacity.
    #
    # THE deleted_at IS NULL FILTER IS LOAD-BEARING. Busy blocks are soft-
    # deleted (schedule_conflicts references them with ON DELETE RESTRICT,
    # so they cannot be removed outright). Omit this predicate and every
    # event the doctor has EVER had keeps blocking its slots forever, which
    # presents as a doctor who mysteriously has no availability and no
    # obvious cause. This is the single place the filter is applied, which
    # is why the whole availability query lives in one function.
    #
    # Half-open overlap, matching the interval convention used everywhere
    # else: a block ending exactly when a slot starts does not collide.
    overlapping_busy = exists().where(
        and_(
            ExternalBusyBlock.doctor_id == doctor_id,
            ExternalBusyBlock.deleted_at.is_(None),
            ExternalBusyBlock.starts_at < TimeSlot.ends_at,
            ExternalBusyBlock.ends_at > TimeSlot.starts_at,
        )
    )

    stmt = (
        select(TimeSlot)
        .where(
            TimeSlot.doctor_id == doctor_id,
            TimeSlot.is_blocked.is_(False),
            TimeSlot.starts_at >= local_start,
            TimeSlot.starts_at <= local_end,
            TimeSlot.id.not_in(active_appt),
            ~overlapping_busy,
        )
        .order_by(TimeSlot.starts_at)
    )
    slots = list((await session.scalars(stmt)).all())
    return doctor, slots
