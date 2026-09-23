"""The SMS reply outbox: the third outbox, and the one with two phases.

The interesting tests here are not "does a reply get sent" but "what
happens when the worker dies between running the conversation loop and
delivering the SMS". That gap is the reason this table has a
`reply_body` column instead of just a status, and it is the one thing
that genuinely differs from the calendar-push and notification outboxes.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.integrations.messaging import MessageSendError
from app.models.enums import ConversationChannel, NotificationStatus
from app.models.sms_reply_job import SmsReplyJob
from app.services import conversation_service, sms_reply_service

PHONE = "+14155550123"


class FakeSender:
    def __init__(self, *, fail_with: Exception | None = None, message_id: str = "SMout1"):
        self.fail_with = fail_with
        self.message_id = message_id
        self.sends: list[dict] = []

    async def send(self, *, recipient: str, body: str, subject: str | None = None) -> str:
        self.sends.append({"recipient": recipient, "body": body})
        if self.fail_with:
            raise self.fail_with
        return self.message_id


def make_turn(reply: str):
    class _Turn:
        def __init__(self) -> None:
            self.reply = reply

    return _Turn()


def counting_turn(reply: str = "Here is your answer."):
    """A run_turn stand-in that records how many times the loop ran."""
    calls: list[dict] = []

    async def run_turn(session, *, conversation_id, text, provider_message_id=None):
        calls.append({"conversation_id": conversation_id, "text": text})
        return make_turn(reply)

    return run_turn, calls


@pytest.fixture
async def convo(db):
    c = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=PHONE
    )
    await db.commit()
    return c


async def enqueue(db, convo, *, body="hello", sid=None):
    job = await sms_reply_service.enqueue(
        db,
        conversation_id=convo.id,
        provider_message_id=sid or f"SM{uuid.uuid4().hex}",
        inbound_body=body,
        reply_to=PHONE,
    )
    await db.commit()
    return job


# ===================================================================== #
# Ingestion
# ===================================================================== #


@pytest.mark.asyncio
async def test_enqueue_creates_a_pending_job(db, convo):
    job = await enqueue(db, convo, body="hi there")
    assert job is not None
    assert job.status is NotificationStatus.PENDING
    assert job.inbound_body == "hi there"
    assert job.reply_body is None


@pytest.mark.asyncio
async def test_duplicate_sid_returns_none_and_creates_nothing(db, convo):
    first = await enqueue(db, convo, sid="SMdup")
    second = await enqueue(db, convo, sid="SMdup")

    assert first is not None
    assert second is None, "a duplicate delivery produced a second job"
    assert len((await db.scalars(select(SmsReplyJob))).all()) == 1


# ===================================================================== #
# Draining
# ===================================================================== #


@pytest.mark.asyncio
async def test_drain_runs_the_loop_then_sends(db, convo):
    job = await enqueue(db, convo, body="can I book?")
    sender = FakeSender()
    run_turn, calls = counting_turn("Sure -- what day?")

    result = await sms_reply_service.drain(db, sender=sender, run_turn=run_turn)

    assert result.sent == 1
    assert len(calls) == 1
    assert calls[0]["text"] == "can I book?"
    assert sender.sends == [{"recipient": PHONE, "body": "Sure -- what day?"}]

    await db.refresh(job)
    assert job.status is NotificationStatus.SENT
    assert job.sent_at is not None
    assert job.reply_body == "Sure -- what day?"
    assert job.provider_send_id == "SMout1"


@pytest.mark.asyncio
async def test_drained_job_is_not_processed_again(db, convo):
    await enqueue(db, convo)
    sender = FakeSender()
    run_turn, calls = counting_turn()

    await sms_reply_service.drain(db, sender=sender, run_turn=run_turn)
    await sms_reply_service.drain(db, sender=sender, run_turn=run_turn)

    assert len(calls) == 1
    assert len(sender.sends) == 1


@pytest.mark.asyncio
async def test_nothing_to_drain_is_not_an_error(db):
    result = await sms_reply_service.drain(db, sender=FakeSender(), run_turn=counting_turn()[0])
    assert result.processed == 0


# ===================================================================== #
# THE TWO-PHASE GUARANTEE -- the reason this design differs from the
# other two outboxes
# ===================================================================== #


@pytest.mark.asyncio
async def test_crash_after_the_loop_does_not_rerun_the_loop(db, convo):
    """The core claim: a retry sends the decided reply, it does NOT
    re-invoke the model.

    Re-running the loop would re-append the patient's turn to the
    transcript and could act twice -- book a second appointment for one
    request. Simulated here by a send that fails AFTER the loop has
    already produced a reply.
    """
    job = await enqueue(db, convo)
    run_turn, calls = counting_turn("Booked for Tuesday.")

    failing = FakeSender(fail_with=MessageSendError("503 upstream", permanent=False))
    await sms_reply_service.drain(db, sender=failing, run_turn=run_turn)

    await db.refresh(job)
    assert job.status is NotificationStatus.FAILED
    # PREMISE: the loop DID run and its reply was persisted before the
    # send was attempted. Without this the next assertion proves nothing.
    assert job.reply_body == "Booked for Tuesday."
    assert len(calls) == 1

    # The retry must send, not think.
    working = FakeSender()
    result = await sms_reply_service.drain(db, sender=working, run_turn=run_turn)

    assert result.sent == 1
    assert len(calls) == 1, "the conversation loop ran a second time on retry"
    assert working.sends[0]["body"] == "Booked for Tuesday."


@pytest.mark.asyncio
async def test_transient_send_failure_retries_then_abandons(db, convo):
    job = await enqueue(db, convo)
    run_turn, calls = counting_turn()
    failing = FakeSender(fail_with=MessageSendError("503", permanent=False))

    for _ in range(5):
        await sms_reply_service.drain(db, sender=failing, run_turn=run_turn)

    await db.refresh(job)
    assert job.status is NotificationStatus.ABANDONED
    assert job.attempts == job.max_attempts
    # And the model was only ever consulted once, across every retry.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_permanent_send_failure_abandons_immediately(db, convo):
    job = await enqueue(db, convo)
    run_turn, _ = counting_turn()
    failing = FakeSender(fail_with=MessageSendError("400 unreachable number", permanent=True))

    await sms_reply_service.drain(db, sender=failing, run_turn=run_turn)

    await db.refresh(job)
    assert job.status is NotificationStatus.ABANDONED
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_a_failing_loop_does_not_stop_the_batch(db, convo):
    """One bad conversation must not block every other patient's reply."""
    bad = await enqueue(db, convo, sid="SMbad")

    other = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref="+14155550999"
    )
    await db.commit()
    good = await sms_reply_service.enqueue(
        db, conversation_id=other.id, provider_message_id="SMgood",
        inbound_body="hello", reply_to="+14155550999",
    )
    await db.commit()

    async def run_turn(session, *, conversation_id, text, provider_message_id=None):
        if conversation_id == bad.conversation_id:
            raise RuntimeError("model exploded")
        return make_turn("fine")

    sender = FakeSender()
    result = await sms_reply_service.drain(db, sender=sender, run_turn=run_turn)

    assert result.sent == 1
    assert result.failed == 1
    await db.refresh(good)
    assert good.status is NotificationStatus.SENT


# ===================================================================== #
# Concurrency
# ===================================================================== #


@pytest.mark.asyncio
async def test_concurrent_workers_send_each_reply_exactly_once(db, session_factory, convo):
    """Three workers, ONE claimable job, exactly one send.

    PREMISE verified independently below: exactly one job was
    deliverable, and the loop ran exactly once. Without those checks a
    passing count could just mean nothing was eligible.
    """
    await enqueue(db, convo, sid="SMrace")
    assert len((await db.scalars(select(SmsReplyJob))).all()) == 1

    shared = FakeSender()
    calls: list[str] = []

    async def run_turn(session, *, conversation_id, text, provider_message_id=None):
        calls.append(provider_message_id or "")
        return make_turn("one reply")

    async def worker():
        async with session_factory() as s:
            return await sms_reply_service.drain(s, sender=shared, run_turn=run_turn)

    results = await asyncio.gather(worker(), worker(), worker())

    assert sum(r.sent for r in results) == 1, "more than one worker sent this reply"
    assert len(shared.sends) == 1
    assert len(calls) == 1, "the conversation loop ran more than once"


# ===================================================================== #
# Reaper
# ===================================================================== #


@pytest.mark.asyncio
async def test_stuck_job_mid_send_becomes_unresolved(db, convo):
    """Had a reply and was mid-send: genuinely ambiguous, needs a human."""
    job = await enqueue(db, convo)
    job.status = NotificationStatus.CLAIMED
    job.claimed_at = datetime.now(UTC) - timedelta(hours=1)
    job.reply_body = "already decided"
    db.add(job)
    await db.commit()

    assert await sms_reply_service.reap_stuck_claims(db) == 1
    await db.refresh(job)
    assert job.status is NotificationStatus.UNRESOLVED

    # And it must NOT be picked up again -- that risks a duplicate SMS.
    sender = FakeSender()
    await sms_reply_service.drain(db, sender=sender, run_turn=counting_turn()[0])
    assert sender.sends == []


@pytest.mark.asyncio
async def test_stuck_job_before_the_loop_is_safely_requeued(db, convo):
    """Never reached the provider, so retrying is safe -- and better
    than parking a patient's message for a human who may never look."""
    job = await enqueue(db, convo)
    job.status = NotificationStatus.CLAIMED
    job.claimed_at = datetime.now(UTC) - timedelta(hours=1)
    job.reply_body = None
    db.add(job)
    await db.commit()

    unresolved = await sms_reply_service.reap_stuck_claims(db)
    assert unresolved == 0  # not ambiguous

    await db.refresh(job)
    assert job.status is NotificationStatus.FAILED

    sender = FakeSender()
    result = await sms_reply_service.drain(db, sender=sender, run_turn=counting_turn()[0])
    assert result.sent == 1


# ===================================================================== #
# Cold identity map
# ===================================================================== #


@pytest.mark.asyncio
async def test_drain_works_with_a_cold_identity_map(db, convo):
    await enqueue(db, convo)
    db.expunge_all()

    sender = FakeSender()
    result = await sms_reply_service.drain(db, sender=sender, run_turn=counting_turn()[0])
    assert result.sent == 1
