"""Patient model."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment


class Patient(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "patients"

    full_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # WHY phone is the unique natural key rather than email: the voice channel
    # identifies a caller by caller ID, and many patients (especially older
    # ones) have no email. Phone is the one identifier every patient has.
    #
    # ASSUMPTION TO REVISIT: stored in E.164 ("+14155550123"), normalized in
    # the service layer before it ever reaches this column. Uniqueness here is
    # only meaningful if normalization is airtight -- "+1 415 555 0123" and
    # "+14155550123" are two different rows to Postgres. When you add the
    # voice channel, put the normalizer (libphonenumber) behind a single
    # function and call it from every entry point.
    #
    # ALSO REVISIT: families share phone numbers. A hard UNIQUE will bite the
    # first time a parent books for two children. The likely fix is a
    # (phone, date_of_birth) composite key, or dropping uniqueness and
    # disambiguating by name during the chatbot conversation.
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, index=True)

    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # Phase 3. The SECOND FACTOR for chatbot identity: the caller's phone
    # identifies them, this proves it.
    #
    # NULLABLE, and that nullability is load-bearing rather than laziness:
    # every patient created before Phase 3 has no date of birth on file,
    # and a clinic's real records will always contain some without one.
    # A patient whose DOB is unknown CANNOT be verified by the chatbot and
    # must be escalated to a human -- see conversation_service. Making this
    # column NOT NULL would instead mean either inventing data or refusing
    # to represent real patients.
    #
    # STORED IN PLAINTEXT, deliberately. It is PHI, and encrypting only
    # this one column while name, phone and appointment history sit in
    # plaintext beside it would be security theatre -- it protects against
    # no threat that does not already expose the rest of the row. The
    # control that actually applies here is encryption at rest for the
    # whole database plus access control, not per-column encryption.
    # FLAGGED so the omission is a decision on record, not an oversight.
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)

    appointments: Mapped[list["Appointment"]] = relationship(
        back_populates="patient",
        # WHY no cascade delete: patient records are medical-adjacent data.
        # Deletion should be a deliberate, audited operation, never a side
        # effect of an ORM call. The FK on appointments is RESTRICT.
        passive_deletes=False,
    )

    __table_args__ = (
        CheckConstraint("phone ~ '^\\+[1-9][0-9]{7,14}$'", name="phone_is_e164"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Patient {self.id} {self.full_name!r}>"
