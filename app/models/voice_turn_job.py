"""Inbound voice turn awaiting background processing. The fourth outbox.

WHY THIS EXISTS: Twilio gives a voice webhook roughly 15 seconds, and a
live measurement during Phase 4 verification put a SINGLE Gemini call at
12.6s -- a normal turn needing two sequential calls (identity, then the
answer) cannot fit inside that budget under ANY reasonable in-request
deadline. See docs/phase-4-voice-transport-decision.md's poll/redirect
section (the mitigation it named up front, not built until this
measurement showed the trigger condition had actually been hit) and
api/v1/webhooks.py's module docstring for the number itself.

THE SHAPE: the Gather webhook does the fast part only (validate, load or
create the conversation, enqueue this job) and replies with "please
hold" TwiML that redirects into a poll loop. The worker actually runs
the conversation loop -- same job-draining shape as sms_reply_service --
with NO Twilio deadline pressure at all, exactly like SMS's existing
worker. Each poll redirect after that is its OWN separate, fast webhook
request (read one row, decide hold-or-speak), so Twilio's per-request
budget is never at risk regardless of how long the underlying model
call takes. The CUMULATIVE wait is bounded instead by
voice_hold_poll_max_cycles x voice_hold_poll_interval_seconds -- a
budget this application controls, not Twilio.

SIMPLER THAN SmsReplyJob, ON PURPOSE. SMS's job wraps TWO side effects
(run the loop, then send via Twilio's REST API), which is why it needs
a reply_body phase marker AND provider_send_id AND an UNRESOLVED status
for a send that might have gone out before a crash. Voice has no
separate send step -- "delivery" IS the next poll's TwiML response,
read straight from this row. So this table wraps exactly ONE side
effect (run the loop), and reply_body is purely a completion marker,
never a send-ambiguity marker.

NO CONTENT-BASED DEDUPE KEY, AND THAT IS A KNOWN, ACCEPTED GAP. Twilio
gives no per-utterance id for voice the way MessageSid is one value per
SMS -- CallSid is one value per CALL, covering every turn in it. A
retried initial Gather POST could enqueue a second job for the same
utterance. A content-hash dedupe key was considered and rejected: it
would wrongly collide two DIFFERENT turns where a caller genuinely
repeats the same words on purpose -- exactly what a DOB correction cycle
can look like. Accepted for the same reason this gap was already
accepted before this table existed (see the prior version of
twilio_voice_gather): the two-phase propose/confirm layer already makes
a duplicated turn structurally safe against a double-booking, and this
table's own claim-once semantics (SELECT ... FOR UPDATE SKIP LOCKED)
make any ONE job processed at most once regardless.

STATUSES REUSE NotificationStatus, same reasoning as SmsReplyJob: one
enum, one lifecycle, no second copy to drift out of sync. The reuse is
LOOSER here than for SMS -- 'sent' means "processed, reply ready to be
spoken", not "delivered" -- recorded here so it doesn't read as a
copy-paste mismatch. FAILED is not used: max_attempts defaults to 1,
because a caller is actively holding on a live call, and a background
retry-over-minutes strategy (the right call for SMS, where nobody is
watching a clock) does not fit that. Any unexpected failure goes
straight to ABANDONED; the poll loop is what tells the caller.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
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

# Deliberately 1, not SmsReplyJob's 3 -- see the module docstring.
DEFAULT_MAX_ATTEMPTS = 1


class VoiceTurnJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voice_turn_jobs"

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )

    # Audit only, NOT a dedupe key -- see the module docstring. Which
    # call this turn belongs to, for anyone debugging a stuck job.
    call_sid: Mapped[str] = mapped_column(String(64), nullable=False)

    # Caller-authored text (Twilio's SpeechResult). Encrypted for the
    # same reason as conversation_messages/sms_reply_jobs -- this is the
    # same patient-authored content, briefly living in a second place
    # while it waits to be processed.
    inbound_text: Mapped[str] = mapped_column(EncryptedString, nullable=False)

    # NULL until the loop has run. The marker that makes a retry (or a
    # reap after a crash) safe: a job whose reply is already decided
    # never re-invokes the model.
    reply_body: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    # Set alongside reply_body. Tells the poll handler whether to hang up
    # or open another Gather once it speaks the reply.
    conversation_ended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

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
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    conversation: Mapped["Conversation"] = relationship()

    __table_args__ = (
        # The worker's claim query. Partial -- processed/abandoned rows
        # accumulate forever and are never work. No 'failed' in the
        # predicate (unlike sms_reply_jobs): this table never uses that
        # status, see the module docstring.
        Index(
            "ix_voice_turn_jobs_claimable",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "status <> 'sent' OR reply_body IS NOT NULL", name="sent_requires_reply_body"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<VoiceTurnJob {self.id} {self.status.value} processed={self.reply_body is not None}>"
