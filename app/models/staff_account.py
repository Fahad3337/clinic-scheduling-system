"""Staff/doctor login accounts.

This is the first thing that makes the API's endpoints not-open-to-anyone.
See docs/security-no-authentication.md for the risk this closes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, Enum as SAEnum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import StaffRole

if TYPE_CHECKING:
    from app.models.doctor import Doctor


class StaffAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "staff_accounts"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)

    # bcrypt's own output ($2b$...), ~60 chars, includes the salt. Never a
    # plaintext or reversibly-encrypted password -- there is no legitimate
    # reason for this application to ever be able to recover one.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role = mapped_column(
        SAEnum(
            StaffRole,
            name="staff_role",
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
            create_constraint=True,
        ),
        nullable=False,
    )

    # Required for DOCTOR, forbidden for FRONT_DESK -- see the CHECK below.
    # RESTRICT: a doctor's login must not silently start acting as
    # "unscoped staff" because their Doctor row was deleted out from under
    # them; deletion must be a deliberate act that also handles this account.
    doctor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("doctors.id", ondelete="RESTRICT"), nullable=True
    )

    # A deactivated account must stop working IMMEDIATELY, not at token
    # expiry. See api/deps.py -- the auth dependency reloads this row on
    # every request rather than trusting only the JWT payload, specifically
    # so flipping this to false takes effect on the very next request.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    doctor: Mapped["Doctor | None"] = relationship()

    __table_args__ = (
        CheckConstraint(
            "(role = 'doctor' AND doctor_id IS NOT NULL) OR "
            "(role = 'front_desk' AND doctor_id IS NULL)",
            name="doctor_role_requires_doctor_id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # No password_hash: a hash is not a secret in the same class as a
        # refresh token, but a __repr__ ending up in a log line is still not
        # where it belongs.
        return f"<StaffAccount {self.email} {self.role.value}>"
