"""The conversation loop, driven by a scripted fake model.

NO REAL API CALL HAPPENS IN THIS FILE, by design. The loop's job is
orchestration -- when to call tools, when to stop, what to persist, what
to do when the model misbehaves -- and all of that is testable with a
model that returns exactly what each test needs. A suite that called
Gemini would cost money per run, be flaky on someone else's uptime, and
still not let us provoke the interesting cases (a model that spins, a
model that returns nothing, a model that calls a tool that does not
exist).
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from app.chatbot import loop
from app.integrations.gemini import GeminiError, ModelTurn, ToolCall
from app.models.appointment import Appointment
from app.models.conversation import Conversation
from app.models.conversation_message import ConversationMessage
from app.models.enums import (
    AppointmentStatus,
    ConversationChannel,
    ConversationStatus,
    MessageRole,
)
from app.models.patient import Patient
from app.services import conversation_service

DOB = date(1985, 3, 14)


class FakeModel:
    """Returns scripted turns; records what it was asked.

    A list of ModelTurns, consumed one per call. Running out is itself a
    test failure worth surfacing loudly rather than silently returning
    empty, since it means the loop called the model more times than the
    test expected.
    """

    def __init__(self, turns: list[ModelTurn]):
        self._turns = list(turns)
        self.calls: list[dict] = []

    async def respond(self, *, transcript, system_instruction, tools):
        self.calls.append(
            {
                "steps": list(transcript.steps),
                "system_instruction": system_instruction,
                "tool_names": [t["name"] for t in tools],
            }
        )
        if not self._turns:
            raise AssertionError("model called more times than the test scripted")
        return self._turns.pop(0)


class ExplodingModel:
    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    async def respond(self, **kwargs):
        self.calls += 1
        raise self._exc


@pytest.fixture
async def dob_patient(db):
    p = Patient(full_name="Alex Chen", phone="+14155550123", date_of_birth=DOB)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


@pytest.fixture
async def convo(db, dob_patient):
    c = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=dob_patient.phone
    )
    await db.commit()
    return c


async def messages(db, conversation_id):
    return list(
        (
            await db.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.sequence)
            )
        ).all()
    )


# ===================================================================== #
# Basic turn handling
# ===================================================================== #


@pytest.mark.asyncio
async def test_plain_reply_is_returned_and_persisted(db, convo):
    model = FakeModel([ModelTurn(text="Hi! How can I help?")])
    result = await loop.handle_message(
        db, conversation_id=convo.id, text="hello", client=model
    )

    assert result.reply == "Hi! How can I help?"
    assert result.tool_calls_made == 0
    assert result.used_fallback is False

    rows = await messages(db, convo.id)
    assert [r.role for r in rows] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert rows[0].content == "hello"
    assert rows[1].content == "Hi! How can I help?"


@pytest.mark.asyncio
async def test_transcript_is_replayed_on_the_next_message(db, convo):
    model = FakeModel([ModelTurn(text="First"), ModelTurn(text="Second")])
    await loop.handle_message(db, conversation_id=convo.id, text="one", client=model)
    await loop.handle_message(db, conversation_id=convo.id, text="two", client=model)

    # The second call must carry the whole prior exchange -- the provider
    # holds nothing for us (store=False), so if this regresses the bot
    # silently loses its memory mid-conversation.
    second_call_steps = model.calls[1]["steps"]
    rendered = json.dumps(second_call_steps)
    assert "one" in rendered
    assert "First" in rendered
    assert "two" in rendered


@pytest.mark.asyncio
async def test_system_prompt_and_tools_are_sent(db, convo):
    model = FakeModel([ModelTurn(text="ok")])
    await loop.handle_message(db, conversation_id=convo.id, text="hi", client=model)

    call = model.calls[0]
    assert "verify_identity" in call["tool_names"]
    assert "appointment assistant" in call["system_instruction"].lower()


# ===================================================================== #
# Tool round trips
# ===================================================================== #


@pytest.mark.asyncio
async def test_tool_call_is_executed_and_result_fed_back(db, convo, dob_patient):
    model = FakeModel(
        [
            ModelTurn(tool_calls=(ToolCall(name="verify_identity", arguments={"date_of_birth": DOB.isoformat()}, call_id="c1"),)),
            ModelTurn(text="Thanks Alex, you're verified."),
        ]
    )
    result = await loop.handle_message(
        db, conversation_id=convo.id, text="my dob is 14 march 1985", client=model
    )

    assert result.tool_calls_made == 1
    assert result.reply == "Thanks Alex, you're verified."

    # The tool actually ran against the real service.
    await db.refresh(convo)
    assert convo.patient_id == dob_patient.id

    # And the model saw the result on its second call.
    assert "verified" in json.dumps(model.calls[1]["steps"])

    rows = await messages(db, convo.id)
    assert [r.role for r in rows] == [
        MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL, MessageRole.ASSISTANT
    ]
    assert rows[2].tool_name == "verify_identity"


@pytest.mark.asyncio
async def test_full_booking_conversation(db, convo, dob_patient, doctor, time_slot):
    """Verify -> check -> propose -> confirm, across four inbound messages."""
    m1 = FakeModel([
        ModelTurn(tool_calls=(ToolCall(name="verify_identity", arguments={"date_of_birth": DOB.isoformat()}),)),
        ModelTurn(text="Thanks Alex. What day suits?"),
    ])
    await loop.handle_message(db, conversation_id=convo.id, text="dob 1985-03-14", client=m1)

    on_date = time_slot.starts_at.date().isoformat()
    m2 = FakeModel([
        ModelTurn(tool_calls=(ToolCall(name="check_availability", arguments={"on_date": on_date}),)),
        ModelTurn(text="I have one slot that day."),
    ])
    await loop.handle_message(db, conversation_id=convo.id, text="any time that day?", client=m2)

    m3 = FakeModel([
        ModelTurn(tool_calls=(ToolCall(name="propose_booking", arguments={"time_slot_id": str(time_slot.id)}),)),
        ModelTurn(text="Shall I book it?"),
    ])
    await loop.handle_message(db, conversation_id=convo.id, text="that one please", client=m3)

    # Nothing booked yet -- the two-phase split survives the loop.
    assert (await db.scalars(select(Appointment))).all() == []

    proposal_payload = json.loads((await messages(db, convo.id))[-2].tool_payload)
    proposal_id = proposal_payload["result"]["proposal_id"]

    m4 = FakeModel([
        ModelTurn(tool_calls=(ToolCall(name="confirm_booking", arguments={"proposal_id": proposal_id}),)),
        ModelTurn(text="Booked."),
    ])
    result = await loop.handle_message(db, conversation_id=convo.id, text="yes", client=m4)

    assert result.reply == "Booked."
    appointment = await db.scalar(select(Appointment))
    assert appointment is not None
    assert appointment.patient_id == dob_patient.id


@pytest.mark.asyncio
async def test_parallel_tool_calls_in_one_turn_all_run(db, convo, time_slot):
    on_date = time_slot.starts_at.date().isoformat()
    model = FakeModel([
        ModelTurn(tool_calls=(
            ToolCall(name="verify_identity", arguments={"date_of_birth": DOB.isoformat()}),
            ToolCall(name="check_availability", arguments={"on_date": on_date}),
        )),
        ModelTurn(text="done"),
    ])
    result = await loop.handle_message(db, conversation_id=convo.id, text="hi", client=model)
    assert result.tool_calls_made == 2


# ===================================================================== #
# Misbehaving models
# ===================================================================== #


@pytest.mark.asyncio
async def test_empty_model_reply_becomes_a_fallback_not_an_empty_sms(db, convo):
    model = FakeModel([ModelTurn(text="   ")])
    result = await loop.handle_message(db, conversation_id=convo.id, text="hi", client=model)
    assert result.used_fallback is True
    assert result.reply.strip()


@pytest.mark.asyncio
async def test_provider_error_returns_a_fallback_without_ending_the_conversation(db, convo):
    """A provider blip is not a reason to permanently hand off."""
    model = ExplodingModel(GeminiError("503 upstream", retryable=True))
    result = await loop.handle_message(db, conversation_id=convo.id, text="hi", client=model)

    assert result.used_fallback is True
    assert result.conversation_ended is False
    await db.refresh(convo)
    assert convo.status is ConversationStatus.ACTIVE


@pytest.mark.asyncio
async def test_runaway_tool_calling_escalates(db, convo):
    """A model going in circles is the 'bot is stuck' escalation case."""
    from app.core.config import get_settings

    limit = get_settings().max_tool_calls_per_turn
    spinning = [
        ModelTurn(tool_calls=(ToolCall(name="check_availability", arguments={"on_date": "2026-10-01"}),))
        for _ in range(limit + 2)
    ]
    model = FakeModel(spinning)
    result = await loop.handle_message(db, conversation_id=convo.id, text="hi", client=model)

    assert result.conversation_ended is True
    await db.refresh(convo)
    assert convo.status is ConversationStatus.ESCALATED
    assert result.tool_calls_made <= limit


@pytest.mark.asyncio
async def test_hallucinated_tool_is_reported_to_the_model_not_crashed(db, convo):
    model = FakeModel([
        ModelTurn(tool_calls=(ToolCall(name="delete_everything", arguments={}),)),
        ModelTurn(text="Sorry, I can't do that."),
    ])
    result = await loop.handle_message(db, conversation_id=convo.id, text="hi", client=model)

    assert result.reply == "Sorry, I can't do that."
    assert "unknown_tool" in json.dumps(model.calls[1]["steps"])


# ===================================================================== #
# Conversation lifecycle
# ===================================================================== #


@pytest.mark.asyncio
async def test_message_to_an_escalated_conversation_does_not_reach_the_model(db, convo):
    await conversation_service.escalate(db, conversation_id=convo.id, reason="test")
    model = FakeModel([ModelTurn(text="should not be called")])

    result = await loop.handle_message(db, conversation_id=convo.id, text="hello?", client=model)

    assert result.conversation_ended is True
    assert model.calls == []  # not one token spent on a closed conversation


@pytest.mark.asyncio
async def test_escalation_mid_turn_is_reported_as_ended(db, convo):
    model = FakeModel([
        ModelTurn(tool_calls=(ToolCall(name="request_human", arguments={"reason": "wants a person"}),)),
        ModelTurn(text="I'll pass you to someone."),
    ])
    result = await loop.handle_message(db, conversation_id=convo.id, text="human please", client=model)

    assert result.conversation_ended is True
    await db.refresh(convo)
    assert convo.status is ConversationStatus.ESCALATED


# ===================================================================== #
# Storage properties
# ===================================================================== #


@pytest.mark.asyncio
async def test_message_content_is_encrypted_at_rest(db, convo):
    from sqlalchemy import text as sql_text

    secret = "the chest pain is back and I am worried"
    model = FakeModel([ModelTurn(text="I'll get you booked in.")])
    await loop.handle_message(db, conversation_id=convo.id, text=secret, client=model)

    stored = (
        await db.execute(
            sql_text(
                "SELECT content FROM conversation_messages "
                "WHERE conversation_id = :c ORDER BY sequence LIMIT 1"
            ),
            {"c": convo.id},
        )
    ).scalar_one()

    assert secret not in stored
    assert stored.startswith("gAAAAA")  # Fernet ciphertext


@pytest.mark.asyncio
async def test_sequence_is_contiguous_and_ordered(db, convo):
    model = FakeModel([
        ModelTurn(tool_calls=(ToolCall(name="check_availability", arguments={"on_date": "2026-10-01"}),)),
        ModelTurn(text="nothing free"),
    ])
    await loop.handle_message(db, conversation_id=convo.id, text="hi", client=model)

    rows = await messages(db, convo.id)
    assert [r.sequence for r in rows] == list(range(len(rows)))


@pytest.mark.asyncio
async def test_history_is_truncated_to_the_configured_limit(db, convo):
    from app.core.config import get_settings

    limit = get_settings().conversation_history_limit
    for i in range(limit):
        await loop.handle_message(
            db, conversation_id=convo.id, text=f"msg{i}", client=FakeModel([ModelTurn(text=f"r{i}")])
        )

    final = FakeModel([ModelTurn(text="last")])
    await loop.handle_message(db, conversation_id=convo.id, text="final", client=final)

    assert len(final.calls[0]["steps"]) <= limit
    # The OLDEST message must have fallen off, and the newest must be there.
    rendered = json.dumps(final.calls[0]["steps"])
    assert "final" in rendered
    assert "msg0" not in rendered


@pytest.mark.asyncio
async def test_loop_works_with_a_cold_identity_map(db, convo, time_slot):
    """The webhook opens a fresh session per inbound message."""
    db.expunge_all()
    model = FakeModel([
        ModelTurn(tool_calls=(ToolCall(name="verify_identity", arguments={"date_of_birth": DOB.isoformat()}),)),
        ModelTurn(text="verified"),
    ])
    result = await loop.handle_message(
        db, conversation_id=convo.id, text="dob", client=model
    )
    assert result.reply == "verified"
