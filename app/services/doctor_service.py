"""Read-only doctor queries.

No prior module owned "list every doctor" -- individual call sites
(chatbot/tools.py's `_resolve_clinic_doctor`, availability endpoints)
each query `Doctor` directly for their own narrow purpose. This module
exists for the staff dashboard (app/web/dashboard.py), which needs an
actual list, not a single-doctor lookup.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.doctor import Doctor


async def list_doctors(session: AsyncSession) -> list[Doctor]:
    """Every doctor, with calendar connection health eager-loaded.

    Small, fixed-size table by this project's own single-clinic
    assumption (see Doctor's docstring) -- no pagination, no limit.
    """
    stmt = select(Doctor).order_by(Doctor.full_name).options(
        selectinload(Doctor.calendar_connection)
    )
    return list((await session.scalars(stmt)).all())
