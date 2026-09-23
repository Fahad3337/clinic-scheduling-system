"""Appointment routes.

Every handler here is intentionally thin: parse/validate via Pydantic, call a
service function, translate the result or the domain exception into HTTP.
No business logic lives in this file -- see services/appointment_service.py.
This split is what lets a future Twilio/chatbot worker call the exact same
service functions without going through HTTP at all.

AUTHORIZATION SHAPE, deliberately stated rather than left implicit:

Every route here requires SOME authenticated staff account
(Depends(get_current_staff)) -- that closes the "anyone on the internet can
read/cancel any appointment" gap from docs/security-no-authentication.md.

Doctor-SCOPING (a doctor account may only touch their own doctor's
appointments) is enforced on read/cancel/reschedule, where the appointment
already exists and its doctor_id is a fact we can check before mutating
anything. It is NOT enforced on create: the doctor is implied by
`time_slot_id` and is not known until the service resolves the slot, so
checking scope beforehand is not possible without loading the slot twice,
and checking it AFTER booking would mean rejecting a booking that already
committed. FLAGGED SIMPLIFICATION: in practice this endpoint's real
audience is front-desk staff, who are unscoped by design; a doctor account
booking through this endpoint is currently trusted rather than restricted
to their own doctor_id. Revisit if doctor-role accounts are ever expected
to use this endpoint directly and multi-doctor makes cross-doctor booking
a real risk rather than a single-clinic non-issue.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_doctor_scope, get_current_staff, get_db
from app.models.staff_account import StaffAccount
from app.schemas.appointment import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentRead,
    AppointmentReschedule,
)
from app.services import appointment_service
from app.services.exceptions import (
    AppointmentNotFoundError,
    InvalidStatusTransitionError,
    PatientNotFoundError,
    RescheduleAcrossDoctorsError,
    RescheduleToSameSlotError,
    SlotAlreadyBookedError,
    SlotBlockedError,
    TimeSlotNotFoundError,
)

router = APIRouter(prefix="/appointments", tags=["appointments"])


@router.post("", response_model=AppointmentRead, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    payload: AppointmentCreate,
    db: AsyncSession = Depends(get_db),
    staff: StaffAccount = Depends(get_current_staff),  # noqa: ARG001 -- any authenticated staff; see module docstring
) -> AppointmentRead:
    # NO assert_doctor_scope(staff, ...) HERE, AND THAT IS DELIBERATE --
    # do not add one without reading this first.
    #
    # Every other mutating route in this file loads the appointment FIRST,
    # reads its (already-existing) doctor_id, and scope-checks before
    # touching anything. That pattern does not exist here: at this point
    # in the request there IS no doctor_id yet -- it lives on
    # `payload.time_slot_id`, and the only way to learn it is to resolve
    # the slot, which is `book_appointment`'s job, not this route's. Two
    # ways to "fix" this both make it worse:
    #   - load the slot here just to check its doctor_id, then load it
    #     AGAIN inside book_appointment under FOR UPDATE -- two reads of
    #     the same row for one request, and the second one can still
    #     legitimately disagree with the first (the slot could be gone or
    #     blocked by the time the lock is taken), so the first check buys
    #     nothing.
    #   - check doctor_id AFTER book_appointment returns, and reject/roll
    #     back if it doesn't match -- meaning a real booking briefly
    #     existed, notifications may already be queued, and now must be
    #     unwound. Rejecting a request that already succeeded is worse
    #     than not scoping it.
    # See the module docstring's AUTHORIZATION SHAPE section for the full
    # reasoning and the accepted risk this leaves open.
    try:
        appointment = await appointment_service.book_appointment(
            db,
            patient_id=payload.patient_id,
            time_slot_id=payload.time_slot_id,
            booking_channel=payload.booking_channel,
            reason=payload.reason,
        )
    except (PatientNotFoundError, TimeSlotNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (SlotAlreadyBookedError, SlotBlockedError) as exc:
        # 409 Conflict: the request was well-formed but the resource state
        # (slot already taken / blocked) prevents it -- the canonical status
        # for "someone else got there first" races.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return AppointmentRead.model_validate(appointment)


@router.get("/{appointment_id}", response_model=AppointmentRead)
async def read_appointment(
    appointment_id: UUID,
    db: AsyncSession = Depends(get_db),
    staff: StaffAccount = Depends(get_current_staff),
) -> AppointmentRead:
    try:
        appointment = await appointment_service.get_appointment(db, appointment_id)
    except AppointmentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    assert_doctor_scope(staff, appointment.doctor_id)
    return AppointmentRead.model_validate(appointment)


@router.patch("/{appointment_id}/cancel", response_model=AppointmentRead)
async def cancel_appointment(
    appointment_id: UUID,
    payload: AppointmentCancel,
    db: AsyncSession = Depends(get_db),
    staff: StaffAccount = Depends(get_current_staff),
) -> AppointmentRead:
    try:
        existing = await appointment_service.get_appointment(db, appointment_id)
    except AppointmentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    # Scope-check BEFORE mutating: an unauthorized caller must never cause a
    # side effect, even one they immediately get told about. This costs one
    # extra unlocked SELECT; the service's own FOR UPDATE re-fetch below is
    # unaffected -- see the module docstring.
    assert_doctor_scope(staff, existing.doctor_id)

    try:
        appointment = await appointment_service.cancel_appointment(
            db,
            appointment_id=appointment_id,
            cancellation_reason=payload.cancellation_reason,
        )
    except AppointmentNotFoundError as exc:  # pragma: no cover - TOCTOU: deleted between the two loads
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvalidStatusTransitionError as exc:
        # 409: appointment exists, request is well-formed, but its current
        # state (already cancelled/completed/no-show) forbids this transition.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return AppointmentRead.model_validate(appointment)


@router.patch("/{appointment_id}/reschedule", response_model=AppointmentRead)
async def reschedule_appointment(
    appointment_id: UUID,
    payload: AppointmentReschedule,
    db: AsyncSession = Depends(get_db),
    staff: StaffAccount = Depends(get_current_staff),
) -> AppointmentRead:
    """Move a booked appointment to a new time slot.

    Implemented as an atomic cancel-old + book-new -- see
    appointment_service.reschedule_appointment for why this is a NEW
    appointment row (`rescheduled_from_id` links it to the old one) rather
    than mutating `time_slot_id` in place.
    """
    try:
        existing = await appointment_service.get_appointment(db, appointment_id)
    except AppointmentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    assert_doctor_scope(staff, existing.doctor_id)

    try:
        appointment = await appointment_service.reschedule_appointment(
            db,
            appointment_id=appointment_id,
            new_time_slot_id=payload.new_time_slot_id,
            reason=payload.reason,
        )
    except AppointmentNotFoundError as exc:  # pragma: no cover - TOCTOU
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except TimeSlotNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (SlotAlreadyBookedError, SlotBlockedError, RescheduleToSameSlotError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except InvalidStatusTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except RescheduleAcrossDoctorsError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return AppointmentRead.model_validate(appointment)
