"""Gemini client: the ONLY place the provider's wire format appears.

Everything above this file speaks in the small domain types defined here
(`Step`, `ToolCall`, `ModelTurn`), never in provider JSON. That boundary is
load-bearing rather than tidy: the wire shape below was verified against
Google's live documentation on 2026-09-22 and had ALREADY changed from
what was current at training time (the older
`:generateContent` + `contents`/`parts`/`functionCall` shape is gone,
replaced by `/v1beta/interactions` with typed steps). It will change
again. When it does, this file changes and the loop does not.

TWO DECISIONS WORTH NAMING EXPLICITLY.

1. `store=False` -- WE DO NOT USE PROVIDER-SIDE CONVERSATION STATE.
   The API offers `previous_interaction_id`, which would let us send only
   the newest message and have Google retain the thread. We decline it,
   for two independent reasons and either would be sufficient:

     a) PHI. The conversation contains patient-authored clinical text
        ("the chest pain is back"). Opting into server-side retention of
        that, for an undocumented period, is a data-protection decision
        nobody at this clinic has made and is not one to make by
        accepting a default. With store=False our database is the only
        copy, which is what the encrypted transcript table is for.
     b) It would not work anyway. SMS conversations span hours or days
        across stateless webhooks, retention of an interaction id is
        undocumented, and a thread whose continuation silently expires
        mid-booking is a worse failure than resending history.

   The cost is tokens: we resend the transcript every turn. That is the
   price, it is bounded by the truncation in the loop, and it is the
   right trade for a medical conversation.

2. Raw httpx, not the google-genai SDK. Same reasoning as every other
   integration here (Calendar, Twilio, SendGrid): the surface used is one
   POST, the SDK is another dependency to pin and audit, and this project
   has already been bitten by unused Google client libraries lingering in
   its dependency list.

NOT YET SMOKE-TESTED AGAINST THE LIVE API: no Gemini API key is
configured. The shape is transcribed from current documentation and
parsed defensively (see `_parse_step`), but "documented" is not "observed"
-- the same distinction that made the Google Calendar fixtures worth
recording. First real call must be treated as a verification step.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"

logger = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    """Any failure talking to the model provider."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(message)


class GeminiNotConfiguredError(GeminiError):
    """No API key. A deployment problem, not a conversation problem."""


# --------------------------------------------------------------------- #
# Domain types -- what the loop speaks. Provider-shaped JSON never
# escapes this module.
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    # The provider's correlation id, echoed back with the result. Optional
    # because it is the provider's to define and we must not break if a
    # response omits it.
    call_id: str | None = None


@dataclass(frozen=True)
class ModelTurn:
    """One response from the model: some text, some tool calls, or both."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    # The provider's own step objects, VERBATIM, for replaying this turn
    # back on the next request.
    #
    # READ THIS BEFORE "SIMPLIFYING" THIS FIELD AWAY.
    #
    # It is tempting to drop raw_steps and rebuild a model turn from
    # `text` and `tool_calls`, since those carry everything we interpret.
    # That is precisely the bug this field exists to prevent, and the
    # history is worth knowing because the wrong version LOOKED FINE.
    #
    # An earlier version synthesized the turn itself and guessed the step
    # type as "model_response". Two things were wrong with it, and only
    # one of them was the name:
    #
    #   1. The type was wrong -- the live API uses "model_output".
    #   2. MORE IMPORTANTLY, synthesis silently DROPS step types we do
    #      not model. A real response (observed 2026-09-22) came back as
    #      TWO steps: a `thought` step carrying an opaque `signature` and
    #      no content, then the `model_output` step with the text. Any
    #      reconstruction from parsed fields discards the thought step
    #      entirely, because we have nowhere to put it.
    #
    # Fixing only (1) -- renaming the string while still synthesizing --
    # would have passed every smoke test and still dropped `thought`
    # steps on every replay in production, silently, forever. The
    # type-name correction was incidental; REPLAYING WHAT WE WERE GIVEN
    # is the actual fix, and it is what the provider's own documentation
    # does (`history.append(step.model_dump())`).
    #
    # The general form: when a provider hands you an extensible
    # structure, store and echo it verbatim rather than modelling it.
    # Your model of it is a snapshot of what you understood on one day;
    # theirs is what is actually true. Verbatim replay also means the
    # next shape change does not break this code path at all.
    raw_steps: tuple[dict[str, Any], ...] = ()

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class Transcript:
    """The conversation as the provider wants to receive it.

    Built by the loop from our own stored messages, never accumulated in
    memory across requests -- the process handling the next inbound SMS is
    probably a different one.
    """

    steps: list[dict[str, Any]] = field(default_factory=list)

    def add_user_text(self, text: str) -> None:
        self.steps.append({"type": "user_input", "content": [{"type": "text", "text": text}]})

    def add_model_steps(self, steps: list[dict[str, Any]]) -> None:
        """Replay a model turn EXACTLY as the provider returned it.

        The preferred path, and the one the provider's own docs use
        (`history.append(step.model_dump())`). Nothing is interpreted, so
        nothing can be lost or mis-shaped.
        """
        self.steps.extend(steps)

    def add_model_tool_calls(self, calls: list[ToolCall]) -> None:
        """Synthesize a model turn that made tool calls. FALLBACK ONLY.

        Same caveat as add_model_text: prefer add_model_steps, which
        replays what the provider actually sent. This exists for turns
        recorded without raw steps.
        """
        self.steps.append(
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "function_call",
                        "name": call.name,
                        "arguments": call.arguments,
                        **({"call_id": call.call_id} if call.call_id else {}),
                    }
                    for call in calls
                ],
            }
        )

    def add_model_text(self, text: str) -> None:
        """Synthesize a model turn from text alone. FALLBACK ONLY.

        Used for transcript rows written before raw steps were stored.
        The type here -- `model_output` -- is OBSERVED from a live
        response (2026-09-22), not inferred: an earlier version of this
        file guessed `model_response`, which does not appear anywhere in
        the API and was never exercised, because a single-turn smoke test
        does not replay a model turn. Prefer add_model_steps.
        """
        self.steps.append({"type": "model_output", "content": [{"type": "text", "text": text}]})

    def add_tool_result(self, call: ToolCall, result: dict[str, Any]) -> None:
        self.steps.append(
            {
                "type": "function_result",
                "name": call.name,
                **({"call_id": call.call_id} if call.call_id else {}),
                "result": [{"type": "text", "text": json.dumps(result)}],
            }
        )


class GeminiClient:
    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client

    async def respond(
        self,
        *,
        transcript: Transcript,
        system_instruction: str,
        tools: list[dict[str, Any]],
    ) -> ModelTurn:
        settings = get_settings()
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise GeminiNotConfiguredError("GEMINI_API_KEY is not configured")

        body: dict[str, Any] = {
            "model": settings.gemini_model,
            # See decision 1 in the module docstring. Do not remove.
            "store": False,
            "system_instruction": system_instruction,
            "input": transcript.steps,
            "tools": [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                }
                for tool in tools
            ],
        }

        owns = self._http is None
        http = self._http or httpx.AsyncClient(timeout=settings.gemini_timeout_seconds)
        try:
            response = await http.post(
                GEMINI_ENDPOINT, json=body, headers={"x-goog-api-key": api_key}
            )
        except httpx.HTTPError as exc:
            raise GeminiError(f"network error: {exc}", retryable=True) from exc
        finally:
            if owns:
                await http.aclose()

        if response.status_code != 200:
            detail = response.text[:300]
            # 429/5xx are worth another go on the next inbound message;
            # 4xx means our request is wrong and retrying sends the same
            # broken thing again.
            retryable = response.status_code == 429 or response.status_code >= 500
            raise GeminiError(
                f"gemini {response.status_code}: {detail}",
                status_code=response.status_code,
                retryable=retryable,
            )

        return _parse_turn(response.json())


def _parse_turn(payload: dict[str, Any]) -> ModelTurn:
    """Pull text and tool calls out of a response, tolerantly.

    DELIBERATELY FORGIVING about the envelope. The provider has already
    changed this shape once since training data, the exact key holding
    output steps is the sort of thing that gets renamed, and a KeyError
    deep in a webhook is a much worse failure than an empty turn we can
    detect and handle. Anything we cannot interpret becomes an empty
    ModelTurn, which the loop treats as "the model said nothing usable"
    and answers with a fallback rather than crashing on a patient.
    """
    steps: list[dict[str, Any]] = []
    # "steps" first: that is the key the live API actually uses (observed
    # 2026-09-22). The others are kept as tolerant fallbacks only.
    for key in ("steps", "output", "response", "candidates"):
        value = payload.get(key)
        if isinstance(value, list):
            steps = value
            break

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    def consume(block: dict[str, Any]) -> None:
        block_type = block.get("type")
        if block_type == "text" and block.get("text"):
            text_parts.append(block["text"])
        elif block_type in ("function_call", "functionCall"):
            raw_args = block.get("arguments", block.get("args", {}))
            if isinstance(raw_args, str):
                # Some providers hand back the argument object as a JSON
                # string. Parse rather than assume -- and a malformed one
                # becomes empty args, which the tool layer will reject
                # cleanly as invalid_arguments.
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    logger.warning("could not parse tool arguments as JSON")
                    raw_args = {}
            if not isinstance(raw_args, dict):
                raw_args = {}
            tool_calls.append(
                ToolCall(
                    name=block.get("name", ""),
                    arguments=raw_args,
                    call_id=block.get("call_id") or block.get("id"),
                )
            )

    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("type") in ("function_call", "functionCall"):
            consume(step)
            continue
        content = step.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    consume(block)
        elif isinstance(content, str):
            text_parts.append(content)

    return ModelTurn(
        text="\n".join(text_parts).strip(),
        tool_calls=tuple(tool_calls),
        raw_steps=tuple(s for s in steps if isinstance(s, dict)),
    )
