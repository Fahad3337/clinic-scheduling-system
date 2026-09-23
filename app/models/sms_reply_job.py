"""Inbound SMS awaiting processing and a reply. The third outbox.

WHY THIS EXISTS: Twilio gives a webhook roughly 15 seconds. A
conversation turn is a model call plus up to five tool calls, which can
exceed that. Blowing Twilio's deadline means the patient gets NOTHING.
So the webhook stops doing the work: it validates, records the inbound
message as a job, and returns an empty TwiML immediately. The worker
runs the loop and sends the reply through Twilio's REST API.

THE IMPORTANT DIFFERENCE FROM THE OTHER TWO OUTBOXES, and the reason
this table has a `reply_body` column rather than just a status:

  The calendar-push and notification outboxes wrap a SINGLE idempotent
  side effect -- create an event, send a message. Retrying a claimed row
  is safe because the operation is the same operation.

  This job wraps TWO side effects of different kinds: running the
  conversation loop (which books appointments, consumes proposals,
  burns identity attempts, and appends to the transcript) and then
  sending an SMS. Re-running the loop on a message already processed is
  NOT safe -- it would re-append the patient's turn, re-invoke the
  model, and potentially act twice.

  So the phases are split by a persisted marker. `reply_body IS NULL`
  means the loop has not run. Once it has, the reply is committed
  BEFORE the send is attempted, and any retry skips straight to sending
  the text already decided. A crash between the two phases therefore
  costs a delayed SMS, never a duplicated booking.

Statuses reuse NotificationStatus deliberately -- the lifecycle is
identical (pending -> claimed -> sent/failed/abandoned, plus UNRESOLVED
for a crash mid-send) and the at-most-once reasoning is the same one
argued at length in models/notification.py. A parallel enum with the
same members would only create somewhere for the two to drift apart.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import EncryptedString
from app.models.enums import NotificationStatus

if TYPE_CHECKING:
    from app.models.conversation import Conversation

DEFAULT_MAX_ATTEMPTS = 3


class SmsReplyJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sms_reply_jobs"

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )

    # Twilio's MessageSid for the INBOUND message.
    #
    # THE INGESTION IDEMPOTENCY KEY, and it has to live here rather than
    # only on conversation_messages: the webhook now returns before the
    # loop has run, so a Twilio retry arriving in that window would find
    # no message row yet and happily enqueue a second job. The unique
    # index on this column is what makes the ingestion point itself
    # idempotent. (conversation_messages keeps its own unique index too
    # -- belt and braces, one per layer.)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Patient-authored text. Encrypted for exactly the reasons given in
    # models/conversation_message.py -- this is the same data, briefly
    # living in a second place while it waits to be processed.
    inbound_body: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    # Snapshot of where the reply goes, captured at ingestion rather than
    # read from the conversation at send time. Same reasoning as
    # notifications.recipient: the audit trail must record where the
    # message ACTUALLY went.
    reply_to: Mapped[str] = mapped_column(String(32), nullable=False)

    # NULL until the loop has run. See the module docstring -- this is
    # the marker that makes a retry safe.
    reply_body: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    status = mapped_column(
        SAEnum(
            NotificationStatus,
            name="notification_status",
            native_enum=False,
            length=32,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
            create_constraint=True,
        ),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_ATTEMPTS, server_default=str(DEFAULT_MAX_ATTEMPTS)
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Twilio's SID for the message WE sent -- the handle for reconciling
    # an UNRESOLVED row against the provider.
    provider_send_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    conversation: Mapped["Conversation"] = relationship()

    __table_args__ = (
        Index("uq_sms_reply_jobs_provider_message_id", "provider_message_id", unique=True),
        # The worker's claim query. Partial for the same reason as the
        # notification one: sent rows accumulate forever and are never
        # work.
        Index(
            "ix_sms_reply_jobs_claimable",
            "created_at",
            postgresql_where=text("status IN ('pending','failed')"),
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("status <> 'sent' OR sent_at IS NOT NULL", name="sent_requires_sent_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SmsReplyJob {self.id} {self.status.value} replied={self.reply_body is not None}>"
