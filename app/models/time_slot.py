"""Time slot model -- the unit of bookable capacity.

DESIGN DECISION (the important one in this file): slots are *materialized
rows*, generated ahead of time by a scheduling job, not computed on the fly
from a recurring-availability rule.

Two viable designs exist:

  (A) Materialized slots [CHOSEN]
      A row per bookable 30-minute window. Booking = attaching an appointment
      to an existing row. Availability = "slots with no active appointment".

  (B) Rule-based availability + interval exclusion
      Store a weekly schedule template; compute candidate slots at query time;
      prevent overlap with a Postgres EXCLUDE constraint over a tstzrange
      (requires the btree_gist extension).

WHY (A) for this phase:
  - The double-booking problem collapses into "at most one active appointment
    per time_slot row", which a *unique index* enforces. Unique indexes are
    the cheapest, most reliable concurrency primitive Postgres offers: no lock
    ordering to reason about, no deadlocks, correct across any number of API
    workers, and correct even for writes that bypass the application.
  - Design (B)'s EXCLUDE constraint is genuinely more powerful -- it handles
    variable-length appointments and arbitrary overlap -- but it is harder to
    explain, needs an extension, and gives worse error messages.
  - Materialized rows also give the doctor a place to hang per-slot state
    (blocked for admin time, overbooked-on-purpose) that a rule cannot.

COST TO REVISIT: someone must generate slots (a nightly job creating a rolling
90-day window). If that job stops, bookings silently fail with "no
availability". Add monitoring on "days of slots remaining" when you build it.
Design (B) has no such failure mode -- that is its real advantage.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.doctor import Doctor


class TimeSlot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "time_slots"

    doctor_id: Mapped[UUID] = mapped_column(
        ForeignKey("doctors.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Half-open interval [starts_at, ends_at). WHY half-open: a 09:00-09:30
    # slot and a 09:30-10:00 slot must not be considered overlapping. Closed
    # intervals make every adjacent pair look like a conflict.
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # WHY a flag instead of deleting the row: once an appointment references a
    # slot you cannot delete it, and a doctor blocking off Friday afternoon
    # must not erase history. `is_blocked` slots are filtered out of
    # availability but keep their audit trail.
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    doctor: Mapped["Doctor"] = relationship(back_populates="time_slots")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="time_slot")

    __table_args__ = (
        # Makes slot generation idempotent: re-running the generator for a day
        # that already has slots raises instead of duplicating capacity.
        UniqueConstraint("doctor_id", "starts_at", name="uq_time_slots_doctor_id_starts_at"),
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        # Covers the availability query: WHERE doctor_id = ? AND starts_at >= ?
        # AND starts_at < ?  -- doctor_id first because it is the equality
        # predicate; a range scan on starts_at then reads a contiguous stripe.
        Index("ix_time_slots_doctor_id_starts_at", "doctor_id", "starts_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TimeSlot {self.id} {self.starts_at.isoformat()}>"
