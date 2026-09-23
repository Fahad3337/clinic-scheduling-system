"""Background job runner.

Runs as its OWN container, separate from the API. See the compose file.

WHY A SEPARATE PROCESS RATHER THAN A SCHEDULER INSIDE FastAPI
-------------------------------------------------------------
An in-process scheduler fires once per web worker. Scale the API to three
uvicorn replicas and every job runs three times per tick. It also means a
deploy interrupts jobs mid-flight, and a CPU-heavy job competes with
request latency. One dedicated single-replica process avoids all three, for
the cost of one stanza in docker-compose.

WHY NO PERSISTENT JOB STORE (MemoryJobStore), AND NO REDIS
-----------------------------------------------------------
Every job here is a POLLER that re-derives its work from the database:
"which notifications are due", "which calendars need syncing", "which
outbox rows are pending". There is no schedule to lose on restart, so
persisting the job definitions buys nothing.

The alternative -- scheduling one job per appointment at booking time --
would need a persistent store, and would also require cancelling and
rescheduling that job whenever the appointment changed. A poller is
restart-safe by construction and has no such bookkeeping. It also dodges a
practical wrinkle: APScheduler 3.x job stores are synchronous, so a
SQLAlchemyJobStore would force a second, sync database driver alongside
asyncpg.

Redis would buy distributed locking, which we do not need: duplicate job
execution is already HARMLESS because correctness lives in the database
(UNIQUE(dedupe_key) for notifications, FOR UPDATE SKIP LOCKED for claims,
ON CONFLICT for calendar upserts). Running exactly one scheduler is an
efficiency choice, not a correctness requirement -- which is exactly the
property that lets us skip the extra infrastructure.

THE TWO SETTINGS THAT MATTER MOST
---------------------------------
`max_instances=1`  a slow run must never overlap the next tick. Without it
                   a sync that takes longer than the interval stacks up,
                   and concurrent runs of the same sync fight over the same
                   rows until something times out.
`coalesce=True`    if the worker was down for an hour, do NOT fire the
                   twelve missed 5-minute runs back to back. Run once and
                   catch up, because each run already re-derives all
                   outstanding work from the database.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionFactory, engine
from app.integrations.google_calendar import GoogleCalendarClient
from app.integrations.messaging import SendGridEmailSender, TwilioSmsSender
from app.models.calendar_connection import CalendarConnection
from app.models.enums import CalendarConnectionState, NotificationChannel
from app.services import calendar_sync_service, notification_service, sms_reply_service, voice_turn_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
)
logger = logging.getLogger("worker")


# --------------------------------------------------------------------- #
# Jobs
#
# Each opens its OWN session and catches its own exceptions. An unhandled
# exception inside a job would otherwise be swallowed by APScheduler and
# the job would keep firing with nobody the wiser -- silent failure is the
# characteristic bug of background work, so every job logs its outcome
# whether it succeeded or not.
# --------------------------------------------------------------------- #


async def sync_calendars() -> None:
    """Pull every active doctor calendar."""
    try:
        async with SessionFactory() as session:
            connection_ids = list(
                (
                    await session.scalars(
                        select(CalendarConnection.id).where(
                            CalendarConnection.state == CalendarConnectionState.ACTIVE
                        )
                    )
                ).all()
            )

        if not connection_ids:
            logger.debug("calendar sync: no active connections")
            return

        client = GoogleCalendarClient()
        for connection_id in connection_ids:
            # A fresh session PER CONNECTION: one doctor's failure must not
            # poison a shared transaction and take everyone else's sync
            # down with it.
            try:
                async with SessionFactory() as session:
                    result = await calendar_sync_service.sync_connection(
                        session, connection_id=connection_id, client=client
                    )
                logger.info(
                    "calendar sync %s: full=%s seen=%d upserted=%d removed=%d "
                    "conflicts=+%d/-%d skipped_own=%d",
                    connection_id, result.full_resync, result.events_seen,
                    result.blocks_upserted, result.blocks_removed,
                    result.conflicts_created, result.conflicts_resolved,
                    result.skipped_own_events,
                )
            except Exception:
                logger.exception("calendar sync failed for connection %s", connection_id)
    except Exception:
        logger.exception("calendar sync job failed")


async def push_calendar_events() -> None:
    """Drain the outbound calendar outbox."""
    try:
        async with SessionFactory() as session:
            counts = await calendar_sync_service.push_pending_events(
                session, client=GoogleCalendarClient()
            )
        if any(counts.values()):
            logger.info("calendar push: %s", counts)
    except Exception:
        logger.exception("calendar push job failed")


async def send_notifications() -> None:
    """Deliver everything that is due."""
    try:
        senders = {
            NotificationChannel.SMS: TwilioSmsSender(),
            NotificationChannel.EMAIL: SendGridEmailSender(),
        }
        async with SessionFactory() as session:
            result = await notification_service.send_due_notifications(
                session, senders=senders
            )
        if result.sent or result.failed or result.skipped or result.abandoned:
            logger.info(
                "notifications: sent=%d failed=%d skipped=%d abandoned=%d %s",
                result.sent, result.failed, result.skipped, result.abandoned,
                result.errors[:3],
            )
    except Exception:
        logger.exception("notification job failed")


async def send_sms_replies() -> None:
    """Run conversation turns for inbound SMS and deliver the replies.

    The job that keeps the Twilio webhook fast. See
    models/sms_reply_job.py for why the loop runs here rather than in
    the request.

    `run_turn` is passed in rather than imported inside the service so
    the outbox has no dependency on the chatbot package -- the service
    knows it must run "something" per job and send the result.
    """
    try:
        from app.chatbot import loop as chatbot_loop

        async with SessionFactory() as session:
            result = await sms_reply_service.drain(
                session,
                sender=TwilioSmsSender(),
                run_turn=chatbot_loop.handle_message,
            )
        if result.processed:
            logger.info(
                "sms replies: processed=%d sent=%d failed=%d abandoned=%d %s",
                result.processed, result.sent, result.failed, result.abandoned,
                result.errors[:3],
            )
    except Exception:
        logger.exception("sms reply job failed")


async def reap_stuck_sms_replies() -> None:
    """Flag inbound SMS jobs whose worker died mid-send."""
    try:
        async with SessionFactory() as session:
            count = await sms_reply_service.reap_stuck_claims(session)
        if count:
            # WARNING: each of these is a patient who may or may not have
            # received a reply to a message they sent.
            logger.warning("reaped %d stuck sms reply job(s) -> UNRESOLVED", count)
    except Exception:
        logger.exception("sms reaper job failed")


async def process_voice_turns() -> None:
    """Run conversation turns for inbound voice, for a caller who is
    live-holding on the "please hold" TwiML -- see models/voice_turn_job.py
    and api/v1/webhooks.py's twilio_voice_poll. Same run_turn injection
    reasoning as send_sms_replies; no delivery step needed here, since the
    reply is read straight off this row by the next poll request rather
    than sent through a provider API.
    """
    try:
        from app.chatbot import loop as chatbot_loop

        async with SessionFactory() as session:
            result = await voice_turn_service.process_pending(
                session, run_turn=chatbot_loop.handle_message
            )
        if result.processed:
            logger.info(
                "voice turns: processed=%d succeeded=%d abandoned=%d %s",
                result.processed, result.succeeded, result.abandoned, result.errors[:3],
            )
    except Exception:
        logger.exception("voice turn job failed")


async def reap_stuck_voice_turns() -> None:
    """Abandon voice turn jobs whose worker died mid-turn.

    Not WARNING-level like the SMS/notification reapers: those flag
    UNRESOLVED because a real external send might have gone out before
    the crash, which someone needs to reconcile. A stuck voice turn job
    has no such ambiguity (see reap_stuck_claims' docstring) and the
    caller who was holding has already heard a fallback and hung up by
    the time this runs -- there is nothing left to reconcile.
    """
    try:
        async with SessionFactory() as session:
            count = await voice_turn_service.reap_stuck_claims(session)
        if count:
            logger.info("reaped %d stuck voice turn job(s) -> abandoned", count)
    except Exception:
        logger.exception("voice turn reaper job failed")


async def reap_stuck_notifications() -> None:
    """Flag notifications whose worker died mid-send."""
    try:
        async with SessionFactory() as session:
            count = await notification_service.reap_stuck_claims(session)
        if count:
            # WARNING, not INFO: each of these is a message that may or may
            # not have reached a patient. Somebody should look.
            logger.warning("reaped %d stuck notification claim(s) -> UNRESOLVED", count)
    except Exception:
        logger.exception("reaper job failed")


async def heartbeat() -> None:
    """Prove the scheduler is alive.

    Without it, "no log output" is ambiguous between a healthy idle system
    and a dead one -- and the failure mode of background work is silence.
    """
    logger.info("worker heartbeat %s", datetime.now(UTC).isoformat(timespec="seconds"))


# --------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------- #


def build_scheduler() -> AsyncIOScheduler:
    settings = get_settings()

    scheduler = AsyncIOScheduler(
        timezone="UTC",  # never the container's local zone; schedules must not shift
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            # How late a missed run may still fire. Beyond this it is
            # dropped -- coalesce plus the pollers' catch-up queries mean
            # nothing is actually lost.
            "misfire_grace_time": 60,
        },
    )

    scheduler.add_job(
        send_notifications,
        "interval",
        seconds=settings.notification_scan_interval_seconds,
        id="send_notifications",
        next_run_time=datetime.now(UTC),  # do not wait a full interval on boot
    )
    scheduler.add_job(
        sync_calendars,
        "interval",
        minutes=settings.calendar_sync_interval_minutes,
        id="sync_calendars",
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        send_sms_replies,
        "interval",
        seconds=settings.sms_reply_scan_interval_seconds,
        id="send_sms_replies",
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        reap_stuck_sms_replies,
        "interval",
        minutes=max(1, settings.notification_claim_timeout_minutes),
        id="reap_stuck_sms_replies",
    )
    scheduler.add_job(
        process_voice_turns,
        "interval",
        seconds=settings.voice_turn_scan_interval_seconds,
        id="process_voice_turns",
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        reap_stuck_voice_turns,
        "interval",
        minutes=max(1, settings.notification_claim_timeout_minutes),
        id="reap_stuck_voice_turns",
    )
    scheduler.add_job(
        push_calendar_events,
        "interval",
        seconds=max(30, settings.notification_scan_interval_seconds * 2),
        id="push_calendar_events",
    )
    scheduler.add_job(
        reap_stuck_notifications,
        "interval",
        minutes=max(1, settings.notification_claim_timeout_minutes),
        id="reap_stuck_notifications",
    )
    scheduler.add_job(heartbeat, "interval", minutes=15, id="heartbeat")
    return scheduler


async def run() -> None:
    settings = get_settings()
    scheduler = build_scheduler()
    stopping = asyncio.Event()

    def _request_stop(sig_name: str) -> None:
        logger.info("received %s, shutting down", sig_name)
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # SIGTERM matters: it is what `docker stop` and every orchestrator
        # send. Without a handler the process is killed outright, and a job
        # that was mid-send dies without recording its outcome -- creating
        # exactly the UNRESOLVED rows the reaper then has to puzzle over.
        loop.add_signal_handler(sig, _request_stop, sig.name)

    scheduler.start()
    logger.info(
        "worker started: sms replies every %ss, notifications every %ss, calendar sync every %smin",
        settings.sms_reply_scan_interval_seconds,
        settings.notification_scan_interval_seconds,
        settings.calendar_sync_interval_minutes,
    )

    try:
        await stopping.wait()
    finally:
        # wait=True lets in-flight jobs finish rather than being cut off
        # partway through a provider call.
        scheduler.shutdown(wait=True)
        await engine.dispose()
        logger.info("worker stopped cleanly")


if __name__ == "__main__":
    asyncio.run(run())
