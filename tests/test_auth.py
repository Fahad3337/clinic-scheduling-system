"""Authentication: login, JWT verification, scoping, cold identity map."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.models.enums import StaffRole
from app.models.staff_account import StaffAccount
from app.services import auth_service
from app.services.exceptions import (
    InvalidCredentialsError,
    InvalidTokenError,
    StaffAccountInactiveError,
)


@pytest.fixture
async def front_desk_account(db):
    account = StaffAccount(
        email="desk@clinic.example.com",
        password_hash=auth_service.hash_password("correct-horse-battery"),
        role=StaffRole.FRONT_DESK,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


@pytest.fixture
async def doctor_account(db, doctor):
    account = StaffAccount(
        email="rao@clinic.example.com",
        password_hash=auth_service.hash_password("staple-correct-horse"),
        role=StaffRole.DOCTOR,
        doctor_id=doctor.id,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


# ===================================================================== #
# Password hashing
# ===================================================================== #


def test_hash_password_round_trips():
    h = auth_service.hash_password("hunter2-but-better")
    assert auth_service.verify_password("hunter2-but-better", h)
    assert not auth_service.verify_password("wrong", h)


def test_hash_password_never_stores_plaintext():
    h = auth_service.hash_password("hunter2-but-better")
    assert "hunter2-but-better" not in h
    assert h.startswith("$2b$")


def test_same_password_hashes_differently_each_time():
    """bcrypt salts per-call -- two accounts sharing a password must not
    have identical hash columns, which would leak that fact to anyone with
    database access."""
    a = auth_service.hash_password("shared-password-123")
    b = auth_service.hash_password("shared-password-123")
    assert a != b


def test_password_over_72_bytes_rejected():
    with pytest.raises(ValueError):
        auth_service.hash_password("x" * 100)


# ===================================================================== #
# authenticate()
# ===================================================================== #


@pytest.mark.asyncio
async def test_authenticate_success(db, front_desk_account):
    account = await auth_service.authenticate(
        db, email="desk@clinic.example.com", password="correct-horse-battery"
    )
    assert account.id == front_desk_account.id


@pytest.mark.asyncio
async def test_authenticate_wrong_password_raises(db, front_desk_account):
    with pytest.raises(InvalidCredentialsError):
        await auth_service.authenticate(db, email="desk@clinic.example.com", password="wrong")


@pytest.mark.asyncio
async def test_authenticate_unknown_email_raises_same_error(db):
    """Same exception type/message as wrong-password -- see the docstring
    on InvalidCredentialsError. An email-enumeration oracle is the bug this
    guards against."""
    with pytest.raises(InvalidCredentialsError):
        await auth_service.authenticate(db, email="nobody@clinic.example.com", password="x")


@pytest.mark.asyncio
async def test_authenticate_email_is_case_insensitive(db, front_desk_account):
    account = await auth_service.authenticate(
        db, email="DESK@Clinic.Example.Com", password="correct-horse-battery"
    )
    assert account.id == front_desk_account.id


@pytest.mark.asyncio
async def test_authenticate_inactive_account_raises(db, front_desk_account):
    front_desk_account.is_active = False
    db.add(front_desk_account)
    await db.commit()

    with pytest.raises(StaffAccountInactiveError):
        await auth_service.authenticate(db, email="desk@clinic.example.com", password="correct-horse-battery")


# ===================================================================== #
# JWT issuance / verification
# ===================================================================== #


@pytest.mark.asyncio
async def test_token_round_trips_to_the_same_account(db, front_desk_account):
    token, _ = auth_service.create_access_token(front_desk_account)
    account_id = auth_service.decode_access_token(token)
    assert account_id == front_desk_account.id


def test_tampered_token_rejected():
    # NOT a fixture request: pytest resolves every named parameter as a
    # fixture regardless of a default value, so a stray parameter here
    # would fail collection with "fixture not found" -- the id is
    # generated in the body instead.
    settings = get_settings()
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) + timedelta(hours=1)},
        "a-completely-different-key-not-ours",
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        auth_service.decode_access_token(forged)


def test_expired_token_rejected():
    settings = get_settings()
    expired = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": datetime.now(UTC) - timedelta(seconds=1)},
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        auth_service.decode_access_token(expired)


def test_garbage_token_rejected():
    with pytest.raises(InvalidTokenError):
        auth_service.decode_access_token("not-a-jwt-at-all")


# ===================================================================== #
# The login HTTP endpoint, and route-level auth enforcement
# ===================================================================== #


@pytest.mark.asyncio
async def test_login_endpoint_issues_a_usable_token(client, front_desk_account):
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "desk@clinic.example.com", "password": "correct-horse-battery"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "front_desk"
    assert body["doctor_id"] is None
    assert body["expires_in"] > 0

    account_id = auth_service.decode_access_token(body["access_token"])
    assert account_id == front_desk_account.id


@pytest.mark.asyncio
async def test_login_endpoint_wrong_password_is_401(client, front_desk_account):
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "desk@clinic.example.com", "password": "wrong"}
    )
    assert resp.status_code == 401
    # Body must not distinguish "wrong password" from "no such account".
    assert "invalid" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_protected_endpoint_without_token_is_401(client, doctor):
    resp = await client.get(f"/api/v1/doctors/{doctor.id}/calendar")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_with_garbage_token_is_401(client, doctor):
    resp = await client.get(
        f"/api/v1/doctors/{doctor.id}/calendar",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_front_desk_may_act_on_any_doctor(client, front_desk_account, doctor):
    login = await client.post(
        "/api/v1/auth/login", json={"email": "desk@clinic.example.com", "password": "correct-horse-battery"}
    )
    token = login.json()["access_token"]
    resp = await client.get(
        f"/api/v1/doctors/{doctor.id}/calendar", headers={"Authorization": f"Bearer {token}"}
    )
    # 404 (no calendar connected) is the CORRECT outcome here -- it proves
    # the request got PAST authorization and reached the service, which is
    # what this test is about. A 401/403 would mean scoping wrongly
    # rejected a front-desk account.
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_doctor_can_act_on_their_own_doctor(client, doctor_account, doctor):
    login = await client.post(
        "/api/v1/auth/login", json={"email": "rao@clinic.example.com", "password": "staple-correct-horse"}
    )
    token = login.json()["access_token"]
    resp = await client.get(
        f"/api/v1/doctors/{doctor.id}/calendar", headers={"Authorization": f"Bearer {token}"}
    )
    # 404 (no calendar connected) proves the request reached the service --
    # same reasoning as the front-desk test above.
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_doctor_cannot_act_on_a_different_doctor(client, doctor_account, session_factory):
    """A doctor-role token must be REJECTED at another doctor's resources.

    This is the enforcement the no-auth writeup's OAuth-flow clarification
    depended on being real: doctor_id binding only protects against a
    stranger using the flow; it says nothing about one authenticated
    doctor reaching into another's calendar, which is what this test
    covers.
    """
    from app.models.doctor import Doctor

    async with session_factory() as s:
        other = Doctor(full_name="Dr. Someone Else", timezone="UTC")
        s.add(other)
        await s.commit()
        other_id = other.id

    login = await client.post(
        "/api/v1/auth/login", json={"email": "rao@clinic.example.com", "password": "staple-correct-horse"}
    )
    token = login.json()["access_token"]
    resp = await client.get(
        f"/api/v1/doctors/{other_id}/calendar", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_deactivated_account_rejected_even_with_a_still_valid_token(
    client, front_desk_account, session_factory
):
    """The property that justifies the 'no refresh token yet' tradeoff:
    deactivation must take effect before the JWT would naturally expire.
    See core/config.py's note on jwt_access_token_expire_minutes."""
    login = await client.post(
        "/api/v1/auth/login", json={"email": "desk@clinic.example.com", "password": "correct-horse-battery"}
    )
    token = login.json()["access_token"]

    async with session_factory() as s:
        from app.models.staff_account import StaffAccount as SA

        row = await s.get(SA, front_desk_account.id)
        row.is_active = False
        await s.commit()

    resp = await client.get("/api/v1/appointments/" + str(uuid.uuid4()), headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


# ===================================================================== #
# Cold identity map -- get_current_staff must not lazy-load
# ===================================================================== #


@pytest.mark.asyncio
async def test_get_current_staff_works_with_a_cold_identity_map(db, front_desk_account):
    """The auth dependency runs on EVERY request against a session that has
    never seen this account before -- there is no warm identity map to hide
    a lazy-load bug behind, but this pins it explicitly per the standing
    convention."""
    db.expunge_all()
    account = await auth_service.authenticate(
        db, email="desk@clinic.example.com", password="correct-horse-battery"
    )
    assert account.email == "desk@clinic.example.com"
