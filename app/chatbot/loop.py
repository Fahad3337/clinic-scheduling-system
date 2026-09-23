"""One inbound message in, one reply out.

THE SHAPE, and why it is not an agent framework:

    load transcript -> ask model -> run tools -> ask model -> ... -> reply

That is the whole thing, and it runs INSIDE a single webhook request. It
is deliberately not `runner.until_done()`: a booking spans conversational
turns (propose, tell the patient, wait for "yes", confirm), and those
turns arrive as separate HTTP requests hours apart. There is no loop
object that can stay alive across them. What lives across them is rows in
Postgres, which is why the transcript is rebuilt from the database on
every message rather than held in memory.

EVERY SECURITY PROPERTY IS SOMEWHERE ELSE. This module chooses what to
say; it cannot choose what may be done. Tool dispatch enforces identity,
scope and the propose/confirm split (chatbot/tools.py), and the service
layer enforces the booking invariants underneath that (Phase 1). A
compromised or confused model running this loop can waste tokens and
produce a bad reply. It cannot book for the wrong patient.

A CONSEQUENCE OF THAT WORTH STATING: because identity and proposals are
server-side state rather than context, TRUNCATING THE TRANSCRIPT CANNOT
CAUSE A SECURITY FAILURE. Dropping the turn where the caller verified does
not un-verify them, and dropping a proposal id does not make it
confirmable by someone else. Truncation degrades the conversation, never
the guarantees -- which is what makes a simple message cap safe here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chatbot import tools
from app.chatbot.prompts import ESCALATION_REPLY, fallback_reply_for, system_prompt_for
from app.chatbot.tools import ToolContext
from app.core.config import get_settings
from app.integrations.gemini import (
    GeminiClient,
    GeminiError,
    ModelTurn,
    ToolCall,
    Transcript,
)
from app.models.conversation import Conversation
from app.models.conversation_message import ConversationMessage
from app.models.enums import ConversationStatus, MessageRole
from app.services import conversation_service

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    reply: str
    conversation_ended: bool = False
    tool_calls_made: int = 0
    # True when the reply is a fallback rather than something the model
    # actually produced -- lets the transport log it and lets tests tell
    # "the bot answered" from "the bot failed politely".
    used_fallback: bool = False


async def _next_sequence(session: AsyncSession, conversation_id: UUID) -> int:
    current = await session.scalar(
        select(func.max(ConversationMessage.sequence)).where(
            ConversationMessage.conversation_id == conversation_id
        )
    )
    return 0 if current is None else current + 1


async def _append(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    role: MessageRole,
    content: str | None = None,
    tool_payload: str | None = None,
    tool_name: str | None = None,
    provider_message_id: str | None = None,
) -> ConversationMessage:
    message = ConversationMessage(
        conversation_id=conversation_id,
        sequence=await _next_sequence(session, conversation_id),
        role=role,
        content=content,
        tool_payload=tool_payload,
        tool_name=tool_name,
        provider_message_id=provider_message_id,
    )
    session.add(message)
    await session.flush()
    return message


async def _load_transcript(session: AsyncSession, conversation_id: UUID) -> Transcript:
    """Rebuild the provider-shaped transcript from our own rows.

    Truncation is a plain cap on the most recent messages. Crude on
    purpose: summarising or compacting the history would mean a second
    model call to produce a lossy artefact that is then the thing the bot
    reasons about, and for a scheduling conversation that is not worth
    the failure modes. See the note in the module docstring about why
    dropping old turns is safe.
    """
    settings = get_settings()
    rows = list(
        (
            await session.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.sequence.desc())
                .limit(settings.conversation_history_limit)
            )
        ).all()
    )
    rows.reverse()

    transcript = Transcript()
    for row in rows:
        if row.role is MessageRole.USER and row.content:
            transcript.add_user_text(row.content)
        elif row.role is MessageRole.ASSISTANT:
            # Replay the provider's OWN step objects verbatim whenever we
            # have them -- see ModelTurn.raw_steps. Rebuilding a model
            # turn from our parsed view of it drops step types we do not
            # model (a live response carries a `thought` step with an
            # opaque signature), and bakes in our guess at the step type
            # name, which was wrong until it was observed.
            if row.tool_payload:
                transcript.add_model_steps(json.loads(row.tool_payload))
            elif row.content:
                # Pre-raw-steps rows only.
                transcript.add_model_text(row.content)
        elif row.role is MessageRole.TOOL and row.tool_payload:
            payload = json.loads(row.tool_payload)
            transcript.add_tool_result(
                ToolCall(name=payload.get("name", row.tool_name or ""), arguments={}, call_id=payload.get("call_id")),
                payload.get("result", {}),
            )
    return transcript


async def handle_message(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    text: str,
    provider_message_id: str | None = None,
    client: GeminiClient | None = None,
) -> TurnResult:
    """Process one inbound patient message and return what to send back."""
    settings = get_settings()
    client = client or GeminiClient()

    # A message arriving on a finished conversation is not an error worth
    # raising at the transport -- it is a patient replying to a thread the
    # system already closed. Say something true and stop.
    try:
        conversation = await conversation_service.load_active_conversation(session, conversation_id)
    except Exception:
        logger.info("message for non-active conversation %s", conversation_id)
        return TurnResult(reply=ESCALATION_REPLY, conversation_ended=True)

    await _append(
        session,
        conversation_id=conversation_id,
        role=MessageRole.USER,
        content=text,
        provider_message_id=provider_message_id,
    )
    conversation.last_activity_at = datetime.now(UTC)
    await session.commit()

    ctx = ToolContext(session=session, conversation_id=conversation_id)
    declarations = tools.function_declarations()
    calls_made = 0

    for _ in range(settings.max_tool_calls_per_turn + 1):
        transcript = await _load_transcript(session, conversation_id)
        try:
            turn: ModelTurn = await client.respond(
                transcript=transcript,
                system_instruction=system_prompt_for(conversation.channel),
                tools=declarations,
            )
        except GeminiError as exc:
            # The model is unreachable or refused. The patient is waiting;
            # answer honestly rather than hanging or leaking an error.
            # NOT escalated: a provider blip is not a reason to end the
            # conversation permanently, and the next message may work.
            logger.warning("gemini call failed for conversation %s: %s", conversation_id, exc)
            return TurnResult(
                reply=fallback_reply_for(conversation.channel), used_fallback=True, tool_calls_made=calls_made
            )

        if not turn.wants_tools:
            reply = turn.text.strip()
            if not reply:
                # Nothing usable. Do not send an empty reply.
                logger.warning("empty model turn for conversation %s", conversation_id)
                return TurnResult(
                    reply=fallback_reply_for(conversation.channel), used_fallback=True, tool_calls_made=calls_made
                )

            # BOTH columns, deliberately: `content` is the human-readable
            # record of what the patient was actually told (the thing an
            # audit or a complaint needs), `tool_payload` is the verbatim
            # provider steps used to replay this turn faithfully. They
            # serve different readers and neither substitutes for the other.
            await _append(
                session,
                conversation_id=conversation_id,
                role=MessageRole.ASSISTANT,
                content=reply,
                tool_payload=json.dumps(list(turn.raw_steps)) if turn.raw_steps else None,
            )
            await session.commit()
            ended = await _is_ended(session, conversation_id)
            return TurnResult(reply=reply, conversation_ended=ended, tool_calls_made=calls_made)

        if calls_made + len(turn.tool_calls) > settings.max_tool_calls_per_turn:
            # The model is going in circles. THIS IS AN ESCALATION, not a
            # retry: a bot that has called five tools without producing an
            # answer is exactly the "the bot is stuck" case the escalation
            # path exists for, and letting it keep spending calls on a
            # waiting patient is worse than handing over.
            logger.warning(
                "conversation %s exceeded %d tool calls; escalating",
                conversation_id,
                settings.max_tool_calls_per_turn,
            )
            await conversation_service.escalate(
                session,
                conversation_id=conversation_id,
                reason=f"assistant exceeded {settings.max_tool_calls_per_turn} tool calls without replying",
            )
            return TurnResult(reply=ESCALATION_REPLY, conversation_ended=True, tool_calls_made=calls_made)

        await _append(
            session,
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            # Verbatim steps when the provider gave them; otherwise
            # synthesize from the parsed calls so the turn is not simply
            # missing from the replayed history.
            tool_payload=json.dumps(
                list(turn.raw_steps)
                if turn.raw_steps
                else [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "function_call",
                                "name": c.name,
                                "arguments": c.arguments,
                                **({"call_id": c.call_id} if c.call_id else {}),
                            }
                            for c in turn.tool_calls
                        ],
                    }
                ]
            ),
        )

        for call in turn.tool_calls:
            result = await tools.dispatch(ctx, call.name, call.arguments)
            calls_made += 1
            await _append(
                session,
                conversation_id=conversation_id,
                role=MessageRole.TOOL,
                tool_name=call.name,
                tool_payload=json.dumps(
                    {"name": call.name, "call_id": call.call_id, "result": result}
                ),
            )
        await session.commit()

    # Loop ran out without the model producing a reply.
    logger.warning("conversation %s produced no reply within the tool budget", conversation_id)
    await conversation_service.escalate(
        session,
        conversation_id=conversation_id,
        reason="assistant did not produce a reply within the tool budget",
    )
    return TurnResult(reply=ESCALATION_REPLY, conversation_ended=True, tool_calls_made=calls_made)


async def _is_ended(session: AsyncSession, conversation_id: UUID) -> bool:
    """Whether a tool ended the conversation during this turn.

    Read back from the row rather than tracked in a local: the escalation
    could have been triggered inside any tool (request_human, or three
    failed identity attempts), and the loop should not have to know which
    tools can end a conversation.
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return True
    await session.refresh(conversation)
    return conversation.status is not ConversationStatus.ACTIVE
