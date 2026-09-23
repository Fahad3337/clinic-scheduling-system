"""Pydantic v2 schemas for authentication."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr

from app.models.enums import StaffRole


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds -- easier for a client to compute a refresh timer from
    role: StaffRole
    doctor_id: UUID | None = None
