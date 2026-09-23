"""Pydantic v2 schemas for Conversation, Phase 3.5's staff reopen action.

Deliberately NO patient_id field anywhere in this file, on either the
request or response side. Reopening releases a phone number; it does not
name a patient. See conversation_service.reopen's docstring and the
"PHASE 3.5" section of models/conversation.py for why that omission is
load-bearing, not an oversight.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ConversationChannel, ConversationStatus


class ConversationReopen(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel: ConversationChannel
    status: ConversationStatus
    escalation_reason: str | None = None
    reopened_at: datetime | None = None
    reopened_by_staff_id: UUID | None = None
    reopen_reason: str | None = None
    created_at: datetime
