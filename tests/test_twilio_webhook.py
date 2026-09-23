"""The Twilio inbound webhook.

Signatures here are computed with Twilio's OWN RequestValidator, not
hand-built. If we signed requests with our own reimplementation of the
canonicalisation, a test suite could pass against a validator that is
wrong in exactly the same way -- proving only that our two copies of a
misunderstanding agree. Using their signer against their validator means
a passing test says something about reality.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from app.core.config import get_settings
from app.models.conversation import Conversation
from app.models.enums import ConversationStatus, NotificationStatus
from app.models.sms_reply_job import SmsReplyJob

WEBHOOK_PATH = "/api/v1/webhooks/twilio/sms"
PATIENT_PHONE = "+14155550123"
DOB = date(1985, 3, 14)


@pytest.fixture(autouse=True)
def twilio_configured(monkeypatch):
    """A real auth token, so signatures can be genuinely valid.

    autouse: every test in this file needs it, and an endpoint that
    fails closed without a token would otherwise return 503 everywhere
    and quietly make the assertions meaningless.
    """
    from pydantic import SecretStr

    settings = get_settings()
    monkeypatch.setattr(settings, "twilio_auth_token", SecretStr("test-auth-token"), raising=False)
    monkeypatch.setattr(settings, "twilio_webhook_base_url", "http://testserver", raising=False)
    return settings


def sign(params: dict[str, str], *, token: str = "test-auth-token", url: str | None = None) -> str:
    return RequestValidator(token).compute_signature(url or f"http://testserver{WEBHOOK_PATH}", params)


def inbound(body: str = "hello", *, sid: str | None = None) -> dict[str, str]:
    """A Twilio-shaped form body.

    The MessageSid is UNIQUE PER CALL unless a test pins one. That is not
    cosmetic: sharing a sid across tests trips the webhook's own
    idempotency check, and every affected test then gets an empty
    TwiML reply and fails for a reason that has nothing to do with what
    it is testing. (Discovered the hard way -- five tests failed
    together with empty responses, which turned out to be the dedupe
    logic working correctly.) Tests that mean to exercise duplicate
    delivery pass an explicit sid.
    """
    sid = sid or f"SM{uuid.uuid4().hex}"
    return {
        "From": PATIENT_PHONE,
        "To": "+14155559999",
        "Body": body,
        "MessageSid": sid,
        "AccountSid": "AC00000000000000000000000000000001",
    }


@pytest.fixture
def stub_loop():
    """No-op placeholder.

    The webhook NO LONGER RUNS THE LOOP -- it enqueues an SmsReplyJob and
    returns empty TwiML, so there is nothing to stub. Kept as a fixture
    name so the signature tests below read unchanged; what they assert
    is now "did a job get created", which is the webhook's actual job.
    """
    return []


async def jobs(db):
    return list((await db.scalars(select(SmsReplyJob).order_by(SmsReplyJob.created_at))).all())


# ===================================================================== #
# Signature validation
# ===================================================================== #


@pytest.mark.asyncio
async def test_valid_signature_is_accepted(client, db, stub_loop):
    params = inbound()
    resp = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})

    assert resp.status_code == 200
    # Empty TwiML: the reply comes from the worker, not this response.
    assert "<Message>" not in resp.text
    assert len(await jobs(db)) == 1


@pytest.mark.asyncio
async def test_missing_signature_is_rejected(client, db, stub_loop):
    resp = await client.post(WEBHOOK_PATH, data=inbound())
    assert resp.status_code == 403
    assert await jobs(db) == []  # nothing was queued


@pytest.mark.asyncio
async def test_signature_from_the_wrong_token_is_rejected(client, db, stub_loop):
    params = inbound()
    forged = sign(params, token="an-attackers-token")
    resp = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": forged})
    assert resp.status_code == 403
    assert await jobs(db) == []


@pytest.mark.asyncio
async def test_tampered_body_invalidates_the_signature(client, db, stub_loop):
    """The signature covers the PARAMETERS, not just the URL."""
    params = inbound(body="hello")
    signature = sign(params)
    params["Body"] = "cancel all appointments"  # tamper after signing

    resp = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": signature})
    assert resp.status_code == 403
    assert await jobs(db) == []


@pytest.mark.asyncio
async def test_signature_for_a_different_url_is_rejected(client, db, stub_loop):
    """Guards the base-url configuration: a signature computed for some
    other host must not validate here."""
    params = inbound()
    wrong_url_sig = sign(params, url="http://evil.example.com/api/v1/webhooks/twilio/sms")
    resp = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": wrong_url_sig})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_fails_closed_without_an_auth_token(client, db, stub_loop, monkeypatch):
    """Unconfigured must mean refused, never unvalidated."""
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "twilio_auth_token", SecretStr(""), raising=False)
    params = inbound()
    resp = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})

    assert resp.status_code == 503
    assert await jobs(db) == []


@pytest.mark.asyncio
async def test_there_is_no_signature_bypass_setting():
    """Structural: no config flag may exist that disables validation.

    Asserted rather than merely intended -- a 'skip validation in dev'
    toggle is the kind of thing that gets added for a good reason and
    found switched on in production later.
    """
    fields = set(type(get_settings()).model_fields)
    for suspicious in ("twilio_validate_signature", "twilio_skip_signature", "twilio_insecure"):
        assert suspicious not in fields


# ===================================================================== #
# Idempotency
# ===================================================================== #


@pytest.mark.asyncio
async def test_duplicate_delivery_enqueues_only_one_job(client, db, stub_loop):
    """Twilio retries. One message must produce one job, hence one reply."""
    params = inbound(sid="SM000000000000000000000000000000AA")
    signature = sign(params)

    first = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": signature})
    second = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": signature})

    # Both accepted -- a 4xx would make Twilio keep retrying forever.
    assert first.status_code == 200
    assert second.status_code == 200

    queued = await jobs(db)
    assert len(queued) == 1, "a retried delivery created a second job"
    assert queued[0].provider_message_id == "SM000000000000000000000000000000AA"


@pytest.mark.asyncio
async def test_distinct_messages_each_enqueue_a_job(client, db, stub_loop):
    for sid in ("SM0000000000000000000000000000000B", "SM0000000000000000000000000000000C"):
        params = inbound(sid=sid)
        resp = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})
        assert resp.status_code == 200
    assert len(await jobs(db)) == 2


# ===================================================================== #
# Conversation wiring and job contents
# ===================================================================== #


@pytest.mark.asyncio
async def test_conversation_is_created_and_keyed_on_the_from_number(client, db, stub_loop):
    params = inbound()
    await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})

    convo = await db.scalar(select(Conversation))
    assert convo is not None
    # The identity anchor comes from the TRANSPORT, not the message body.
    assert convo.external_ref == PATIENT_PHONE
    assert convo.status is ConversationStatus.ACTIVE
    assert convo.patient_id is None  # nothing is verified just by texting in


@pytest.mark.asyncio
async def test_second_message_reuses_the_same_conversation(client, db, stub_loop):
    for sid in ("SM0000000000000000000000000000000D", "SM0000000000000000000000000000000E"):
        params = inbound(sid=sid)
        await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})

    assert len((await db.scalars(select(Conversation))).all()) == 1
    assert len(await jobs(db)) == 2


@pytest.mark.asyncio
async def test_job_captures_body_and_reply_destination(client, db, stub_loop):
    params = inbound(body="  can I move my appointment?  ")
    await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})

    job = (await jobs(db))[0]
    assert job.inbound_body == "can I move my appointment?"
    assert job.reply_to == PATIENT_PHONE
    assert job.status is NotificationStatus.PENDING
    assert job.reply_body is None  # the loop has not run yet


@pytest.mark.asyncio
async def test_inbound_body_is_encrypted_at_rest(client, db, stub_loop):
    from sqlalchemy import text as sql_text

    secret = "the chest pain is back and I am worried"
    params = inbound(body=secret)
    await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})

    stored = (
        await db.execute(sql_text("SELECT inbound_body FROM sms_reply_jobs LIMIT 1"))
    ).scalar_one()
    assert secret not in stored
    assert stored.startswith("gAAAAA")


# ===================================================================== #
# Response shape -- the webhook must be fast and say nothing
# ===================================================================== #


@pytest.mark.asyncio
async def test_webhook_never_calls_the_model(client, db, stub_loop, monkeypatch):
    """The whole point of the outbox: no model call on the request path.

    If the loop were still invoked here, this would raise and fail --
    which is a stronger assertion than timing the response.
    """
    from app.chatbot import loop as loop_module

    async def must_not_run(*args, **kwargs):
        raise AssertionError("the webhook invoked the conversation loop")

    monkeypatch.setattr(loop_module, "handle_message", must_not_run)

    params = inbound()
    resp = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})
    assert resp.status_code == 200
    assert len(await jobs(db)) == 1


@pytest.mark.asyncio
async def test_response_is_valid_empty_twiml(client, db, stub_loop):
    from xml.etree import ElementTree

    params = inbound()
    resp = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})

    assert resp.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(resp.text)  # raises if malformed
    assert root.tag == "Response"
    assert list(root) == []  # no <Message>: the worker sends it


@pytest.mark.asyncio
async def test_missing_from_or_sid_is_accepted_without_queueing(client, db, stub_loop):
    """Accept so Twilio stops retrying, but queue nothing actionable."""
    params = {"To": "+14302183801", "Body": "hi"}  # no From, no MessageSid
    resp = await client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sign(params)})
    assert resp.status_code == 200
    assert await jobs(db) == []
