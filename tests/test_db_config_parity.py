"""Proves the test engine actually inherits production's connection-level
settings -- not just that build_engine/build_session_factory exist, but
that the server-side guards they configure are genuinely enforced.

WHY THIS FILE EXISTS: the 2026-09-21 config audit (see
test-prod-env-parity in project memory) found that `lock_timeout` and
`statement_timeout` -- set via `connect_args.server_settings` in
app/db/session.py -- had NEVER reached a test connection. The test engine
built its own bare `create_async_engine(url)` with no connect_args at all.
That meant this codebase's central concurrency claim ("a stuck FOR UPDATE
fails fast instead of hanging forever") had zero test coverage: a
regression here would make the test suite hang, not fail.

Fixed by having both engines come from the same `build_engine()` function.
This file is the proof that the fix actually closes the gap, not just that
the refactor compiles.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.models.time_slot import TimeSlot


def _pg_duration_to_ms(value: str) -> int:
    """Parse a Postgres GUC duration string ('5s', '3000ms', '1min') to ms.

    WHY THIS EXISTS: `SHOW statement_timeout` does not echo back the exact
    string we configured it with -- Postgres normalizes to whatever unit
    divides evenly (our 5000ms comes back as '5s'). A test asserting
    literal string equality against "5000ms" fails even when the setting
    is correctly applied; comparing the parsed millisecond value is what
    actually proves the configured number reached the connection.
    """
    value = value.strip()
    if value.endswith("ms"):
        return int(value[:-2])
    if value.endswith("min"):
        return int(value[:-3]) * 60_000
    if value.endswith("s"):
        return int(value[:-1]) * 1_000
    return int(value)  # bare number = milliseconds, per Postgres's own convention


@pytest.mark.asyncio
async def test_lock_timeout_is_actually_enforced_on_test_connections(
    session_factory, time_slot
):
    """A second FOR UPDATE on an already-locked row must fail within
    lock_timeout_ms, not hang -- proving the server_settings connect_args
    genuinely reached this connection, not just that the code compiled."""
    settings = get_settings()
    lock_timeout_seconds = settings.lock_timeout_ms / 1000

    async with session_factory() as holder:
        await holder.execute(
            select(TimeSlot).where(TimeSlot.id == time_slot.id).with_for_update()
        )
        # Deliberately NOT committed/rolled back -- the lock is held open
        # for the lifetime of this `async with` block.

        async with session_factory() as contender:
            started = time.monotonic()
            with pytest.raises(DBAPIError) as exc_info:
                # A generous OUTER timeout: if lock_timeout were silently
                # not applied (the bug this test exists to catch), Postgres
                # would hold this open indefinitely and the test would hang
                # forever rather than fail cleanly -- wait_for turns that
                # into a fast, legible test failure instead of a stuck CI
                # job.
                await asyncio.wait_for(
                    contender.execute(
                        select(TimeSlot).where(TimeSlot.id == time_slot.id).with_for_update()
                    ),
                    timeout=lock_timeout_seconds + 5,
                )
            elapsed = time.monotonic() - started

            # The specific signal that OUR configured lock_timeout fired,
            # not some other DBAPIError.
            assert "lock timeout" in str(exc_info.value).lower()

            # Must fail close to lock_timeout_ms, not after the generous
            # outer wait_for ceiling -- that distinguishes "Postgres
            # actually enforced our configured timeout" from "asyncio's
            # safety net kicked in because the real timeout never applied".
            assert elapsed < lock_timeout_seconds + 2, (
                f"took {elapsed:.1f}s, expected close to configured "
                f"lock_timeout_ms={settings.lock_timeout_ms} ({lock_timeout_seconds}s) -- "
                "server_settings may not be reaching this connection"
            )
            await contender.rollback()
        # holder's `async with` exit rolls back and releases the lock.


@pytest.mark.asyncio
async def test_statement_timeout_matches_configured_value(session_factory):
    """Confirms the CONFIGURED value reached the connection, read back from
    Postgres itself (as opposed to lock_timeout above, which has no single
    query that proves a timeout without triggering one -- it must be shown
    behaviorally instead)."""
    settings = get_settings()
    async with session_factory() as session:
        statement_timeout = await session.execute(text("SHOW statement_timeout"))
        assert _pg_duration_to_ms(statement_timeout.scalar_one()) == settings.statement_timeout_ms

        lock_timeout = await session.execute(text("SHOW lock_timeout"))
        assert _pg_duration_to_ms(lock_timeout.scalar_one()) == settings.lock_timeout_ms
