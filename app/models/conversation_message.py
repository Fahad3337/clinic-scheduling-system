"""The durable transcript of a conversation.

WHY WE KEEP OUR OWN TRANSCRIPT AT ALL, given the provider can hold
conversation state for us: see the `store=False` decision in
integrations/gemini.py. In short -- this table is the only copy, by
design, and it is the audit record of what a bot told a patient about
their medical appointments. That is not a thing to outsource.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import EncryptedString
from app.models.enums import MessageRole

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class ConversationMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversation_messages"

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )

    # Monotonic within a conversation. NOT created_at ordering: two rows
    # written in the same millisecond (an assistant tool-call step and its
    # result) would tie, and replaying a transcript in the wrong order to
    # a model produces nonsense. An explicit sequence makes the order a
    # fact rather than a timing accident, and makes a gap visible.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    role = mapped_column(
        SAEnum(
            MessageRole,
            name="message_role",
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
            create_constraint=True,
        ),
        nullable=False,
    )

    # ENCRYPTED AT REST, per the decision taken before this was built.
    #
    # This is patient-authored free text on a medical channel: "I need to
    # come in about the chest pain again". It is the most sensitive column
    # in the database -- more so than an appointment time -- and unlike
    # date_of_birth (stored plain, see models/patient.py) there is no
    # query that needs to match on it, so encryption costs nothing here.
    # That asymmetry is the whole reason the two decisions differ.
    content: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    # Tool calls and tool results, as JSON. Encrypted for the same reason:
    # a tool result carries appointment times and a patient's name.
    tool_payload: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    # Plain, deliberately: a tool NAME is not patient data, and having it
    # queryable without decrypting every row is what makes "how often does
    # the bot escalate" answerable.
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The provider's id for an inbound message (Twilio's MessageSid).
    #
    # THE WEBHOOK IDEMPOTENCY KEY. Twilio retries deliveries, and a retried
    # webhook must not be processed as a second patient message -- the bot
    # would answer twice and could act twice. Same shape as the
    # notifications dedupe key: the uniqueness is the guarantee, not the
    # handler's care.
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    conversation: Mapped["Conversation"] = relationship()

    __table_args__ = (
        Index("uq_conversation_messages_conversation_id_sequence", "conversation_id", "sequence", unique=True),
        # PARTIAL unique: only inbound rows carry a provider id, and NULLs
        # would otherwise all be distinct anyway -- stating it as partial
        # keeps the index to the rows that actually need enforcing.
        Index(
            "uq_conversation_messages_provider_message_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        CheckConstraint("sequence >= 0", name="sequence_non_negative"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # No content: this is the PHI row.
        return f"<ConversationMessage {self.conversation_id}#{self.sequence} {self.role.value}>"
