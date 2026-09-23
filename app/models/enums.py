"""Domain enumerations.

WHY `str, Enum`: the member is a real Python enum (exhaustive matching, IDE
completion) but also serializes to a plain string for Pydantic/JSON with no
custom encoder.

WHY these are stored as VARCHAR + CHECK rather than a native Postgres ENUM
type (see `models/appointment.py` for the column definition):
  - Adding a value to a native enum requires `ALTER TYPE ... ADD VALUE`, which
    historically could not run inside a transaction block -- awkward in
    Alembic, and irreversible (Postgres has no `DROP VALUE`).
  - You *will* add statuses ("pending_confirmation" once the voice assistant
    needs a hold, "rescheduled" for an audit trail). A CHECK constraint is a
    one-line migration; a native enum is not.
  - Cost: slightly larger rows and no ordering guarantees. Both irrelevant here.
"""

from __future__ import annotations

from enum import Enum


class AppointmentStatus(str, Enum):
    """Lifecycle of a single appointment.

    Legal transitions (enforced in the service layer, not the DB):
        BOOKED -> CANCELLED | COMPLETED | NO_SHOW
        CANCELLED -> (terminal; rebooking creates a NEW appointment row)
        COMPLETED -> (terminal)
        NO_SHOW   -> (terminal)

    WHY cancellation creates a new row on rebook instead of flipping the old
    one back: the appointment row is the audit record. A patient who cancelled
    and rebooked twice should leave three rows, which is what a clinic's
    no-show policy and any future billing integration will want to see.
    """

    BOOKED = "booked"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class BookingChannel(str, Enum):
    """How the appointment reached us.

    Phase 1 only ever writes WEB, but the column exists now because adding it
    later means backfilling every historical row with a guess. Once the
    chatbot and Twilio/Whisper flows land, this is the dimension you will slice
    every metric by ("what % of voice bookings get cancelled?").
    """

    WEB = "web"
    CHAT = "chat"
    VOICE = "voice"
    STAFF = "staff"  # entered by front-desk staff on the patient's behalf


# --------------------------------------------------------------------------
# Phase 2: calendar sync + notifications
# --------------------------------------------------------------------------


class CalendarProvider(str, Enum):
    """External calendar systems we can sync with.

    Only GOOGLE is implemented. The column exists so that adding Outlook
    later is a new value plus a new adapter class, not a schema migration on
    a table with live OAuth tokens in it.
    """

    GOOGLE = "google"


class CalendarConnectionState(str, Enum):
    """Health of a doctor's calendar link.

    WHY NEEDS_REAUTH is a distinct state rather than just "disabled": it is
    the difference between "we turned this off" and "the doctor must click a
    button before this can ever work again". Only the latter should raise an
    alert and show a re-connect prompt in the UI. Conflating them means
    either nagging doctors who deliberately disconnected, or silently never
    syncing a doctor who revoked access six weeks ago.
    """

    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"  # terminal until a human re-authorizes
    DISABLED = "disabled"  # deliberately switched off by staff


class CalendarPushState(str, Enum):
    """Lifecycle of OUR appointment as an event on the doctor's calendar.

    This is a transactional-outbox state machine -- see
    models/appointment_external_event.py for why the push is deferred rather
    than done inline during booking.
    """

    PENDING = "pending"  # needs creating on the external calendar
    SYNCED = "synced"  # exists externally, matches our state
    UPDATE_PENDING = "update_pending"  # exists externally but is stale
    DELETE_PENDING = "delete_pending"  # appointment cancelled, event still out there
    DELETED = "deleted"  # removed externally, nothing left to do
    FAILED = "failed"  # gave up after repeated errors; needs attention


class NotificationChannel(str, Enum):
    SMS = "sms"
    EMAIL = "email"


class NotificationKind(str, Enum):
    """What a given message is FOR.

    Part of the idempotency key -- see models/notification.py. Adding a value
    here is safe; renaming one is not, because existing dedupe keys embed the
    string and a rename would let an already-sent message send again.
    """

    BOOKING_CONFIRMATION = "booking_confirmation"
    REMINDER_24H = "reminder_24h"
    CANCELLATION_CONFIRMATION = "cancellation_confirmation"
    RESCHEDULE_CONFIRMATION = "reschedule_confirmation"
    # Phase 3.5. The one kind on this enum that is NOT patient-facing --
    # a staff page when a conversation escalates. Targets
    # notifications.conversation_id instead of appointment_id; see the
    # module docstring in models/notification.py for why this kind alone
    # needed the table's target column widened.
    ESCALATION = "escalation"


class NotificationStatus(str, Enum):
    """Delivery lifecycle of a single message.

    Transitions:
        PENDING -> CLAIMED -> SENT
                           -> FAILED -> CLAIMED (retry) -> ...
                           -> UNRESOLVED (crashed mid-send; see below)
        PENDING -> SKIPPED (appointment cancelled before it went out)
        FAILED  -> ABANDONED (exhausted max attempts)

    WHY UNRESOLVED exists: if the process dies between "we called Twilio" and
    "we recorded the result", we genuinely do not know whether the SMS went
    out. Auto-retrying would risk a duplicate; marking it SENT would hide a
    miss. A distinct state says "a human or a provider-API reconciliation
    job must decide", which is the only honest option. See the at-most-once
    discussion in models/notification.py.
    """

    PENDING = "pending"
    CLAIMED = "claimed"  # a worker has taken ownership and is about to send
    SENT = "sent"
    FAILED = "failed"  # transient failure, eligible for retry
    ABANDONED = "abandoned"  # retries exhausted
    SKIPPED = "skipped"  # no longer applicable (e.g. appointment cancelled)
    UNRESOLVED = "unresolved"  # crashed mid-send, delivery unknown


class ConflictResolution(str, Enum):
    """How a doctor-calendar-vs-booking collision was settled.

    UNRESOLVED is the default and the only state the sync job itself may
    write. Every other value requires a human decision -- see the
    "conflict resolution" discussion in models/schedule_conflict.py.
    """

    UNRESOLVED = "unresolved"
    APPOINTMENT_RESCHEDULED = "appointment_rescheduled"
    APPOINTMENT_CANCELLED = "appointment_cancelled"
    EXTERNAL_EVENT_REMOVED = "external_event_removed"
    IGNORED_BY_STAFF = "ignored_by_staff"  # doctor will double up; not our problem


# --------------------------------------------------------------------------
# Phase 3: authentication
# --------------------------------------------------------------------------


class StaffRole(str, Enum):
    """Who a staff_accounts row represents, and what it may act on.

    DOCTOR accounts are scoped to exactly one doctor_id (enforced by a CHECK
    on staff_accounts -- see models/staff_account.py) and may only touch
    their own doctor's calendar/appointments. FRONT_DESK accounts are not
    tied to a doctor and may act for any doctor -- this is the single-clinic
    assumption from Phase 1 showing up again: a multi-clinic generalization
    would need a clinic_id on FRONT_DESK accounts to scope them too.
    """

    DOCTOR = "doctor"
    FRONT_DESK = "front_desk"


# --------------------------------------------------------------------------
# Phase 3: chatbot conversations
# --------------------------------------------------------------------------


class ConversationChannel(str, Enum):
    """The TRANSPORT a conversation is happening over.

    Distinct from BookingChannel, which records how an APPOINTMENT came to
    exist. They are related (an SMS conversation that books produces
    BookingChannel.CHAT) but not the same axis: a conversation may never
    book anything, and an appointment may be created by staff with no
    conversation at all. Mapping between them happens once, in the booking
    tool, rather than by pretending one enum serves both.
    """

    SMS = "sms"
    VOICE = "voice"
    WEB = "web"


class ConversationStatus(str, Enum):
    """Lifecycle of a chatbot conversation.

    ESCALATED is TERMINAL and enforced by the service, not by the model.
    Once a conversation escalates, every tool refuses to act on it -- see
    the guard in chatbot/tools.py. A language model that keeps talking
    after an escalation cannot do anything, which is the property that
    matters; whether it also stops talking is a prompt concern.
    """

    ACTIVE = "active"
    ESCALATED = "escalated"  # handed to a human (FDO); terminal
    COMPLETED = "completed"
    EXPIRED = "expired"


class ProposalKind(str, Enum):
    """What a booking_proposals row is a proposal TO DO.

    Every mutation a patient can trigger through the chatbot is two-phase:
    the service creates a proposal describing exactly what will happen, and
    a later confirm tool executes that specific proposal. The language model
    never gets to name the thing being mutated at confirm time -- it can
    only point at a proposal the service itself wrote. See
    services/proposal_service.py.
    """

    BOOK = "book"
    CANCEL = "cancel"


class MessageRole(str, Enum):
    """Who produced a conversation_messages row.

    TOOL rows are our own tool RESULTS, not anything the model said --
    kept as their own role so an audit of "what did the bot tell this
    patient" can filter to ASSISTANT text without wading through
    machine-readable payloads.
    """

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
