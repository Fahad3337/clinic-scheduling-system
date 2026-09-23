"""Calendar sync tests: event interpretation, conflicts, and the feedback loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.integrations.google_calendar import (
    CLINIC_APPOINTMENT_PROPERTY,
    EventPage,
    GoogleAccessTokenExpiredError,
    SyncTokenExpiredError,
)
from app.models.appointment import Appointment
from app.models.calendar_connection import CalendarConnection
from app.models.enums import (
    AppointmentStatus,
    BookingChannel,
    CalendarProvider,
    ConflictResolution,
)
from app.models.external_busy_block import ExternalBusyBlock
from app.models.schedule_conflict import ScheduleConflict
from app.models.time_slot import TimeSlot
from app.services import calendar_sync_service
from app.services.calendar_sync_service import _parse_event_window, _blocks_time, _is_our_own_event

NY = ZoneInfo("America/New_York")


# ===================================================================== #
# Pure event interpretation -- no DB, no network
# ===================================================================== #


def test_timed_event_parsed_to_utc():
    ev = {"start": {"dateTime": "2026-09-21T10:00:00-04:00"},
          "end": {"dateTime": "2026-09-21T11:00:00-04:00"}}
    start, end, all_day = _parse_event_window(ev, NY)
    assert start == datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    assert all_day is False


def test_all_day_event_uses_doctor_timezone_and_exclusive_end():
    """The two classic all-day bugs, pinned.

    A single-day event on the 21st arrives as start=2026-09-21,
    end=2026-09-22 (END IS EXCLUSIVE). In New York in September (EDT,
    UTC-4) that is 04:00Z on the 21st to 04:00Z on the 22nd -- exactly 24
    hours, not 48, and offset by 4 hours rather than starting at midnight
    UTC.
    """
    ev = {"start": {"date": "2026-09-21"}, "end": {"date": "2026-09-22"}}
    start, end, all_day = _parse_event_window(ev, NY)
    assert all_day is True
    assert start == datetime(2026, 9, 21, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 22, 4, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=24)  # NOT 48


def test_multi_day_all_day_event():
    ev = {"start": {"date": "2026-09-21"}, "end": {"date": "2026-09-24"}}
    start, end, _ = _parse_event_window(ev, NY)
    assert end - start == timedelta(days=3)


def test_all_day_event_across_dst_transition():
    """US DST ends 2026-11-01. A 3-day block spanning it is 73 hours, not 72.

    Using a fixed UTC offset instead of ZoneInfo would silently produce 72
    and leave an hour of the doctor's holiday bookable.
    """
    ev = {"start": {"date": "2026-10-31"}, "end": {"date": "2026-11-03"}}
    start, end, _ = _parse_event_window(ev, NY)
    assert end - start == timedelta(hours=73)


def test_event_without_usable_times_returns_none():
    assert _parse_event_window({"start": {}, "end": {}}, NY) is None


def test_transparent_event_does_not_block():
    assert _blocks_time({"transparency": "transparent"}) is False
    assert _blocks_time({"transparency": "opaque"}) is True
    assert _blocks_time({}) is True


def test_working_location_event_does_not_block():
    """These span the whole workday; treating them as busy blocks everything."""
    assert _blocks_time({"eventType": "workingLocation"}) is False
    assert _blocks_time({"eventType": "default"}) is True


def test_declined_invitation_still_blocks():
    """Documented, deliberate: erring toward blocking beats double-booking."""
    ev = {"attendees": [{"self": True, "responseStatus": "declined"}]}
    assert _blocks_time(ev) is True


def test_our_own_events_are_recognised():
    ours = {"extendedProperties": {"private": {CLINIC_APPOINTMENT_PROPERTY: "abc"}}}
    theirs = {"extendedProperties": {"private": {"something_else": "x"}}}
    assert _is_our_own_event(ours) is True
    assert _is_our_own_event(theirs) is False
    assert _is_our_own_event({}) is False


# ===================================================================== #
# Fakes
# ===================================================================== #


class FakeCalendarClient:
    """Returns scripted pages; records calls."""

    def __init__(self, pages=None, raise_on_first=None):
        self._pages = list(pages or [])
        self._raise_on_first = raise_on_first
        self.list_calls: list[dict] = []
        self.inserted: list[dict] = []
        self.deleted: list[str] = []

    async def list_events(self, **kwargs):
        self.list_calls.append(kwargs)
        if self._raise_on_first is not None and len(self.list_calls) == 1:
            exc, self._raise_on_first = self._raise_on_first, None
            raise exc
        if not self._pages:
            return EventPage(events=[], next_page_token=None, next_sync_token="sync-token-1")
        return self._pages.pop(0)

    async def insert_event(self, *, access_token, calendar_id, event):
        self.inserted.append(event)
        return {"id": f"gcal-{len(self.inserted)}", "etag": "etag-1"}

    async def patch_event(self, *, access_token, calendar_id, event_id, event):
        return {"id": event_id, "etag": "etag-2"}

    async def delete_event(self, *, access_token, calendar_id, event_id):
        self.deleted.append(event_id)


@pytest.fixture
async def connection(db, doctor):
    conn = CalendarConnection(
        doctor_id=doctor.id,
        provider=CalendarProvider.GOOGLE,
        account_email="rao@example.com",
        refresh_token="refresh-token",
        access_token="access-token",
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        granted_scopes="https://www.googleapis.com/auth/calendar",
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


def timed_event(event_id, start, end, **extra):
    ev = {
        "id": event_id,
        "etag": f"etag-{event_id}",
        "status": "confirmed",
        "start": {"dateTime": start.astimezone(UTC).isoformat()},
        "end": {"dateTime": end.astimezone(UTC).isoformat()},
    }
    ev.update(extra)
    return ev


# ===================================================================== #
# Pull
# ===================================================================== #


@pytest.mark.asyncio
async def test_sync_mirrors_busy_blocks(db, connection, doctor):
    start = datetime.now(UTC) + timedelta(days=1)
    client = FakeCalendarClient(pages=[EventPage(
        events=[timed_event("evt-1", start, start + timedelta(hours=1))],
        next_page_token=None, next_sync_token="tok-1",
    )])

    result = await calendar_sync_service.sync_connection(db, connection_id=connection.id, client=client)

    assert result.blocks_upserted == 1
    block = await db.scalar(select(ExternalBusyBlock))
    assert block.external_event_id == "evt-1"
    assert block.deleted_at is None
    assert block.doctor_id == doctor.id
    await db.refresh(connection)
    assert connection.sync_token == "tok-1"
    assert connection.last_synced_at is not None


@pytest.mark.asyncio
async def test_sync_is_idempotent(db, connection):
    """Re-running the same page must not duplicate capacity blocks."""
    start = datetime.now(UTC) + timedelta(days=1)
    ev = timed_event("evt-1", start, start + timedelta(hours=1))

    for _ in range(3):
        client = FakeCalendarClient(pages=[EventPage([ev], None, "tok")])
        await calendar_sync_service.sync_connection(db, connection_id=connection.id, client=client)

    assert len((await db.scalars(select(ExternalBusyBlock))).all()) == 1


@pytest.mark.asyncio
async def test_cancelled_event_soft_deletes_block(db, connection):
    start = datetime.now(UTC) + timedelta(days=1)
    ev = timed_event("evt-1", start, start + timedelta(hours=1))
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([ev], None, "t1")])
    )

    cancelled = {"id": "evt-1", "status": "cancelled"}
    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id,
        client=FakeCalendarClient(pages=[EventPage([cancelled], None, "t2")]),
    )

    assert result.blocks_removed == 1
    block = await db.scalar(select(ExternalBusyBlock))
    assert block.deleted_at is not None  # soft, not hard


@pytest.mark.asyncio
async def test_restored_event_is_revived(db, connection):
    """A Google event restored from trash keeps its ID; the upsert un-deletes."""
    start = datetime.now(UTC) + timedelta(days=1)
    ev = timed_event("evt-1", start, start + timedelta(hours=1))
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([ev], None, "t1")])
    )
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id,
        client=FakeCalendarClient(pages=[EventPage([{"id": "evt-1", "status": "cancelled"}], None, "t2")]),
    )
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([ev], None, "t3")])
    )

    blocks = (await db.scalars(select(ExternalBusyBlock))).all()
    assert len(blocks) == 1
    assert blocks[0].deleted_at is None


@pytest.mark.asyncio
async def test_expired_sync_token_triggers_full_resync(db, connection):
    """410 GONE is a documented signal, not a failure."""
    connection.sync_token = "stale-token"
    connection.last_full_sync_at = datetime.now(UTC)
    db.add(connection)
    await db.commit()

    start = datetime.now(UTC) + timedelta(days=1)
    client = FakeCalendarClient(
        pages=[EventPage([timed_event("evt-1", start, start + timedelta(hours=1))], None, "fresh")],
        raise_on_first=SyncTokenExpiredError("410: gone"),
    )
    result = await calendar_sync_service.sync_connection(db, connection_id=connection.id, client=client)

    assert result.full_resync is True
    assert result.blocks_upserted == 1
    # First attempt used the stale token; the retry used none.
    assert client.list_calls[0]["sync_token"] == "stale-token"
    assert client.list_calls[1]["sync_token"] is None


@pytest.mark.asyncio
async def test_401_refreshes_token_once_and_retries(db, connection):
    start = datetime.now(UTC) + timedelta(days=1)

    class RefreshingClient(FakeCalendarClient):
        async def refresh_access_token(self, **kwargs):
            from app.integrations.google_calendar import RefreshedToken
            return RefreshedToken(
                access_token="new-access", expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes=("https://www.googleapis.com/auth/calendar",), refresh_token=None,
            )

    client = RefreshingClient(
        pages=[EventPage([timed_event("evt-1", start, start + timedelta(hours=1))], None, "t")],
        raise_on_first=GoogleAccessTokenExpiredError("401"),
    )
    result = await calendar_sync_service.sync_connection(db, connection_id=connection.id, client=client)

    assert result.blocks_upserted == 1
    await db.refresh(connection)
    assert connection.access_token == "new-access"


@pytest.mark.asyncio
async def test_full_resync_removes_blocks_google_no_longer_sends(db, connection):
    start = datetime.now(UTC) + timedelta(days=1)
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id,
        client=FakeCalendarClient(pages=[EventPage([
            timed_event("evt-1", start, start + timedelta(hours=1)),
            timed_event("evt-2", start + timedelta(hours=2), start + timedelta(hours=3)),
        ], None, "t1")]),
    )
    # Force a full resync that only returns evt-1.
    connection.sync_token = None
    connection.last_full_sync_at = None
    db.add(connection)
    await db.commit()

    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id,
        client=FakeCalendarClient(pages=[EventPage(
            [timed_event("evt-1", start, start + timedelta(hours=1))], None, "t2")]),
    )

    blocks = {b.external_event_id: b for b in (await db.scalars(select(ExternalBusyBlock))).all()}
    assert blocks["evt-1"].deleted_at is None
    assert blocks["evt-2"].deleted_at is not None


@pytest.mark.asyncio
async def test_our_own_pushed_events_are_ignored(db, connection, patient, time_slot):
    """THE FEEDBACK LOOP GUARD.

    Without it, an appointment we pushed comes back as a busy block,
    overlaps its own slot, and conflicts with itself.
    """
    appt = Appointment(
        patient_id=patient.id, doctor_id=connection.doctor_id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()

    ours = timed_event(
        "our-evt", time_slot.starts_at, time_slot.ends_at,
        extendedProperties={"private": {CLINIC_APPOINTMENT_PROPERTY: str(appt.id)}},
    )
    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([ours], None, "t")])
    )

    assert result.skipped_own_events == 1
    assert result.blocks_upserted == 0
    assert (await db.scalars(select(ExternalBusyBlock))).all() == []
    assert (await db.scalars(select(ScheduleConflict))).all() == []


@pytest.mark.asyncio
async def test_sync_works_with_a_cold_identity_map(db, connection):
    """Regression: no lazy relationship access anywhere in the sync path.

    `expunge_all()` empties the session's identity map, so any attribute
    that is actually a lazy relationship must hit the database -- which in
    async SQLAlchemy raises MissingGreenlet rather than quietly loading.

    This is how the background worker will always run: a fresh session with
    nothing preloaded. Without this test the suite passed only because the
    fixtures happened to leave the Doctor in memory.
    """
    start = datetime.now(UTC) + timedelta(days=1)
    connection_id = connection.id
    db.expunge_all()

    result = await calendar_sync_service.sync_connection(
        db,
        connection_id=connection_id,
        client=FakeCalendarClient(pages=[EventPage(
            [timed_event("evt-cold", start, start + timedelta(hours=1))], None, "tok")]),
    )
    assert result.blocks_upserted == 1


@pytest.mark.asyncio
async def test_inactive_connection_is_skipped_not_failed(db, connection):
    from app.models.enums import CalendarConnectionState
    connection.state = CalendarConnectionState.DISABLED
    connection.refresh_token = None
    db.add(connection)
    await db.commit()

    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient()
    )
    assert result.events_seen == 0
    assert any("disabled" in e for e in result.errors)


# ===================================================================== #
# Conflicts
# ===================================================================== #


@pytest.mark.asyncio
async def test_conflict_recorded_but_appointment_untouched(db, connection, patient, time_slot):
    """The sync job may block future capacity; it may NEVER cancel a booking."""
    appt = Appointment(
        patient_id=patient.id, doctor_id=connection.doctor_id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()

    clash = timed_event("personal-1", time_slot.starts_at, time_slot.ends_at)
    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([clash], None, "t")])
    )

    assert result.conflicts_created == 1
    conflict = await db.scalar(select(ScheduleConflict))
    assert conflict.resolution is ConflictResolution.UNRESOLVED
    await db.refresh(appt)
    assert appt.status is AppointmentStatus.BOOKED  # untouched
    assert appt.cancelled_at is None


@pytest.mark.asyncio
async def test_conflict_not_duplicated_across_syncs(db, connection, patient, time_slot):
    appt = Appointment(
        patient_id=patient.id, doctor_id=connection.doctor_id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()
    clash = timed_event("personal-1", time_slot.starts_at, time_slot.ends_at)

    for _ in range(3):
        await calendar_sync_service.sync_connection(
            db, connection_id=connection.id,
            client=FakeCalendarClient(pages=[EventPage([clash], None, "t")]),
        )

    assert len((await db.scalars(select(ScheduleConflict))).all()) == 1


@pytest.mark.asyncio
async def test_conflict_auto_resolves_when_event_deleted(db, connection, patient, time_slot):
    appt = Appointment(
        patient_id=patient.id, doctor_id=connection.doctor_id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()
    clash = timed_event("personal-1", time_slot.starts_at, time_slot.ends_at)
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([clash], None, "t1")])
    )

    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id,
        client=FakeCalendarClient(pages=[EventPage([{"id": "personal-1", "status": "cancelled"}], None, "t2")]),
    )

    assert result.conflicts_resolved == 1
    conflict = await db.scalar(select(ScheduleConflict))
    assert conflict.resolution is ConflictResolution.EXTERNAL_EVENT_REMOVED
    assert conflict.resolved_at is not None
    # The audit row survives -- that was the whole point of RESTRICT.
    assert conflict is not None


@pytest.mark.asyncio
async def test_adjacent_event_does_not_conflict(db, connection, patient, time_slot):
    """Half-open intervals: an event ending exactly when a slot starts is fine."""
    appt = Appointment(
        patient_id=patient.id, doctor_id=connection.doctor_id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()

    adjacent = timed_event("adj", time_slot.starts_at - timedelta(hours=1), time_slot.starts_at)
    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([adjacent], None, "t")])
    )
    assert result.conflicts_created == 0


@pytest.mark.asyncio
async def test_cancelled_appointment_does_not_conflict(db, connection, patient, time_slot):
    appt = Appointment(
        patient_id=patient.id, doctor_id=connection.doctor_id, time_slot_id=time_slot.id,
        status=AppointmentStatus.CANCELLED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()

    clash = timed_event("personal-1", time_slot.starts_at, time_slot.ends_at)
    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([clash], None, "t")])
    )
    assert result.conflicts_created == 0


# ===================================================================== #
# Availability integration
# ===================================================================== #


@pytest.mark.asyncio
async def test_busy_block_removes_slot_from_availability(db, connection, doctor, time_slot):
    from app.services import availability_service

    on_date = time_slot.starts_at.astimezone(ZoneInfo(doctor.timezone)).date()
    _, before = await availability_service.get_availability(db, doctor_id=doctor.id, on_date=on_date)
    assert any(s.id == time_slot.id for s in before)

    clash = timed_event("personal-1", time_slot.starts_at, time_slot.ends_at)
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([clash], None, "t")])
    )

    _, after = await availability_service.get_availability(db, doctor_id=doctor.id, on_date=on_date)
    assert all(s.id != time_slot.id for s in after)


@pytest.mark.asyncio
async def test_soft_deleted_block_stops_blocking(db, connection, doctor, time_slot):
    """THE load-bearing deleted_at filter. If this regresses, doctors lose
    availability permanently for every event they ever had."""
    from app.services import availability_service

    on_date = time_slot.starts_at.astimezone(ZoneInfo(doctor.timezone)).date()
    clash = timed_event("personal-1", time_slot.starts_at, time_slot.ends_at)
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[EventPage([clash], None, "t1")])
    )
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id,
        client=FakeCalendarClient(pages=[EventPage([{"id": "personal-1", "status": "cancelled"}], None, "t2")]),
    )

    _, after = await availability_service.get_availability(db, doctor_id=doctor.id, on_date=on_date)
    assert any(s.id == time_slot.id for s in after)  # bookable again


# ===================================================================== #
# Push outbox
# ===================================================================== #


@pytest.mark.asyncio
async def test_booking_enqueues_calendar_push(db, connection, patient, time_slot):
    from app.models.appointment_external_event import AppointmentExternalEvent
    from app.models.enums import CalendarPushState
    from app.services import appointment_service

    appt = await appointment_service.book_appointment(
        db, patient_id=patient.id, time_slot_id=time_slot.id
    )
    row = await db.scalar(select(AppointmentExternalEvent))
    assert row is not None
    assert row.appointment_id == appt.id
    assert row.push_state is CalendarPushState.PENDING
    assert row.external_event_id is None  # nothing pushed yet


@pytest.mark.asyncio
async def test_push_creates_event_and_marks_synced(db, connection, patient, time_slot):
    from app.models.appointment_external_event import AppointmentExternalEvent
    from app.models.enums import CalendarPushState
    from app.services import appointment_service

    await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    client = FakeCalendarClient()
    counts = await calendar_sync_service.push_pending_events(db, client=client)

    assert counts["synced"] == 1
    assert len(client.inserted) == 1
    # The marker that prevents the feedback loop MUST be on the payload.
    assert CLINIC_APPOINTMENT_PROPERTY in client.inserted[0]["extendedProperties"]["private"]

    row = await db.scalar(select(AppointmentExternalEvent))
    assert row.push_state is CalendarPushState.SYNCED
    assert row.external_event_id == "gcal-1"


@pytest.mark.asyncio
async def test_cancelling_queues_event_deletion(db, connection, patient, time_slot):
    from app.models.appointment_external_event import AppointmentExternalEvent
    from app.models.enums import CalendarPushState
    from app.services import appointment_service

    appt = await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    await calendar_sync_service.push_pending_events(db, client=FakeCalendarClient())
    await appointment_service.cancel_appointment(db, appointment_id=appt.id)

    row = await db.scalar(select(AppointmentExternalEvent))
    assert row.push_state is CalendarPushState.DELETE_PENDING

    client = FakeCalendarClient()
    counts = await calendar_sync_service.push_pending_events(db, client=client)
    assert counts["deleted"] == 1
    assert client.deleted == ["gcal-1"]
    await db.refresh(row)
    assert row.push_state is CalendarPushState.DELETED


@pytest.mark.asyncio
async def test_cancelling_never_pushed_appointment_skips_google(db, connection, patient, time_slot):
    """Nothing was created remotely, so there is nothing to delete."""
    from app.models.appointment_external_event import AppointmentExternalEvent
    from app.models.enums import CalendarPushState
    from app.services import appointment_service

    appt = await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    await appointment_service.cancel_appointment(db, appointment_id=appt.id)

    row = await db.scalar(select(AppointmentExternalEvent))
    assert row.push_state is CalendarPushState.DELETED

    client = FakeCalendarClient()
    await calendar_sync_service.push_pending_events(db, client=client)
    assert client.deleted == []


@pytest.mark.asyncio
async def test_booking_without_calendar_connection_works(db, patient, time_slot):
    """Booking must never depend on an optional integration being set up."""
    from app.models.appointment_external_event import AppointmentExternalEvent
    from app.services import appointment_service

    appt = await appointment_service.book_appointment(db, patient_id=patient.id, time_slot_id=time_slot.id)
    assert appt.status is AppointmentStatus.BOOKED
    assert (await db.scalars(select(AppointmentExternalEvent))).all() == []
