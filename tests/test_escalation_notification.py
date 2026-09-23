"""Phase 3.5, item 1: staff paging on escalation.

Covers the notification-outbox side only. The lockout-bypass fix this
sits alongside (terminal conversations never superseded) is tested in
tests/test_injection_suite.py; this file is about "does the page
actually get queued, rendered and sent", not identity.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.models.enums import (
    ConversationChannel,
    ConversationStatus,
    NotificationChannel,
    NotificationKind,
    NotificationStatus,
)
from app.models.notification import Notification, build_escalation_dedupe_key
from app.services import conversation_service, notification_service

DESTINATION = "+14155559000"


class FakeSender:
    def __init__(self):
        self.sends: list[dict] = []

    async def send(self, *, recipient: str, body: str, subject: str | None = None) -> str:
        self.sends.append({"recipient": recipient, "body": body, "subject": subject})
        return "SMescalation1"


@pytest.fixture(autouse=True)
def escalation_destination(monkeypatch):
    monkeypatch.setattr(get_settings(), "escalation_notify_phone", DESTINATION, raising=False)


@pytest.fixture
async def convo(db):
    c = await conversation_service.get_or_create_conversation(
        db, channel=ConversationChannel.SMS, external_ref="+14155550123"
    )
    await db.commit()
    return c


# ===================================================================== #
# Enqueue
# ===================================================================== #


@pytest.mark.asyncio
async def test_escalating_queues_a_staff_notification(db, convo):
    result = await conversation_service.escalate(db, conversation_id=convo.id, reason="test reason")
    assert result.status is ConversationStatus.ESCALATED

    notif = await db.scalar(select(Notification).where(Notification.kind == NotificationKind.ESCALATION))
    assert notif is not None
    assert notif.conversation_id == convo.id
    assert notif.appointment_id is None
    assert notif.recipient == DESTINATION
    assert notif.status is NotificationStatus.PENDING


@pytest.mark.asyncio
async def test_escalation_state_change_happens_even_without_a_destination_configured(db, convo, monkeypatch):
    """The escalation itself must never depend on paging being configured."""
    monkeypatch.setattr(get_settings(), "escalation_notify_phone", "", raising=False)

    result = await conversation_service.escalate(db, conversation_id=convo.id, reason="test reason")
    assert result.status is ConversationStatus.ESCALATED  # still happened

    assert await db.scalar(select(Notification).where(Notification.kind == NotificationKind.ESCALATION)) is None


@pytest.mark.asyncio
async def test_dedupe_key_prevents_a_second_page_for_one_conversation(db, convo):
    key = build_escalation_dedupe_key(channel=NotificationChannel.SMS, conversation_id=convo.id)

    # Simulate a duplicate enqueue attempt directly, the way a retried
    # transaction or a bug re-calling _escalate would -- same ON CONFLICT
    # DO NOTHING guarantee as every other outbox in this codebase.
    first = await notification_service._enqueue_escalation(
        db, conversation=convo, recipient=DESTINATION, now=datetime.now(UTC)
    )
    second = await notification_service._enqueue_escalation(
        db, conversation=convo, recipient=DESTINATION, now=datetime.now(UTC)
    )
    await db.commit()

    assert first is True
    assert second is False
    rows = (await db.scalars(select(Notification).where(Notification.dedupe_key == key))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_target_check_rejects_a_row_with_neither_target_set(db, convo):
    """The schema-level guarantee, not the service's discipline: an
    escalation row must point at a conversation. This one points at
    nothing, which the CHECK must refuse regardless of what any Python
    code would have done."""
    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO notifications "
                "(id, conversation_id, appointment_id, kind, channel, dedupe_key, "
                " recipient, scheduled_for, created_at, updated_at) "
                "VALUES (gen_random_uuid(), NULL, NULL, 'escalation', 'sms', "
                " :key, :dest, now(), now(), now())"
            ),
            {"key": "bad-neither-target", "dest": DESTINATION},
        )
    await db.rollback()


@pytest.mark.asyncio
async def test_target_check_rejects_a_row_with_both_targets_set(db, convo, doctor, patient, time_slot):
    """The other half of the same guarantee: an escalation row must NOT
    also claim to be about an appointment."""
    from app.models.appointment import Appointment
    from app.models.enums import AppointmentStatus, BookingChannel

    appt = Appointment(
        patient_id=patient.id, doctor_id=doctor.id, time_slot_id=time_slot.id,
        status=AppointmentStatus.BOOKED, booking_channel=BookingChannel.WEB,
    )
    db.add(appt)
    await db.commit()

    with pytest.raises(IntegrityError):
        await db.execute(
            text(
                "INSERT INTO notifications "
                "(id, conversation_id, appointment_id, kind, channel, dedupe_key, "
                " recipient, scheduled_for, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :cid, :aid, 'escalation', 'sms', "
                " :key, :dest, now(), now(), now())"
            ),
            {"cid": convo.id, "aid": appt.id, "key": "bad-both-targets", "dest": DESTINATION},
        )
    await db.rollback()


# ===================================================================== #
# Rendering
# ===================================================================== #


@pytest.mark.asyncio
async def test_render_body_for_escalation_names_the_caller_and_reason(db, convo):
    convo.escalation_reason = "identity verification failed 3 times"
    db.add(convo)
    await db.commit()

    notif = Notification(
        conversation_id=convo.id,
        kind=NotificationKind.ESCALATION,
        channel=NotificationChannel.SMS,
        dedupe_key="render-test-key",
        recipient=DESTINATION,
        scheduled_for=datetime.now(UTC),
    )
    db.add(notif)
    await db.commit()
    await db.refresh(notif)

    subject, body = await notification_service.render_body(db, notif)
    assert convo.external_ref in body
    assert "identity verification failed" in body
    assert str(convo.id) in body


@pytest.mark.asyncio
async def test_render_body_escalation_works_with_a_cold_identity_map(db, convo):
    notif = Notification(
        conversation_id=convo.id, kind=NotificationKind.ESCALATION, channel=NotificationChannel.SMS,
        dedupe_key="cold-key", recipient=DESTINATION, scheduled_for=datetime.now(UTC),
    )
    db.add(notif)
    await db.commit()
    notif_id = notif.id
    db.expunge_all()

    notif = await db.get(Notification, notif_id)
    subject, body = await notification_service.render_body(db, notif)
    assert subject
    assert body


# ===================================================================== #
# End to end through the sender
# ===================================================================== #


@pytest.mark.asyncio
async def test_escalation_notification_is_sent_through_the_normal_outbox(db, convo):
    """Proves it inherits send-once, retries and the audit trail for
    free -- it is a row in the SAME table the sender already drains."""
    await conversation_service.escalate(db, conversation_id=convo.id, reason="wants a human")

    sender = FakeSender()
    result = await notification_service.send_due_notifications(
        db, senders={NotificationChannel.SMS: sender}
    )

    assert result.sent == 1
    assert sender.sends[0]["recipient"] == DESTINATION
    assert convo.external_ref in sender.sends[0]["body"]

    notif = await db.scalar(select(Notification).where(Notification.kind == NotificationKind.ESCALATION))
    assert notif.status is NotificationStatus.SENT
    assert notif.sent_at is not None


@pytest.mark.asyncio
async def test_escalation_notification_survives_a_cold_identity_map(db, convo):
    await conversation_service.escalate(db, conversation_id=convo.id, reason="test")
    db.expunge_all()

    sender = FakeSender()
    result = await notification_service.send_due_notifications(
        db, senders={NotificationChannel.SMS: sender}
    )
    assert result.sent == 1
