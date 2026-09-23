"""Two-way sync between a doctor's Google Calendar and our schedule.

PULL  (their calendar -> our external_busy_blocks): a busy block stops the
      overlapping slots being offered for NEW bookings.
PUSH  (our appointments -> their calendar): so the doctor sees clinic work
      in the same place as everything else.

THE FEEDBACK LOOP, and the guard against it
-------------------------------------------
These two directions form a cycle. We push appointment X to the doctor's
calendar; the next pull sees an event covering X's slot and mirrors it as a
busy block; that block overlaps X's own appointment, so conflict detection
raises a collision between X and itself; and the slot is now "busy" for
reasons that trace back to us. The clinic would fill up with phantom
conflicts within one sync cycle.

The guard is `extendedProperties.private[clinic_appointment_id]`, stamped on
every event we create and checked on every event we read. Private extended
properties are visible only to our OAuth client, so nothing a doctor does in
the Google UI can forge or accidentally strip them in a way that matters.

Any two-way integration has some version of this problem. Writing the marker
on push is easy to remember; checking it on pull is easy to forget, and the
symptom (mysterious self-conflicts) looks nothing like the cause.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.google_calendar import (
    CLINIC_APPOINTMENT_PROPERTY,
    GoogleAccessTokenExpiredError,
    GoogleApiError,
    GoogleCalendarClient,
    SyncTokenExpiredError,
)
from app.models.appointment import Appointment
from app.models.appointment_external_event import AppointmentExternalEvent
from app.models.calendar_connection import CalendarConnection
from app.models.doctor import Doctor
from app.models.enums import (
    AppointmentStatus,
    CalendarConnectionState,
    CalendarPushState,
    ConflictResolution,
)
from app.models.external_busy_block import ExternalBusyBlock
from app.models.schedule_conflict import ScheduleConflict
from app.models.time_slot import TimeSlot
from app.services import calendar_token_service

logger = logging.getLogger(__name__)

# Google event types that describe where someone is, not that they are
# unavailable. A "workingLocation" event covers the whole working day and
# would block every slot if treated as busy -- a spectacular way to make a
# doctor look permanently unavailable.
NON_BLOCKING_EVENT_TYPES = {"workingLocation"}

# Google's explicit "I am not working" event type. These are frequently
# marked transparent ("Free") even though they mean the exact opposite, so
# they must override the transparency rule or a doctor's out-of-office day
# stays fully bookable.
ALWAYS_BLOCKING_EVENT_TYPES = {"outOfOffice"}

MAX_PUSH_ATTEMPTS = 5


@dataclass
class SyncResult:
    connection_id: UUID
    full_resync: bool = False
    events_seen: int = 0
    blocks_upserted: int = 0
    blocks_removed: int = 0
    conflicts_created: int = 0
    conflicts_resolved: int = 0
    skipped_own_events: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- #
# Event interpretation
# --------------------------------------------------------------------- #


def _parse_event_window(event: dict[str, Any], tz: ZoneInfo) -> tuple[datetime, datetime, bool] | None:
    """Turn a Google event's start/end into a concrete UTC interval.

    Returns (starts_at, ends_at, is_all_day), or None if unusable.

    ALL-DAY EVENTS ARE THE TRAP HERE. Google sends them as {"date":
    "2026-09-21"} with no time and no zone, and the END DATE IS EXCLUSIVE --
    a one-day event on the 21st has end.date == "2026-09-22". Two bugs
    follow from getting this wrong:

      * interpreting the date in UTC instead of the DOCTOR's timezone shifts
        the block by the UTC offset, leaving bookable slivers at one end of
        the day and over-blocking at the other;
      * treating the end date as inclusive extends every all-day event by 24
        hours, wiping out the following day's availability.

    A doctor's one-day holiday silently eating two days of slots is exactly
    the kind of bug that gets noticed by a patient, not by us.
    """
    start = event.get("start") or {}
    end = event.get("end") or {}

    if "dateTime" in start and "dateTime" in end:
        # RFC3339 with an offset. fromisoformat handles "Z" on 3.11+.
        starts_at = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        ends_at = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
        return starts_at.astimezone(UTC), ends_at.astimezone(UTC), False

    if "date" in start and "date" in end:
        start_date = date.fromisoformat(start["date"])
        end_date_exclusive = date.fromisoformat(end["date"])
        starts_at = datetime.combine(start_date, time.min, tzinfo=tz)
        # Exclusive end: midnight at the START of end.date, in local time.
        ends_at = datetime.combine(end_date_exclusive, time.min, tzinfo=tz)
        if ends_at <= starts_at:  # malformed; treat as a single day
            ends_at = starts_at + timedelta(days=1)
        return starts_at.astimezone(UTC), ends_at.astimezone(UTC), True

    return None


def _is_our_own_event(event: dict[str, Any]) -> bool:
    """See the feedback-loop warning in the module docstring."""
    props = (event.get("extendedProperties") or {}).get("private") or {}
    return CLINIC_APPOINTMENT_PROPERTY in props


def _blocks_time(event: dict[str, Any]) -> bool:
    """Whether this event should make the doctor unavailable.

    ASSUMPTIONS, all worth confirming with the clinic:
      * transparency == "transparent" means the doctor marked it "Free", so
        it does not block. Respecting this is what lets a doctor keep
        informational events on their calendar without losing appointments.
      * declined invitations still block. A doctor who declined a meeting is
        probably free, but "probably" is doing a lot of work, and wrongly
        freeing a slot double-books a patient -- a worse failure than
        wrongly blocking one. Erring toward blocking is the safe direction.
      * workingLocation events never block (see NON_BLOCKING_EVENT_TYPES).
      * outOfOffice events ALWAYS block, even when transparent.

      * MULTI-DAY all-day events always block, transparency ignored.
        Single-day all-day events still respect it. See the inline comment
        for the reasoning and its limits.

    STILL UNCONFIRMED: whether the Google UI really defaults all-day events
    to "Free". The multi-day rule above makes the answer harmless for leave
    and conferences either way, but a SINGLE-day "Free" all-day event
    (e.g. a one-day course) will not block. Confirm with a real calendar.
    """
    event_type = event.get("eventType")
    if event_type in NON_BLOCKING_EVENT_TYPES:
        return False
    # Checked BEFORE transparency, deliberately -- see
    # ALWAYS_BLOCKING_EVENT_TYPES.
    if event_type in ALWAYS_BLOCKING_EVENT_TYPES:
        return True

    # MULTI-DAY ALL-DAY EVENTS BLOCK REGARDLESS OF TRANSPARENCY.
    #
    # Google Calendar is reported to default all-day events to "Free"
    # (transparent). If a doctor's week of annual leave inherits that
    # default, respecting transparency would leave the whole week bookable
    # and patients would arrive to an empty clinic.
    #
    # Same asymmetric bias as declined invitations: wrongly blocking costs
    # capacity, wrongly freeing double-books a patient. A multi-day all-day
    # event is overwhelmingly leave, a conference or similar -- things that
    # genuinely mean "not here".
    #
    # SINGLE-day all-day events still respect transparency, because those
    # are dominated by birthdays and reminders that should not erase a
    # day's clinic. That split is a heuristic, not a truth; it is the least
    # bad line available without asking the doctor to tag events.
    if _is_multi_day_all_day(event):
        return True

    if event.get("transparency") == "transparent":
        return False
    return True


def _is_multi_day_all_day(event: dict[str, Any]) -> bool:
    """True for an all-day event covering more than one calendar day.

    Google sends all-day events as bare dates with an EXCLUSIVE end, so a
    single day is end - start == 1.
    """
    start = (event.get("start") or {}).get("date")
    end = (event.get("end") or {}).get("date")
    if not start or not end:
        return False
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days > 1
    except ValueError:
        return False


# --------------------------------------------------------------------- #
# Pull
# --------------------------------------------------------------------- #


async def sync_connection(
    session: AsyncSession,
    *,
    connection_id: UUID,
    client: GoogleCalendarClient | None = None,
    now: datetime | None = None,
) -> SyncResult:
    """Pull one doctor's calendar into external_busy_blocks.

    TODO(multi-doctor): the scheduler currently runs every connection on one
    fixed interval, so with many doctors every sync tick fires every request
    at Google simultaneously -- a self-inflicted thundering herd that will
    hit rate limits and make all of them retry together. Before onboarding a
    second doctor, stagger each connection by a deterministic offset derived
    from a hash of doctor_id (e.g. offset = hash(doctor_id) % interval), so
    load spreads evenly across the window instead of spiking. Deliberately
    not built yet: single-doctor deployment makes it dead code today.
    """
    settings = get_settings()
    client = client or GoogleCalendarClient()
    now = now or datetime.now(UTC)

    connection = await session.get(CalendarConnection, connection_id)
    if connection is None:
        raise ValueError(f"No calendar connection {connection_id}")

    result = SyncResult(connection_id=connection_id)

    if connection.state is not CalendarConnectionState.ACTIVE:
        # Not an error: disabled and needs-reauth connections are skipped by
        # design, and the scheduler should not treat that as a failure.
        result.errors.append(f"skipped: connection is {connection.state.value}")
        return result

    # Load the doctor EXPLICITLY rather than touching `connection.doctor`.
    #
    # A lazy relationship access is a hidden database round trip, and in
    # async SQLAlchemy it does not merely block -- it raises MissingGreenlet,
    # because the lazy loader tries to do IO outside the greenlet context
    # that bridges sync and async.
    #
    # WHY THIS SURVIVED THE TEST SUITE: fixtures had already loaded the
    # Doctor into the same session's identity map, so `connection.doctor`
    # resolved from memory and never touched the database. Running against a
    # cold session -- which is exactly what the background worker will do --
    # it failed immediately. A test with a warm identity map can pass for
    # reasons that have nothing to do with the code being correct.
    doctor = await session.get(Doctor, connection.doctor_id)
    tz = ZoneInfo(doctor.timezone) if doctor else ZoneInfo("UTC")

    # Decide full vs incremental. We re-baseline periodically because a
    # sync token pins the time window from the original full sync, so a
    # purely incremental strategy never learns about events beyond that
    # original horizon -- availability silently stops being checked past it.
    stale_baseline = (
        connection.last_full_sync_at is None
        or connection.last_full_sync_at < now - timedelta(days=settings.calendar_full_resync_days)
    )
    use_sync_token = connection.sync_token is not None and not stale_baseline
    result.full_resync = not use_sync_token

    time_min = now - timedelta(days=1)  # small look-back catches just-edited past events
    time_max = now + timedelta(days=settings.calendar_sync_window_days)

    try:
        pages = await _fetch_all_pages(
            session,
            client=client,
            connection=connection,
            sync_token=connection.sync_token if use_sync_token else None,
            time_min=None if use_sync_token else time_min,
            time_max=None if use_sync_token else time_max,
        )
    except SyncTokenExpiredError:
        # DOCUMENTED, EXPECTED. Google is telling us the cursor is too old.
        logger.info("sync token expired for connection %s; falling back to full resync", connection_id)
        connection.sync_token = None
        await session.commit()
        result.full_resync = True
        pages = await _fetch_all_pages(
            session, client=client, connection=connection,
            sync_token=None, time_min=time_min, time_max=time_max,
        )

    events, next_sync_token = pages

    if result.full_resync:
        # A full resync is authoritative for its window. Anything we hold
        # that Google did not just send has gone; mark it so before applying
        # the new set, then let the upserts revive whatever is still real.
        seen_ids = {e["id"] for e in events if e.get("id")}
        removed = await _soft_delete_missing(session, connection=connection, keep_ids=seen_ids, now=now)
        result.blocks_removed += removed

    for event in events:
        result.events_seen += 1
        event_id = event.get("id")
        if not event_id:
            continue

        if _is_our_own_event(event):
            result.skipped_own_events += 1
            continue

        cancelled = event.get("status") == "cancelled"
        if cancelled or not _blocks_time(event):
            if await _soft_delete_block(session, connection=connection, event_id=event_id, now=now):
                result.blocks_removed += 1
            continue

        window = _parse_event_window(event, tz)
        if window is None:
            logger.warning("event %s has unusable start/end; skipping", event_id)
            continue
        starts_at, ends_at, is_all_day = window

        await _upsert_block(
            session,
            connection=connection,
            event=event,
            event_id=event_id,
            starts_at=starts_at,
            ends_at=ends_at,
            is_all_day=is_all_day,
            now=now,
        )
        result.blocks_upserted += 1

    connection.sync_token = next_sync_token or connection.sync_token
    connection.last_synced_at = now
    if result.full_resync:
        connection.last_full_sync_at = now
    connection.consecutive_failures = 0
    connection.last_error = None
    await session.commit()

    result.conflicts_created = await detect_conflicts(session, doctor_id=connection.doctor_id, now=now)
    result.conflicts_resolved = await resolve_vanished_conflicts(session, doctor_id=connection.doctor_id, now=now)
    return result


async def _fetch_all_pages(
    session: AsyncSession,
    *,
    client: GoogleCalendarClient,
    connection: CalendarConnection,
    sync_token: str | None,
    time_min: datetime | None,
    time_max: datetime | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Walk pagination, refreshing the access token once on a 401."""
    # Pass the SAME client through: the token service needs it to refresh,
    # and constructing a fresh one per refresh would discard the connection
    # pool and make the client uninjectable in tests.
    access_token = await calendar_token_service.get_valid_access_token(
        session, connection_id=connection.id, client=client
    )

    events: list[dict[str, Any]] = []
    page_token: str | None = None
    next_sync_token: str | None = None
    refreshed_once = False

    while True:
        try:
            page = await client.list_events(
                access_token=access_token,
                calendar_id=connection.calendar_id,
                sync_token=sync_token,
                time_min=time_min,
                time_max=time_max,
                page_token=page_token,
            )
        except GoogleAccessTokenExpiredError:
            # Reactive refresh -- exactly ONCE. If a token minted seconds
            # ago is also rejected, the cause is not staleness and looping
            # would only hide the real problem.
            if refreshed_once:
                raise
            refreshed_once = True
            access_token = await calendar_token_service.get_valid_access_token(
                session, connection_id=connection.id, client=client, force=True
            )
            continue

        events.extend(page.events)
        next_sync_token = page.next_sync_token or next_sync_token
        if not page.next_page_token:
            break
        page_token = page.next_page_token

    return events, next_sync_token


async def _upsert_block(
    session: AsyncSession,
    *,
    connection: CalendarConnection,
    event: dict[str, Any],
    event_id: str,
    starts_at: datetime,
    ends_at: datetime,
    is_all_day: bool,
    now: datetime,
) -> None:
    """Insert or update one mirrored block.

    ON CONFLICT DO UPDATE on (connection_id, external_event_id) makes the
    whole sync idempotent: replaying the same page, or overlapping runs,
    converge on one row instead of duplicating capacity blocks.

    `deleted_at=None` in the update set is what revives an event restored
    from Google's trash -- it keeps its ID, so the row already exists.
    """
    summary = event.get("summary") if connection.store_event_details else None

    stmt = (
        pg_insert(ExternalBusyBlock)
        .values(
            connection_id=connection.id,
            doctor_id=connection.doctor_id,
            external_event_id=event_id,
            external_etag=event.get("etag"),
            starts_at=starts_at,
            ends_at=ends_at,
            is_all_day=is_all_day,
            summary=summary,
            synced_at=now,
            deleted_at=None,
        )
        .on_conflict_do_update(
            index_elements=["connection_id", "external_event_id"],
            set_={
                "starts_at": starts_at,
                "ends_at": ends_at,
                "is_all_day": is_all_day,
                "summary": summary,
                "external_etag": event.get("etag"),
                "synced_at": now,
                "deleted_at": None,
            },
        )
    )
    await session.execute(stmt)


async def _soft_delete_block(
    session: AsyncSession, *, connection: CalendarConnection, event_id: str, now: datetime
) -> bool:
    result = await session.execute(
        update(ExternalBusyBlock)
        .where(
            ExternalBusyBlock.connection_id == connection.id,
            ExternalBusyBlock.external_event_id == event_id,
            ExternalBusyBlock.deleted_at.is_(None),
        )
        .values(deleted_at=now)
    )
    return bool(result.rowcount)


async def _soft_delete_missing(
    session: AsyncSession, *, connection: CalendarConnection, keep_ids: set[str], now: datetime
) -> int:
    stmt = update(ExternalBusyBlock).where(
        ExternalBusyBlock.connection_id == connection.id,
        ExternalBusyBlock.deleted_at.is_(None),
    )
    if keep_ids:
        stmt = stmt.where(ExternalBusyBlock.external_event_id.not_in(keep_ids))
    result = await session.execute(stmt.values(deleted_at=now))
    return int(result.rowcount or 0)


# --------------------------------------------------------------------- #
# Conflict detection
# --------------------------------------------------------------------- #


async def detect_conflicts(session: AsyncSession, *, doctor_id: UUID, now: datetime | None = None) -> int:
    """Record collisions between live bookings and the doctor's own events.

    THE SYNC JOB NEVER MUTATES AN APPOINTMENT. It records the collision and
    stops. Cancelling a patient's medical appointment because a doctor
    pencilled something in is not a decision software gets to make -- see
    models/schedule_conflict.py for the full argument.
    """
    now = now or datetime.now(UTC)

    overlapping = (
        select(Appointment.id, ExternalBusyBlock.id)
        .join(TimeSlot, TimeSlot.id == Appointment.time_slot_id)
        .join(
            ExternalBusyBlock,
            and_(
                ExternalBusyBlock.doctor_id == Appointment.doctor_id,
                ExternalBusyBlock.deleted_at.is_(None),
                # Half-open overlap: touching intervals do not collide.
                ExternalBusyBlock.starts_at < TimeSlot.ends_at,
                ExternalBusyBlock.ends_at > TimeSlot.starts_at,
            ),
        )
        .where(
            Appointment.doctor_id == doctor_id,
            Appointment.status != AppointmentStatus.CANCELLED,
        )
    )
    pairs = (await session.execute(overlapping)).all()
    if not pairs:
        return 0

    created = 0
    for appointment_id, block_id in pairs:
        # DO NOTHING rather than a prior existence check: the unique
        # constraint makes re-running the sync every five minutes a no-op
        # instead of piling up duplicate rows for one unresolved collision.
        stmt = (
            pg_insert(ScheduleConflict)
            .values(
                appointment_id=appointment_id,
                busy_block_id=block_id,
                detected_at=now,
                resolution=ConflictResolution.UNRESOLVED.value,
            )
            .on_conflict_do_nothing(index_elements=["appointment_id", "busy_block_id"])
            .returning(ScheduleConflict.id)
        )
        if (await session.execute(stmt)).scalar_one_or_none() is not None:
            created += 1

    await session.commit()
    return created


async def resolve_vanished_conflicts(
    session: AsyncSession, *, doctor_id: UUID, now: datetime | None = None
) -> int:
    """Close conflicts whose calendar event has gone away.

    Without this the staff triage queue fills with collisions that resolved
    themselves when the doctor deleted their event, and a queue full of
    stale items is a queue nobody reads.
    """
    now = now or datetime.now(UTC)
    vanished = (
        select(ScheduleConflict.id)
        .join(ExternalBusyBlock, ExternalBusyBlock.id == ScheduleConflict.busy_block_id)
        .join(Appointment, Appointment.id == ScheduleConflict.appointment_id)
        .where(
            Appointment.doctor_id == doctor_id,
            ScheduleConflict.resolution == ConflictResolution.UNRESOLVED,
            ExternalBusyBlock.deleted_at.is_not(None),
        )
    )
    ids = [row[0] for row in (await session.execute(vanished)).all()]
    if not ids:
        return 0

    await session.execute(
        update(ScheduleConflict)
        .where(ScheduleConflict.id.in_(ids))
        .values(
            resolution=ConflictResolution.EXTERNAL_EVENT_REMOVED,
            resolved_at=now,
        )
    )
    await session.commit()
    return len(ids)


# --------------------------------------------------------------------- #
# Push (the outbox)
# --------------------------------------------------------------------- #


def build_event_payload(appointment: Appointment, slot: TimeSlot, *, patient_label: str) -> dict[str, Any]:
    """The Google event body for one of our appointments.

    PRIVACY: the summary carries a patient LABEL chosen by the caller, not
    clinical detail. Whatever goes here lands in the doctor's Google
    account, syncs to their phone, and may appear on a lock screen or a
    shared display. Diagnoses and reasons for visit do not belong in it.
    """
    return {
        "summary": f"Clinic: {patient_label}",
        "start": {"dateTime": slot.starts_at.astimezone(UTC).isoformat()},
        "end": {"dateTime": slot.ends_at.astimezone(UTC).isoformat()},
        # THE FEEDBACK-LOOP GUARD. Private to our OAuth client.
        "extendedProperties": {
            "private": {CLINIC_APPOINTMENT_PROPERTY: str(appointment.id)}
        },
        # Our system is the source of truth for clinic bookings; a doctor
        # dragging the event in Google must not silently move the patient's
        # appointment. Locking it down makes the one-way-ness visible.
        "guestsCanModify": False,
        "transparency": "opaque",
    }


async def push_pending_events(
    session: AsyncSession,
    *,
    client: GoogleCalendarClient | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> dict[str, int]:
    """Drain the appointment_external_events outbox.

    Runs OUTSIDE the booking transaction on purpose -- see the model
    docstring. Rows are claimed with FOR UPDATE SKIP LOCKED so several
    workers can drain in parallel without fighting over the same row, the
    same primitive the notification sender will use.
    """
    client = client or GoogleCalendarClient()
    now = now or datetime.now(UTC)
    counts = {"synced": 0, "deleted": 0, "failed": 0, "skipped": 0}

    claimable = (
        select(AppointmentExternalEvent)
        .where(
            AppointmentExternalEvent.push_state.in_(
                [
                    CalendarPushState.PENDING,
                    CalendarPushState.UPDATE_PENDING,
                    CalendarPushState.DELETE_PENDING,
                ]
            ),
            AppointmentExternalEvent.attempts < MAX_PUSH_ATTEMPTS,
        )
        .order_by(AppointmentExternalEvent.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    rows = list((await session.scalars(claimable)).all())

    for row in rows:
        connection = await session.get(CalendarConnection, row.connection_id)
        if connection is None or connection.state is not CalendarConnectionState.ACTIVE:
            counts["skipped"] += 1
            continue

        try:
            access_token = await calendar_token_service.get_valid_access_token(
                session, connection_id=connection.id, client=client
            )
            if row.push_state is CalendarPushState.DELETE_PENDING:
                if row.external_event_id:
                    await client.delete_event(
                        access_token=access_token,
                        calendar_id=connection.calendar_id,
                        event_id=row.external_event_id,
                    )
                row.push_state = CalendarPushState.DELETED
                counts["deleted"] += 1
            else:
                appointment = await session.get(Appointment, row.appointment_id)
                slot = await session.get(TimeSlot, appointment.time_slot_id)
                payload = build_event_payload(appointment, slot, patient_label="appointment")

                if row.external_event_id:
                    created = await client.patch_event(
                        access_token=access_token,
                        calendar_id=connection.calendar_id,
                        event_id=row.external_event_id,
                        event=payload,
                    )
                else:
                    created = await client.insert_event(
                        access_token=access_token,
                        calendar_id=connection.calendar_id,
                        event=payload,
                    )
                row.external_event_id = created.get("id", row.external_event_id)
                row.external_etag = created.get("etag")
                row.push_state = CalendarPushState.SYNCED
                counts["synced"] += 1

            row.last_pushed_at = now
            row.last_error = None
        except GoogleApiError as exc:
            row.attempts += 1
            row.last_error = str(exc)[:500]
            if row.attempts >= MAX_PUSH_ATTEMPTS:
                row.push_state = CalendarPushState.FAILED
            counts["failed"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
            row.attempts += 1
            row.last_error = f"unexpected: {exc}"[:500]
            if row.attempts >= MAX_PUSH_ATTEMPTS:
                row.push_state = CalendarPushState.FAILED
            counts["failed"] += 1

    await session.commit()
    return counts
