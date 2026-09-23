"""The Twilio inbound VOICE webhooks (Phase 4).

Same signing discipline as tests/test_twilio_webhook.py: signatures are
computed with Twilio's OWN RequestValidator, never hand-built, so a
passing test says something about reality rather than about two copies
of the same misunderstanding agreeing with each other.

NO REAL GEMINI CALL HAPPENS IN THIS FILE. This file covers the
TRANSPORT only -- signatures, TwiML shape, how a Gather turn becomes a
VoiceTurnJob, and how the poll loop reads one back. The model never runs
here; see tests/test_voice_turn_outbox.py for the worker side (running
`run_turn`, crash-safety, the ABANDONED path) and
tests/test_voice_dob_confirm.py for the identity gate itself.

WHY /gather NO LONGER TAKES A `chatbot_loop.handle_message` STUB: it used
to call the model synchronously and this file stubbed that call directly.
It no longer does -- see api/v1/webhooks.py's module docstring for why
(a live measurement showed the synchronous design could not fit inside
Twilio's webhook budget). /gather's only job now is enqueuing a
VoiceTurnJob and replying with hold TwiML; /poll is what later reads the
job and decides what the call does.
"""

from __future__ import annotations

import uuid
from xml.etree import ElementTree

import pytest
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from app.core.config import get_settings
from app.models.conversation import Conversation
from app.models.enums import ConversationChannel, ConversationStatus, NotificationStatus
from app.models.voice_turn_job import VoiceTurnJob
from app.services import conversation_service, voice_turn_service

INCOMING_PATH = "/api/v1/webhooks/twilio/voice/incoming"
GATHER_PATH = "/api/v1/webhooks/twilio/voice/gather"
POLL_PATH = "/api/v1/webhooks/twilio/voice/poll"
CALLER = "+14155550123"


@pytest.fixture(autouse=True)
def twilio_configured(monkeypatch):
    """Real auth token so signatures can be genuinely valid -- see the
    identical fixture in test_twilio_webhook.py for why this is autouse."""
    from pydantic import SecretStr

    settings = get_settings()
    monkeypatch.setattr(settings, "twilio_auth_token", SecretStr("test-auth-token"), raising=False)
    monkeypatch.setattr(settings, "twilio_webhook_base_url", "http://testserver", raising=False)
    return settings


def sign(params: dict[str, str], *, path: str, token: str = "test-auth-token", url: str | None = None) -> str:
    return RequestValidator(token).compute_signature(url or f"http://testserver{path}", params)


def call_params(*, call_sid: str | None = None, speech: str | None = None) -> dict[str, str]:
    params = {
        "From": CALLER,
        "To": "+14155559999",
        "CallSid": call_sid or f"CA{uuid.uuid4().hex}",
        "AccountSid": "AC00000000000000000000000000000001",
        "CallStatus": "in-progress",
    }
    if speech is not None:
        params["SpeechResult"] = speech
        params["Confidence"] = "0.9"
    return params


def xml_root(resp) -> ElementTree.Element:
    assert resp.headers["content-type"].startswith("application/xml")
    return ElementTree.fromstring(resp.text)  # raises if malformed


def tags(root: ElementTree.Element) -> list[str]:
    return [child.tag for child in root]


async def conversations(db):
    return list((await db.scalars(select(Conversation))).all())


async def jobs(db):
    return list((await db.scalars(select(VoiceTurnJob).order_by(VoiceTurnJob.created_at))).all())


async def poll(client, *, job_id, cycle: int = 1, extra: dict[str, str] | None = None):
    params = call_params()
    if extra:
        params.update(extra)
    path_with_query = f"{POLL_PATH}?job={job_id}&cycle={cycle}"
    url = f"http://testserver{path_with_query}"
    return await client.post(
        path_with_query, data=params, headers={"X-Twilio-Signature": sign(params, path=POLL_PATH, url=url)}
    )


# ===================================================================== #
# Signature validation -- all three routes, same discipline as SMS
# ===================================================================== #


@pytest.mark.asyncio
async def test_incoming_valid_signature_is_accepted(client, db):
    params = call_params()
    resp = await client.post(
        INCOMING_PATH, data=params, headers={"X-Twilio-Signature": sign(params, path=INCOMING_PATH)}
    )
    assert resp.status_code == 200
    xml_root(resp)


@pytest.mark.asyncio
async def test_incoming_missing_signature_is_rejected(client, db):
    resp = await client.post(INCOMING_PATH, data=call_params())
    assert resp.status_code == 403
    assert await conversations(db) == []


@pytest.mark.asyncio
async def test_incoming_wrong_token_signature_is_rejected(client, db):
    params = call_params()
    forged = sign(params, path=INCOMING_PATH, token="an-attackers-token")
    resp = await client.post(INCOMING_PATH, data=params, headers={"X-Twilio-Signature": forged})
    assert resp.status_code == 403
    assert await conversations(db) == []


@pytest.mark.asyncio
async def test_incoming_tampered_body_invalidates_signature(client, db):
    params = call_params()
    signature = sign(params, path=INCOMING_PATH)
    params["From"] = "+19999999999"  # tamper after signing
    resp = await client.post(INCOMING_PATH, data=params, headers={"X-Twilio-Signature": signature})
    assert resp.status_code == 403
    assert await conversations(db) == []


@pytest.mark.asyncio
async def test_incoming_fails_closed_without_auth_token(client, db, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "twilio_auth_token", SecretStr(""), raising=False)
    params = call_params()
    resp = await client.post(
        INCOMING_PATH, data=params, headers={"X-Twilio-Signature": sign(params, path=INCOMING_PATH)}
    )
    assert resp.status_code == 503
    assert await conversations(db) == []


@pytest.mark.asyncio
async def test_gather_valid_signature_is_accepted(client, db):
    params = call_params(speech="I'd like to book an appointment")
    url = f"http://testserver{GATHER_PATH}?retry=0"
    resp = await client.post(
        f"{GATHER_PATH}?retry=0",
        data=params,
        headers={"X-Twilio-Signature": sign(params, path=GATHER_PATH, url=url)},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_gather_wrong_signature_is_rejected_and_nothing_enqueued(client, db):
    params = call_params(speech="cancel everything")
    resp = await client.post(
        f"{GATHER_PATH}?retry=0", data=params, headers={"X-Twilio-Signature": "garbage"}
    )
    assert resp.status_code == 403
    assert await jobs(db) == []


@pytest.mark.asyncio
async def test_poll_wrong_signature_is_rejected(client, db):
    resp = await client.post(
        f"{POLL_PATH}?job={uuid.uuid4()}&cycle=1",
        data=call_params(),
        headers={"X-Twilio-Signature": "garbage"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_there_is_no_signature_bypass_setting():
    """Structural, same assertion as the SMS suite -- one settings object,
    one answer, regardless of which webhook file is asking."""
    fields = set(type(get_settings()).model_fields)
    for suspicious in ("twilio_validate_signature", "twilio_skip_signature", "twilio_insecure"):
        assert suspicious not in fields


# ===================================================================== #
# Incoming call: conversation wiring, greeting, terminal handling
# ===================================================================== #


@pytest.mark.asyncio
async def test_incoming_creates_conversation_keyed_on_from_number(client, db):
    params = call_params()
    await client.post(
        INCOMING_PATH, data=params, headers={"X-Twilio-Signature": sign(params, path=INCOMING_PATH)}
    )

    convo = await db.scalar(select(Conversation))
    assert convo is not None
    assert convo.channel is ConversationChannel.VOICE
    assert convo.external_ref == CALLER
    assert convo.status is ConversationStatus.ACTIVE
    assert convo.patient_id is None  # nothing is verified just by calling in


@pytest.mark.asyncio
async def test_incoming_speaks_greeting_and_opens_a_gather(client, db):
    params = call_params()
    resp = await client.post(
        INCOMING_PATH, data=params, headers={"X-Twilio-Signature": sign(params, path=INCOMING_PATH)}
    )
    root = xml_root(resp)
    assert tags(root)[:2] == ["Say", "Gather"]
    say_text = root.find("Say").text
    assert "clinic" in say_text.lower()
    gather = root.find("Gather")
    assert gather.get("input") == "speech"
    assert "retry=0" in gather.get("action")


@pytest.mark.asyncio
async def test_incoming_on_escalated_conversation_hangs_up_without_gathering(client, db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=CALLER
    )
    await db.commit()
    await conversation_service.escalate(db, conversation_id=convo.id, reason="prior lockout")

    params = call_params()
    resp = await client.post(
        INCOMING_PATH, data=params, headers={"X-Twilio-Signature": sign(params, path=INCOMING_PATH)}
    )
    root = xml_root(resp)
    assert "Gather" not in tags(root)
    assert "Hangup" in tags(root)


@pytest.mark.asyncio
async def test_missing_from_or_callsid_ends_call_gracefully(client, db):
    params = {"To": "+14155559999", "CallStatus": "in-progress"}  # no From, no CallSid
    resp = await client.post(
        INCOMING_PATH, data=params, headers={"X-Twilio-Signature": sign(params, path=INCOMING_PATH)}
    )
    assert resp.status_code == 200
    root = xml_root(resp)
    assert "Hangup" in tags(root)
    assert await conversations(db) == []


# ===================================================================== #
# Gather: enqueues a job and holds. Silence retries are unaffected --
# they never touch a job at all.
# ===================================================================== #


@pytest.mark.asyncio
async def test_gather_with_speech_enqueues_a_job_and_holds(client, db):
    params = call_params(speech="I'd like to book an appointment")
    url = f"http://testserver{GATHER_PATH}?retry=0"
    resp = await client.post(
        f"{GATHER_PATH}?retry=0",
        data=params,
        headers={"X-Twilio-Signature": sign(params, path=GATHER_PATH, url=url)},
    )
    root = xml_root(resp)
    assert tags(root) == ["Say", "Pause", "Redirect"]
    assert "moment" in root.find("Say").text.lower()
    redirect_url = root.find("Redirect").text
    assert "voice/poll" in redirect_url
    assert "cycle=1" in redirect_url

    queued = await jobs(db)
    assert len(queued) == 1
    assert queued[0].inbound_text == "I'd like to book an appointment"
    assert queued[0].status is NotificationStatus.PENDING

    convo = await db.scalar(select(Conversation))
    assert convo is not None
    assert queued[0].conversation_id == convo.id


@pytest.mark.asyncio
async def test_gather_on_escalated_conversation_hangs_up_without_enqueueing(client, db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=CALLER
    )
    await db.commit()
    await conversation_service.escalate(db, conversation_id=convo.id, reason="prior lockout")

    params = call_params(speech="hello?")
    url = f"http://testserver{GATHER_PATH}?retry=0"
    resp = await client.post(
        f"{GATHER_PATH}?retry=0",
        data=params,
        headers={"X-Twilio-Signature": sign(params, path=GATHER_PATH, url=url)},
    )
    root = xml_root(resp)
    assert "Gather" not in tags(root)
    assert "Pause" not in tags(root)
    assert "Hangup" in tags(root)
    assert await jobs(db) == []


@pytest.mark.asyncio
async def test_gather_with_no_speech_reprompts_without_enqueueing(client, db):
    params = call_params()  # no SpeechResult at all
    url = f"http://testserver{GATHER_PATH}?retry=0"
    resp = await client.post(
        f"{GATHER_PATH}?retry=0",
        data=params,
        headers={"X-Twilio-Signature": sign(params, path=GATHER_PATH, url=url)},
    )
    root = xml_root(resp)
    # The reprompt is SAID INSIDE the Gather (Twilio plays it before
    # listening again), not a separate top-level Say -- that's why this
    # is ["Gather", "Say", "Hangup"] rather than ["Say", "Gather", ...]:
    # the trailing Say+Hangup are the belt-and-braces fallback, only
    # reached if Twilio doesn't call `action` on a second timeout.
    assert tags(root) == ["Gather", "Say", "Hangup"]
    gather = root.find("Gather")
    assert "retry=1" in gather.get("action")  # bumped, not reset
    assert gather.find("Say").text == "Sorry, I didn't catch that. Could you say that again?"
    assert await conversations(db) == []  # never even loaded/created a conversation
    assert await jobs(db) == []


@pytest.mark.asyncio
async def test_gather_silence_retries_are_bounded_independent_of_identity_attempts(client, db):
    """The call-length hygiene bound this is about is NOT
    identity_attempts -- see docs/phase-4-dob-over-voice-decision.md.
    No conversation exists yet in this test at all, which is the point:
    a silent caller never even reaches identity."""
    settings = get_settings()
    max_retries = settings.voice_gather_max_silence_retries

    params = call_params()
    url = f"http://testserver{GATHER_PATH}?retry={max_retries}"
    resp = await client.post(
        f"{GATHER_PATH}?retry={max_retries}",
        data=params,
        headers={"X-Twilio-Signature": sign(params, path=GATHER_PATH, url=url)},
    )
    root = xml_root(resp)
    assert tags(root) == ["Say", "Hangup"]  # gave up, no further Gather


@pytest.mark.asyncio
async def test_gather_missing_from_or_callsid_ends_call_gracefully(client, db):
    params = {"To": "+14155559999", "SpeechResult": "hi"}  # no From, no CallSid
    url = f"http://testserver{GATHER_PATH}?retry=0"
    resp = await client.post(
        f"{GATHER_PATH}?retry=0",
        data=params,
        headers={"X-Twilio-Signature": sign(params, path=GATHER_PATH, url=url)},
    )
    assert resp.status_code == 200
    root = xml_root(resp)
    assert "Hangup" in tags(root)
    assert await jobs(db) == []


# ===================================================================== #
# Poll: reads one job, decides hold / speak / hang up
# ===================================================================== #


@pytest.mark.asyncio
async def test_poll_still_pending_holds_silently_and_bumps_cycle(client, db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=CALLER
    )
    await db.commit()
    job = await voice_turn_service.enqueue(
        db, conversation_id=convo.id, call_sid="CAtest", inbound_text="what's available"
    )
    await db.commit()

    resp = await poll(client, job_id=job.id, cycle=1)
    root = xml_root(resp)
    assert tags(root) == ["Pause", "Redirect"]  # no Say -- not a multiple of 5
    assert "cycle=2" in root.find("Redirect").text


@pytest.mark.asyncio
async def test_poll_reassures_periodically_not_every_cycle(client, db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=CALLER
    )
    await db.commit()
    job = await voice_turn_service.enqueue(
        db, conversation_id=convo.id, call_sid="CAtest", inbound_text="what's available"
    )
    await db.commit()

    resp = await poll(client, job_id=job.id, cycle=5)
    root = xml_root(resp)
    assert tags(root) == ["Say", "Pause", "Redirect"]
    assert "still working" in root.find("Say").text.lower()


@pytest.mark.asyncio
async def test_poll_done_speaks_reply_and_reopens_gather(client, db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=CALLER
    )
    await db.commit()
    job = await voice_turn_service.enqueue(
        db, conversation_id=convo.id, call_sid="CAtest", inbound_text="what's available"
    )
    job.status = NotificationStatus.SENT
    job.reply_body = "Tuesday at two or Wednesday at ten."
    job.conversation_ended = False
    await db.commit()

    resp = await poll(client, job_id=job.id, cycle=3)
    root = xml_root(resp)
    assert tags(root)[:2] == ["Say", "Gather"]
    assert root.find("Say").text == "Tuesday at two or Wednesday at ten."
    assert "retry=0" in root.find("Gather").get("action")


@pytest.mark.asyncio
async def test_poll_done_and_ended_hangs_up_without_a_new_gather(client, db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=CALLER
    )
    await db.commit()
    job = await voice_turn_service.enqueue(
        db, conversation_id=convo.id, call_sid="CAtest", inbound_text="that's all, thanks"
    )
    job.status = NotificationStatus.SENT
    job.reply_body = "You're all set. Goodbye!"
    job.conversation_ended = True
    await db.commit()

    resp = await poll(client, job_id=job.id, cycle=2)
    root = xml_root(resp)
    assert tags(root) == ["Say", "Hangup"]
    assert root.find("Say").text == "You're all set. Goodbye!"


@pytest.mark.asyncio
async def test_poll_abandoned_speaks_fallback_and_hangs_up(client, db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=CALLER
    )
    await db.commit()
    job = await voice_turn_service.enqueue(
        db, conversation_id=convo.id, call_sid="CAtest", inbound_text="hello"
    )
    job.status = NotificationStatus.ABANDONED
    job.last_error = "unexpected: boom"
    await db.commit()

    resp = await poll(client, job_id=job.id, cycle=4)
    root = xml_root(resp)
    assert tags(root) == ["Say", "Hangup"]
    assert "trouble" in root.find("Say").text.lower()


@pytest.mark.asyncio
async def test_poll_gives_up_at_max_cycles(client, db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=CALLER
    )
    await db.commit()
    job = await voice_turn_service.enqueue(
        db, conversation_id=convo.id, call_sid="CAtest", inbound_text="still thinking"
    )
    await db.commit()  # left PENDING -- worker never got to it

    settings = get_settings()
    resp = await poll(client, job_id=job.id, cycle=settings.voice_hold_poll_max_cycles)
    root = xml_root(resp)
    assert tags(root) == ["Say", "Hangup"]
    assert "Redirect" not in tags(root)


@pytest.mark.asyncio
async def test_poll_unknown_job_ends_call_gracefully(client, db):
    resp = await poll(client, job_id=uuid.uuid4(), cycle=1)
    assert resp.status_code == 200
    root = xml_root(resp)
    assert "Hangup" in tags(root)


@pytest.mark.asyncio
async def test_poll_malformed_job_id_ends_call_gracefully(client, db):
    path_with_query = f"{POLL_PATH}?job=not-a-uuid&cycle=1"
    params = call_params()
    url = f"http://testserver{path_with_query}"
    resp = await client.post(
        path_with_query, data=params, headers={"X-Twilio-Signature": sign(params, path=POLL_PATH, url=url)}
    )
    assert resp.status_code == 200
    root = xml_root(resp)
    assert "Hangup" in tags(root)
