"""Ingesting inbound SMS jobs and draining them.

The webhook calls `enqueue`; the worker calls `drain`. Nothing here
knows about Twilio's request format or FastAPI -- the transport adapter
handles that and hands over plain values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.messaging import MessageSender, MessageSendError
from app.models.enums import NotificationStatus
from app.models.sms_reply_job import SmsReplyJob

logger = logging.getLogger(__name__)


@dataclass
class DrainResult:
    processed: int = 0
    sent: int = 0
    failed: int = 0
    abandoned: int = 0
    errors: list[str] = field(default_factory=list)


async def enqueue(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    provider_message_id: str,
    inbound_body: str,
    reply_to: str,
) -> SmsReplyJob | None:
    """Record an inbound message for processing. Returns None if already seen.

    ON CONFLICT DO NOTHING on provider_message_id: a Twilio retry is a
    duplicate DELIVERY of one message, not a second message. One atomic
    statement rather than SELECT-then-INSERT, for the reason given in
    every other outbox here -- a read followed by a write is not atomic,
    and two retries arriving together would both pass a prior existence
    check.
    """
    stmt = (
        pg_insert(SmsReplyJob)
        .values(
            conversation_id=conversation_id,
            provider_message_id=provider_message_id,
            inbound_body=inbound_body,
            reply_to=reply_to,
            status=NotificationStatus.PENDING.value,
        )
        .on_conflict_do_nothing(index_elements=["provider_message_id"])
        .returning(SmsReplyJob.id)
    )
    job_id = (await session.execute(stmt)).scalar_one_or_none()
    if job_id is None:
        return None
    return await session.get(SmsReplyJob, job_id)


async def drain(
    session: AsyncSession,
    *,
    sender: MessageSender,
    run_turn,
    limit: int = 10,
    now: datetime | None = None,
) -> DrainResult:
    """Process claimable jobs: run the loop if needed, then send.

    `run_turn` is injected rather than imported so this module does not
    depend on the chatbot package (and so tests can drive it without a
    model). It is called as `run_turn(session, conversation_id=..., text=...,
    provider_message_id=...)` and must return an object with `.reply`.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    result = DrainResult()

    claimable = (
        select(SmsReplyJob)
        .where(
            SmsReplyJob.status.in_([NotificationStatus.PENDING, NotificationStatus.FAILED]),
            SmsReplyJob.attempts < SmsReplyJob.max_attempts,
        )
        .order_by(SmsReplyJob.created_at)
        .limit(limit)
        # SKIP LOCKED, not plain FOR UPDATE: the loser of a race should
        # take DIFFERENT work, not queue behind this row. populate_existing
        # because a locked read that returns a cached copy is not locking
        # -- see conversation_service.load_active_conversation.
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
    # Commit the claims BEFORE any slow work. Same ordering argument as
    # the notification sender: this is what makes the system at-most-once
    # rather than at-least-once, and it releases the row locks so a slow
    # model call does not hold them for the whole batch.
    await session.commit()

    for job in jobs:
        result.processed += 1
        try:
            # --- PHASE 1: run the conversation loop, ONCE --------------
            #
            # Skipped entirely when reply_body is already set, which is
            # what makes a retry after a crash safe. See the module
            # docstring on models/sms_reply_job.py.
            if job.reply_body is None:
                turn = await run_turn(
                    session,
                    conversation_id=job.conversation_id,
                    text=job.inbound_body or "",
                    provider_message_id=job.provider_message_id,
                )
                job.reply_body = turn.reply
                # COMMIT THE REPLY BEFORE SENDING. If the process dies
                # now, the retry sends this text instead of re-running
                # the model and possibly acting twice.
                await session.commit()

            # --- PHASE 2: deliver ---------------------------------------
            message_id = await sender.send(recipient=job.reply_to, body=job.reply_body or "")
            job.status = NotificationStatus.SENT
            job.sent_at = datetime.now(UTC)
            job.provider_send_id = message_id or None
            job.last_error = None
            result.sent += 1
        except MessageSendError as exc:
            job.last_error = str(exc)[:500]
            if exc.permanent or job.attempts >= job.max_attempts:
                job.status = NotificationStatus.ABANDONED
                result.abandoned += 1
            else:
                job.status = NotificationStatus.FAILED
                result.failed += 1
            result.errors.append(str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the batch
            job.last_error = f"unexpected: {exc}"[:500]
            job.status = (
                NotificationStatus.ABANDONED
                if job.attempts >= job.max_attempts
                else NotificationStatus.FAILED
            )
            result.failed += 1
            result.errors.append(str(exc)[:200])
            logger.exception("sms reply job %s failed", job.id)

    await session.commit()
    return result


async def reap_stuck_claims(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Flag jobs whose worker died mid-send.

    SAME PRINCIPLE AS notification_service.reap_stuck_claims, DIFFERENT
    MECHANISM -- read both before assuming one is a copy of the other.

    The shared principle: never auto-retry a stuck claim that MIGHT have
    already caused an irreversible external side effect (an SMS actually
    sent), because retrying risks a duplicate and marking it SENT risks
    hiding a genuine miss. UNRESOLVED means "a human or a provider-API
    reconciliation must decide" -- exactly-once delivery does not exist
    across a network boundary, so somewhere a decision like this has to
    be made honestly rather than guessed.

    Where they differ: notifications treat EVERY stuck CLAIMED row as
    ambiguous, uniformly, because that table has no column recording
    whether the provider call was even attempted before the worker died
    -- CLAIMED alone does not distinguish "about to call Twilio" from
    "just called Twilio and died before recording the result". This
    table CAN distinguish those two moments, because `reply_body` is a
    genuine phase marker for a different reason entirely (making a retry
    skip re-running the model -- see the module docstring): a stuck job
    with reply_body NULL provably never got as far as attempting the
    send, so retrying it is not a guess, it is a fact. A job with
    reply_body already set is the one that is genuinely ambiguous here,
    and only that one becomes UNRESOLVED.

    Do not "simplify" this by copying notifications' uniform treatment
    over here -- that would throw away real information this table
    happens to have. Equally, do not assume notifications is buggy for
    not making the same split -- it has no equivalent marker to split on
    without adding one.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=settings.notification_claim_timeout_minutes)

    result = await session.execute(
        update(SmsReplyJob)
        .where(
            SmsReplyJob.status == NotificationStatus.CLAIMED,
            SmsReplyJob.claimed_at < cutoff,
            # Only the genuinely ambiguous ones. A job still without a
            # reply never reached the provider, so it can safely go back
            # in the queue rather than needing a human.
            SmsReplyJob.reply_body.is_not(None),
        )
        .values(
            status=NotificationStatus.UNRESOLVED,
            last_error="worker did not report an outcome; SMS delivery unknown",
        )
    )
    unresolved = int(result.rowcount or 0)

    # The unambiguous ones go back to FAILED so the normal retry path
    # picks them up.
    requeued = await session.execute(
        update(SmsReplyJob)
        .where(
            SmsReplyJob.status == NotificationStatus.CLAIMED,
            SmsReplyJob.claimed_at < cutoff,
            SmsReplyJob.reply_body.is_(None),
        )
        .values(status=NotificationStatus.FAILED, last_error="worker died before producing a reply")
    )
    await session.commit()
    if requeued.rowcount:
        logger.info("requeued %d sms reply job(s) that never reached the provider", requeued.rowcount)
    return unresolved
