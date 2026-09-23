"""Appointment model -- and the database-level guarantee against double-booking.

READ THIS BEFORE CHANGING `uq_appointments_active_slot` BELOW.

The race we are defending against
---------------------------------
Two requests arrive for the same slot at the same millisecond, on two
different API workers:

    worker A                          worker B
    SELECT ... slot is free           SELECT ... slot is free
    INSERT appointment                INSERT appointment
    COMMIT                            COMMIT        <-- double booked

Every check-then-act pattern in application code has this window, no matter
how tight. Under Postgres' default READ COMMITTED isolation, neither
transaction can see the other's uncommitted INSERT, so both checks pass. The
bug is not "our check is too slow"; it is that the check and the write are not
atomic. Retrying, sleeping, or double-checking cannot close it.

Strategy: a partial unique index (the guarantee) + SELECT FOR UPDATE (the
ergonomics). These solve different problems and we want both.

1. PARTIAL UNIQUE INDEX -- correctness. This is the load-bearing one.
   `uq_appointments_active_slot` makes it *physically impossible* for two
   non-cancelled appointments to share a time_slot_id. Postgres enforces
   uniqueness at the index level during INSERT: the second writer blocks until
   the first commits, then fails with a unique violation. It cannot be raced,
   because the index is the same shared structure both writers must pass
   through.

   WHY this over locking alone: it holds regardless of how the write arrived
   -- a second API replica, a background job, a chatbot worker, a nurse
   running raw SQL, or a future service we haven't written. A lock only
   protects code that remembers to take it. An invariant this important
   belongs in the schema, not in a convention.

   WHY *partial* (WHERE status <> 'cancelled'): a plain UNIQUE(time_slot_id)
   would mean a cancelled 10:00 appointment permanently burns the 10:00 slot,
   since the cancelled row still occupies the index. The predicate excludes
   cancelled rows, so cancelling genuinely frees capacity while the historical
   row survives. Completed and no-show rows stay in the index deliberately --
   a slot that was actually used is not available, even retroactively.

2. SELECT ... FOR UPDATE on the parent time_slots row -- ergonomics.
   The index alone gives a correct but *ugly* outcome: the loser's transaction
   aborts with an IntegrityError after doing work, and in async SQLAlchemy a
   failed statement poisons the session (every later statement raises
   PendingRollbackError until you roll back). The service layer therefore
   takes a row lock on the time_slot first, which serializes the two bookers
   before either writes: the loser waits, then re-reads and sees the winner's
   appointment, and returns a clean 409 without ever hitting the constraint.

   So: the lock turns the common case into an orderly queue; the constraint
   catches everything the lock cannot see. The service still wraps the INSERT
   in an IntegrityError handler -- treating the constraint as unreachable
   would defeat the point of having it.

   COST: FOR UPDATE holds the row lock until COMMIT, so concurrent bookings
   for one slot are serialized. That is exactly the semantics we want, and
   contention is per-slot, not global -- different slots never block each
   other. Keep the transaction short: no HTTP calls (SMS, payment) inside it.

Rejected alternatives, and why
------------------------------
  - SERIALIZABLE isolation: correct, but pushes retry logic into every caller
    and costs throughput across the whole app to fix one table's invariant.
  - Advisory locks (pg_advisory_xact_lock on a hash of the slot id): works,
    but hash collisions silently serialize unrelated slots and the lock is
    invisible to anyone reading the schema.
  - Redis/distributed lock: adds a second system that can disagree with the
    database. The database already has the answer.
  - Application-level mutex: only correct with exactly one worker process,
    which is not a system, it's a demo.
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
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AppointmentStatus, BookingChannel

if TYPE_CHECKING:
    from app.models.appointment_external_event import AppointmentExternalEvent
    from app.models.doctor import Doctor
    from app.models.notification import Notification
    from app.models.patient import Patient
    from app.models.schedule_conflict import ScheduleConflict
    from app.models.time_slot import TimeSlot


def _enum_column(enum_cls, name: str, length: int = 20):
    """Build a VARCHAR-backed enum column.

    WHY `values_callable`: by default SQLAlchemy persists the enum member's
    *name* ("BOOKED"), not its value ("booked"). That would be a silent
    landmine here -- the partial index predicate below is written against the
    literal string 'cancelled', and a mismatch means the index quietly stops
    excluding cancelled rows. Forcing `.value` keeps Python, the stored data,
    the CHECK constraint and the index predicate all speaking one language.

    WHY native_enum=False: see the module docstring in models/enums.py -- it
    makes adding a status a normal migration instead of an ALTER TYPE dance.

    WHY create_constraint=True: SQLAlchemy defaults this to False, so for
    most of Phase 1 these columns were plain VARCHARs in the ORM's view even
    though models/enums.py promised "VARCHAR + CHECK" and the 0001 migration
    hand-wrote the CHECKs. The result was that production had constraints the
    test database did not -- tests were weaker than prod, and a code path
    writing a bogus status would pass CI and fail on deploy. Declaring it
    here makes `create_all` (tests) and the migration (prod) produce the same
    schema. Corrected in migration 0003.
    """
    return mapped_column(
        SAEnum(
            enum_cls,
            name=name,
            native_enum=False,
            length=length,
            values_callable=lambda e: [member.value for member in e],
            validate_strings=True,
            create_constraint=True,
        ),
        nullable=False,
    )


class Appointment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointments"

    patient_id: Mapped[UUID] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )

    # DENORMALIZATION, on purpose: doctor_id is derivable via time_slot.
    # It is stored anyway because "all appointments for doctor X this month"
    # is the single most common query in the system (and the first thing the
    # chatbot will ask), and this turns a join into an index scan. The cost is
    # that it can drift from time_slot.doctor_id -- the service layer always
    # copies it from the slot, never from client input. If you want belt and
    # braces, add a composite FK (time_slot_id, doctor_id) -> time_slots so
    # Postgres enforces agreement; skipped for now to keep the schema legible.
    doctor_id: Mapped[UUID] = mapped_column(
        ForeignKey("doctors.id", ondelete="RESTRICT"), nullable=False
    )

    time_slot_id: Mapped[UUID] = mapped_column(
        ForeignKey("time_slots.id", ondelete="RESTRICT"), nullable=False
    )

    # Phase 3. NULL for an appointment booked directly; set to the OLD
    # appointment's id when this row exists because of a reschedule.
    #
    # WHY a new row + this pointer, instead of mutating time_slot_id on the
    # existing appointment: identical reasoning to Phase 1's cancel-and-
    # rebook design (see the module docstring in models/enums.py under
    # AppointmentStatus) -- the appointment row is the audit record, so
    # "the patient was originally booked for 10:00, then moved to 14:00"
    # must stay readable after the fact. It also means the notification
    # dedupe key (kind, channel, appointment_id, time_slot_id) never needs
    # to change: a reschedule is a new appointment_id, so it is
    # automatically a new key, not a collision to reason about.
    #
    # RESTRICT rather than SET NULL/CASCADE: this is the same audit-trail
    # argument as every other RESTRICT in this schema. Self-referential, so
    # ondelete has no effect on appointments deleted normally (they never
    # are -- Phase 1 never hard-deletes an appointment), but is correct in
    # principle if that ever changes.
    rescheduled_from_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=True
    )

    status: Mapped[AppointmentStatus] = _enum_column(
        AppointmentStatus, "appointment_status"
    )
    booking_channel: Mapped[BookingChannel] = _enum_column(
        BookingChannel, "booking_channel"
    )

    # Free-text "why are you coming in". Text, not String(n): clinical notes
    # have no natural length limit and Postgres stores both identically.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    patient: Mapped["Patient"] = relationship(back_populates="appointments")
    doctor: Mapped["Doctor"] = relationship(back_populates="appointments")
    time_slot: Mapped["TimeSlot"] = relationship(back_populates="appointments")

    # Phase 2. All three are append-only trails hanging off an appointment;
    # none cascade-delete, matching the RESTRICT FKs on their own side.
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="appointment"
    )
    external_events: Mapped[list["AppointmentExternalEvent"]] = relationship(
        back_populates="appointment"
    )
    conflicts: Mapped[list["ScheduleConflict"]] = relationship(
        back_populates="appointment"
    )

    # Self-referential: the appointment this one replaced, if rescheduled.
    # remote_side pins which column is the "one" side for SQLAlchemy, since
    # both sides of the FK live on the same table.
    rescheduled_from: Mapped["Appointment | None"] = relationship(
        remote_side="Appointment.id", foreign_keys=[rescheduled_from_id]
    )

    __table_args__ = (
        # ===================================================================
        # THE ANTI-DOUBLE-BOOKING CONSTRAINT. See module docstring.
        # ===================================================================
        # Expressed as a partial unique Index rather than a UniqueConstraint
        # because only an Index accepts a WHERE predicate. Alembic emits:
        #   CREATE UNIQUE INDEX uq_appointments_active_slot
        #       ON appointments (time_slot_id)
        #       WHERE status <> 'cancelled';
        Index(
            "uq_appointments_active_slot",
            "time_slot_id",
            unique=True,
            postgresql_where=text("status <> 'cancelled'"),
        ),
        # Supports "my upcoming appointments" without touching the heap.
        Index("ix_appointments_patient_id_status", "patient_id", "status"),
        Index("ix_appointments_doctor_id_status", "doctor_id", "status"),
    )

    @property
    def is_active(self) -> bool:
        """True when this appointment currently occupies its slot.

        Mirrors the partial index predicate above. If you change one, change
        the other -- they are two expressions of the same rule.
        """
        return self.status is not AppointmentStatus.CANCELLED

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Appointment {self.id} {self.status.value}>"
