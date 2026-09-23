"""Async engine, session factory, and the FastAPI session dependency.

`build_engine` / `build_session_factory` exist so tests can build an engine
and session factory that are IDENTICAL to production's in every setting
except the URL, by calling the same two functions production calls, rather
than a second hand-written copy of the kwargs that can silently drift.

THIS IS NOT HYPOTHETICAL. It already happened once: the test fixture used
to construct its own `async_sessionmaker(...)` with a slightly different
kwarg list, `autoflush` among them, and that one difference hid a real bug
through 100+ passing tests until it crashed in production on the first live
booking. See tests/conftest.py and the memory note this produced
(test-session-must-match-prod-session) for the full story. Building both
from one function is the structural fix, not just a promise to remember.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

settings = get_settings()


def build_engine(database_url: str, settings: Settings) -> AsyncEngine:
    """The one place every engine-level setting is decided.

    Takes `settings` explicitly (not the module-level singleton) so a
    caller building a SECOND engine against a different URL -- which is
    exactly what the test suite does -- still gets every non-URL setting
    from the same Settings object production uses, rather than needing to
    know or repeat them.
    """
    return create_async_engine(
        database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # survives Postgres restarts / idle connection reaping
        echo=settings.debug,
        connect_args={
            # Server-side guards, applied at connection open. `lock_timeout`
            # is the one that matters for booking: if a FOR UPDATE wait
            # exceeds it, we get a clean error instead of a request that
            # hangs until the client gives up. THESE MUST REACH TEST
            # CONNECTIONS TOO -- they were the one setting entirely absent
            # from the test engine until this audit, meaning the fail-fast
            # behavior this whole codebase's concurrency design depends on
            # had never once been exercised by a test.
            "server_settings": {
                "statement_timeout": str(settings.statement_timeout_ms),
                "lock_timeout": str(settings.lock_timeout_ms),
            }
        },
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """The one place every session-level setting is decided."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        # expire_on_commit=False: after commit, SQLAlchemy would normally
        # expire every attribute, so touching `appointment.id` to build the
        # response would trigger a *new* lazy SELECT -- which in async code
        # raises MissingGreenlet rather than quietly refetching. Keeping
        # objects usable after commit is the standard async setting.
        expire_on_commit=False,
        # explicit flushes only; surprise flushes mid-lock are hard to
        # reason about -- see the module docstring for why this specific
        # setting is the one that already bit us once.
        autoflush=False,
    )


engine = build_engine(str(settings.database_url), settings)
SessionFactory = build_session_factory(engine)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a session per request.

    WHY the transaction is NOT opened here: the booking flow needs to control
    its own transaction boundary precisely (lock, read, write, commit). A
    blanket `async with session.begin()` in the dependency would hide that
    boundary and make the lock's lifetime a property of the HTTP layer instead
    of the business operation. Services commit explicitly; this dependency
    only guarantees the session is closed and rolled back on error.
    """
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
