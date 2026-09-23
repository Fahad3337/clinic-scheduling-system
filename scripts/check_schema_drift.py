#!/usr/bin/env python
"""Fail if the migrations and the ORM models disagree about the schema.

WHY THIS EXISTS: `alembic check` does NOT compare CHECK constraints. It
reports tables, columns, nullability, indexes and unique constraints only.
On this project that gap hid real drift across two migrations while
reporting "No new upgrade operations detected" the whole time -- production
had CHECK constraints the test database lacked, so tests were weaker than
prod and bad data would pass CI and fail on deploy.

WHAT THIS DOES: builds the schema twice, two different ways, and diffs the
full catalog.

    A: an empty database with `alembic upgrade head`   (what production gets)
    B: an empty database with `Base.metadata.create_all` (what tests get)

Then compares every constraint (CHECK, FK, UNIQUE, PK, EXCLUDE) and every
index, by name AND by definition. Any difference is drift and fails the run.

Run locally:
    python scripts/check_schema_drift.py \
        --migrated postgresql+asyncpg://... --models postgresql+asyncpg://...
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

CONSTRAINT_QUERY = text(
    """
    SELECT conrelid::regclass::text AS tbl,
           conname,
           pg_get_constraintdef(oid) AS def
    FROM pg_constraint
    WHERE connamespace = 'public'::regnamespace
      AND conrelid::regclass::text <> 'alembic_version'
    ORDER BY 1, 2
    """
)

INDEX_QUERY = text(
    """
    SELECT tablename, indexname, indexdef
    FROM pg_indexes
    WHERE schemaname = 'public' AND tablename <> 'alembic_version'
    ORDER BY 1, 2
    """
)


async def _catalog(url: str) -> tuple[set[str], set[str]]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            constraints = {
                f"{t} | {n} | {d}" for t, n, d in (await conn.execute(CONSTRAINT_QUERY)).all()
            }
            indexes = {
                f"{t} | {n} | {d}" for t, n, d in (await conn.execute(INDEX_QUERY)).all()
            }
        return constraints, indexes
    finally:
        await engine.dispose()


async def _build_models_schema(url: str) -> None:
    from app.models import Base  # imported late so --help works without config

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _report(label: str, migrated: set[str], models: set[str]) -> bool:
    only_migrated = sorted(migrated - models)
    only_models = sorted(models - migrated)
    if not only_migrated and not only_models:
        print(f"  {label}: OK ({len(migrated)} compared)")
        return True

    print(f"  {label}: DRIFT")
    for row in only_migrated:
        print(f"    only in MIGRATIONS (prod has it, tests do not): {row}")
    for row in only_models:
        print(f"    only in MODELS (tests have it, prod does not):  {row}")
    return False


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrated", default=os.environ.get("MIGRATED_URL"))
    parser.add_argument("--models", default=os.environ.get("MODELS_URL"))
    args = parser.parse_args()

    if not args.migrated or not args.models:
        parser.error("both --migrated and --models are required (or MIGRATED_URL/MODELS_URL)")

    print("Building model schema with create_all...")
    await _build_models_schema(args.models)

    print("Comparing migrated schema against models:")
    migrated_c, migrated_i = await _catalog(args.migrated)
    models_c, models_i = await _catalog(args.models)

    ok = _report("constraints", migrated_c, models_c)
    ok = _report("indexes", migrated_i, models_i) and ok

    if ok:
        print("\nNo schema drift.")
        return 0
    print(
        "\nSCHEMA DRIFT DETECTED.\n"
        "A constraint present in only one place means production and the test\n"
        "suite are enforcing different rules. Common cause: an Enum column\n"
        "declared without create_constraint=True, so the CHECK exists only in\n"
        "the hand-written migration. Note that `alembic check` cannot see this."
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
