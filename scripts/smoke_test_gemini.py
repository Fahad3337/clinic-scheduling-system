#!/usr/bin/env python
"""Verify the transcribed Gemini wire format against the live API.

A DIAGNOSTIC LADDER, deliberately ordered so a failure localises itself:

  Step 1  raw POST, no tools, one turn  -> is the envelope right at all?
                                           (url, header, model id, `input`,
                                            `store`, `system_instruction`)
  Step 2  dump the RAW response JSON    -> what are the ACTUAL step types?
                                           This is the one unverified claim
                                           in the whole transcription; see
                                           the note on model turns below.
  Step 3  two-turn replay               -> does our reconstructed history
                                           shape get accepted?
  Step 4  through loop.handle_message   -> does the real code path work,
                                           tools advertised and all?

If step 1 fails, the transcription is wrong. If step 1 passes and step 4
fails, the transcription is right and our code is wrong. That separation
is the entire point of running these in order rather than jumping to the
end.

Run:  python -m scripts.smoke_test_gemini
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"


def _key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("GEMINI_API_KEY is not set in the environment.", file=sys.stderr)
        sys.exit(2)
    return key


async def step_1_and_2_raw_call(model: str) -> dict | None:
    print("=" * 70)
    print("STEP 1/2: raw single-turn call, no tools")
    print("=" * 70)
    body = {
        "model": model,
        "store": False,
        "system_instruction": "You are a terse assistant. Reply with one short sentence.",
        "input": [{"type": "user_input", "content": [{"type": "text", "text": "Say hello."}]}],
    }
    print(f"POST {ENDPOINT}")
    print(f"request body:\n{json.dumps(body, indent=2)}\n")

    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(ENDPOINT, json=body, headers={"x-goog-api-key": _key()})

    print(f"HTTP {response.status_code}")
    if response.status_code != 200:
        print(f"body: {response.text[:1500]}")
        print("\n>>> STEP 1 FAILED: the transcribed request shape was rejected.")
        return None

    payload = response.json()
    print("RAW RESPONSE JSON (this is the ground truth for step types):")
    print(json.dumps(payload, indent=2)[:4000])

    # The load-bearing unknown: what `type` does a model-produced step
    # actually carry? Our code currently guesses "model_response".
    print("\n--- observed top-level keys:", list(payload.keys()))
    for key in ("output", "steps", "response", "candidates"):
        if isinstance(payload.get(key), list):
            print(f"--- output steps found under key: {key!r}")
            for i, step in enumerate(payload[key]):
                if isinstance(step, dict):
                    print(f"    step[{i}] type = {step.get('type')!r}  keys = {list(step)}")
            break
    else:
        print("--- WARNING: no list-valued output key found among the ones we parse")
    return payload


async def step_3_two_turn(model: str, first_response: dict) -> bool:
    """Echo the model's own returned steps back, as the docs' own pattern does."""
    print("\n" + "=" * 70)
    print("STEP 3: two-turn replay (echoing the model's returned steps verbatim)")
    print("=" * 70)

    returned_steps: list = []
    for key in ("output", "steps", "response", "candidates"):
        if isinstance(first_response.get(key), list):
            returned_steps = first_response[key]
            break

    body = {
        "model": model,
        "store": False,
        "system_instruction": "You are a terse assistant. Reply with one short sentence.",
        "input": [
            {"type": "user_input", "content": [{"type": "text", "text": "Say hello."}]},
            *returned_steps,
            {"type": "user_input", "content": [{"type": "text", "text": "What did you just say?"}]},
        ],
    }
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(ENDPOINT, json=body, headers={"x-goog-api-key": _key()})

    print(f"HTTP {response.status_code}")
    print(response.text[:1500])
    if response.status_code != 200:
        print("\n>>> STEP 3 FAILED: replaying returned steps was rejected.")
        return False
    print("\n>>> STEP 3 OK: history replay accepted.")
    return True


async def step_4_through_the_loop() -> bool:
    print("\n" + "=" * 70)
    print("STEP 4: through loop.handle_message (real code path, tools advertised)")
    print("=" * 70)

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.chatbot import loop
    from app.core.config import get_settings
    from app.db.session import build_engine, build_session_factory
    from app.models import Base
    from app.models.enums import ConversationChannel
    from app.services import conversation_service

    url = os.environ["SMOKE_DATABASE_URL"]
    engine = build_engine(url, get_settings())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)

    try:
        async with factory() as session:
            convo = await conversation_service.get_or_create_conversation(
                session, channel=ConversationChannel.SMS, external_ref="+14155550000"
            )
            await session.commit()
            result = await loop.handle_message(
                session, conversation_id=convo.id, text="hello"
            )
        print(f"turn 1 reply   : {result.reply!r}")
        print(f"used_fallback  : {result.used_fallback}")
        if result.used_fallback:
            print("\n>>> STEP 4 FAILED: the loop fell back on turn 1.")
            return False

        # STEP 5 -- the one the original single-turn plan could not catch.
        #
        # A first message never replays a model turn, so it cannot
        # exercise the model-turn step type at all. This second message
        # forces the loop to rebuild a transcript CONTAINING a prior
        # model turn and send it back. That is precisely where the
        # invented `model_response` type lived, undetected, while a
        # single-turn smoke test passed.
        print("\n" + "=" * 70)
        print("STEP 5: SECOND message -- exercises model-turn replay through our code")
        print("=" * 70)
        async with factory() as session:
            convo2 = await conversation_service.get_or_create_conversation(
                session, channel=ConversationChannel.SMS, external_ref="+14155550000"
            )
            await session.commit()
            result2 = await loop.handle_message(
                session,
                conversation_id=convo2.id,
                text="What did you just say to me?",
            )
        print(f"turn 2 reply   : {result2.reply!r}")
        print(f"used_fallback  : {result2.used_fallback}")
        if result2.used_fallback:
            print("\n>>> STEP 5 FAILED: replaying our stored model turn was rejected.")
            return False
        print("\n>>> STEPS 4+5 OK: full loop works, including multi-turn replay.")
        return True
    finally:
        await engine.dispose()


async def main() -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    print(f"model under test: {model}\n")

    payload = await step_1_and_2_raw_call(model)
    if payload is None:
        return 1
    await step_3_two_turn(model, payload)
    if os.environ.get("SMOKE_DATABASE_URL"):
        ok = await step_4_through_the_loop()
        return 0 if ok else 1
    print("\n(SMOKE_DATABASE_URL not set -- skipping step 4)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
