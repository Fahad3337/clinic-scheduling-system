"""Test fixtures.

WHY these tests run against real Postgres, not SQLite-in-memory:

  The entire point of this codebase's concurrency design is a Postgres-
  specific mechanism -- `SELECT ... FOR UPDATE` row locking plus a partial
  unique index. SQLite has neither real row-level locking (it locks the
  whole database file) nor partial indexes with the same semantics. A test
  suite that mocked this out with SQLite would pass while testing nothing
  about the actual bug it exists to catch. Use Postgres for tests, always.

SETUP: point TEST_DATABASE_URL at a scratch Postgres database before running
pytest, e.g.:

    docker compose up -d db
    createdb -h localhost -U clinic clinic_test   # or let conftest do it
    TEST_DATABASE_URL=postgresql+asyncpg://clinic:clinic@localhost:5432/clinic_test pytest

If TEST_DATABASE_URL is unset, it defaults to the same DB as docker-compose's
`db` service with a `_test` suffix.
"""

from __future__ import annotations

import base64
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import build_engine, build_session_factory
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.time_slot import TimeSlot

# Set BEFORE any `app.*` import so app.core.config picks it up. A Fernet key
# is just urlsafe-base64 of 32 bytes, so we can mint one without importing
# cryptography here.
#
# WHY generate rather than hardcode: a committed key is a key that eventually
# gets copy-pasted into a real deployment. A fresh one per run also proves
# nothing in the suite secretly depends on a specific key value.
#
# WHY set it here rather than requiring the caller to export it: the suite
# must be runnable with a bare `pytest`. Passing an invalid key (e.g.
# TOKEN_ENCRYPTION_KEYS=dummy) fails deep inside Fernet with an opaque
# error -- as it should, but that is a terrible first experience.
os.environ.setdefault(
    "TOKEN_ENCRYPTION_KEYS",
    base64.urlsafe_b64encode(os.urandom(32)).decode(),
)
# Same reasoning, same "set before any app import" requirement, for the
# JWT signing key (Phase 3 auth).
os.environ.setdefault("JWT_SECRET_KEY", secrets.token_urlsafe(48))

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://clinic:clinic@localhost:5432/clinic_test",
)


@pytest_asyncio.fixture(scope="session")
async def engine():
    # build_engine, not a second create_async_engine(...) call: this is the
    # SAME function app/db/session.py uses for the production engine, so
    # pool_size, max_overflow, pool_pre_ping, echo, and -- the setting that
    # actually matters -- the connect-time statement_timeout/lock_timeout
    # are identical to production by construction, not by two people
    # remembering to keep two argument lists in sync. See the module
    # docstring in app/db/session.py; this is a standing convention now
    # (test-prod-env-parity), not a one-off fix.
    eng = build_engine(TEST_DATABASE_URL, get_settings())
    async with eng.begin() as conn:
        # WHY create_all instead of `alembic upgrade head` for tests: speed
        # and no subprocess dependency. TRADE-OFF, flagged: this means the
        # test schema can drift from what the migration file actually
        # produces if someone edits a model without updating the migration.
        # Mitigation: run `alembic upgrade head` against a throwaway DB in CI
        # as a separate check that the migration matches the models -- not
        # implemented here, worth adding.
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    # build_session_factory: same reasoning as the engine fixture above.
    return build_session_factory(engine)


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncIterator[AsyncSession]:
    """A session for tests that only need one -- most of them.

    Each test truncates its own tables at the end so tests stay independent
    without paying for a fresh schema per test.
    """
    async with session_factory() as session:
        yield session
        await session.rollback()
        # Truncate rather than drop/recreate: much faster across a full test
        # run, and RESTART IDENTITY / CASCADE keeps FK order from mattering.
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(table.delete())
        await session.commit()


@pytest_asyncio.fixture
async def doctor(db: AsyncSession) -> Doctor:
    doc = Doctor(full_name="Dr. Priya Rao", specialty="General Practice", timezone="UTC")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@pytest_asyncio.fixture
async def patient(db: AsyncSession) -> Patient:
    p = Patient(full_name="Alex Chen", phone="+14155550123")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


@pytest_asyncio.fixture
async def second_patient(db: AsyncSession) -> Patient:
    p = Patient(full_name="Jordan Lee", phone="+14155550124")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


@pytest_asyncio.fixture
async def client(session_factory) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client against the real FastAPI app, routed at the test DB.

    WHY override the get_db DEPENDENCY rather than pointing DATABASE_URL at
    the test database: app.db.session builds its engine from Settings at
    IMPORT time, which has already happened by the time a test runs.
    Overriding the dependency is the supported way to redirect a route's DB
    access without fighting import order or re-importing the app module.

    Imports app.main lazily (inside the fixture body) rather than at
    conftest module scope, so a test file that never requests `client`
    never pays the cost of constructing the FastAPI app.
    """
    from app.api.deps import get_db
    from app.main import app

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def time_slot(db: AsyncSession, doctor: Doctor) -> TimeSlot:
    # 3 days out, NOT 1. At exactly 24h the 24-hour reminder lands on its
    # own scheduling boundary (reminder_at == now, give or take the minute
    # rounding), so tests would silently exercise the "too late to remind"
    # branch instead of the normal one. The boundary itself is covered
    # explicitly by test_reminder_boundary_* in test_notifications.py.
    starts = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=3)
    slot = TimeSlot(doctor_id=doctor.id, starts_at=starts, ends_at=starts + timedelta(minutes=30))
    db.add(slot)
    await db.commit()
    await db.refresh(slot)
    return slot
