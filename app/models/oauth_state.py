"""Short-lived server-side state for an in-flight OAuth authorization.

WHY THIS TABLE EXISTS AT ALL (a stateless signed `state` would be tempting)
---------------------------------------------------------------------------
The OAuth `state` parameter defends against CSRF: without it, an attacker can
craft a callback URL containing THEIR authorization code and trick a logged-in
user's browser into visiting it, silently connecting the attacker's calendar
to the victim's account. So `state` must be unguessable and verified.

A signed/HMAC'd stateless token would satisfy "unguessable and verifiable"
without a table. It was rejected for two reasons:

  1. SINGLE USE. A stateless token is replayable for its whole lifetime --
     anyone who observes one (browser history, a proxy log, a Referer header)
     can resubmit it. A row we delete-on-use makes replay impossible, and the
     claim is a single atomic UPDATE, so even two simultaneous callbacks
     cannot both succeed. Same check-then-act reasoning as the booking and
     notification paths.

  2. PKCE NEEDS A SECRET AT REST. The code_verifier must survive between the
     authorize call and the callback, and it must NOT travel through the
     browser -- that is the entire point of PKCE. Stuffing it into the state
     token would hand it to anyone who sees the URL, defeating the mechanism.

COST: a table, a migration, and rows that need pruning. Cheap, and the rows
are tiny and short-lived.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import EncryptedString


class OAuthState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "oauth_states"

    # The opaque value handed to Google and echoed back to us. Generated with
    # secrets.token_urlsafe -- a CSPRNG, never uuid4 or random.
    state_token: Mapped[str] = mapped_column(String(128), nullable=False)

    doctor_id: Mapped[UUID] = mapped_column(
        ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False
    )

    # PKCE code_verifier. Encrypted for the same reason refresh tokens are:
    # it is a bearer secret, and a leaked database backup should not contain
    # usable ones. Short-lived, but "short-lived" is not "harmless".
    code_verifier: Mapped[str] = mapped_column(EncryptedString, nullable=False)

    # The exact redirect_uri sent in the authorize request. Echoed back on the
    # token exchange because Google requires the two to match byte-for-byte,
    # and pinning it here means a config change mid-flight cannot silently
    # break an authorization that is already in progress.
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Set atomically when the callback claims this row. A non-NULL value means
    # the state has already been used and any further attempt is a replay.
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # UNIQUE so a duplicate token can never exist; the callback looks the
        # row up by this value.
        Index("uq_oauth_states_state_token", "state_token", unique=True),
        # Supports the cleanup job that deletes expired rows.
        # TODO(phase-2-jobs): schedule that cleanup. Until it exists this
        # table grows by one row per authorization attempt forever. Harmless
        # at clinic scale, but it is unbounded, which is never fine long-term.
        Index("ix_oauth_states_expires_at", "expires_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # No state_token, no code_verifier: both are secrets and __repr__
        # ends up in tracebacks and log lines.
        return f"<OAuthState doctor={self.doctor_id} consumed={self.consumed_at is not None}>"
