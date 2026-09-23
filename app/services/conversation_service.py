"""Conversation lifecycle and patient identity verification.

Transport-agnostic and LLM-agnostic: this module knows nothing about
Twilio, nothing about tool schemas, and nothing about prompts. It is the
place that decides WHO a conversation is for and WHETHER it may continue.

THE IDENTITY MODEL, stated plainly:

  The phone number IDENTIFIES. The date of birth AUTHENTICATES.

That split matters because caller ID is spoofable and phones are shared.
Treating "this SMS came from +1415..." as proof of identity would mean a
teenager on a parent's phone, or anyone who can spoof a From header, can
read and cancel someone's medical appointments. So the phone selects a
candidate patient and nothing more; the caller must then produce a fact
about that patient before the conversation is bound to them.

This is weaker than real authentication and is not pretending otherwise:
date of birth is knowable by family members and appears in plenty of
leaked data. It is the strongest factor available over SMS without
building an enrollment flow, and it is a large improvement on caller ID
alone. FLAGGED: for anything more sensitive than scheduling (results,
prescriptions), this is not sufficient and a real enrollment/PIN flow is
required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.conversation import MAX_IDENTITY_ATTEMPTS, Conversation
from app.models.enums import ConversationChannel, ConversationStatus
from app.models.patient import Patient
from app.services.exceptions import (
    ConversationNotActiveError,
    ConversationNotFoundError,
    ConversationNotTerminalError,
)

logger = logging.getLogger(__name__)


class IdentityOutcome(str, Enum):
    """What happened when a caller tried to prove who they are.

    These are FACTS the service determined, not instructions to the model.
    What the bot should SAY about each is a prompt concern; what the
    system DOES about each (binding, locking out, escalating) has already
    happened by the time one of these is returned.
    """

    VERIFIED = "verified"
    WRONG_DETAILS = "wrong_details"
    NOT_REGISTERED = "not_registered"
    CANNOT_VERIFY = "cannot_verify"  # patient on file has no DOB recorded
    ESCALATED = "escalated"  # attempts exhausted; conversation is over
    ALREADY_VERIFIED = "already_verified"
    # PHASE 4, voice only. A date was stated but not yet confirmed as
    # correctly heard -- see verify_identity's docstring. NOT a rejection:
    # attempts_remaining is unchanged, and no comparison against a patient
    # record has happened yet.
    PENDING_CONFIRMATION = "pending_confirmation"


@dataclass(frozen=True)
class IdentityResult:
    outcome: IdentityOutcome
    attempts_remaining: int
    patient_first_name: str | None = None
    # Set only when outcome is PENDING_CONFIRMATION -- the value to read
    # back to the caller for a yes/no, in the tool layer's rendering.
    pending_date_of_birth: date | None = None


async def get_or_create_conversation(
    session: AsyncSession,
    *,
    channel: ConversationChannel,
    external_ref: str,
    now: datetime | None = None,
) -> Conversation:
    """Find the conversation for this phone/channel, or start one.

    Called by the transport adapter (the Twilio webhook), never by a
    tool -- a conversation exists before any tool runs.

    SECURITY-CRITICAL BEHAVIOUR, found missing by the injection suite
    (test_10_escalated_conversation_cannot_be_reactivated_by_asking) and
    fixed here rather than papered over at the call site:

    This function used to look ONLY at ACTIVE conversations. Once a
    conversation was ESCALATED, that query found nothing, and the very
    next inbound message on the same phone number silently created a
    brand-new, un-escalated conversation. Two consequences, both real:

      1. The human handoff the patient was told about evaporated with no
         trace -- their next message got a fresh, cooperative bot
         instead of the person they were promised.
      2. WORSE: `identity_attempts` lives on the conversation row. A
         caller locked out after three wrong guesses could simply send
         one more message, get an entirely new conversation with the
         counter back at zero, and try three more guesses -- repeatedly,
         with no limit. The three-strike lockout was not actually a
         lockout against a persistent attacker from one phone number.

    THE FIX: look at the MOST RECENT conversation regardless of status.
    ESCALATED and COMPLETED are treated as PERMANENTLY TERMINAL for this
    phone number -- returned as-is, never superseded by a new row. The
    caller (ultimately loop.handle_message, via load_active_conversation)
    then refuses to run the model at all for a non-ACTIVE conversation,
    which is the correct behaviour and was already implemented -- it
    just never used to be reachable, because a fresh conversation was
    handed over instead.

    ASSUMPTION, FLAGGED: this means an escalated phone number can NEVER
    book through the chatbot again without a human explicitly reopening
    it -- and no such reopening action exists yet in Phase 3. That is
    the safe default given the alternative is a silent lockout bypass,
    but it is a real product gap: build a staff-facing "reopen this
    conversation" action before this becomes a support burden.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    idle_cutoff = now - timedelta(minutes=settings.conversation_ttl_minutes)

    existing = await session.scalar(
        select(Conversation)
        .where(
            Conversation.channel == channel,
            Conversation.external_ref == external_ref,
        )
        .order_by(Conversation.last_activity_at.desc())
        .limit(1)
    )

    if existing is not None:
        if existing.status in (ConversationStatus.ESCALATED, ConversationStatus.COMPLETED):
            # Terminal, permanently, for this phone number. See the
            # docstring -- do not fall through to creating a new one.
            return existing
        if existing.status is ConversationStatus.ACTIVE and existing.last_activity_at < idle_cutoff:
            # Stale. Close it rather than resuming days later with an
            # identity that was verified in a different context entirely.
            existing.status = ConversationStatus.EXPIRED
            await session.flush()
        elif existing.status is ConversationStatus.ACTIVE:
            existing.last_activity_at = now
            await session.flush()
            return existing
        # else: EXPIRED already -- fall through and start a fresh one.
        # An idle timeout is not adversarial; there is no reason to
        # treat it like a terminal escalation.

    conversation = Conversation(
        channel=channel,
        external_ref=external_ref,
        status=ConversationStatus.ACTIVE,
        last_activity_at=now,
    )
    session.add(conversation)
    # Explicit flush so the caller has a usable id -- see the long note in
    # appointment_service._insert_appointment about why this is never left
    # to an incidental autoflush.
    await session.flush()
    return conversation


async def load_active_conversation(
    session: AsyncSession, conversation_id: UUID, *, for_update: bool = False
) -> Conversation:
    """Fetch a conversation, refusing if it is no longer actionable."""
    stmt = select(Conversation).where(Conversation.id == conversation_id)
    if for_update:
        # populate_existing IS LOAD-BEARING, NOT TIDINESS.
        #
        # Without it, `SELECT ... FOR UPDATE` correctly WAITS for the lock
        # and then hands back a STALE object: if this session already
        # loaded this conversation (the guard in chatbot/tools.dispatch
        # does exactly that, one call earlier), the row is in the identity
        # map, and SQLAlchemy returns the mapped instance without
        # overwriting its column values from the row it just locked.
        #
        # The consequence is a real lost update, not a cosmetic staleness:
        # two concurrent wrong DOB guesses both read identity_attempts=0
        # -- the second one AFTER waiting for the lock and after the first
        # committed 1 -- both compute 1, and the three-strike lockout is
        # bypassable by sending guesses in pairs. Caught by
        # test_concurrent_wrong_guesses_each_consume_an_attempt, but only
        # when run repeatedly: it reproduced roughly one run in five.
        #
        # Locking a row and then reading a cached copy of it is not
        # locking. Any FOR UPDATE on a row this session may already hold
        # needs this.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    conversation = await session.scalar(stmt)
    if conversation is None:
        raise ConversationNotFoundError(conversation_id)
    if conversation.status is not ConversationStatus.ACTIVE:
        raise ConversationNotActiveError(conversation_id, conversation.status)
    return conversation


def identity_is_fresh(conversation: Conversation, *, now: datetime | None = None) -> bool:
    """Whether this conversation's verification still counts.

    Presence of `patient_id` is not enough -- see the note on
    Conversation.identity_verified_at about long-lived SMS threads.
    """
    if conversation.patient_id is None or conversation.identity_verified_at is None:
        return False
    now = now or datetime.now(UTC)
    ttl = timedelta(minutes=get_settings().conversation_identity_ttl_minutes)
    return conversation.identity_verified_at + ttl > now


async def verify_identity(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    date_of_birth: date,
    now: datetime | None = None,
) -> IdentityResult:
    """Attempt to bind this conversation to a patient.

    LOCKS the conversation row for the whole check. Two messages arriving
    together (a patient double-texting their DOB) would otherwise both
    read `identity_attempts = 2`, both increment to 3, and burn two
    strikes for one guess -- or worse, both pass a check that should have
    locked out after the first. The counter is only meaningful if the
    read-modify-write is serialized, the same check-then-act reasoning as
    every other lock in this codebase.

    PHASE 4, VOICE READ-BACK-CONFIRM GATE. See
    docs/phase-4-dob-over-voice-decision.md for the full reasoning; this
    is the structural half of that decision (the deterministic half lives
    in the voice system prompt's instruction to read the date back and
    wait for a yes before calling this again).

    For a VOICE conversation, a call with a `date_of_birth` that does not
    match `conversation.pending_dob_candidate` is NEVER checked against a
    patient record and NEVER consumes an attempt -- it is stored as the
    new pending candidate and PENDING_CONFIRMATION is returned so the
    caller can be asked to confirm it. Only a call whose `date_of_birth`
    MATCHES the stored candidate (the model calling back with the same
    value after the caller confirmed) proceeds to the real check below,
    identical to SMS from that point on. SMS never enters this branch at
    all -- `pending_dob_candidate` stays NULL for every SMS conversation,
    by construction, since this gate is the only code that ever writes it
    and it is gated on `conversation.channel is VOICE`.

    WHY THIS DOES NOT WEAKEN THE GUESS BUDGET: a caller can cycle this
    gate as many times as they like without spending a strike, but each
    cycle only proves "the system now agrees with what the caller most
    recently said" -- it reveals nothing about whether that value is the
    real DOB, and the eventual real check below is exactly as strict as
    it has always been. See the decision doc's sanity check for the full
    argument.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)

    conversation = await load_active_conversation(session, conversation_id, for_update=True)
    conversation.last_activity_at = now

    if identity_is_fresh(conversation, now=now):
        patient = await session.get(Patient, conversation.patient_id)
        await session.commit()
        return IdentityResult(
            outcome=IdentityOutcome.ALREADY_VERIFIED,
            attempts_remaining=MAX_IDENTITY_ATTEMPTS - conversation.identity_attempts,
            patient_first_name=_first_name(patient),
        )

    if conversation.channel is ConversationChannel.VOICE:
        if conversation.pending_dob_candidate != date_of_birth:
            conversation.pending_dob_candidate = date_of_birth
            await session.commit()
            return IdentityResult(
                outcome=IdentityOutcome.PENDING_CONFIRMATION,
                attempts_remaining=MAX_IDENTITY_ATTEMPTS - conversation.identity_attempts,
                pending_date_of_birth=date_of_birth,
            )
        # Confirmed: this call restates the same value that was just
        # echoed back. Clear the marker and fall through -- everything
        # below this point is unchanged, channel-agnostic behaviour.
        conversation.pending_dob_candidate = None

    # The phone selects the candidate. Note this is the conversation's
    # external_ref -- observed by the transport -- not anything the caller
    # or the model supplied.
    patient = await session.scalar(
        select(Patient).where(Patient.phone == conversation.external_ref)
    )

    # Every failing branch below consumes an attempt. WHY even
    # "not registered" costs a strike: otherwise an unregistered caller
    # can probe DOBs indefinitely, and the lockout that exists to stop
    # guessing would apply only to callers who guessed a real number
    # first.
    if patient is None:
        return await _register_failure(
            session,
            conversation,
            outcome=IdentityOutcome.NOT_REGISTERED,
            now=now,
            reason="caller's number is not on file",
        )

    if patient.date_of_birth is None:
        # On file, but unverifiable by this channel. Not the caller's
        # fault and not a guess, so it does NOT consume an attempt -- it
        # goes straight to a human, because no number of retries can ever
        # succeed. Counting strikes here would just delay the inevitable
        # handover by two pointless questions.
        await _escalate(
            session,
            conversation,
            reason="patient record has no date of birth on file; cannot verify by chatbot",
            now=now,
        )
        return IdentityResult(
            outcome=IdentityOutcome.CANNOT_VERIFY,
            attempts_remaining=0,
            patient_first_name=_first_name(patient),
        )

    if patient.date_of_birth != date_of_birth:
        return await _register_failure(
            session,
            conversation,
            outcome=IdentityOutcome.WRONG_DETAILS,
            now=now,
            reason="date of birth did not match",
        )

    conversation.patient_id = patient.id
    conversation.identity_verified_at = now
    await session.commit()
    logger.info("conversation %s verified", conversation.id)
    return IdentityResult(
        outcome=IdentityOutcome.VERIFIED,
        attempts_remaining=MAX_IDENTITY_ATTEMPTS - conversation.identity_attempts,
        patient_first_name=_first_name(patient),
    )


async def _register_failure(
    session: AsyncSession,
    conversation: Conversation,
    *,
    outcome: IdentityOutcome,
    now: datetime,
    reason: str,
) -> IdentityResult:
    conversation.identity_attempts += 1
    remaining = MAX_IDENTITY_ATTEMPTS - conversation.identity_attempts

    if remaining <= 0:
        await _escalate(
            session,
            conversation,
            reason=f"identity verification failed {conversation.identity_attempts} times ({reason})",
            now=now,
        )
        return IdentityResult(outcome=IdentityOutcome.ESCALATED, attempts_remaining=0)

    await session.commit()
    return IdentityResult(outcome=outcome, attempts_remaining=remaining)


async def _escalate(
    session: AsyncSession, conversation: Conversation, *, reason: str, now: datetime
) -> None:
    """Hand the conversation to a human. TERMINAL.

    PHASE 3.5: this now also pages staff, in the same transaction as the
    state change -- see notification_service.enqueue_escalation_notification
    for the outbox reasoning and for what happens when no destination is
    configured (logged loudly, does not block the escalation itself).

    Marking the conversation ESCALATED and ending it is NOT conditional on
    the page succeeding or being configured: the caller must stop talking
    to a bot that cannot help them regardless of whether anyone gets
    paged about it. Do not restructure this to skip the state change on a
    paging failure -- that would make a notification misconfiguration
    take down the one thing (ending the conversation cleanly) that has no
    fallback.
    """
    conversation.status = ConversationStatus.ESCALATED
    conversation.escalation_reason = reason
    conversation.last_activity_at = now

    # Local import: avoids a module-level cycle, matching the convention
    # already used for cross-service calls in this codebase (see
    # appointment_service.book_appointment importing notification_service
    # the same way).
    from app.services import notification_service

    await notification_service.enqueue_escalation_notification(session, conversation=conversation, now=now)

    await session.commit()
    logger.warning("conversation %s escalated: %s", conversation.id, reason)


async def escalate(
    session: AsyncSession, *, conversation_id: UUID, reason: str, now: datetime | None = None
) -> Conversation:
    """Escalate on demand -- the caller asked for a human, or the bot is stuck."""
    now = now or datetime.now(UTC)
    conversation = await load_active_conversation(session, conversation_id, for_update=True)
    await _escalate(session, conversation, reason=reason, now=now)
    return conversation


async def reopen(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    staff_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> Conversation:
    """Release a phone number stuck behind a terminal conversation.

    STRUCTURAL GUARANTEE, not a convention: this function writes exactly
    five columns -- status, reopened_at, reopened_by_staff_id,
    reopen_reason, last_activity_at -- and none of them is patient_id,
    identity_verified_at, or identity_attempts. There is no parameter
    here for a patient id, and there must never be one.

    Setting status to EXPIRED (not ACTIVE) is deliberate: it routes the
    next inbound message through the exact fall-through path in
    get_or_create_conversation that an ordinary idle timeout already
    takes, so that message starts a BRAND NEW conversation row with
    identity_bound = false and identity_attempts = 0 straight from
    Conversation's own column defaults -- never resurrected, never
    inherited from this row. See the "PHASE 3.5" section of
    Conversation's module docstring for the full reasoning. Do not
    shortcut this by setting patient_id or identity_verified_at here
    "since staff already know who it is" -- that is precisely the
    lockout-bypass this function exists to avoid reintroducing; the
    caller must verify again, from zero attempts, like anyone texting in
    for the first time.

    Refuses anything not currently ESCALATED or COMPLETED: an ACTIVE
    conversation is not stuck on anything, and EXPIRED conversations
    already resolve themselves on the next message with no staff action
    needed.
    """
    now = now or datetime.now(UTC)
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise ConversationNotFoundError(conversation_id)
    if conversation.status not in (ConversationStatus.ESCALATED, ConversationStatus.COMPLETED):
        raise ConversationNotTerminalError(conversation_id, conversation.status)

    conversation.status = ConversationStatus.EXPIRED
    conversation.reopened_at = now
    conversation.reopened_by_staff_id = staff_id
    conversation.reopen_reason = reason
    conversation.last_activity_at = now
    await session.commit()
    logger.info("conversation %s reopened by staff %s", conversation.id, staff_id)
    return conversation


def _first_name(patient: Patient | None) -> str | None:
    if patient is None or not patient.full_name:
        return None
    return patient.full_name.split()[0]
