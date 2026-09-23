"""Login route. The only unauthenticated staff-facing endpoint by design."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.schemas.auth import LoginRequest, TokenResponse
from app.services import auth_service
from app.services.exceptions import InvalidCredentialsError, StaffAccountInactiveError

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    try:
        account = await auth_service.authenticate(
            db, email=payload.email, password=payload.password
        )
    except (InvalidCredentialsError, StaffAccountInactiveError) as exc:
        # SAME status and SAME body for both. Distinguishing "wrong
        # password" from "account disabled" tells an attacker which emails
        # are real staff accounts -- see InvalidCredentialsError's docstring.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password") from exc

    token, expires_at = auth_service.create_access_token(account)
    from datetime import UTC, datetime

    return TokenResponse(
        access_token=token,
        expires_in=int((expires_at - datetime.now(UTC)).total_seconds()),
        role=account.role,
        doctor_id=account.doctor_id,
    )
