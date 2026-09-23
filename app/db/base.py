"""Declarative base, metadata conventions, and shared column mixins.

Every model inherits from `Base`. Keeping the metadata here (rather than in
each model file) means Alembic has exactly one object to autogenerate from.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# WHY an explicit naming convention: Alembic autogenerate needs deterministic,
# stable names for indexes and constraints. Without this, Postgres invents
# names ("appointments_patient_id_fkey"), and a later migration that wants to
# DROP a constraint has to hardcode a name nobody wrote down. With a
# convention, SQLAlchemy always knows the name, so `alembic downgrade` works
# and diffs stay clean. Set this on day one -- retrofitting it means renaming
# every constraint in an existing database.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """UUIDv4 primary keys.

    WHY UUID over bigserial:
      - IDs are exposed in URLs and will later be spoken/typed into a chatbot
        and read back over the phone. Sequential integers leak volume ("you're
        patient 41") and invite enumeration of other people's appointments.
      - The default is generated *client-side* (Python) rather than by the
        database. That matters for the service layer: we know an object's ID
        before INSERT, so we can build response payloads, log correlation IDs,
        and write child rows without an extra round trip or a flush.

    TRADE-OFF / REVISIT: random UUIDs fragment B-tree indexes on write-heavy
    tables. At clinic scale (thousands of rows) this is irrelevant. If this
    ever becomes a multi-tenant system with millions of appointments, switch
    to UUIDv7 (time-ordered) via a `uuid7()` helper -- same column type, so
    it's a code change, not a migration.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


class TimestampMixin:
    """Audit timestamps, always stored as TIMESTAMPTZ.

    WHY server_default=now() instead of a Python default: the database clock is
    the single source of truth. Application containers drift, and a chatbot
    worker in another region must not be able to write a row that appears to
    predate one written a second earlier elsewhere.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    # NOTE: `onupdate` is applied by SQLAlchemy, not Postgres, so a raw SQL
    # UPDATE outside the ORM will not bump this column. Acceptable for now;
    # if you ever need it enforced, add a trigger. Flagged as a simplification.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
