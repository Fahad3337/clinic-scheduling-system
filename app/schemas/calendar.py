"""Pydantic v2 schemas for calendar connection endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import CalendarConnectionState, CalendarProvider


class AuthorizationStartResponse(BaseModel):
    """Where to send the doctor to grant access."""

    authorization_url: str
    # Echoed back so a caller that wants to correlate the eventual callback
    # can. It is NOT a secret to the initiator -- they just generated it --
    # but it must not be logged anywhere a third party could read.
    state: str
    expires_at: datetime


class CalendarConnectionRead(BaseModel):
    """Public view of a connection.

    DELIBERATELY OMITS refresh_token, access_token and sync_token. Response
    models are a security boundary, not just a serialization convenience:
    `from_attributes` reads whatever fields are declared here, so a secret
    that is not declared cannot leak through a handler that forgets to strip
    it. Adding a token field to this class should require a very good reason.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    doctor_id: UUID
    provider: CalendarProvider
    account_email: str
    calendar_id: str
    state: CalendarConnectionState
    granted_scopes: str
    store_event_details: bool
    last_synced_at: datetime | None = None
    consecutive_failures: int
    created_at: datetime

    @property
    def is_connected(self) -> bool:
        return self.state is CalendarConnectionState.ACTIVE
