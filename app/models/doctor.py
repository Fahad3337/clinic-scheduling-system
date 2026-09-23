"""Doctor model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.calendar_connection import CalendarConnection
    from app.models.time_slot import TimeSlot


class Doctor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A practitioner who can be booked.

    SIMPLIFICATION: the spec says "single doctor for now", but modelling the
    table anyway costs nothing and the FK from time_slots means the
    multi-doctor generalization is a seeding change, not a migration. There is
    deliberately no `is_the_one_doctor` shortcut anywhere.
    """

    __tablename__ = "doctors"

    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    specialty: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # WHY store an IANA timezone per doctor: all instants in this system are
    # TIMESTAMPTZ (UTC on the wire). But "show me Tuesday's availability" is a
    # question about a *local* calendar day, and the boundaries of that day
    # depend on the clinic's zone, including DST transitions. The availability
    # service converts a local date into a UTC half-open range using this.
    # Hardcoding UTC here would silently break the first clinic that isn't in
    # London -- and would break twice a year even for one that is.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    # Default consultation length, used by the slot generator.
    # SIMPLIFICATION: one duration per doctor. Real clinics have appointment
    # *types* (15min follow-up vs 45min new-patient) which is a separate table
    # and changes availability from "list of slots" to "list of slots filtered
    # by requested duration". Flagged for Phase 2.
    slot_duration_minutes: Mapped[int] = mapped_column(nullable=False, default=30)

    time_slots: Mapped[list["TimeSlot"]] = relationship(back_populates="doctor")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="doctor")

    # Phase 2. uselist=False because doctor_id is UNIQUE on the connection
    # -- one calendar per doctor for now (see CalendarConnection docstring).
    calendar_connection: Mapped["CalendarConnection | None"] = relationship(
        back_populates="doctor", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Doctor {self.id} {self.full_name!r}>"
