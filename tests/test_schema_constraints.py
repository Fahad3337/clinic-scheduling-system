"""Constraint-level tests: prove the DATABASE enforces our invariants.

WHY THIS FILE EXISTS
--------------------
Every other test in this suite goes through the service layer, so they pass
as long as the Python code is correct. These tests deliberately bypass the
ORM's validation and fire raw SQL at Postgres, because the whole argument of
this codebase is that the important invariants live in the SCHEMA, not in
application control flow. A test that only exercises the service layer
cannot tell the difference between "the constraint exists" and "the Python
happened not to try it".

That distinction was not academic: Phase 1 shipped with CHECK constraints in
production that the test database did not have (see migration 0003). Nothing
caught it, because `alembic check` does not compare CHECK constraints at all.
The `test_enum_check_constraints_exist` test below is the guard that would
have caught it on day one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.appointment import Appointment
from app.models.calendar_connection import CalendarConnection
from app.models.enums import AppointmentStatus, BookingChannel, CalendarProvider
from app.models.external_busy_block import ExternalBusyBlock
from app.models.schedule_conflict import ScheduleConflict

# Constraints that MUST exist in whatever database the tests run against.
# If create_all and the migrations ever diverge again, this list is what
# fails first. Keep it in sync with what migration files actually create.
#
# NOT A COMPLETE DIFF: this catches constraints that go MISSING, not ones
# that get ADDED. A new constraint nobody lists here passes silently.
# A full pg_constraint-vs-create_all diff in CI is the stricter check.
REQUIRED_CHECK_CONSTRAINTS = {
    "appointments": {
        "ck_appointments_appointment_status",
        "ck_appointments_booking_channel",
    },
    "patients": {"ck_patients_phone_is_e164"},
    "time_slots": {"ck_time_slots_ends_after_start"},
    "notifications": {
        "ck_notifications_notification_kind",
        "ck_notifications_notification_channel",
        "ck_notifications_notification_status",
        "ck_notifications_sent_requires_sent_at",
        "ck_notifications_attempts_non_negative",
    },
    "calendar_connections": {
        "ck_calendar_connections_calendar_provider",
        "ck_calendar_connections_calendar_connection_state",
        "ck_calendar_connections_failures_non_negative",
    },
    "external_busy_blocks": {"ck_external_busy_blocks_ends_after_start"},
    "appointment_external_events": {
        "ck_appointment_external_events_calendar_push_state",
        "ck_appointment_external_events_synced_requires_external_id",
        "ck_appointment_external_events_attempts_non_negative",
    },
    "schedule_conflicts": {"ck_schedule_conflicts_conflict_resolution"},
}


@pytest.mark.asyncio
async def test_enum_check_constraints_exist(db):
    """The regression guard for the Phase 1 / migration 0003 drift.

    Asserts by NAME, not just by behaviour, so a constraint that gets
    silently renamed (and therefore diverges from the migrations) fails here
    rather than becoming a mystery years later.
    """
    rows = (
        await db.execute(
            text(
                "SELECT conrelid::regclass::text AS tbl, conname "
                "FROM pg_constraint "
                "WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
            )
        )
    ).all()
    actual: dict[str, set[str]] = {}
    for tbl, name in rows:
        actual.setdefault(tbl, set()).add(name)

    missing: list[str] = []
    for table, required in REQUIRED_CHECK_CONSTRAINTS.items():
        gap = required - actual.get(table, set())
        missing.extend(f"{table}.{c}" for c in sorted(gap))

    assert not missing, (
        "CHECK constraints missing from the test database:\n  "
        + "\n  ".join(missing)
        + "\n\nThis usually means a model declares an Enum without "
        "create_constraint=True, so create_all (tests) and the migration "
        "(production) no longer agree."
    )


@pytest.mark.asyncio
async def test_invalid_appointment_status_rejected_by_database(db, patient, time_slot, doctor):
    """Raw SQL with a bogus status must be refused by the DB, not just by Python.

    Before migration 0003 this test FAILED against the test database while
    passing against production -- exactly the asymmetry 0003 removes.
    """
    with pytest.raises(IntegrityError) as exc:
        await db.execute(
            text(
                "INSERT INTO appointments "
                "(id, patient_id, doctor_id, time_slot_id, status, booking_channel, created_at, updated_at) "
                # Must fit VARCHAR(20): a longer literal trips a length
                # error before the CHECK is ever evaluated, which would make
                # this test pass without proving the constraint exists.
                "VALUES (:i, :p, :d, :t, 'bogus_status', 'web', now(), now())"
            ),
            {"i": uuid.uuid4(), "p": patient.id, "d": doctor.id, "t": time_slot.id},
        )
    assert "ck_appointments_appointment_status" in str(exc.value)
    await db.rollback()


@pytest.mark.asyncio
async def test_invalid_booking_channel_rejected_by_database(db, patient, time_slot, doctor):
    with pytest.raises(IntegrityError) as exc:
        await db.execute(
            text(
                "INSERT INTO appointments "
                "(id, patient_id, doctor_id, time_slot_id, status, booking_channel, created_at, updated_at) "
                "VALUES (:i, :p, :d, :t, 'booked', 'carrier_pigeon', now(), now())"
            ),
            {"i": uuid.uuid4(), "p": patient.id, "d": doctor.id, "t": time_slot.id},
        )
    assert "ck_appointments_booking_channel" in str(exc.value)
    await db.rollback()


@pytest.mark.asyncio
async def test_time_slot_end_must_follow_start(db, doctor):
    start = datetime.now(UTC) + timedelta(days=1)
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO time_slots (id, doctor_id, starts_at, ends_at, is_blocked, created_at, updated_at) "
                "VALUES (:i, :d, :s, :e, false, now(), now())"
            ),
            {"i": uuid.uuid4(), "d": doctor.id, "s": start, "e": start - timedelta(minutes=30)},
        )
    await db.rollback()


@pytest.mark.asyncio
async def test_non_e164_phone_rejected(db):
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO patients (id, full_name, phone, created_at, updated_at) "
                "VALUES (:i, 'Bad Number', '07700 900123', now(), now())"
            ),
            {"i": uuid.uuid4()},
        )
    await db.rollback()


# ---------------------------------------------------------------------- #
# Phase 2 constraints
# ---------------------------------------------------------------------- #


@pytest.fixture
async def busy_block(db, doctor):
    """A calendar connection plus one mirrored busy block."""
    conn = CalendarConnection(
        doctor_id=doctor.id,
        provider=CalendarProvider.GOOGLE,
        account_email="rao@example.com",
        refresh_token="not-a-real-token",
    )
    db.add(conn)
    await db.commit()

    start = datetime.now(UTC) + timedelta(days=1)
    block = ExternalBusyBlock(
        connection_id=conn.id,
        doctor_id=doctor.id,
        external_event_id="gcal-evt-1",
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        synced_at=datetime.now(UTC),
    )
    db.add(block)
    await db.commit()
    await db.refresh(block)
    return block


@pytest.mark.asyncio
async def test_conflicted_busy_block_cannot_be_hard_deleted(db, patient, time_slot, doctor, busy_block):
    """RESTRICT must protect the conflict audit trail.

    This is the constraint that forced external_busy_blocks to be
    soft-deleted: if this DELETE were allowed to raise inside the sync job,
    that doctor's sync would wedge and retry the same failure forever.
    """
    appt = Appointment(
        patient_id=patient.id, doctor_id=doctor.id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()

    db.add(ScheduleConflict(
        appointment_id=appt.id, busy_block_id=busy_block.id, detected_at=datetime.now(UTC)
    ))
    await db.commit()

    with pytest.raises(IntegrityError) as exc:
        await db.execute(
            text("DELETE FROM external_busy_blocks WHERE id = :i"), {"i": busy_block.id}
        )
    assert "fk_schedule_conflicts_busy_block_id_external_busy_blocks" in str(exc.value)
    await db.rollback()


@pytest.mark.asyncio
async def test_soft_delete_is_the_supported_path(db, patient, time_slot, doctor, busy_block):
    """Soft-deleting succeeds and leaves the conflict row intact."""
    appt = Appointment(
        patient_id=patient.id, doctor_id=doctor.id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()
    db.add(ScheduleConflict(
        appointment_id=appt.id, busy_block_id=busy_block.id, detected_at=datetime.now(UTC)
    ))
    await db.commit()

    busy_block.deleted_at = datetime.now(UTC)
    db.add(busy_block)
    await db.commit()

    surviving = (
        await db.execute(
            text("SELECT count(*) FROM schedule_conflicts WHERE busy_block_id = :i"),
            {"i": busy_block.id},
        )
    ).scalar_one()
    assert surviving == 1


@pytest.mark.asyncio
async def test_notification_sent_status_requires_sent_at(db, patient, time_slot, doctor):
    appt = Appointment(
        patient_id=patient.id, doctor_id=doctor.id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()

    with pytest.raises(IntegrityError) as exc:
        await db.execute(
            text(
                "INSERT INTO notifications "
                "(id, appointment_id, kind, channel, status, dedupe_key, recipient, scheduled_for, created_at, updated_at) "
                "VALUES (:i, :a, 'reminder_24h', 'sms', 'sent', 'k-no-sent-at', '+14155550123', now(), now(), now())"
            ),
            {"i": uuid.uuid4(), "a": appt.id},
        )
    assert "ck_notifications_sent_requires_sent_at" in str(exc.value)
    await db.rollback()
