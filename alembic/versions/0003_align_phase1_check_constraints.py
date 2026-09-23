"""align phase 1 CHECK constraint names with the models

Revision ID: 0003_align_phase1_checks
Revises: 0002_calendar_notifications
Create Date: 2026-09-20

THE DRIFT THIS CLOSES
---------------------
Phase 1's models declared `Enum(..., native_enum=False)` WITHOUT
`create_constraint=True`. SQLAlchemy defaults that flag to False, so the ORM
metadata contained no CHECK constraint at all -- while migration 0001
hand-wrote two of them. The consequences were subtle and worth naming,
because this is a whole category of bug rather than a one-off:

  1. TESTS WERE WEAKER THAN PRODUCTION. The test suite builds its schema with
     `Base.metadata.create_all`, which only knows about the models. So every
     test ran against a database with no status/booking_channel CHECK, while
     production had both. Code writing a bogus status would pass CI green and
     fail on deploy -- the worst possible place to find out.

  2. `alembic check` COULD NOT SEE IT. Alembic's autogenerate does not
     compare CHECK constraints; it reports only tables, columns, indexes and
     unique constraints. So this drift sat there reporting "no changes" the
     whole time. FLAGGED: a clean `alembic check` does NOT mean a clean
     schema. For CHECK constraints you must diff pg_constraint yourself,
     which is what the tests added alongside this migration now do.

WHAT CHANGED, AND WHY IT IS ONLY A RENAME
-----------------------------------------
With `create_constraint=True` now set on the Phase 1 enum columns, the models
generate CHECK constraints whose EXPRESSIONS are byte-identical to the ones
0001 created -- verified by diffing pg_get_constraintdef() between a
migrated database and a create_all database. Only the NAMES differ:

    0001 hand-written                                     models generate
    ------------------------------------------------      ----------------------------------
    ck_appointments_ck_appointments_status_valid      ->  ck_appointments_appointment_status
    ck_appointments_ck_appointments_booking_channel_valid -> ck_appointments_booking_channel

(The doubled "ck_appointments_ck_appointments_" prefix was itself a Phase 1
slip: the CheckConstraint was given a name that already included the prefix
the naming convention adds.)

WHY RENAME RATHER THAN DROP + ADD
---------------------------------
Same reasoning as the index rename in 0002, and it matters more here.
`ALTER TABLE ... RENAME CONSTRAINT` is a catalog-only operation: instant,
no table scan, and the constraint stays enforced for the entire duration.

DROP + ADD would instead (a) leave the table completely unconstrained in the
window between the two statements, so a concurrent transaction could insert
a row that the new constraint would have rejected, and then (b) force
Postgres to full-scan the table to validate the re-added constraint -- which
on a large appointments table means a long ACCESS EXCLUSIVE lock, blocking
every booking. Renaming avoids both problems entirely.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_align_phase1_checks"
down_revision: Union[str, None] = "0002_calendar_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, old_name, new_name)
_RENAMES: list[tuple[str, str, str]] = [
    (
        "appointments",
        "ck_appointments_ck_appointments_status_valid",
        "ck_appointments_appointment_status",
    ),
    (
        "appointments",
        "ck_appointments_ck_appointments_booking_channel_valid",
        "ck_appointments_booking_channel",
    ),
]


def upgrade() -> None:
    for table, old, new in _RENAMES:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}"')


def downgrade() -> None:
    for table, old, new in _RENAMES:
        op.execute(f'ALTER TABLE {table} RENAME CONSTRAINT "{new}" TO "{old}"')
