"""The test this whole exercise is about: prove double-booking is impossible.

We fire N concurrent booking attempts at the SAME time_slot from N separate
database sessions (separate connections, exactly like N separate API workers
handling N simultaneous HTTP requests) and assert exactly one wins.

WHY asyncio.gather with separate sessions, not separate threads/processes:
asyncpg connections are async, and each session_factory() call checks out a
distinct connection from the pool -- that's sufficient to reproduce a real
race at the database level. What matters is N *separate transactions*
overlapping in time, not N separate OS threads.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.enums import AppointmentStatus
from app.models.patient import Patient
from app.services import appointment_service
from app.services.exceptions import SlotAlreadyBookedError

CONCURRENT_BOOKERS = 10


@pytest.mark.asyncio
async def test_concurrent_bookings_on_same_slot_only_one_wins(
    session_factory, doctor, time_slot
):
    # A distinct patient per attempt -- otherwise a patient-double-books-
    # themselves check (not implemented in Phase 1) could confound the result.
    async with session_factory() as setup_session:
        patients = [
            Patient(full_name=f"Patient {i}", phone=f"+1415555{1000 + i}")
            for i in range(CONCURRENT_BOOKERS)
        ]
        setup_session.add_all(patients)
        await setup_session.commit()
        patient_ids = [p.id for p in patients]

    async def attempt(patient_id):
        # Each attempt gets its OWN session/connection/transaction -- this is
        # what makes the race real instead of serialized by a shared session.
        async with session_factory() as session:
            try:
                appt = await appointment_service.book_appointment(
                    session, patient_id=patient_id, time_slot_id=time_slot.id
                )
                return ("won", appt.id)
            except SlotAlreadyBookedError:
                return ("lost", None)

    results = await asyncio.gather(*(attempt(pid) for pid in patient_ids))

    wins = [r for r in results if r[0] == "won"]
    losses = [r for r in results if r[0] == "lost"]

    # The core invariant: exactly one of the N concurrent attempts succeeds.
    assert len(wins) == 1, f"expected exactly 1 winner, got {len(wins)}: {results}"
    assert len(losses) == CONCURRENT_BOOKERS - 1

    # Confirm the database agrees with the service layer's account of events
    # -- i.e. we didn't just get lucky with in-memory bookkeeping.
    async with session_factory() as verify_session:
        from sqlalchemy import select

        from app.models.appointment import Appointment

        active = (
            await verify_session.scalars(
                select(Appointment).where(
                    Appointment.time_slot_id == time_slot.id,
                    Appointment.status != AppointmentStatus.CANCELLED,
                )
            )
        ).all()
        assert len(active) == 1
        assert active[0].id == wins[0][1]


@pytest.mark.asyncio
async def test_cancelling_then_rebooking_frees_the_slot(session_factory, patient, second_patient, time_slot):
    """Regression guard for the partial-index design decision.

    A plain UNIQUE(time_slot_id) would make this test fail forever, because
    the cancelled row would still occupy the index. This is what proves the
    WHERE status <> 'cancelled' predicate is doing its job.
    """
    async with session_factory() as s1:
        appt = await appointment_service.book_appointment(
            s1, patient_id=patient.id, time_slot_id=time_slot.id
        )
        await appointment_service.cancel_appointment(s1, appointment_id=appt.id)

    async with session_factory() as s2:
        rebooked = await appointment_service.book_appointment(
            s2, patient_id=second_patient.id, time_slot_id=time_slot.id
        )
        assert rebooked.status is AppointmentStatus.BOOKED
        assert rebooked.time_slot_id == time_slot.id
