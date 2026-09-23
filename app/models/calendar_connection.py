"""A doctor's authorized link to an external calendar, and its OAuth tokens."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import EncryptedString
from app.models.enums import CalendarConnectionState, CalendarProvider

if TYPE_CHECKING:
    from app.models.doctor import Doctor
    from app.models.external_busy_block import ExternalBusyBlock


def _enum_column(enum_cls, name: str, length: int = 32, **kw):
    """VARCHAR-backed enum, matching the Phase 1 convention.

    See models/appointment.py for why native_enum=False and values_callable
    are both required (storing member VALUES, not NAMES).
    """
    return mapped_column(
        SAEnum(
            enum_cls,
            name=name,
            native_enum=False,
            length=length,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
            # create_constraint=True so the CHECK that models/enums.py
            # PROMISES ("stored as VARCHAR + CHECK") actually exists in the
            # metadata. SQLAlchemy defaults this to False, which means the
            # constraint silently would not exist -- and, worse, would differ
            # between `create_all` (tests) and the migration (production).
            # Declaring it here keeps autogenerate, tests and prod identical.
            create_constraint=True,
        ),
        nullable=False,
        **kw,
    )


class CalendarConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One doctor <-> one external calendar, plus the credentials to reach it.

    SIMPLIFICATION: doctor_id is UNIQUE, so a doctor may connect exactly one
    calendar. Real doctors often keep a personal calendar and a clinic
    calendar and want both treated as busy. Lifting this is just dropping the
    unique constraint and iterating connections in the sync job -- the rest
    of the schema already supports it (busy blocks hang off the connection,
    not the doctor). Flagged for Phase 3.
    """

    __tablename__ = "calendar_connections"

    doctor_id: Mapped[UUID] = mapped_column(
        ForeignKey("doctors.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # WHY CASCADE here but RESTRICT everywhere in Phase 1: a calendar
    # connection is derived infrastructure, not a medical record. If a doctor
    # row is ever deleted, their OAuth tokens SHOULD disappear with it --
    # leaving live refresh tokens for a deleted user is a security problem,
    # not an audit trail worth keeping.

    provider = _enum_column(CalendarProvider, "calendar_provider")

    # The Google account that granted access. Stored so staff can see WHICH
    # account is linked ("you connected your personal gmail, not your clinic
    # account") and so re-auth can verify the same account came back.
    account_email: Mapped[str] = mapped_column(String(320), nullable=False)

    # Google's ID for the specific calendar within that account. "primary" is
    # the account's default calendar. ASSUMPTION: we sync exactly one
    # calendar per connection.
    calendar_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="primary", server_default="primary"
    )

    # ---------------------------------------------------------------- #
    # Credentials. Both encrypted at rest -- see db/types.py for the
    # threat model and the (real) limits of doing this with an env-var key.
    # ---------------------------------------------------------------- #
    #
    # The refresh token is the crown jewel: it is long-lived and can mint
    # access tokens indefinitely until revoked. Never log it, never return it
    # from an API, never put it in an error message.
    #
    # NULLABLE, and the CHECK below ties it to `state`. WHY nullable when a
    # connection is useless without one: "disconnect" must destroy the
    # credential, and it cannot do that by deleting the row.
    #
    # Deleting a calendar_connections row CASCADEs to external_busy_blocks,
    # which schedule_conflicts references with RESTRICT -- so a hard delete
    # raises a ForeignKeyViolation for exactly the doctors who have had a
    # conflict, i.e. the ones the audit trail matters most for. Keeping the
    # row and nulling the secret destroys the credential while leaving the
    # conflict history readable.
    refresh_token: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    # WHY cache the access token at all instead of refreshing every time:
    # Google rate-limits token minting, and a refresh is a full extra HTTPS
    # round trip on every sync tick. Caching it until ~expiry turns N
    # refreshes per sync into roughly one per hour.
    access_token: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Space-separated OAuth scopes actually granted. WHY store them: the user
    # can untick scopes on Google's consent screen. If we asked for write
    # access and only got read, pushing events will fail on every attempt --
    # far better to detect that at connect time and tell them.
    granted_scopes: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    # ---------------------------------------------------------------- #
    # Incremental sync bookkeeping
    # ---------------------------------------------------------------- #
    #
    # Google's opaque cursor from events.list. Passing it on the next call
    # returns only what changed since. WHY this matters: without it every
    # sync re-downloads the doctor's entire calendar window, which burns API
    # quota and makes a 5-minute sync interval untenable.
    #
    # It expires. Google answers 410 GONE when it does, which is NOT an error
    # condition -- it is the documented signal to drop the token and do a
    # full resync. See the sync strategy notes in the Phase 2 write-up.
    sync_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_full_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    state = _enum_column(
        CalendarConnectionState,
        "calendar_connection_state",
        default=CalendarConnectionState.ACTIVE,
        server_default=CalendarConnectionState.ACTIVE.value,
    )

    # Circuit breaker. Reset to 0 on every success. WHY: a doctor whose
    # calendar consistently 500s should not be retried every 5 minutes
    # forever -- back off, and after enough failures stop and alert instead
    # of generating infinite noise.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # PRIVACY DECISION, defaults to OFF. When false we store only the busy
    # INTERVALS from the doctor's calendar, never the event titles.
    #
    # WHY this matters more than it looks: we are ingesting a person's
    # personal calendar. Titles routinely contain things like "Oncology
    # follow-up", "AA meeting", "divorce mediation". Copying those into the
    # clinic's database means they land in backups, logs, and any future
    # admin UI -- a serious privacy leak that nobody asked for, to power a
    # feature ("why is this slot blocked?") that staff rarely need.
    # Opt-in per doctor, with informed consent, or not at all.
    store_event_details: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    doctor: Mapped["Doctor"] = relationship(back_populates="calendar_connection")
    busy_blocks: Mapped[list["ExternalBusyBlock"]] = relationship(
        back_populates="connection",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("consecutive_failures >= 0", name="failures_non_negative"),
        # An ACTIVE connection without a refresh token is a connection that
        # will stop working within the hour and cannot recover. Making that
        # state unrepresentable in the database means no code path -- not a
        # buggy re-auth, not a half-finished disconnect -- can leave a doctor
        # silently un-syncable.
        CheckConstraint(
            "state <> 'active' OR refresh_token IS NOT NULL",
            name="active_requires_refresh_token",
        ),
    )

    @property
    def is_syncable(self) -> bool:
        """Whether a sync job should touch this connection at all."""
        return self.state is CalendarConnectionState.ACTIVE

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # NOTE: deliberately does NOT include tokens. A __repr__ ends up in
        # tracebacks, Sentry events and log lines.
        return f"<CalendarConnection {self.id} doctor={self.doctor_id} {self.state.value}>"
