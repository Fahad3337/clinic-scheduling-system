"""initial schema: patients, doctors, time_slots, appointments

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-19

Hand-written to match app/models/*.py exactly (no live DB was available in
this environment to run `alembic revision --autogenerate`). Before trusting
this in a real environment, run:

    alembic upgrade head
    alembic check          # or: autogenerate against a live DB and diff

to confirm it produces the same schema the ORM models describe. That
autogenerate-diff step is the real safety net for any migration going
forward -- treat this file as a draft until it's been run once.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # patients
    # ------------------------------------------------------------------ #
    op.create_table(
        "patients",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_patients"),
        sa.CheckConstraint("phone ~ '^\\+[1-9][0-9]{7,14}$'", name="phone_is_e164"),
    )
    op.create_index("uq_patients_phone", "patients", ["phone"], unique=True)

    # ------------------------------------------------------------------ #
    # doctors
    # ------------------------------------------------------------------ #
    op.create_table(
        "doctors",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("specialty", sa.String(length=120), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("slot_duration_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_doctors"),
    )

    # ------------------------------------------------------------------ #
    # time_slots
    # ------------------------------------------------------------------ #
    op.create_table(
        "time_slots",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("doctor_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_time_slots"),
        sa.ForeignKeyConstraint(
            ["doctor_id"], ["doctors.id"], name="fk_time_slots_doctor_id_doctors", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("doctor_id", "starts_at", name="uq_time_slots_doctor_id_starts_at"),
        sa.CheckConstraint("ends_at > starts_at", name="ends_after_start"),
    )
    op.create_index(
        "ix_time_slots_doctor_id_starts_at", "time_slots", ["doctor_id", "starts_at"]
    )

    # ------------------------------------------------------------------ #
    # appointments
    # ------------------------------------------------------------------ #
    op.create_table(
        "appointments",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("patient_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("doctor_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("time_slot_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
        ),
        sa.Column(
            "booking_channel",
            sa.String(length=20),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_appointments"),
        sa.ForeignKeyConstraint(
            ["patient_id"], ["patients.id"], name="fk_appointments_patient_id_patients", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["doctor_id"], ["doctors.id"], name="fk_appointments_doctor_id_doctors", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["time_slot_id"],
            ["time_slots.id"],
            name="fk_appointments_time_slot_id_time_slots",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('booked','cancelled','completed','no_show')",
            name="ck_appointments_status_valid",
        ),
        sa.CheckConstraint(
            "booking_channel IN ('web','chat','voice','staff')",
            name="ck_appointments_booking_channel_valid",
        ),
    )

    # THE anti-double-booking constraint -- see models/appointment.py for the
    # full rationale. This partial unique index is the one line in this whole
    # migration that the correctness of the booking flow depends on.
    op.create_index(
        "uq_appointments_active_slot",
        "appointments",
        ["time_slot_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.create_index(
        "ix_appointments_patient_id_status", "appointments", ["patient_id", "status"]
    )
    op.create_index(
        "ix_appointments_doctor_id_status", "appointments", ["doctor_id", "status"]
    )


def downgrade() -> None:
    # Reverse order: drop dependents before the tables they reference.
    op.drop_index("ix_appointments_doctor_id_status", table_name="appointments")
    op.drop_index("ix_appointments_patient_id_status", table_name="appointments")
    op.drop_index("uq_appointments_active_slot", table_name="appointments")
    op.drop_table("appointments")

    op.drop_index("ix_time_slots_doctor_id_starts_at", table_name="time_slots")
    op.drop_table("time_slots")

    op.drop_table("doctors")

    op.drop_index("uq_patients_phone", table_name="patients")
    op.drop_table("patients")
