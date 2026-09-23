"""Outbound mirror: OUR appointment as an event on the doctor's calendar.

This is a transactional outbox, and it exists because of a rule Phase 1
already established: never make an external HTTP call inside the booking
transaction.

Phase 1's booking flow holds a FOR UPDATE lock on the time_slot from the
moment it checks availability until COMMIT. If we called the Google Calendar
API in the middle of that, then:

  - every booking would be as slow as Google's p99 latency,
  - a Google outage or timeout would hold the slot lock open until
    lock_timeout fired, blocking every other patient trying for that slot,
  - and a failure after the API call but before COMMIT would leave an event
    on the doctor's calendar for an appointment that does not exist.

Instead, booking writes a row HERE, in the same transaction as the
appointment. It commits atomically with the booking -- so the intent to push
can never be lost -- and a background job does the actual API call later and
retries on its own schedule. Same pattern as the notifications table.

The cost is eventual consistency: for a few seconds the appointment exists
in our system and not on the doctor's calendar. That is the right trade.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import CalendarPushState

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.calendar_connection import CalendarConnection


class AppointmentExternalEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Tracks one appointment's representation on one external calendar.

    SIMPLIFICATION considered and rejected: putting `external_event_id` and
    a push status directly on `appointments` as two columns. That is fewer
    tables, but it hardcodes "one calendar per appointment" into the core
    booking table, and it means every push retry UPDATEs the appointments row
    -- contending with the booking path's locks on the busiest table in the
    system. A side table keeps retry churn off the hot path.
    """

    __tablename__ = "appointment_external_events"

    appointment_id: Mapped[UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendar_connections.id", ondelete="CASCADE"), nullable=False
    )

    # NULL until the first successful create. WHY nullable: the outbox row is
    # written before Google has ever seen this appointment, so there is no
    # external id yet. A non-null column would force a placeholder value that
    # code would then have to special-case anyway.
    external_event_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    external_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)

    push_state = mapped_column(
        SAEnum(
            CalendarPushState,
            name="calendar_push_state",
            native_enum=False,
            length=32,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
            create_constraint=True,
        ),
        nullable=False,
        default=CalendarPushState.PENDING,
        server_default=CalendarPushState.PENDING.value,
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    appointment: Mapped["Appointment"] = relationship(back_populates="external_events")
    connection: Mapped["CalendarConnection"] = relationship()

    __table_args__ = (
        # One outbox row per (appointment, calendar). Makes "ensure this
        # appointment is pushed" an idempotent upsert rather than something
        # that needs a prior existence check.
        UniqueConstraint(
            "appointment_id", "connection_id", name="uq_appointment_external_events_appointment_id_connection_id"
        ),
        # The pusher's work queue: rows in a non-terminal state. Partial for
        # the same reason as the notifications index -- SYNCED rows
        # accumulate forever and are never work.
        Index(
            "ix_appointment_external_events_pending",
            "push_state",
            postgresql_where=text(
                "push_state IN ('pending','update_pending','delete_pending','failed')"
            ),
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # You cannot be SYNCED without knowing what you are synced to.
        CheckConstraint(
            "push_state <> 'synced' OR external_event_id IS NOT NULL",
            name="synced_requires_external_id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AppointmentExternalEvent {self.appointment_id} {self.push_state.value}>"
