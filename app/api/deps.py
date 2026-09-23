"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.enums import StaffRole
from app.models.staff_account import StaffAccount
from app.services import auth_service
from app.services.exceptions import (
    InsufficientRoleError,
    InvalidTokenError,
    StaffAccountInactiveError,
)

# Re-exported under an API-layer name so route modules import from `api.deps`
# rather than reaching into `db.session` directly -- keeps the dependency
# graph pointing one direction (api -> db), not routes scattered across both.
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async for session in get_session():
        yield session


# auto_error=False: a missing header should produce OUR 401 with our body,
# not FastAPI's default "Not authenticated" -- keeps every auth failure on
# this API shaped the same way regardless of which check caught it.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_staff(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> StaffAccount:
    """Resolve the bearer token to a live StaffAccount, or reject.

    WHY THIS RE-READS THE DATABASE ON EVERY REQUEST rather than trusting the
    JWT's own claims for role/doctor_id/active status: a JWT is a bearer
    token with no server-side revocation in this phase (see the
    jwt_access_token_expire_minutes note in core/config.py). If role or
    active-status changes take effect only when the token happens to expire,
    then deactivating a compromised or ex-employee's account does nothing
    for up to 12 hours. Reading fresh on every request makes deactivation
    take effect on the very next call, which is the property that actually
    matters for an access-control system -- the JWT's only job is proving
    WHO is asking; the database still decides WHAT they may do.

    This is also exactly the cold-identity-map hazard: `account.doctor` is
    never touched here, and callers needing doctor_id use
    `account.doctor_id` (a plain column) rather than the relationship.
    """
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        account_id: UUID = auth_service.decode_access_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc

    account = await db.get(StaffAccount, account_id)
    if account is None:
        # The account behind a still-validly-signed token was deleted.
        # Treat identically to "deactivated" -- both mean "this token no
        # longer represents anyone who may act".
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account no longer exists")
    if not account.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is deactivated")

    return account


def assert_doctor_scope(staff: StaffAccount, doctor_id: UUID) -> None:
    """Raise 403 unless `staff` may act on `doctor_id`'s resources.

    FRONT_DESK may act on any doctor (the single-clinic assumption -- front
    desk staff serve the whole clinic). DOCTOR may only act on their own
    doctor_id. Anything else is 403: the caller proved who they are (that
    was get_current_staff's job), and who they are still isn't allowed here.

    WHY A PLAIN FUNCTION, NOT A Depends() FACTORY: the check needs the
    doctor_id from the URL PATH, but FastAPI resolves every `Depends(...)`
    default before it has bound path parameters for that request -- there
    is no `path_doctor_id` available yet at the point a dependency would be
    constructed. So the route itself takes `doctor_id: UUID` (from the path)
    and `staff: StaffAccount = Depends(get_current_staff)` as ordinary
    parameters, and calls this function as its first line. See
    api/v1/calendar.py for the call pattern.
    """
    if staff.role is StaffRole.FRONT_DESK:
        return
    if staff.role is StaffRole.DOCTOR and staff.doctor_id == doctor_id:
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "Not authorized for this doctor's resources",
    )
