"""Password hashing, credential checks, and JWT issuance/verification.

Deliberately thin: hashing and JWT encoding are single library calls, and
wrapping them here is about having ONE place that knows the algorithm
choices, not about hiding complexity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.staff_account import StaffAccount
from app.services.exceptions import (
    InvalidCredentialsError,
    InvalidTokenError,
    StaffAccountInactiveError,
)

# bcrypt truncates input at 72 BYTES silently -- not characters, bytes, so a
# password with multi-byte UTF-8 characters can hit the limit sooner than
# its character count suggests. We reject longer passwords explicitly at
# hashing time rather than let bcrypt silently ignore the tail, which would
# mean "correcthorsebatterystaple-plus-73-more-chars" and
# "correcthorsebatterystaple" (first 72 bytes matching) hash identically.
_MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {_MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        # Malformed hash in the column (should be unreachable via
        # hash_password, but a corrupt row must fail closed, not raise past
        # the caller as an unhandled 500 that might look like a different
        # kind of bug).
        return False


async def authenticate(
    session: AsyncSession, *, email: str, password: str
) -> StaffAccount:
    """Verify credentials and return the account, or raise.

    WHY THE PASSWORD IS CHECKED EVEN WHEN THE EMAIL DOESN'T EXIST: without
    this, a login attempt for a real email takes measurably longer (a real
    bcrypt comparison) than one for a fake email (an early return). That
    timing difference is a working oracle for enumerating valid staff
    emails. Running bcrypt against a fixed dummy hash in the miss case
    keeps both paths doing the same amount of work.
    """
    account = await session.scalar(
        select(StaffAccount).where(StaffAccount.email == email.lower())
    )

    if account is None:
        # Constant-time-ish: burn a bcrypt cycle on a hash nobody's password
        # will ever match, so "no such email" and "wrong password" take
        # comparable wall-clock time.
        verify_password(password, "$2b$12$" + "0" * 53)
        raise InvalidCredentialsError()

    if not verify_password(password, account.password_hash):
        raise InvalidCredentialsError()

    if not account.is_active:
        raise StaffAccountInactiveError(account.id)

    return account


def create_access_token(account: StaffAccount) -> tuple[str, datetime]:
    """Mint a signed JWT for this account. Returns (token, expires_at)."""
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)

    payload = {
        # 'sub' is the account id -- the ONLY thing the auth dependency
        # trusts from the token. Role and doctor_id are re-read from the
        # database on every request (see api/deps.py), not taken from
        # these claims, so a stale or forged claim in an old token can't
        # grant access a fresh DB row wouldn't.
        "sub": str(account.id),
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, expires_at


def decode_access_token(token: str) -> UUID:
    """Verify signature and expiry, return the account id, or raise.

    Deliberately returns ONLY the id. Everything else about the account
    (role, active status, doctor_id) must come from a fresh database read --
    see the note in create_access_token about why claims are not trusted
    for authorization decisions.
    """
    settings = get_settings()
    if not settings.jwt_secret_key.get_secret_value():
        raise InvalidTokenError("server has no JWT signing key configured")
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key.get_secret_value(), algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        # Base class for every other PyJWT failure: bad signature, bad
        # format, wrong algorithm. One outcome for all of them -- the
        # specifics are not the caller's business, same reasoning as
        # InvalidCredentialsError.
        raise InvalidTokenError() from exc

    try:
        return UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("malformed subject claim") from exc
