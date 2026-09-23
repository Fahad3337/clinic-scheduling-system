"""A proposed mutation, created by the service, executed later by id.

THE TWO-PHASE COMMIT THAT KEEPS THE LANGUAGE MODEL OUT OF THE TRUST PATH.

The model never names the thing being mutated at the moment of mutation.
It can only say "confirm proposal X" -- and X is a row THIS SERVICE wrote,
for THIS conversation, describing exactly one slot or one appointment. The
worst a hallucinated or injected id can do is fail to resolve.

Compare the alternative, where the model calls `book(time_slot_id=...)`
directly: then any id the model emits -- hallucinated, or injected by a
patient typing "book slot 7f3a..." -- is acted on. The proposal indirection
means the set of things confirmable at any moment is exactly the set the
service already decided was legitimate for this caller.

WHY THIS ALSO EXISTS FOR CANCELLATION, not just booking: cancelling is the
destructive one. The same argument applies with more force.
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ProposalKind

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.conversation import Conversation
    from app.models.time_slot import TimeSlot


class BookingProposal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "booking_proposals"

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )

    # Denormalized from the conversation AT PROPOSAL TIME, and re-checked at
    # confirm time. WHY both: it pins the proposal to the patient who was
    # verified when it was made. If a conversation could ever re-bind to a
    # different patient (re-verification, a handover, a bug), a proposal
    # created for the first patient must not become confirmable for the
    # second.
    patient_id: Mapped[UUID] = mapped_column(
        ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )

    kind = mapped_column(
        SAEnum(
            ProposalKind,
            name="proposal_kind",
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
            create_constraint=True,
        ),
        nullable=False,
    )

    # Exactly one of these is set, per the CHECK below.
    time_slot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("time_slots.id", ondelete="RESTRICT"), nullable=True
    )
    appointment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=True
    )

    # Short-lived. A proposal is an offer made in a conversation, and an
    # offer from twenty minutes ago is stale: the slot may be gone, and the
    # patient has probably moved on. Expiry also bounds how long a leaked
    # proposal id is worth anything.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Set atomically by the confirm step. Single-use: the same conditional
    # UPDATE pattern as oauth_states, for the same reason -- two confirms
    # arriving together must not both succeed.
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    conversation: Mapped["Conversation"] = relationship()
    time_slot: Mapped["TimeSlot | None"] = relationship()
    appointment: Mapped["Appointment | None"] = relationship()

    __table_args__ = (
        # A BOOK proposal points at a slot; a CANCEL proposal points at an
        # appointment. Neither may point at both or at nothing -- the
        # confirm handlers would then have to guess what they were
        # confirming.
        CheckConstraint(
            "(kind = 'book' AND time_slot_id IS NOT NULL AND appointment_id IS NULL) OR "
            "(kind = 'cancel' AND appointment_id IS NOT NULL AND time_slot_id IS NULL)",
            name="kind_matches_target",
        ),
        # The confirm lookup: unconsumed proposals for a conversation.
        Index(
            "ix_booking_proposals_conversation_id",
            "conversation_id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BookingProposal {self.id} {self.kind.value} consumed={self.consumed_at is not None}>"
