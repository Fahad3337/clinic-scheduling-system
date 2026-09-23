"""The voice turn outbox: the fourth outbox, and the simplest one.

See models/voice_turn_job.py for why it is simpler than SmsReplyJob --
one side effect (run the loop), not two, so there is no send-ambiguity
window and no UNRESOLVED state. The interesting tests here are: does a
duplicate job never process twice, does the worker treat any unexpected
failure as ABANDONED (not a background retry -- a caller is holding
live), and does one bad job leave the rest of the batch alone.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.enums import ConversationChannel, NotificationStatus
from app.models.voice_turn_job import VoiceTurnJob
from app.services import conversation_service, voice_turn_service

PHONE = "+14155550123"


def make_turn(reply: str, *, ended: bool = False):
    class _Turn:
        def __init__(self) -> None:
            self.reply = reply
            self.conversation_ended = ended

    return _Turn()


def counting_turn(reply: str = "Here is your answer.", *, ended: bool = False):
    """A run_turn stand-in that records how many times the loop ran."""
    calls: list[dict] = []

    async def run_turn(session, *, conversation_id, text):
        calls.append({"conversation_id": conversation_id, "text": text})
        return make_turn(reply, ended=ended)

    return run_turn, calls


@pytest.fixture
async def convo(db):
    c = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=PHONE
    )
    await db.commit()
    return c


async def enqueue(db, convo, *, text="hello", call_sid=None):
    job = await voice_turn_service.enqueue(
        db, conversation_id=convo.id, call_sid=call_sid or f"CA{uuid.uuid4().hex}", inbound_text=text
    )
    await db.commit()
    return job


# ===================================================================== #
# Ingestion
# ===================================================================== #


@pytest.mark.asyncio
async def test_enqueue_creates_a_pending_job(db, convo):
    job = await enqueue(db, convo, text="what's available Tuesday?")
    assert job is not None
    assert job.status is NotificationStatus.PENDING
    assert job.inbound_text == "what's available Tuesday?"
    assert job.reply_body is None
    assert job.conversation_ended is None


@pytest.mark.asyncio
async def test_repeated_identical_utterances_each_get_their_own_job(db, convo):
    """NO CONTENT-BASED DEDUPE, on purpose -- see models/voice_turn_job.py.
    A caller genuinely repeating themselves (a DOB correction cycle can
    look exactly like this) must not be silently collapsed into one job."""
    first = await enqueue(db, convo, text="March fifteenth nineteen ninety")
    second = await enqueue(db, convo, text="March fifteenth nineteen ninety")
    assert first.id != second.id
    assert len((await db.scalars(select(VoiceTurnJob))).all()) == 2


# ===================================================================== #
# Processing
# ===================================================================== #


@pytest.mark.asyncio
async def test_process_pending_runs_the_loop_and_records_the_reply(db, convo):
    job = await enqueue(db, convo, text="can I book?")
    run_turn, calls = counting_turn("Sure -- what day?")

    result = await voice_turn_service.process_pending(db, run_turn=run_turn)

    assert result.succeeded == 1
    assert len(calls) == 1
    assert calls[0]["text"] == "can I book?"

    await db.refresh(job)
    assert job.status is NotificationStatus.SENT
    assert job.reply_body == "Sure -- what day?"
    assert job.conversation_ended is False
    assert job.processed_at is not None


@pytest.mark.asyncio
async def test_process_pending_records_conversation_ended(db, convo):
    await enqueue(db, convo, text="that's all, thanks")
    run_turn, _ = counting_turn("Goodbye!", ended=True)

    await voice_turn_service.process_pending(db, run_turn=run_turn)

    job = await db.scalar(select(VoiceTurnJob))
    assert job.conversation_ended is True


@pytest.mark.asyncio
async def test_processed_job_is_not_processed_again(db, convo):
    await enqueue(db, convo)
    run_turn, calls = counting_turn()

    await voice_turn_service.process_pending(db, run_turn=run_turn)
    await voice_turn_service.process_pending(db, run_turn=run_turn)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_nothing_to_process_is_not_an_error(db):
    result = await voice_turn_service.process_pending(db, run_turn=counting_turn()[0])
    assert result.processed == 0


@pytest.mark.asyncio
async def test_unexpected_failure_abandons_immediately_no_retry(db, convo):
    """UNLIKE SmsReplyJob: no FAILED-then-retry cycle. A caller is
    holding live on this, and max_attempts defaults to 1 -- see
    models/voice_turn_job.py."""
    job = await enqueue(db, convo)

    async def boom(session, *, conversation_id, text):
        raise RuntimeError("unexpected")

    result = await voice_turn_service.process_pending(db, run_turn=boom)
    assert result.abandoned == 1

    await db.refresh(job)
    assert job.status is NotificationStatus.ABANDONED
    assert job.attempts == 1
    assert "unexpected" in job.last_error


@pytest.mark.asyncio
async def test_a_failing_job_does_not_stop_the_batch(db, convo):
    """One bad conversation must not block every other caller's turn."""
    bad = await enqueue(db, convo, call_sid="CAbad")

    other = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref="+14155550999"
    )
    await db.commit()
    good = await voice_turn_service.enqueue(
        db, conversation_id=other.id, call_sid="CAgood", inbound_text="hello"
    )
    await db.commit()

    async def run_turn(session, *, conversation_id, text):
        if conversation_id == bad.conversation_id:
            raise RuntimeError("model exploded")
        return make_turn("fine")

    result = await voice_turn_service.process_pending(db, run_turn=run_turn)

    assert result.succeeded == 1
    assert result.abandoned == 1
    await db.refresh(good)
    assert good.status is NotificationStatus.SENT


# ===================================================================== #
# Concurrency
# ===================================================================== #


@pytest.mark.asyncio
async def test_concurrent_workers_process_each_job_exactly_once(db, session_factory, convo):
    """Three workers, ONE claimable job, exactly one run of the loop.

    PREMISE verified independently below: exactly one job existed and
    the loop ran exactly once. Without those checks a passing count
    could just mean nothing was eligible.
    """
    await enqueue(db, convo, call_sid="CArace")
    assert len((await db.scalars(select(VoiceTurnJob))).all()) == 1

    calls: list[str] = []

    async def run_turn(session, *, conversation_id, text):
        calls.append(text)
        return make_turn("one reply")

    async def worker():
        async with session_factory() as s:
            return await voice_turn_service.process_pending(s, run_turn=run_turn)

    results = await asyncio.gather(worker(), worker(), worker())

    assert sum(r.succeeded for r in results) == 1, "more than one worker processed this job"
    assert len(calls) == 1, "the conversation loop ran more than once"


# ===================================================================== #
# Reaper
# ===================================================================== #


@pytest.mark.asyncio
async def test_stuck_claim_is_abandoned_never_unresolved(db, convo):
    """SIMPLER than SmsReplyJob's reaper, correctly -- see
    voice_turn_service.reap_stuck_claims' docstring on why there is no
    ambiguity to preserve here: no send step means a stuck CLAIMED row
    provably never finished."""
    job = await enqueue(db, convo)
    job.status = NotificationStatus.CLAIMED
    job.claimed_at = datetime.now(UTC) - timedelta(hours=1)
    db.add(job)
    await db.commit()

    reaped = await voice_turn_service.reap_stuck_claims(db)
    assert reaped == 1

    await db.refresh(job)
    assert job.status is NotificationStatus.ABANDONED

    # Never picked up again -- max_attempts is 1, and this table has no
    # PENDING-requeue path at all for a reaped claim.
    result = await voice_turn_service.process_pending(db, run_turn=counting_turn()[0])
    assert result.processed == 0


@pytest.mark.asyncio
async def test_fresh_claim_is_not_reaped(db, convo):
    job = await enqueue(db, convo)
    job.status = NotificationStatus.CLAIMED
    job.claimed_at = datetime.now(UTC)  # just claimed, not stuck
    db.add(job)
    await db.commit()

    assert await voice_turn_service.reap_stuck_claims(db) == 0
    await db.refresh(job)
    assert job.status is NotificationStatus.CLAIMED


# ===================================================================== #
# Cold identity map
# ===================================================================== #


@pytest.mark.asyncio
async def test_process_pending_works_with_a_cold_identity_map(db, convo):
    await enqueue(db, convo)
    db.expunge_all()

    result = await voice_turn_service.process_pending(db, run_turn=counting_turn()[0])
    assert result.succeeded == 1
