"""Twilio inbound webhooks: SMS and (Phase 4) Voice.

THE ONLY UNAUTHENTICATED WRITE PATH INTO THIS SYSTEM, and the only one
whose caller is a third party rather than a person. Its defences differ
in kind from the rest of the API:

  - There is no bearer token, because Twilio does not have one. The
    signature IS the authentication: it proves the request came from
    someone holding our Twilio auth token.
  - There is NO BYPASS FLAG. A `validate_signature: bool` setting would
    be convenient for local testing and would, one day, be false in
    production. Tests sign their requests properly instead. A security
    control with an off switch is a security control that is off.
  - If the auth token is not configured, this FAILS CLOSED (503) rather
    than skipping validation. "Not configured" must never mean
    "not checked".

`_validate_twilio_request` is the ONE place that signature check lives.
Every handler below -- SMS, and now two voice routes -- calls it first
and returns immediately if it hands back a Response instead of params.
Three near-identical copies of a security check is exactly the kind of
duplication that drifts silently when only one copy gets a fix; see
memory note loosening-a-constraint-requires-auditing-readers for the
same family of mistake in a different shape.

SMS vs VOICE, WHY THE MODEL CALL HAPPENS IN DIFFERENT PLACES:

SMS deliberately does NOT talk to a language model here. Twilio allows a
webhook roughly 15 seconds; a conversation turn is a model call plus up
to five tool calls and can exceed that, and blowing Twilio's deadline
means the patient receives NOTHING. So the SMS handler does the fast,
bounded work only -- validate, deduplicate, record -- and returns an
empty TwiML; the worker runs the loop and sends the reply through
Twilio's REST API. See models/sms_reply_job.py.

VOICE NOW WORKS THE SAME WAY, FOR THE SAME REASON -- but it took two
tries to get there, and the history is worth keeping because it is
evidence, not just narration.

The first cut (see docs/phase-4-voice-transport-decision.md, Option A:
webhook-per-turn) called the model SYNCHRONOUSLY in the Gather webhook,
reasoning that a phone call has no "ack now, reply whenever" primitive
the way SMS does -- the caller is on the line, and Twilio is waiting on
THIS response to know what happens next. That reasoning about the
PRIMITIVE was correct. What followed from it was not: live verification
measured a SINGLE Gemini call at 12.6s, and a normal turn needs at least
two sequential calls (identity verification, then the actual answer)
before it can reply -- so no in-request deadline could both fit under
Twilio's ~15s budget and leave room for a real response. That is the
exact trigger condition the transport decision doc named up front for
the poll/redirect hold pattern, so that is what is built now.

THE ACTUAL SHAPE: the Gather webhook does the fast part only --
validate, load or create the conversation, enqueue a VoiceTurnJob -- and
replies with "please hold" TwiML that pauses and redirects into
`twilio_voice_poll`. The worker runs the model, with no Twilio deadline
pressure at all, exactly like SMS's worker. Each poll redirect after
that is its own separate, fast webhook request (read one row, decide
hold-or-speak), so Twilio's per-request budget is never at risk
regardless of how long the underlying model call takes -- the
CUMULATIVE wait is bounded instead by voice_hold_poll_max_cycles x
voice_hold_poll_interval_seconds, a budget this application controls.
See models/voice_turn_job.py for the full mechanism and for why it is
simpler than SmsReplyJob despite the similar shape.

Identity is still not this file's business on either channel: it
establishes that the request really came from Twilio and, for voice,
enqueues a turn for the existing transport-agnostic conversation loop
to run. Every question about WHO the caller is happens below that, in
conversation_service and the tool layer -- unchanged by adding a second
channel, and unchanged again by this rework.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Gather, VoiceResponse

from app.api.deps import get_db
from app.chatbot.prompts import (
    ESCALATION_REPLY,
    VOICE_GREETING,
    VOICE_HOLD_MESSAGE,
    VOICE_NO_SPEECH_REPROMPT,
    VOICE_STILL_WORKING,
    fallback_reply_for,
)
from app.core.config import get_settings
from app.models.enums import ConversationChannel, ConversationStatus, NotificationStatus
from app.services import conversation_service, sms_reply_service, voice_turn_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# An explicitly empty TwiML document. Twilio accepts this as "no
# immediate reply", which is exactly right: the reply is coming from the
# worker over the REST API a moment later.
_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _empty() -> Response:
    return Response(content=_EMPTY_TWIML, media_type="application/xml")


def _twiml(response: VoiceResponse) -> Response:
    return Response(content=str(response), media_type="application/xml")


async def _validate_twilio_request(request: Request) -> dict[str, str] | Response:
    """Validate an inbound Twilio request. Returns the form params on
    success, or the Response the caller should return immediately on
    failure. See the module docstring -- this is the ONE copy of the
    signature check; every handler below must use it, not reimplement it.
    """
    settings = get_settings()
    auth_token = settings.twilio_auth_token.get_secret_value()

    if not auth_token:
        # FAIL CLOSED. Processing an unvalidated request because we
        # happen to lack the key to validate it is exactly backwards.
        logger.error("inbound Twilio webhook rejected: no auth token configured")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")

    # The URL Twilio SIGNED, not the one we happen to observe. See the
    # note on twilio_webhook_base_url in core/config.py -- deriving this
    # from request.url is the classic way to make validation fail behind
    # a tunnel or proxy. Includes the query string deliberately: the
    # voice Gather action URL carries a `retry` counter in its query
    # string, and Twilio signs the exact URL it POSTs to, retry value
    # included.
    signed_url = f"{settings.twilio_webhook_base_url.rstrip('/')}{request.url.path}"
    if request.url.query:
        signed_url = f"{signed_url}?{request.url.query}"

    if not RequestValidator(auth_token).validate(signed_url, params, signature):
        # 403 and nothing else. A caller probing this endpoint should not
        # learn whether the url, the parameters or the signature was the
        # problem.
        logger.warning("rejected Twilio webhook with an invalid signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    return params


@router.post("/twilio/sms")
async def twilio_sms(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    result = await _validate_twilio_request(request)
    if isinstance(result, Response):
        return result
    params = result

    from_number = params.get("From", "").strip()
    body = (params.get("Body") or "").strip()
    message_sid = (params.get("MessageSid") or "").strip()

    if not from_number or not message_sid:
        # Nothing actionable. Accept it so Twilio stops retrying, and say
        # nothing.
        logger.warning("Twilio webhook missing From or MessageSid")
        return _empty()

    conversation = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=from_number
    )

    job = await sms_reply_service.enqueue(
        db,
        conversation_id=conversation.id,
        provider_message_id=message_sid,
        inbound_body=body,
        # Reply to the number that messaged us, snapshotted now.
        reply_to=from_number,
    )
    await db.commit()

    if job is None:
        # A duplicate DELIVERY of a message already queued or handled.
        # Accepted so Twilio stops retrying; no second job, no second
        # reply.
        logger.info("ignoring duplicate Twilio delivery %s", message_sid)

    return _empty()


# ======================================================================= #
# Voice (Phase 4)
# ======================================================================= #

# Literal path constants, not request.url_for(): the exact strings used
# to build action/redirect URLs below and the exact strings this router
# registers can be visually diffed against each other -- a mismatch here
# is a silent "the call just hangs up" bug, not something that raises.
_VOICE_GATHER_PATH = "/api/v1/webhooks/twilio/voice/gather"
_VOICE_POLL_PATH = "/api/v1/webhooks/twilio/voice/poll"


def _gather_action_url(*, retry: int) -> str:
    settings = get_settings()
    return f"{settings.twilio_webhook_base_url.rstrip('/')}{_VOICE_GATHER_PATH}?retry={retry}"


def _poll_redirect_url(*, job_id: UUID, cycle: int) -> str:
    settings = get_settings()
    return f"{settings.twilio_webhook_base_url.rstrip('/')}{_VOICE_POLL_PATH}?job={job_id}&cycle={cycle}"


def _gather(*, retry: int) -> Gather:
    return Gather(
        input="speech",
        action=_gather_action_url(retry=retry),
        method="POST",
        speech_timeout="auto",
    )


def _end_call(vr: VoiceResponse, *, say: str) -> Response:
    vr.say(say)
    vr.hangup()
    return _twiml(vr)


def _hold_response(*, job_id: UUID, say: str | None) -> Response:
    """The 'please hold' TwiML: an optional Say, a bounded Pause, then a
    Redirect back into the poll loop. `say` is None on every cycle after
    the first -- see VOICE_HOLD_MESSAGE's docstring on why this must not
    repeat itself every couple of seconds."""
    settings = get_settings()
    vr = VoiceResponse()
    if say:
        vr.say(say)
    vr.pause(length=settings.voice_hold_poll_interval_seconds)
    vr.redirect(_poll_redirect_url(job_id=job_id, cycle=1), method="POST")
    return _twiml(vr)


def _terminal_conversation_response(conversation) -> Response | None:
    """None if the conversation is usable; a hangup Response if it is
    ESCALATED/COMPLETED. Same terminal check load_active_conversation
    makes inside handle_message -- duplicated here because the voice
    entry points need to decide whether to even OFFER a Gather before
    any turn text exists to hand to handle_message at all."""
    if conversation.status is ConversationStatus.ACTIVE:
        return None
    vr = VoiceResponse()
    return _end_call(vr, say=ESCALATION_REPLY)


@router.post("/twilio/voice/incoming")
async def twilio_voice_incoming(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """Twilio calls this when a call comes in. Speaks a greeting and
    opens the first Gather. No model call here -- there is no caller
    utterance yet to hand it; see VOICE_GREETING's docstring note."""
    result = await _validate_twilio_request(request)
    if isinstance(result, Response):
        return result
    params = result

    from_number = params.get("From", "").strip()
    call_sid = params.get("CallSid", "").strip()
    if not from_number or not call_sid:
        logger.warning("Twilio voice webhook missing From or CallSid")
        vr = VoiceResponse()
        return _end_call(vr, say="Sorry, something went wrong. Goodbye.")

    conversation = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=from_number
    )
    await db.commit()

    terminal = _terminal_conversation_response(conversation)
    if terminal is not None:
        return terminal

    vr = VoiceResponse()
    vr.say(VOICE_GREETING)
    vr.append(_gather(retry=0))
    # Reached only if Gather times out with NO speech at all and Twilio
    # does not call `action` for that case -- see the retry-loop note in
    # twilio_voice_gather below for the belt-and-braces reasoning.
    return _end_call(vr, say=fallback_reply_for(ConversationChannel.VOICE))


@router.post("/twilio/voice/gather")
async def twilio_voice_gather(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """The action URL for every Gather in a call, including retries.

    Does the FAST part only when real speech was captured: validate,
    load/create the conversation, enqueue a VoiceTurnJob, reply with
    "please hold". See the module docstring for why -- this used to run
    the model here synchronously, measured why that could not work, and
    was rebuilt around the poll/redirect hold pattern instead.
    """
    result = await _validate_twilio_request(request)
    if isinstance(result, Response):
        return result
    params = result

    from_number = params.get("From", "").strip()
    call_sid = params.get("CallSid", "").strip()
    speech_result = (params.get("SpeechResult") or "").strip()

    try:
        retry = int(request.query_params.get("retry", "0"))
    except ValueError:
        retry = 0

    if not from_number or not call_sid:
        logger.warning("Twilio voice gather webhook missing From or CallSid")
        vr = VoiceResponse()
        return _end_call(vr, say="Sorry, something went wrong. Goodbye.")

    if not speech_result:
        # No speech captured this turn -- silence, or an utterance Twilio
        # could not transcribe into anything at all. This is NOT a wrong
        # guess and it never reaches handle_message, so it costs nothing
        # at the identity layer (see docs/phase-4-dob-over-voice-decision.md).
        # Bounded instead by `voice_gather_max_silence_retries`, a
        # call-length hygiene limit, deliberately independent of
        # identity_attempts -- see the setting's docstring in core/config.py.
        # Purely deterministic, no job needed, unaffected by the hold
        # pattern below.
        settings = get_settings()
        if retry >= settings.voice_gather_max_silence_retries:
            vr = VoiceResponse()
            return _end_call(vr, say=fallback_reply_for(ConversationChannel.VOICE))
        vr = VoiceResponse()
        gather = _gather(retry=retry + 1)
        gather.say(VOICE_NO_SPEECH_REPROMPT)
        vr.append(gather)
        # Same belt-and-braces fallback as the incoming handler: reached
        # only if this Gather ALSO times out with no `action` callback.
        return _end_call(vr, say=fallback_reply_for(ConversationChannel.VOICE))

    conversation = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=from_number
    )
    await db.commit()

    terminal = _terminal_conversation_response(conversation)
    if terminal is not None:
        return terminal

    job = await voice_turn_service.enqueue(
        db, conversation_id=conversation.id, call_sid=call_sid, inbound_text=speech_result
    )
    await db.commit()

    return _hold_response(job_id=job.id, say=VOICE_HOLD_MESSAGE)


@router.post("/twilio/voice/poll")
async def twilio_voice_poll(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """The redirect target of the hold loop. Reads ONE job row and
    decides: still working (hold again), done (speak the reply), or
    abandoned (apologise and hang up). Never touches the model, the
    conversation loop, or conversation_service directly -- everything
    about WHAT was said was already decided by the worker; this endpoint
    only decides what the CALL does about it.
    """
    result = await _validate_twilio_request(request)
    if isinstance(result, Response):
        return result

    settings = get_settings()
    job_id_raw = request.query_params.get("job", "")
    try:
        job_id = UUID(job_id_raw)
    except ValueError:
        logger.warning("Twilio voice poll webhook with a malformed job id %r", job_id_raw)
        vr = VoiceResponse()
        return _end_call(vr, say="Sorry, something went wrong. Goodbye.")
    try:
        cycle = int(request.query_params.get("cycle", "1"))
    except ValueError:
        cycle = 1

    job = await voice_turn_service.get_job(db, job_id)
    if job is None:
        logger.warning("Twilio voice poll webhook for unknown job %s", job_id)
        vr = VoiceResponse()
        return _end_call(vr, say="Sorry, something went wrong. Goodbye.")

    if job.status is NotificationStatus.SENT:
        # 'sent' means processed here, not delivered -- see the status
        # note in models/voice_turn_job.py. The reply IS the delivery.
        vr = VoiceResponse()
        vr.say(job.reply_body or fallback_reply_for(ConversationChannel.VOICE))
        if job.conversation_ended:
            vr.hangup()
            return _twiml(vr)
        vr.append(_gather(retry=0))
        return _end_call(vr, say=fallback_reply_for(ConversationChannel.VOICE))

    if job.status is NotificationStatus.ABANDONED:
        vr = VoiceResponse()
        return _end_call(vr, say=fallback_reply_for(ConversationChannel.VOICE))

    # Still PENDING or CLAIMED -- keep holding, up to the cycle cap.
    if cycle >= settings.voice_hold_poll_max_cycles:
        logger.warning(
            "voice turn job %s did not finish within %d hold cycles (~%ds); ending the call",
            job.id,
            settings.voice_hold_poll_max_cycles,
            settings.voice_hold_poll_max_cycles * settings.voice_hold_poll_interval_seconds,
        )
        vr = VoiceResponse()
        return _end_call(vr, say=fallback_reply_for(ConversationChannel.VOICE))

    vr = VoiceResponse()
    # Periodic reassurance only -- not every cycle, so the call does not
    # repeat itself every couple of seconds during an ordinary-length wait.
    if cycle % 5 == 0:
        vr.say(VOICE_STILL_WORKING)
    vr.pause(length=settings.voice_hold_poll_interval_seconds)
    vr.redirect(_poll_redirect_url(job_id=job.id, cycle=cycle + 1), method="POST")
    return _twiml(vr)
