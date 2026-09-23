"""Collisions between an existing booking and the doctor's external calendar.

THE QUESTION YOU FLAGGED: a doctor manually adds a personal event on top of
an appointment a patient has already booked. What happens?

Three possible behaviours, and why only one is defensible:

  (a) Auto-cancel the appointment.
      Absolutely not. A patient's medical appointment disappearing because a
      doctor tentatively pencilled in a lunch is an unacceptable outcome, and
      the patient would find out (at best) via a cancellation SMS they did
      not ask for. Software must not silently cancel care.

  (b) Ignore the external event.
      The sync is then lying: the doctor is genuinely double-booked in real
      life, and the first anyone notices is when two people are in the
      waiting room. "The calendar said it was fine" is worse than no sync.

  (c) Record the conflict, change NOTHING, and escalate to a human.  <-- CHOSEN
      The appointment stands. The overlapping slots stop being offered for
      NEW bookings (that falls out of the availability query automatically).
      A row lands here, and staff get notified so a person can call the
      patient, or tell the doctor to move their event.

So the rule for the sync job is: **it may block future capacity, but it may
never mutate an existing appointment.** Writes to the appointments table stay
exclusively in appointment_service, where Phase 1's state machine and locking
live. The sync job's only power over a booked appointment is to raise a flag.

ASSUMPTION worth revisiting with the clinic: "escalate to a human" presumes
someone is actually watching. If nobody triages these, (c) degrades into (b)
with extra database rows. The staff notification and a visible queue are the
load-bearing parts of this design, not the table itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ConflictResolution

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.external_busy_block import ExternalBusyBlock


class ScheduleConflict(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "schedule_conflicts"

    appointment_id: Mapped[UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=False
    )
    busy_block_id: Mapped[UUID] = mapped_column(
        ForeignKey("external_busy_blocks.id", ondelete="RESTRICT"), nullable=False
    )
    # RESTRICT: a conflict is an audit record ("on the 21st we nearly
    # double-booked this patient"), and it must survive the disappearance of
    # the calendar event that caused it. A near-miss that erases itself when
    # the doctor deletes their event is exactly the history you want during
    # an incident review.
    #
    # CONSEQUENCE, and it is not optional: the sync job can no longer DELETE
    # a busy block once a conflict references it -- Postgres will refuse, the
    # sync will raise, and it will keep raising on every tick forever. So
    # external_busy_blocks is SOFT-deleted (deleted_at) instead of removed.
    # See the note on ExternalBusyBlock.deleted_at. Soft deletion also makes
    # this table's audit trail actually readable: the conflict row still
    # points at a row that records when the offending event ran, rather than
    # at a dangling ID.
    #
    # The sync job marks resolution=EXTERNAL_EVENT_REMOVED when the
    # underlying event disappears, so a self-resolving conflict is recorded
    # as resolved rather than left to clutter the staff triage queue.

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    resolution = mapped_column(
        SAEnum(
            ConflictResolution,
            name="conflict_resolution",
            native_enum=False,
            length=32,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
            create_constraint=True,
        ),
        nullable=False,
        default=ConflictResolution.UNRESOLVED,
        server_default=ConflictResolution.UNRESOLVED.value,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    appointment: Mapped["Appointment"] = relationship(back_populates="conflicts")
    busy_block: Mapped["ExternalBusyBlock"] = relationship()

    __table_args__ = (
        # Re-running the sync must not pile up a new conflict row every five
        # minutes for the same unresolved collision. Same idempotency
        # reasoning as everywhere else in this phase.
        UniqueConstraint(
            "appointment_id", "busy_block_id", name="uq_schedule_conflicts_appointment_id_busy_block_id"
        ),
        # The staff triage queue.
        Index(
            "ix_schedule_conflicts_unresolved",
            "detected_at",
            postgresql_where=text("resolution = 'unresolved'"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ScheduleConflict appt={self.appointment_id} {self.resolution.value}>"
