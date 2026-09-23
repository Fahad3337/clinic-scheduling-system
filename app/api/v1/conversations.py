"""Staff-facing conversation routes.

Phase 3.5: exactly one route lives here, reopening a conversation stuck
behind a terminal (ESCALATED/COMPLETED) state so its phone number can talk
to the bot again. See conversation_service.reopen for the mechanism.

NO ROUTE ON THIS ROUTER MAY ACCEPT A patient_id, AND NONE EVER SHOULD.
Reopening releases a phone number; it does not vouch for a patient. The
absence of that field on ConversationReopen is structural, not
incidental -- see the schema and service docstrings for the full
reasoning. Do not "helpfully" add a patient_id or an
already_verified/skip_identity flag to this endpoint to save a caller a
round of DOB verification: that is exactly the lockout bypass
get_or_create_conversation's terminal-state handling (and this whole
Phase 3.5 item) was built to close.

Any authenticated staff account may reopen -- conversations have no
doctor_id to scope against (a chat thread is not one doctor's resource),
so there is no assert_doctor_scope call here, unlike appointments.py.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_staff, get_db
from app.models.staff_account import StaffAccount
from app.schemas.conversation import ConversationRead, ConversationReopen
from app.services import conversation_service
from app.services.exceptions import ConversationNotFoundError, ConversationNotTerminalError

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.patch("/{conversation_id}/reopen", response_model=ConversationRead)
async def reopen_conversation(
    conversation_id: UUID,
    payload: ConversationReopen,
    db: AsyncSession = Depends(get_db),
    staff: StaffAccount = Depends(get_current_staff),
) -> ConversationRead:
    try:
        conversation = await conversation_service.reopen(
            db,
            conversation_id=conversation_id,
            staff_id=staff.id,
            reason=payload.reason,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ConversationNotTerminalError as exc:
        # 409: the conversation exists and the request is well-formed, but
        # its current status (ACTIVE/EXPIRED) means there is nothing to
        # release -- same reasoning as InvalidStatusTransitionError on
        # appointments.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return ConversationRead.model_validate(conversation)
