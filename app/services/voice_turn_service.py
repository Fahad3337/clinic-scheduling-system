"""Ingesting inbound voice turns and draining them.

Half of the poll/redirect hold pattern -- see models/voice_turn_job.py
for why it exists. The Gather webhook calls `enqueue`; the worker calls
`process_pending`; a later poll request calls `get_job`. Nothing here
knows about Twilio's request format, TwiML, or FastAPI -- the transport
adapter handles that and hands over plain values, same separation as
sms_reply_service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.enums import NotificationStatus
from app.models.voice_turn_job import VoiceTurnJob

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    processed: int = 0
    succeeded: int = 0
    abandoned: int = 0
    errors: list[str] = field(default_factory=list)


async def enqueue(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    call_sid: str,
    inbound_text: str,
) -> VoiceTurnJob:
    """Record an inbound voice turn for background processing.

    NO DEDUPE HERE -- see models/voice_turn_job.py's module docstring for
    why a content-based key was rejected. A plain INSERT, not
    SmsReplyJob's ON CONFLICT DO NOTHING, is the honest reflection of
    that: there is no key to conflict on.

    Flushes rather than commits so the caller (the webhook) decides the
    transaction boundary -- it still needs this row's id for the poll
    redirect URL before it can safely commit and return a response.
    """
    job = VoiceTurnJob(
        conversation_id=conversation_id,
        call_sid=call_sid,
        inbound_text=inbound_text,
        status=NotificationStatus.PENDING,
    )
    session.add(job)
    await session.flush()
    return job


async def get_job(session: AsyncSession, job_id: UUID) -> VoiceTurnJob | None:
    """Read one job's current state -- what a poll request checks."""
    return await session.get(VoiceTurnJob, job_id)


async def process_pending(
    session: AsyncSession,
    *,
    run_turn,
    limit: int = 10,
) -> ProcessResult:
    """Claim and run pending voice turns. Called by the worker.

    `run_turn` is injected exactly like sms_reply_service.drain -- see
    that module's note on why (this service has no dependency on the
    chatbot package). Signature: `run_turn(session, *, conversation_id,
    text)` -> an object with `.reply` and `.conversation_ended` (i.e.
    chatbot.loop.TurnResult, or a test double shaped like one).
    """
    result = ProcessResult()
    now = datetime.now(UTC)

    claimable = (
        select(VoiceTurnJob)
        .where(VoiceTurnJob.status == NotificationStatus.PENDING)
        .order_by(VoiceTurnJob.created_at)
        .limit(limit)
        # SKIP LOCKED, not plain FOR UPDATE: a loser here should take
        # different work, not queue behind this row. populate_existing
        # because a locked read that returns a cached copy is not
        # locking -- see conversation_service.load_active_conversation.
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    jobs = list((await session.scalars(claimable)).all())
    if not jobs:
        return result

    for job in jobs:
        job.status = NotificationStatus.CLAIMED
        job.claimed_at = now
        job.attempts += 1
    # Commit the claims BEFORE any slow work -- same ordering argument as
    # every other outbox here: releases the row locks before a slow
    # model call, and is what makes this at-most-once rather than
    # at-least-once.
    await session.commit()

    for job in jobs:
        result.processed += 1
        try:
            turn = await run_turn(session, conversation_id=job.conversation_id, text=job.inbound_text)
            job.reply_body = turn.reply
            job.conversation_ended = turn.conversation_ended
            job.status = NotificationStatus.SENT
            job.processed_at = datetime.now(UTC)
            job.last_error = None
            result.succeeded += 1
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the batch
            # ABANDONED, not FAILED-then-retry -- see the module
            # docstring on models/voice_turn_job.py. A caller is holding
            # live on this; max_attempts defaults to 1 for exactly that
            # reason, and a retry-over-minutes strategy that suits SMS
            # does not suit someone on the phone right now.
            job.status = NotificationStatus.ABANDONED
            job.last_error = f"unexpected: {exc}"[:500]
            result.abandoned += 1
            result.errors.append(str(exc)[:200])
            logger.exception("voice turn job %s failed", job.id)

    await session.commit()
    return result


async def reap_stuck_claims(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Abandon jobs whose worker died mid-turn.

    SIMPLER THAN sms_reply_service.reap_stuck_claims, and correctly so:
    that one splits behaviour on whether reply_body was already set,
    because a stuck SMS job might have already run the loop and be
    ambiguous only about whether the SEND then happened. This table has
    no send step -- a stuck CLAIMED row here provably never finished
    (reply_body would be set and status would be SENT if it had), so
    there is nothing ambiguous to preserve either way. Straight to
    ABANDONED, never back to PENDING: max_attempts is 1, and the caller
    who was holding is long gone by the time a reaper runs regardless.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=settings.notification_claim_timeout_minutes)

    result = await session.execute(
        update(VoiceTurnJob)
        .where(
            VoiceTurnJob.status == NotificationStatus.CLAIMED,
            VoiceTurnJob.claimed_at < cutoff,
        )
        .values(status=NotificationStatus.ABANDONED, last_error="reaped: worker died mid-turn")
    )
    await session.commit()
    return result.rowcount or 0
