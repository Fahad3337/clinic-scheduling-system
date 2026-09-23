"""Sync behaviour against recorded Google Calendar API payloads.

These use realistic full event bodies (tests/fixtures/google_events.py)
rather than the minimal dicts a test author would invent, so they exercise
field combinations we did not design for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.integrations.google_calendar import EventPage
from app.models.external_busy_block import ExternalBusyBlock
from app.services import calendar_sync_service
from app.services.calendar_sync_service import _blocks_time, _parse_event_window
from tests.fixtures.google_events import (
    ALL_DAY_EVENT,
    CANCELLED_EVENT,
    CANCELLED_RECURRING_INSTANCE,
    OUT_OF_OFFICE_EVENT,
    RECURRING_INSTANCE,
    SECOND_RECURRING_INSTANCE,
    TIMED_EVENT,
    TRANSPARENT_ALL_DAY_EVENT,
    WORKING_LOCATION_EVENT,
)
from tests.test_calendar_sync import FakeCalendarClient, connection  # noqa: F401

NY = ZoneInfo("America/New_York")


# ===================================================================== #
# Interpretation of the recorded payloads
# ===================================================================== #


def test_recorded_timed_event():
    start, end, all_day = _parse_event_window(TIMED_EVENT, NY)
    assert start == datetime(2026, 9, 21, 14, 0, tzinfo=UTC)  # 10:00 EDT
    assert end == datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    assert all_day is False
    assert _blocks_time(TIMED_EVENT) is True


def test_recorded_recurring_instance_has_its_own_id():
    """singleEvents=true gives each occurrence a distinct id.

    That is what lets UNIQUE(connection_id, external_event_id) store one
    row per occurrence. Keying on recurringEventId instead would collapse a
    weekly meeting into a single row and block only one week.
    """
    assert RECURRING_INSTANCE["id"] != SECOND_RECURRING_INSTANCE["id"]
    assert RECURRING_INSTANCE["recurringEventId"] == SECOND_RECURRING_INSTANCE["recurringEventId"]

    start, end, _ = _parse_event_window(RECURRING_INSTANCE, NY)
    assert start == datetime(2026, 9, 22, 14, 0, tzinfo=UTC)
    assert end - start == timedelta(minutes=30)


def test_recorded_all_day_event_exclusive_end():
    """23rd -> 26th is THREE days, anchored to the doctor's midnight."""
    start, end, all_day = _parse_event_window(ALL_DAY_EVENT, NY)
    assert all_day is True
    assert start == datetime(2026, 9, 23, 4, 0, tzinfo=UTC)  # midnight EDT
    assert end == datetime(2026, 9, 26, 4, 0, tzinfo=UTC)
    assert end - start == timedelta(days=3)


def test_recorded_cancelled_event_has_no_start_or_end():
    """Deletions arrive almost empty; code must check status FIRST.

    Reaching for event["start"] before checking status crashes on every
    deletion during incremental sync.
    """
    assert "start" not in CANCELLED_EVENT
    assert "end" not in CANCELLED_EVENT
    assert CANCELLED_EVENT["status"] == "cancelled"
    assert _parse_event_window(CANCELLED_EVENT, NY) is None


def test_recorded_working_location_does_not_block():
    assert _blocks_time(WORKING_LOCATION_EVENT) is False


def test_recorded_out_of_office_blocks_despite_being_transparent():
    """The case that transparency alone gets wrong."""
    assert OUT_OF_OFFICE_EVENT["transparency"] == "transparent"
    assert _blocks_time(OUT_OF_OFFICE_EVENT) is True


def test_multi_day_all_day_event_blocks_even_when_transparent():
    """The decided policy: leave blocks even if Google marked it Free.

    TRANSPARENT_ALL_DAY_EVENT spans 23rd -> 26th (three days) and carries
    transparency="transparent". Under the old rule it would not have
    blocked, leaving a doctor's week of leave fully bookable.
    """
    assert TRANSPARENT_ALL_DAY_EVENT["transparency"] == "transparent"
    assert _blocks_time(TRANSPARENT_ALL_DAY_EVENT) is True


def test_single_day_transparent_all_day_event_does_not_block():
    """The other side of the heuristic: a birthday must not erase a clinic day."""
    birthday = {
        **TRANSPARENT_ALL_DAY_EVENT,
        "id": "single-day-birthday",
        "summary": "Dad's birthday",
        "start": {"date": "2026-09-23"},
        "end": {"date": "2026-09-24"},  # exclusive -> exactly one day
    }
    assert _blocks_time(birthday) is False


def test_timed_transparent_event_still_does_not_block():
    """Only ALL-DAY events get the override; timed 'Free' events are honoured."""
    assert _blocks_time({**TIMED_EVENT, "transparency": "transparent"}) is False


# ===================================================================== #
# End-to-end through the sync
# ===================================================================== #


@pytest.mark.asyncio
async def test_sync_handles_a_realistic_mixed_page(db, connection):  # noqa: F811
    """One page containing every event shape Google actually sends."""
    page = EventPage(
        events=[
            TIMED_EVENT,
            RECURRING_INSTANCE,
            SECOND_RECURRING_INSTANCE,
            ALL_DAY_EVENT,
            WORKING_LOCATION_EVENT,
            OUT_OF_OFFICE_EVENT,
            TRANSPARENT_ALL_DAY_EVENT,
        ],
        next_page_token=None,
        next_sync_token="tok-mixed",
    )
    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id, client=FakeCalendarClient(pages=[page])
    )

    assert result.events_seen == 7
    # Blocking: timed, 2 recurring instances, all-day, out-of-office, AND
    # the transparent multi-day all-day event (policy override) = 6.
    # Non-blocking: workingLocation only.
    assert result.blocks_upserted == 6

    stored = {b.external_event_id for b in (await db.scalars(
        select(ExternalBusyBlock).where(ExternalBusyBlock.deleted_at.is_(None))
    )).all()}
    assert TIMED_EVENT["id"] in stored
    assert RECURRING_INSTANCE["id"] in stored
    assert SECOND_RECURRING_INSTANCE["id"] in stored
    assert ALL_DAY_EVENT["id"] in stored
    assert OUT_OF_OFFICE_EVENT["id"] in stored
    assert TRANSPARENT_ALL_DAY_EVENT["id"] in stored  # multi-day leave blocks
    assert WORKING_LOCATION_EVENT["id"] not in stored


@pytest.mark.asyncio
async def test_sync_handles_cancellation_of_one_recurring_instance(db, connection):  # noqa: F811
    """Cancelling next week's meeting must not unblock this week's."""
    await calendar_sync_service.sync_connection(
        db, connection_id=connection.id,
        client=FakeCalendarClient(pages=[EventPage(
            [RECURRING_INSTANCE, SECOND_RECURRING_INSTANCE], None, "t1")]),
    )
    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id,
        client=FakeCalendarClient(pages=[EventPage([CANCELLED_RECURRING_INSTANCE], None, "t2")]),
    )

    assert result.blocks_removed == 1
    blocks = {b.external_event_id: b for b in (await db.scalars(select(ExternalBusyBlock))).all()}
    assert blocks[RECURRING_INSTANCE["id"]].deleted_at is None
    assert blocks[SECOND_RECURRING_INSTANCE["id"]].deleted_at is not None


@pytest.mark.asyncio
async def test_sync_survives_cancelled_event_with_no_times(db, connection):  # noqa: F811
    """A deletion for an event we never stored must not crash the batch."""
    result = await calendar_sync_service.sync_connection(
        db, connection_id=connection.id,
        client=FakeCalendarClient(pages=[EventPage([CANCELLED_EVENT], None, "t")]),
    )
    assert result.events_seen == 1
    assert result.blocks_upserted == 0
