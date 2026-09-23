"""Notification outbox + audit trail. This is where send-once is enforced.

THE REQUIREMENT: "if a reminder job fails and retries, it must NOT send the
SMS twice."

WHY `if notification.sent_at is None: send()` DOES NOT WORK
-----------------------------------------------------------
It is the identical bug to Phase 1's double-booking, wearing a different hat:

    worker A                        worker B
    SELECT ... sent_at IS NULL      SELECT ... sent_at IS NULL
    -> nothing sent yet             -> nothing sent yet
    twilio.send()                   twilio.send()      <-- patient gets two

Check-then-act across two statements is never atomic under READ COMMITTED.
Two scheduler replicas, an overlapping retry, or a job that runs long enough
to be started again all reproduce it. As in Phase 1, the fix is to make the
database enforce the invariant rather than to write a more careful check.

THE TWO MECHANISMS (they solve different problems -- both are needed)
---------------------------------------------------------------------
1. UNIQUE(dedupe_key) -- prevents DUPLICATE ROWS.
   The key is deterministic (see build_dedupe_key), so "the 24h SMS reminder
   for appointment X" can only ever exist once as a row. Whatever creates
   notifications does so with:

       INSERT INTO notifications (...) VALUES (...)
       ON CONFLICT (dedupe_key) DO NOTHING

   One atomic statement, no read-then-write window. If the booking service
   runs twice, or the scheduler double-fires, the second insert is a no-op.
   This is the guarantee, and it holds no matter how many schedulers run.

2. SELECT ... FOR UPDATE SKIP LOCKED -- prevents TWO WORKERS GRABBING ONE ROW.
   The unique constraint stops duplicate rows; it does nothing to stop two
   senders picking up the same existing row simultaneously. The sender job
   claims work with:

       SELECT * FROM notifications
        WHERE status IN ('pending','failed') AND scheduled_for <= now()
        ORDER BY scheduled_for
        LIMIT :batch
        FOR UPDATE SKIP LOCKED

   SKIP LOCKED is the key difference from Phase 1's plain FOR UPDATE. In
   Phase 1 the loser should WAIT (it wants that specific slot). Here the
   loser should move on to different work -- blocking would serialize the
   whole queue behind one row. SKIP LOCKED makes N workers pull disjoint
   batches with no coordination and no Redis.

THE ORDERING PROBLEM (the part most implementations get wrong)
--------------------------------------------------------------
Claiming and sending cannot be atomic, because Twilio is not in our
transaction. Something must be decided:

  Option A -- commit the claim BEFORE calling Twilio.
      Crash after commit, before send  -> message never goes out (a MISS).
      Crash after send, before recording -> row sits in CLAIMED (UNRESOLVED).
      Worst case: at-most-once. A patient misses a reminder.

  Option B -- hold the transaction open across the Twilio call.
      Crash anywhere -> the claim rolls back, the row returns to PENDING,
      the retry sends AGAIN even though Twilio already delivered.
      Worst case: at-least-once. A patient gets duplicate 3am texts.

WE CHOOSE A. That is a PRODUCT decision, not a technical one, and it should
be revisited with the clinic: a duplicate SMS is a visible annoyance that
erodes trust in the system, while a missed reminder degrades to the status
quo before this feature existed. Some clinics would choose B for reminders
about procedures with fasting requirements. FLAGGED.

Note what this means honestly: exactly-once delivery does not exist across a
network boundary. You pick a failure direction and you make the ambiguous
case visible (status=UNRESOLVED) instead of pretending it cannot happen.

REMAINING WINDOW, and how to close it: if the provider supports an
idempotency key on send, pass `dedupe_key` as that key. Then even a
duplicate call collapses provider-side and Option B becomes safe too.
VERIFY against current Twilio/SendGrid docs before relying on it -- provider
idempotency support varies by endpoint and changes over time. Until then,
`provider_message_id` plus a reconciliation job querying the provider's API
for messages matching our key is the fallback for resolving UNRESOLVED rows.

PHASE 3.5: THIS TABLE ALSO CARRIES STAFF-FACING NOTIFICATIONS, NOT ONLY
PATIENT-FACING ONES -- specifically, "a conversation escalated to a
human". That message is about a CONVERSATION, and may exist before any
appointment ever does (identity verification can fail, and escalate,
before a single tool call touches a booking) -- so it cannot hang off
`appointment_id` the way every other kind here does.

Rather than a second table (and a second copy of send-once, retries and
the audit trail), `appointment_id` became NULLABLE and a parallel
NULLABLE `conversation_id` was added, with a CHECK enforcing that EXACTLY
ONE of the two is set, keyed off `kind`. This is the SAME idiom already
used for `booking_proposals.kind_matches_target` -- one row, one `kind`,
exactly one target column populated, enforced by the database rather
than by callers remembering to leave the other NULL. See
`build_escalation_dedupe_key` for the parallel dedupe-key shape this
needs (appointment-kind rows key off appointment+slot; escalation rows
have neither, and key off conversation+channel instead).
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
from app.models.enums import NotificationChannel, NotificationKind, NotificationStatus

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.conversation import Conversation

# Statuses the sender job is allowed to pick up. Mirrored by the partial
# index below -- change one, change the other.
CLAIMABLE_STATUSES = (NotificationStatus.PENDING, NotificationStatus.FAILED)

DEFAULT_MAX_ATTEMPTS = 3


def build_dedupe_key(
    *,
    kind: NotificationKind,
    channel: NotificationChannel,
    appointment_id: UUID,
    time_slot_id: UUID,
) -> str:
    """Deterministic idempotency key for one logical message.

    WHY time_slot_id is in the key even though appointment_id looks
    sufficient: in Phase 1, rescheduling produces a NEW appointment row, so
    appointment_id alone would be fine today. But the moment Phase 3 adds an
    in-place reschedule (mutating time_slot_id on an existing appointment),
    a key without the slot would treat "reminder for the old 10:00" and
    "reminder for the new 14:00" as the same message -- and the patient would
    never be reminded about the time that actually matters. Including the
    slot makes the key correct under both designs.

    WHY channel is in the key: the same appointment legitimately gets both an
    SMS and an email confirmation. Those are two messages, not one.
    """
    return f"{kind.value}:{channel.value}:{appointment_id}:{time_slot_id}"


def build_escalation_dedupe_key(*, channel: NotificationChannel, conversation_id: UUID) -> str:
    """Idempotency key for a staff escalation notice.

    One conversation escalates AT MOST ONCE -- `conversation_service._escalate`
    is only reachable from an ACTIVE conversation, and escalating makes it
    terminal (see get_or_create_conversation's docstring on why terminal
    states are never superseded). So the key needs nothing beyond
    conversation + channel; there is no "which slot" or "which attempt"
    dimension the way there is for appointment reminders.

    WHY A SEPARATE FUNCTION rather than overloading build_dedupe_key with
    optional appointment/conversation arguments: a function whose
    parameters are sometimes required and sometimes forbidden depending on
    a THIRD parameter is exactly the shape of bug this whole table's CHECK
    constraint exists to prevent at the schema level. Two small, fully
    -required-argument functions are harder to call wrong than one
    flexible one.
    """
    return f"escalation:{channel.value}:{conversation_id}"


def _enum_column(enum_cls, name: str, length: int = 32, **kw):
    return mapped_column(
        SAEnum(
            enum_cls,
            name=name,
            native_enum=False,
            length=length,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
            # create_constraint=True so the CHECK that models/enums.py
            # PROMISES ("stored as VARCHAR + CHECK") actually exists in the
            # metadata. SQLAlchemy defaults this to False, which means the
            # constraint silently would not exist -- and, worse, would differ
            # between `create_all` (tests) and the migration (production).
            # Declaring it here keeps autogenerate, tests and prod identical.
            create_constraint=True,
        ),
        nullable=False,
        **kw,
    )


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notifications"

    # NULLABLE as of Phase 3.5 -- see the module docstring. Exactly one of
    # appointment_id / conversation_id is set, enforced by
    # ck_notifications_kind_matches_target below, keyed on `kind`.
    appointment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=True
    )
    # RESTRICT, matching Phase 1: this table is the audit trail of what we
    # told a patient and when. "We reminded you on the 20th" is exactly the
    # kind of record a no-show dispute turns on, so it must outlive casual
    # deletes.

    # Set only for kind='escalation'. RESTRICT for the same reason as
    # appointment_id: "staff were notified of this escalation at this
    # time" is itself an audit-relevant fact -- arguably more so, since it
    # is the proof that the notify-on-escalation policy (Phase 3.5) was
    # actually followed, not just designed.
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="RESTRICT"), nullable=True
    )

    kind = _enum_column(NotificationKind, "notification_kind")
    channel = _enum_column(NotificationChannel, "notification_channel", length=16)
    status = _enum_column(
        NotificationStatus,
        "notification_status",
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
    )

    # THE idempotency key. See the module docstring.
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # Snapshot of where we sent it, captured at creation time rather than
    # read from patient.phone at send time. WHY: if the patient updates their
    # number between booking and the reminder, the audit trail must say where
    # the message ACTUALLY went, not where it would go today.
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)

    # When this becomes eligible to send. For a booking confirmation this is
    # now(); for the 24h reminder it is starts_at - 24h.
    #
    # WHY the sender polls `scheduled_for <= now()` rather than scanning for
    # appointments in a 23-25h window: a window scan permanently LOSES work
    # if the worker is down while the window passes. A `<= now()` query is a
    # catch-up query -- after an outage it simply finds everything overdue
    # and drains it. (Whether a 4-hours-late "24h reminder" should still go
    # out is a policy question; the sender applies a staleness cutoff.)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_ATTEMPTS, server_default=str(DEFAULT_MAX_ATTEMPTS)
    )

    # Set when a worker claims the row, cleared/superseded on completion. A
    # row stuck in CLAIMED with an old claimed_at is the crash signature the
    # reaper looks for before marking it UNRESOLVED.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Twilio message SID / SendGrid message id. The handle for reconciling an
    # UNRESOLVED row against the provider, and for support ("did it send?").
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    appointment: Mapped["Appointment | None"] = relationship(back_populates="notifications")
    conversation: Mapped["Conversation | None"] = relationship()

    __table_args__ = (
        # ===================================================================
        # THE send-once GUARANTEE. Everything else is ergonomics.
        # ===================================================================
        Index("uq_notifications_dedupe_key", "dedupe_key", unique=True),
        # Drives the sender's claim query. PARTIAL, so the index covers only
        # outstanding work: a clinic accumulates millions of SENT rows over
        # time and none of them are ever claim candidates. Same reasoning as
        # Phase 1's partial booking index -- keep the hot index small.
        Index(
            "ix_notifications_claimable",
            "scheduled_for",
            postgresql_where=text("status IN ('pending','failed')"),
        ),
        # Both partial now: appointment_id/conversation_id are each NULL for
        # roughly half the table's kinds, and an index over mostly-NULL
        # values earns nothing.
        Index(
            "ix_notifications_appointment_id", "appointment_id",
            postgresql_where=text("appointment_id IS NOT NULL"),
        ),
        Index(
            "ix_notifications_conversation_id", "conversation_id",
            postgresql_where=text("conversation_id IS NOT NULL"),
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # Integrity guard: a row cannot claim to be sent without a timestamp.
        # Cheap insurance against a code path that sets one and not the other.
        CheckConstraint(
            "status <> 'sent' OR sent_at IS NOT NULL", name="sent_requires_sent_at"
        ),
        # THE Phase 3.5 widening's own guard: exactly one target per row,
        # determined by kind. Same idiom as
        # booking_proposals.kind_matches_target -- see that model's
        # docstring for why this belongs in the schema rather than in every
        # caller's discipline.
        CheckConstraint(
            "(kind <> 'escalation' AND appointment_id IS NOT NULL AND conversation_id IS NULL) OR "
            "(kind = 'escalation' AND appointment_id IS NULL AND conversation_id IS NOT NULL)",
            name="kind_matches_target",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Notification {self.kind.value}/{self.channel.value} {self.status.value}>"
