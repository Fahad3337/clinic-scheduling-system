"""Phase 3.5, item 2: the staff reopen action.

What this file has to prove, per the user's own requirement before this
was built: a reopened conversation is a genuinely FRESH start for its
phone number, never a resurrection of the identity that was bound to the
terminal conversation being released. That is tested here at the level
that actually matters -- what get_or_create_conversation hands back on
the NEXT inbound message -- not just that reopen() runs without error.

Also covers: non-staff cannot reach the endpoint at all, reopen refuses
anything that isn't currently terminal, and the reopened_iff_staff_recorded
CHECK constraint backs the pairing invariant at the schema level too.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.conversation import Conversation
from app.models.enums import ConversationChannel, ConversationStatus, StaffRole
from app.models.staff_account import StaffAccount
from app.services import auth_service, conversation_service
from app.services.exceptions import ConversationNotFoundError, ConversationNotTerminalError

EXTERNAL_REF = "+14155550199"


@pytest.fixture
async def front_desk_account(db):
    account = StaffAccount(
        email="desk-reopen@clinic.example.com",
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
        email="doc-reopen@clinic.example.com",
        password_hash=auth_service.hash_password("staple-correct-horse"),
        role=StaffRole.DOCTOR,
        doctor_id=doctor.id,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def _escalated_after_verification(db, patient) -> Conversation:
    """The adversarial case that matters most: a conversation that WAS
    successfully verified (patient_id set, identity_verified_at set) and
    only later escalated -- e.g. the caller asked for a human after being
    identified. This is the shape of row most likely to tempt a shortcut
    ("staff already knows who this is, just carry it forward"). Reopen
    must refuse that temptation regardless.
    """
    from datetime import date

    patient.date_of_birth = date(1990, 1, 1)
    db.add(patient)
    await db.commit()

    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=patient.phone
    )
    await db.commit()
    result = await conversation_service.verify_identity(
        db, conversation_id=convo.id, date_of_birth=patient.date_of_birth
    )
    assert result.outcome is conversation_service.IdentityOutcome.VERIFIED
    await conversation_service.escalate(db, conversation_id=convo.id, reason="caller asked for a human")
    await db.refresh(convo)
    assert convo.status is ConversationStatus.ESCALATED
    assert convo.patient_id == patient.id  # sanity: this row really was bound
    return convo


# ===================================================================== #
# Service layer: the property that matters
# ===================================================================== #


@pytest.mark.asyncio
async def test_reopen_sets_expired_not_active_and_records_audit_fields(db, front_desk_account):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()
    await conversation_service.escalate(db, conversation_id=convo.id, reason="lockout")

    reopened = await conversation_service.reopen(
        db,
        conversation_id=convo.id,
        staff_id=front_desk_account.id,
        reason="caller called the front desk directly",
    )

    # EXPIRED, not ACTIVE -- see the service docstring: this is what routes
    # the next message through get_or_create_conversation's existing
    # fall-through path instead of a bespoke one.
    assert reopened.status is ConversationStatus.EXPIRED
    assert reopened.reopened_at is not None
    assert reopened.reopened_by_staff_id == front_desk_account.id
    assert reopened.reopen_reason == "caller called the front desk directly"


@pytest.mark.asyncio
async def test_reopened_conversation_never_carries_identity_forward(db, front_desk_account, patient):
    """The actual requirement: the NEXT conversation for this phone number
    must be indistinguishable from a first-time caller's."""
    old = await _escalated_after_verification(db, patient)

    await conversation_service.reopen(
        db, conversation_id=old.id, staff_id=front_desk_account.id, reason="patient called in"
    )

    fresh = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=patient.phone
    )
    await db.commit()

    assert fresh.id != old.id
    assert fresh.status is ConversationStatus.ACTIVE
    assert fresh.patient_id is None
    assert fresh.identity_verified_at is None
    assert fresh.identity_attempts == 0

    # The OLD row is untouched history, not silently rewritten -- reopen
    # must not have reached back and cleared what actually happened.
    await db.refresh(old)
    assert old.patient_id == patient.id
    assert old.status is ConversationStatus.EXPIRED


@pytest.mark.asyncio
async def test_reopen_completed_conversation_also_works(db, front_desk_account):
    """COMPLETED has no producer yet anywhere in this codebase, but it is
    a declared terminal status alongside ESCALATED -- reopen must treat it
    the same way. Set directly since nothing else can reach it yet."""
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    convo.status = ConversationStatus.COMPLETED
    await db.commit()

    reopened = await conversation_service.reopen(
        db, conversation_id=convo.id, staff_id=front_desk_account.id, reason="test"
    )
    assert reopened.status is ConversationStatus.EXPIRED


@pytest.mark.asyncio
async def test_reopen_refuses_an_active_conversation(db, front_desk_account):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()

    with pytest.raises(ConversationNotTerminalError):
        await conversation_service.reopen(
            db, conversation_id=convo.id, staff_id=front_desk_account.id, reason="nothing to release"
        )


@pytest.mark.asyncio
async def test_reopen_refuses_an_already_expired_conversation(db, front_desk_account):
    """EXPIRED resolves itself on the next message -- reopening it again
    would be a no-op dressed up as an action; refuse it the same as ACTIVE."""
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    convo.status = ConversationStatus.EXPIRED
    await db.commit()

    with pytest.raises(ConversationNotTerminalError):
        await conversation_service.reopen(
            db, conversation_id=convo.id, staff_id=front_desk_account.id, reason="already resolves itself"
        )


@pytest.mark.asyncio
async def test_reopen_unknown_conversation_raises_not_found(db, front_desk_account):
    with pytest.raises(ConversationNotFoundError):
        await conversation_service.reopen(
            db, conversation_id=uuid.uuid4(), staff_id=front_desk_account.id, reason="test"
        )


@pytest.mark.asyncio
async def test_reopen_survives_a_cold_identity_map(db, front_desk_account):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()
    await conversation_service.escalate(db, conversation_id=convo.id, reason="test")
    convo_id = convo.id
    staff_id = front_desk_account.id
    db.expunge_all()

    reopened = await conversation_service.reopen(
        db, conversation_id=convo_id, staff_id=staff_id, reason="cold map check"
    )
    assert reopened.status is ConversationStatus.EXPIRED


# ===================================================================== #
# Schema: the pairing invariant backed by a CHECK, not just the service
# ===================================================================== #


@pytest.mark.asyncio
async def test_check_rejects_reopened_at_without_staff_id(db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()

    with pytest.raises(IntegrityError):
        await db.execute(
            text("UPDATE conversations SET reopened_at = now() WHERE id = :id"),
            {"id": convo.id},
        )
    await db.rollback()


@pytest.mark.asyncio
async def test_check_rejects_staff_id_without_reopened_at(db, front_desk_account):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()

    with pytest.raises(IntegrityError):
        await db.execute(
            text("UPDATE conversations SET reopened_by_staff_id = :sid WHERE id = :id"),
            {"sid": front_desk_account.id, "id": convo.id},
        )
    await db.rollback()


# ===================================================================== #
# HTTP layer: auth is enforced, and no route here ever asks for a patient
# ===================================================================== #


@pytest.mark.asyncio
async def test_reopen_endpoint_requires_staff_auth(client, db):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()

    resp = await client.patch(
        f"/api/v1/conversations/{convo.id}/reopen", json={"reason": "no token supplied"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_reopen_endpoint_success_front_desk(client, db, front_desk_account):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()
    await conversation_service.escalate(db, conversation_id=convo.id, reason="lockout")

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "desk-reopen@clinic.example.com", "password": "correct-horse-battery"},
    )
    token = login.json()["access_token"]

    resp = await client.patch(
        f"/api/v1/conversations/{convo.id}/reopen",
        json={"reason": "caller phoned the front desk"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "expired"
    assert body["reopened_by_staff_id"] == str(front_desk_account.id)
    assert body["reopen_reason"] == "caller phoned the front desk"
    # Structural proof, not just a code-reading claim: the response shape
    # has no patient_id field at all.
    assert "patient_id" not in body


@pytest.mark.asyncio
async def test_reopen_endpoint_success_doctor_role_too(client, db, doctor_account):
    """No doctor-scoping on this router by design -- conversations have no
    doctor_id. Any authenticated staff role may reopen."""
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()
    await conversation_service.escalate(db, conversation_id=convo.id, reason="lockout")

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "doc-reopen@clinic.example.com", "password": "staple-correct-horse"},
    )
    token = login.json()["access_token"]

    resp = await client.patch(
        f"/api/v1/conversations/{convo.id}/reopen",
        json={"reason": "doctor took the call directly"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_reopen_endpoint_rejects_active_conversation(client, db, front_desk_account):
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "desk-reopen@clinic.example.com", "password": "correct-horse-battery"},
    )
    token = login.json()["access_token"]

    resp = await client.patch(
        f"/api/v1/conversations/{convo.id}/reopen",
        json={"reason": "nothing to release"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_reopen_endpoint_unknown_conversation_is_404(client, front_desk_account):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "desk-reopen@clinic.example.com", "password": "correct-horse-battery"},
    )
    token = login.json()["access_token"]

    resp = await client.patch(
        f"/api/v1/conversations/{uuid.uuid4()}/reopen",
        json={"reason": "test"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reopen_endpoint_request_schema_has_no_patient_field(client, db, front_desk_account):
    """Even a well-intentioned caller passing an extra patient_id must not
    have it do anything -- Pydantic ignores unknown fields by default, and
    ConversationReopen declares no such field to ignore INTO."""
    convo = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref=EXTERNAL_REF
    )
    await db.commit()
    await conversation_service.escalate(db, conversation_id=convo.id, reason="lockout")

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "desk-reopen@clinic.example.com", "password": "correct-horse-battery"},
    )
    token = login.json()["access_token"]

    resp = await client.patch(
        f"/api/v1/conversations/{convo.id}/reopen",
        json={"reason": "test", "patient_id": str(uuid.uuid4()), "identity_verified": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    row = await db.scalar(select(Conversation).where(Conversation.id == convo.id))
    await db.refresh(row)
    assert row.patient_id is None
    assert row.identity_verified_at is None
