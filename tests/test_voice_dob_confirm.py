"""Phase 4: the voice read-back-confirm gate on verify_identity.

Covers the DECISION from docs/phase-4-dob-over-voice-decision.md at the
level that actually matters: does the mechanism preserve the exact
3-strike guess budget while insulating it from transcription noise, and
is SMS provably unaffected. The deliberate, explicit answer to "does a
'no' cost a strike" is tested directly (it does not -- restating a
DIFFERENT value is free; only a REPEATED, confirmed value is ever
checked against a patient record).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.chatbot import loop, tools
from app.chatbot.tools import ToolContext
from app.core.config import get_settings
from app.integrations.gemini import ModelTurn, ToolCall
from app.models.conversation import Conversation
from app.models.enums import ConversationChannel, ConversationStatus
from app.models.patient import Patient
from app.services import conversation_service
from app.services.conversation_service import IdentityOutcome

REAL_DOB = date(1985, 3, 14)
MISHEARD_DOB = date(1950, 3, 15)  # a plausible mis-hearing, not REAL_DOB
WRONG_DOB = date(1999, 1, 1)


@pytest.fixture
async def patient(db):
    p = Patient(full_name="Alex Chen", phone="+14155550123", date_of_birth=REAL_DOB)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


@pytest.fixture
async def voice_convo(db, patient):
    c = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=patient.phone
    )
    await db.commit()
    return c


@pytest.fixture
async def sms_convo(db, patient):
    c = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=patient.phone
    )
    await db.commit()
    return c


# ===================================================================== #
# The gate itself
# ===================================================================== #


@pytest.mark.asyncio
async def test_first_voice_attempt_is_pending_not_checked(db, voice_convo, patient):
    result = await conversation_service.verify_identity(
        db, conversation_id=voice_convo.id, date_of_birth=REAL_DOB
    )
    assert result.outcome is IdentityOutcome.PENDING_CONFIRMATION
    assert result.pending_date_of_birth == REAL_DOB
    # No strike spent, even though this happens to be the CORRECT date --
    # the gate does not peek at correctness before confirmation.
    assert result.attempts_remaining == 3

    row = await db.get(Conversation, voice_convo.id)
    await db.refresh(row)
    assert row.pending_dob_candidate == REAL_DOB
    assert row.patient_id is None
    assert row.identity_attempts == 0


@pytest.mark.asyncio
async def test_confirming_the_same_value_runs_the_real_check(db, voice_convo, patient):
    await conversation_service.verify_identity(db, conversation_id=voice_convo.id, date_of_birth=REAL_DOB)
    result = await conversation_service.verify_identity(
        db, conversation_id=voice_convo.id, date_of_birth=REAL_DOB
    )
    assert result.outcome is IdentityOutcome.VERIFIED

    row = await db.get(Conversation, voice_convo.id)
    await db.refresh(row)
    assert row.patient_id == patient.id
    assert row.pending_dob_candidate is None  # cleared once resolved
    assert row.identity_attempts == 0  # a CORRECT confirmed guess is still free


@pytest.mark.asyncio
async def test_a_different_second_value_is_a_free_correction_not_a_strike(db, voice_convo, patient):
    """THE explicit decision: 'no, that's wrong' -- modelled here as the
    model calling back with a DIFFERENT value, which is exactly what
    happens if the caller corrects a misheard date -- costs nothing."""
    first = await conversation_service.verify_identity(
        db, conversation_id=voice_convo.id, date_of_birth=MISHEARD_DOB
    )
    assert first.outcome is IdentityOutcome.PENDING_CONFIRMATION
    assert first.attempts_remaining == 3

    second = await conversation_service.verify_identity(
        db, conversation_id=voice_convo.id, date_of_birth=REAL_DOB
    )
    # Still pending -- a NEW candidate, not yet confirmed -- not a strike.
    assert second.outcome is IdentityOutcome.PENDING_CONFIRMATION
    assert second.pending_date_of_birth == REAL_DOB
    assert second.attempts_remaining == 3

    row = await db.get(Conversation, voice_convo.id)
    await db.refresh(row)
    assert row.pending_dob_candidate == REAL_DOB  # overwritten, not accumulated
    assert row.identity_attempts == 0


@pytest.mark.asyncio
async def test_cycling_many_corrections_never_spends_a_strike(db, voice_convo):
    """No amount of 'no, try again' burns the guess budget -- see the
    decision doc's sanity check: this proves it against the real gate,
    not just by argument."""
    for i in range(10):
        result = await conversation_service.verify_identity(
            db, conversation_id=voice_convo.id, date_of_birth=date(1990, 1, 1 + i)
        )
        assert result.outcome is IdentityOutcome.PENDING_CONFIRMATION
        assert result.attempts_remaining == 3

    row = await db.get(Conversation, voice_convo.id)
    await db.refresh(row)
    assert row.identity_attempts == 0


@pytest.mark.asyncio
async def test_confirmed_wrong_guess_spends_exactly_one_strike(db, voice_convo, patient):
    await conversation_service.verify_identity(db, conversation_id=voice_convo.id, date_of_birth=WRONG_DOB)
    result = await conversation_service.verify_identity(
        db, conversation_id=voice_convo.id, date_of_birth=WRONG_DOB
    )
    assert result.outcome is IdentityOutcome.WRONG_DETAILS
    assert result.attempts_remaining == 2

    row = await db.get(Conversation, voice_convo.id)
    await db.refresh(row)
    assert row.identity_attempts == 1
    assert row.pending_dob_candidate is None  # cleared even on a wrong result


@pytest.mark.asyncio
async def test_the_gate_re_arms_for_every_new_real_attempt(db, voice_convo, patient):
    """After one confirmed-and-checked wrong guess, the NEXT real guess
    must ALSO go through a confirm round -- the gate is per-attempt, not
    spent once for the whole conversation."""
    await conversation_service.verify_identity(db, conversation_id=voice_convo.id, date_of_birth=WRONG_DOB)
    await conversation_service.verify_identity(db, conversation_id=voice_convo.id, date_of_birth=WRONG_DOB)

    # A second real guess -- even the CORRECT date -- must be pending again.
    result = await conversation_service.verify_identity(
        db, conversation_id=voice_convo.id, date_of_birth=REAL_DOB
    )
    assert result.outcome is IdentityOutcome.PENDING_CONFIRMATION

    confirmed = await conversation_service.verify_identity(
        db, conversation_id=voice_convo.id, date_of_birth=REAL_DOB
    )
    assert confirmed.outcome is IdentityOutcome.VERIFIED


@pytest.mark.asyncio
async def test_three_confirmed_wrong_guesses_still_escalates(db, voice_convo, patient):
    """The lockout threshold is unchanged -- three REAL (confirmed)
    wrong attempts, same as SMS, regardless of how many free corrections
    happened along the way."""
    for _ in range(3):
        await conversation_service.verify_identity(db, conversation_id=voice_convo.id, date_of_birth=WRONG_DOB)
        result = await conversation_service.verify_identity(
            db, conversation_id=voice_convo.id, date_of_birth=WRONG_DOB
        )

    assert result.outcome is IdentityOutcome.ESCALATED
    row = await db.get(Conversation, voice_convo.id)
    await db.refresh(row)
    assert row.status is ConversationStatus.ESCALATED
    assert row.identity_attempts == 3


@pytest.mark.asyncio
async def test_not_registered_still_requires_confirmation_first(db):
    """An unregistered phone number still goes through the pending gate
    -- the gate runs before patient lookup, by design."""
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref="+19995550000"
    )
    await db.commit()

    first = await conversation_service.verify_identity(db, conversation_id=convo.id, date_of_birth=REAL_DOB)
    assert first.outcome is IdentityOutcome.PENDING_CONFIRMATION

    second = await conversation_service.verify_identity(db, conversation_id=convo.id, date_of_birth=REAL_DOB)
    assert second.outcome is IdentityOutcome.NOT_REGISTERED
    assert second.attempts_remaining == 2


@pytest.mark.asyncio
async def test_cannot_verify_still_requires_confirmation_first(db):
    convo_patient = Patient(full_name="No DOB On File", phone="+14155559876", date_of_birth=None)
    db.add(convo_patient)
    await db.commit()

    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.VOICE, external_ref=convo_patient.phone
    )
    await db.commit()

    first = await conversation_service.verify_identity(db, conversation_id=convo.id, date_of_birth=REAL_DOB)
    assert first.outcome is IdentityOutcome.PENDING_CONFIRMATION

    second = await conversation_service.verify_identity(db, conversation_id=convo.id, date_of_birth=REAL_DOB)
    assert second.outcome is IdentityOutcome.CANNOT_VERIFY
    assert second.attempts_remaining == 0  # not a strike -- see the existing SMS behaviour


# ===================================================================== #
# SMS is provably unaffected
# ===================================================================== #


@pytest.mark.asyncio
async def test_sms_is_checked_immediately_never_pending(db, sms_convo, patient):
    """The exact pre-Phase-4 behaviour, unchanged: one call, one answer."""
    result = await conversation_service.verify_identity(
        db, conversation_id=sms_convo.id, date_of_birth=REAL_DOB
    )
    assert result.outcome is IdentityOutcome.VERIFIED
    assert result.pending_date_of_birth is None

    row = await db.get(Conversation, sms_convo.id)
    await db.refresh(row)
    assert row.pending_dob_candidate is None  # never written for SMS
    assert row.patient_id == patient.id


@pytest.mark.asyncio
async def test_sms_wrong_guess_is_checked_immediately(db, sms_convo, patient):
    result = await conversation_service.verify_identity(
        db, conversation_id=sms_convo.id, date_of_birth=WRONG_DOB
    )
    assert result.outcome is IdentityOutcome.WRONG_DETAILS
    assert result.attempts_remaining == 2

    row = await db.get(Conversation, sms_convo.id)
    await db.refresh(row)
    assert row.identity_attempts == 1
    assert row.pending_dob_candidate is None


# ===================================================================== #
# Cold identity map (standing project convention)
# ===================================================================== #


@pytest.mark.asyncio
async def test_pending_confirmation_survives_a_cold_identity_map(db, voice_convo):
    convo_id = voice_convo.id
    db.expunge_all()

    result = await conversation_service.verify_identity(
        db, conversation_id=convo_id, date_of_birth=REAL_DOB
    )
    assert result.outcome is IdentityOutcome.PENDING_CONFIRMATION


# ===================================================================== #
# Tool layer: the payload voice actually sees
# ===================================================================== #


@pytest.mark.asyncio
async def test_tool_payload_carries_pending_date_of_birth(db, voice_convo):
    ctx = ToolContext(session=db, conversation_id=voice_convo.id)
    result = await tools.dispatch(ctx, "verify_identity", {"date_of_birth": REAL_DOB.isoformat()})
    assert result["status"] == "pending_confirmation"
    assert result["pending_date_of_birth"] == REAL_DOB.isoformat()
    assert "conversation_ended" not in result


@pytest.mark.asyncio
async def test_tool_payload_omits_pending_date_of_birth_when_verified(db, voice_convo, patient):
    ctx = ToolContext(session=db, conversation_id=voice_convo.id)
    await tools.dispatch(ctx, "verify_identity", {"date_of_birth": REAL_DOB.isoformat()})
    result = await tools.dispatch(ctx, "verify_identity", {"date_of_birth": REAL_DOB.isoformat()})
    assert result["status"] == "verified"
    assert "pending_date_of_birth" not in result


@pytest.mark.asyncio
async def test_sms_tool_payload_never_carries_pending_date_of_birth(db, sms_convo, patient):
    ctx = ToolContext(session=db, conversation_id=sms_convo.id)
    result = await tools.dispatch(ctx, "verify_identity", {"date_of_birth": REAL_DOB.isoformat()})
    assert result["status"] == "verified"
    assert "pending_date_of_birth" not in result


# ===================================================================== #
# Interaction with the runaway tool-call guard (loop.py)
#
# A DOB correction cycle over voice means many separate spoken
# utterances, each its own Gather turn -- and, since Phase 4's
# poll/redirect rework, each its own VoiceTurnJob and its own
# `handle_message` call. This proves that per-turn shape actually holds:
# `calls_made` in loop.handle_message is a plain local reset to 0 at the
# top of every call, never persisted, so a caller correcting their DOB
# many times across SEPARATE turns can never accumulate toward
# max_tool_calls_per_turn -- only many tool calls WITHIN one turn can
# (see test_chatbot_loop.test_runaway_tool_calling_escalates, which
# this test deliberately complements rather than duplicates).
# ===================================================================== #


class _OneToolCallThenTextModel:
    """Each entry is consumed by ONE `handle_message` call: a single
    tool call, then (once the loop asks again) a plain text reply --
    exactly what a real model does in one turn that makes one tool call
    and then answers. Running out of scripted turns is a test bug worth
    surfacing loudly, same reasoning as test_chatbot_loop.FakeModel.
    """

    def __init__(self, turns: list[ModelTurn]):
        self._turns = list(turns)

    async def respond(self, *, transcript, system_instruction, tools):
        if not self._turns:
            raise AssertionError("model called more times than the test scripted")
        return self._turns.pop(0)


@pytest.mark.asyncio
async def test_dob_correction_across_many_turns_never_trips_the_runaway_guard(db, voice_convo, patient):
    limit = get_settings().max_tool_calls_per_turn
    attempts = limit + 1  # strictly more identity-related tool calls than the per-turn budget, spread across turns

    script: list[ModelTurn] = []
    for i in range(attempts):
        script.append(
            ModelTurn(
                tool_calls=(
                    ToolCall(name="verify_identity", arguments={"date_of_birth": f"1990-01-{i + 1:02d}"}),
                ),
            )
        )
        script.append(ModelTurn(text="I heard a date -- is that right?"))
    model = _OneToolCallThenTextModel(script)

    for _ in range(attempts):
        result = await loop.handle_message(
            db, conversation_id=voice_convo.id, text="a spoken date of birth", client=model
        )
        assert result.tool_calls_made == 1  # nowhere near this turn's own budget
        assert result.conversation_ended is False

    await db.refresh(voice_convo)
    # Never escalated by the runaway guard, and the pending-confirm gate
    # means none of these ever reached a real, strike-counted check either.
    assert voice_convo.status is ConversationStatus.ACTIVE
    assert voice_convo.identity_attempts == 0
