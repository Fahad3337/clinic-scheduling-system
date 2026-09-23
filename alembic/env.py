"""Alembic environment: async-engine aware, driven by app.core.config.Settings.

WHY run_sync for migrations: Alembic's migration runner is fundamentally
synchronous (autogenerate diffing, DDL execution order). Rather than fight
that, we open one async connection and hand a *sync-compatible* proxy of it
to Alembic via `connection.run_sync(...)`. This is the standard pattern for
running Alembic against an async SQLAlchemy engine.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.models import Base  # noqa: F401 -- imports every model, populating metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    # Single source of truth for the DB URL -- see the WHY note in alembic.ini.
    return str(get_settings().database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`alembic upgrade --sql`)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # WHY compare_type=True: without it, autogenerate ignores column type
        # changes entirely (e.g. String(20) -> String(30) on phone would
        # generate no migration). The default is off for historical reasons;
        # turning it on is almost always what you want.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable: AsyncEngine = create_async_engine(get_url(), poolclass=None)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
