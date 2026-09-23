"""Mirrored "busy" intervals pulled from a doctor's external calendar.

DESIGN DECISION: why a separate table instead of just flipping
`time_slots.is_blocked = true` when the doctor's calendar is busy.

`is_blocked` (Phase 1) means "a human deliberately blocked this off in OUR
system". If the sync job also wrote to it, the two meanings collide and the
job can no longer clean up after itself: when the doctor deletes their
personal event, should the slot become bookable again? With one shared flag
there is no way to know whether it was blocked by the sync or by the front
desk, so you either strand permanently-blocked slots or you clobber a staff
decision.

Keeping one row per external event, owned by the sync job, makes both
directions trivially correct:

    bookable(slot) = NOT slot.is_blocked            (staff decision)
                     AND no active appointment       (Phase 1 constraint)
                     AND no overlapping busy block   (this table)

Deleting the external event deletes the row here and the slot is bookable
again, with the staff flag untouched.

COST: availability becomes an overlap query rather than a column read. That
is one extra indexed join, which at clinic scale is nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.calendar_connection import CalendarConnection
    from app.models.doctor import Doctor


class ExternalBusyBlock(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "external_busy_blocks"

    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendar_connections.id", ondelete="CASCADE"), nullable=False
    )
    # CASCADE: these rows are a pure cache of someone else's calendar. If the
    # connection goes away they are meaningless and must not linger blocking
    # slots forever.

    # Denormalized from the connection, exactly like appointments.doctor_id in
    # Phase 1: the availability query filters by doctor and would otherwise
    # need a join to calendar_connections on every call.
    doctor_id: Mapped[UUID] = mapped_column(
        ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False
    )

    # Google's event ID. For a recurring event expanded via singleEvents=true
    # this is the INSTANCE id (e.g. "abc123_20260921T140000Z"), so each
    # occurrence gets its own row -- which is what we want, since each
    # occurrence blocks a different set of slots.
    external_event_id: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Google's ETag. Lets a future optimization skip rows that have not
    # changed without comparing every field.
    external_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Half-open [starts_at, ends_at), same convention as time_slots, so
    # adjacency is not mistaken for overlap.
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # WHY track this separately: Google returns all-day events as a bare
    # `date` with no time and no zone. Turning "2026-09-21" into an instant
    # range requires the DOCTOR's timezone, not UTC and not the server's.
    # Getting this wrong shifts a vacation day by several hours and leaves
    # bookable slots at the edges -- a classic and very annoying bug.
    is_all_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Populated ONLY when the connection has store_event_details enabled.
    # See the privacy note on CalendarConnection.store_event_details.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # SOFT DELETE. Set when the event disappears from the doctor's calendar
    # (deleted, or moved out of our sync window).
    #
    # WHY not just DELETE the row: schedule_conflicts references this table
    # with ON DELETE RESTRICT, because a near-miss must stay in the audit
    # trail after the offending event is gone. A hard delete of a conflicted
    # block would therefore raise a ForeignKeyViolation -- and since the sync
    # job would retry the same delete on every tick, that doctor's sync would
    # be permanently wedged. Soft deletion sidesteps that entirely and keeps
    # the conflict row pointing at readable history instead of a dangling ID.
    #
    # THE TRAP THIS CREATES: every query that treats a block as "currently
    # busy" MUST filter `deleted_at IS NULL`. Forget it in one place and
    # cancelled events go on blocking slots forever, which looks like a
    # doctor mysteriously having no availability. The filter lives in
    # availability_service so there is exactly one place to get it right, and
    # there is a test for precisely this.
    #
    # Revived events (an event restored from Google's trash keeps its ID) are
    # handled by the upsert setting deleted_at back to NULL -- no special
    # case needed, since the unique constraint below still matches the row.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    connection: Mapped["CalendarConnection"] = relationship(back_populates="busy_blocks")
    doctor: Mapped["Doctor"] = relationship()

    __table_args__ = (
        # Makes the sync job idempotent: re-processing the same event
        # upserts one row instead of piling up duplicates. This is the
        # calendar-sync equivalent of Phase 1's booking constraint -- the
        # database, not the job's control flow, guarantees "one row per
        # external event".
        UniqueConstraint(
            "connection_id", "external_event_id", name="uq_external_busy_blocks_connection_id_external_event_id"
        ),
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        # Supports the overlap predicate:
        #   WHERE doctor_id = ? AND starts_at < :slot_end AND ends_at > :slot_start
        # A btree on (doctor_id, starts_at) narrows to the doctor and then
        # range-scans forward. REVISIT: if a clinic ever mirrors hundreds of
        # thousands of events, a GiST index over tstzrange(starts_at, ends_at)
        # with btree_gist is the proper tool for overlap -- overkill now.
        # PARTIAL on deleted_at IS NULL: soft-deleted rows accumulate forever
        # and are never candidates for an overlap check, so keeping them out
        # of the index keeps it small and makes the intended access path
        # ("live busy blocks for this doctor") explicit.
        Index(
            "ix_external_busy_blocks_doctor_id_starts_at",
            "doctor_id",
            "starts_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    @property
    def is_live(self) -> bool:
        """Whether this block should currently prevent bookings."""
        return self.deleted_at is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ExternalBusyBlock {self.starts_at.isoformat()}->{self.ends_at.isoformat()}>"
