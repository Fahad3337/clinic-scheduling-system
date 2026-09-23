"""phase 2: calendar sync and notifications

Revision ID: 0002_calendar_notifications
Revises: 0001_initial
Create Date: 2026-09-20

Produced with `alembic revision --autogenerate` against a database at
0001_initial, then reviewed and adjusted by hand. Three changes were made to
the generated output -- each is marked [ADJUSTED] below. Autogenerate is a
drafting tool, not an authority; it does not know intent.

WHAT AUTOGENERATE CAUGHT: a real naming drift left over from Phase 1. The
patients.phone index was created as "uq_patients_phone" by the hand-written
0001 migration, but the model declares `unique=True, index=True`, which
SQLAlchemy names "ix_patients_phone". Same index, different name -- harmless
in isolation, but it meant EVERY future autogenerate run would emit a
spurious drop/create for it, training whoever reads the diff to ignore
changes to the patients table. Fixed here, once. This is exactly the check
0001's docstring asked for.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_calendar_notifications"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# [ADJUSTED #1] Autogenerate emitted `app.db.types.EncryptedString()` for the
# token columns and did not import it, so the migration would have failed
# with NameError on first run.
#
# The fix is not to add the import -- it is to NOT reference application code
# from a migration at all. A migration is a historical record that must still
# run years from now; if it imports app.db.types, then renaming, moving or
# deleting that class silently breaks the ability to rebuild the database
# from scratch. EncryptedString's `impl` is Text, so the DDL is byte-for-byte
# identical. Encryption is an application-layer concern and leaves no trace
# in the schema, which is precisely why substituting the underlying type here
# is safe.
_ENCRYPTED = sa.Text


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # calendar_connections -- OAuth credentials + sync cursor per doctor
    # ------------------------------------------------------------------ #
    op.create_table(
        "calendar_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("doctor_id", sa.UUID(), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("google", name="calendar_provider", native_enum=False, create_constraint=True, length=32),
            nullable=False,
        ),
        sa.Column("account_email", sa.String(length=320), nullable=False),
        sa.Column("calendar_id", sa.String(length=255), server_default="primary", nullable=False),
        # Encrypted at rest by the application; see [ADJUSTED #1] above.
        sa.Column("refresh_token", _ENCRYPTED(), nullable=False),
        sa.Column("access_token", _ENCRYPTED(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("granted_scopes", sa.Text(), server_default="", nullable=False),
        sa.Column("sync_token", sa.Text(), nullable=True),
        sa.Column("last_full_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "active", "needs_reauth", "disabled",
                name="calendar_connection_state", native_enum=False, create_constraint=True, length=32,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("store_event_details", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("consecutive_failures >= 0", name=op.f("ck_calendar_connections_failures_non_negative")),
        sa.ForeignKeyConstraint(
            ["doctor_id"], ["doctors.id"],
            name=op.f("fk_calendar_connections_doctor_id_doctors"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_connections")),
        # One calendar per doctor for now -- see the CalendarConnection docstring.
        sa.UniqueConstraint("doctor_id", name=op.f("uq_calendar_connections_doctor_id")),
    )

    # ------------------------------------------------------------------ #
    # external_busy_blocks -- mirror of the doctor's calendar
    # ------------------------------------------------------------------ #
    op.create_table(
        "external_busy_blocks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("doctor_id", sa.UUID(), nullable=False),
        sa.Column("external_event_id", sa.String(length=1024), nullable=False),
        sa.Column("external_etag", sa.String(length=255), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_all_day", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        # Soft delete. Required because schedule_conflicts references this
        # table with ON DELETE RESTRICT -- a hard delete of a conflicted block
        # would raise, and the sync job would retry it forever.
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_external_busy_blocks_ends_after_start")),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["calendar_connections.id"],
            name=op.f("fk_external_busy_blocks_connection_id_calendar_connections"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"], ["doctors.id"],
            name=op.f("fk_external_busy_blocks_doctor_id_doctors"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_busy_blocks")),
        # Makes the sync job's upsert idempotent.
        sa.UniqueConstraint(
            "connection_id", "external_event_id",
            name="uq_external_busy_blocks_connection_id_external_event_id",
        ),
    )
    op.create_index(
        "ix_external_busy_blocks_doctor_id_starts_at",
        "external_busy_blocks",
        ["doctor_id", "starts_at"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ------------------------------------------------------------------ #
    # appointment_external_events -- outbox for pushing OUR bookings out
    # ------------------------------------------------------------------ #
    op.create_table(
        "appointment_external_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("appointment_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("external_event_id", sa.String(length=1024), nullable=True),
        sa.Column("external_etag", sa.String(length=255), nullable=True),
        sa.Column(
            "push_state",
            sa.Enum(
                "pending", "synced", "update_pending", "delete_pending", "deleted", "failed",
                name="calendar_push_state", native_enum=False, create_constraint=True, length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "push_state <> 'synced' OR external_event_id IS NOT NULL",
            name=op.f("ck_appointment_external_events_synced_requires_external_id"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_appointment_external_events_attempts_non_negative")),
        sa.ForeignKeyConstraint(
            ["appointment_id"], ["appointments.id"],
            name=op.f("fk_appointment_external_events_appointment_id_appointments"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["calendar_connections.id"],
            name=op.f("fk_appointment_external_events_connection_id_calendar_connections"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointment_external_events")),
        sa.UniqueConstraint(
            "appointment_id", "connection_id",
            name="uq_appointment_external_events_appointment_id_connection_id",
        ),
    )
    op.create_index(
        "ix_appointment_external_events_pending",
        "appointment_external_events",
        ["push_state"],
        unique=False,
        postgresql_where=sa.text("push_state IN ('pending','update_pending','delete_pending','failed')"),
    )

    # ------------------------------------------------------------------ #
    # notifications -- outbox + audit trail. Enforces send-once.
    # ------------------------------------------------------------------ #
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("appointment_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "booking_confirmation", "reminder_24h", "cancellation_confirmation",
                name="notification_kind", native_enum=False, create_constraint=True, length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "channel",
            sa.Enum("sms", "email", name="notification_channel", native_enum=False, create_constraint=True, length=16),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "claimed", "sent", "failed", "abandoned", "skipped", "unresolved",
                name="notification_status", native_enum=False, create_constraint=True, length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status <> 'sent' OR sent_at IS NOT NULL", name=op.f("ck_notifications_sent_requires_sent_at")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_notifications_attempts_non_negative")),
        sa.ForeignKeyConstraint(
            ["appointment_id"], ["appointments.id"],
            name=op.f("fk_notifications_appointment_id_appointments"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index("ix_notifications_appointment_id", "notifications", ["appointment_id"], unique=False)
    op.create_index(
        "ix_notifications_claimable",
        "notifications",
        ["scheduled_for"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending','failed')"),
    )
    # ===================================================================
    # THE send-once GUARANTEE. Everything the notification sender does for
    # idempotency rests on this one unique index. See models/notification.py.
    # ===================================================================
    op.create_index("uq_notifications_dedupe_key", "notifications", ["dedupe_key"], unique=True)

    # ------------------------------------------------------------------ #
    # schedule_conflicts -- booking vs. doctor's personal calendar
    # ------------------------------------------------------------------ #
    op.create_table(
        "schedule_conflicts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("appointment_id", sa.UUID(), nullable=False),
        sa.Column("busy_block_id", sa.UUID(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolution",
            sa.Enum(
                "unresolved", "appointment_rescheduled", "appointment_cancelled",
                "external_event_removed", "ignored_by_staff",
                name="conflict_resolution", native_enum=False, create_constraint=True, length=32,
            ),
            server_default="unresolved",
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["appointment_id"], ["appointments.id"],
            name=op.f("fk_schedule_conflicts_appointment_id_appointments"), ondelete="RESTRICT",
        ),
        # RESTRICT: conflict rows are an audit trail and must outlive the
        # calendar event that caused them. This is what forces the soft
        # delete on external_busy_blocks above.
        sa.ForeignKeyConstraint(
            ["busy_block_id"], ["external_busy_blocks.id"],
            name=op.f("fk_schedule_conflicts_busy_block_id_external_busy_blocks"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule_conflicts")),
        sa.UniqueConstraint(
            "appointment_id", "busy_block_id",
            name="uq_schedule_conflicts_appointment_id_busy_block_id",
        ),
    )
    op.create_index(
        "ix_schedule_conflicts_unresolved",
        "schedule_conflicts",
        ["detected_at"],
        unique=False,
        postgresql_where=sa.text("resolution = 'unresolved'"),
    )

    # ------------------------------------------------------------------ #
    # [ADJUSTED #2] Phase 1 drift fix: rename, do not drop and recreate.
    #
    # Autogenerate proposed:
    #     op.drop_index("uq_patients_phone", ...)
    #     op.create_index("ix_patients_phone", ..., unique=True)
    #
    # That is functionally right and operationally wrong. Dropping a unique
    # index releases the uniqueness guarantee for the duration of the
    # rebuild, so a concurrent INSERT could slip a duplicate phone number in
    # and then the CREATE INDEX fails, leaving the table with no unique index
    # at all. It also rewrites the whole index for a cosmetic change.
    #
    # ALTER INDEX ... RENAME is a catalog-only operation: instant, keeps the
    # constraint enforced throughout, and no rebuild.
    # ------------------------------------------------------------------ #
    op.execute("ALTER INDEX uq_patients_phone RENAME TO ix_patients_phone")


def downgrade() -> None:
    # Reverse order: schedule_conflicts references external_busy_blocks with
    # RESTRICT, so it must go first or the drop is refused.
    op.execute("ALTER INDEX ix_patients_phone RENAME TO uq_patients_phone")

    op.drop_index("ix_schedule_conflicts_unresolved", table_name="schedule_conflicts")
    op.drop_table("schedule_conflicts")

    op.drop_index("uq_notifications_dedupe_key", table_name="notifications")
    op.drop_index("ix_notifications_claimable", table_name="notifications")
    op.drop_index("ix_notifications_appointment_id", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_appointment_external_events_pending", table_name="appointment_external_events")
    op.drop_table("appointment_external_events")

    op.drop_index("ix_external_busy_blocks_doctor_id_starts_at", table_name="external_busy_blocks")
    op.drop_table("external_busy_blocks")

    op.drop_table("calendar_connections")
