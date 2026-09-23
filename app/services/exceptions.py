"""Domain exceptions.

WHY these exist instead of raising HTTPException from the service layer: the
service layer must stay transport-agnostic. An HTTPException makes sense to a
FastAPI route handler; it means nothing to a future Twilio webhook or a
chatbot worker, which need to turn "slot taken" into spoken words, not a
status code. Each exception is mapped to an HTTP status ONCE, in
api/v1/appointments.py -- the mapping lives at the edge, not the core.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all service-layer errors."""


class PatientNotFoundError(DomainError):
    def __init__(self, patient_id) -> None:
        self.patient_id = patient_id
        super().__init__(f"Patient {patient_id} not found")


class TimeSlotNotFoundError(DomainError):
    def __init__(self, time_slot_id) -> None:
        self.time_slot_id = time_slot_id
        super().__init__(f"Time slot {time_slot_id} not found")


class SlotAlreadyBookedError(DomainError):
    """Raised when a slot has an active (non-cancelled) appointment already.

    This is the exception the concurrency strategy exists to make routine
    rather than exceptional: under contention, the loser of the race gets
    this cleanly instead of a raw IntegrityError. See models/appointment.py.
    """

    def __init__(self, time_slot_id) -> None:
        self.time_slot_id = time_slot_id
        super().__init__(f"Time slot {time_slot_id} is already booked")


class SlotBlockedError(DomainError):
    def __init__(self, time_slot_id) -> None:
        self.time_slot_id = time_slot_id
        super().__init__(f"Time slot {time_slot_id} is blocked and cannot be booked")


class AppointmentNotFoundError(DomainError):
    def __init__(self, appointment_id) -> None:
        self.appointment_id = appointment_id
        super().__init__(f"Appointment {appointment_id} not found")


class InvalidStatusTransitionError(DomainError):
    """Raised when an operation would move an appointment to an illegal state.

    e.g. cancelling an already-completed appointment. See the state machine
    documented on AppointmentStatus in models/enums.py.
    """

    def __init__(self, current_status, attempted_action: str) -> None:
        self.current_status = current_status
        self.attempted_action = attempted_action
        super().__init__(
            f"Cannot {attempted_action}: appointment is {current_status.value}"
        )


# --------------------------------------------------------------------------
# Phase 2: calendar authorization
# --------------------------------------------------------------------------


class DoctorNotFoundError(DomainError):
    def __init__(self, doctor_id) -> None:
        self.doctor_id = doctor_id
        super().__init__(f"Doctor {doctor_id} not found")


class InvalidOAuthStateError(DomainError):
    """The `state` from the callback is unknown, expired, or already used.

    Deliberately ONE exception for all three cases. Telling a caller which of
    "no such state", "expired" or "already consumed" applies hands an
    attacker a probing oracle for a CSRF token, and no legitimate user can
    act on the distinction anyway. The specifics go in the server log.
    """

    def __init__(self) -> None:
        super().__init__("Invalid or expired authorization state")


class CalendarAuthorizationError(DomainError):
    """Google refused, or returned something unusable."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Calendar authorization failed: {detail}")


class InsufficientCalendarScopeError(DomainError):
    """The doctor completed the flow but withheld a scope we require."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(
            "Calendar access was granted without required permissions: "
            + ", ".join(missing)
        )


class CalendarNotConnectedError(DomainError):
    def __init__(self, doctor_id) -> None:
        self.doctor_id = doctor_id
        super().__init__(f"Doctor {doctor_id} has no connected calendar")


# --------------------------------------------------------------------------
# Phase 3: authentication
# --------------------------------------------------------------------------


class InvalidCredentialsError(DomainError):
    """Wrong email, wrong password, or unknown account.

    Deliberately ONE exception for all three -- same reasoning as
    InvalidOAuthStateError. Telling a caller "no such email" vs "wrong
    password" hands an attacker a way to enumerate valid staff email
    addresses one guess at a time.
    """

    def __init__(self) -> None:
        super().__init__("Invalid email or password")


class StaffAccountInactiveError(DomainError):
    def __init__(self, staff_id) -> None:
        self.staff_id = staff_id
        super().__init__(f"Staff account {staff_id} is deactivated")


class InvalidTokenError(DomainError):
    """Token missing, malformed, expired, or signed with a different key."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"Invalid or expired token. {detail}".strip())


class InsufficientRoleError(DomainError):
    """Authenticated, but not permitted for THIS resource.

    e.g. a doctor's token used against a different doctor's calendar. This
    is 403, not 401 -- the caller is who they say they are, and still may
    not do this.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


# --------------------------------------------------------------------------
# Phase 3: rescheduling
# --------------------------------------------------------------------------


class RescheduleToSameSlotError(DomainError):
    def __init__(self, time_slot_id) -> None:
        self.time_slot_id = time_slot_id
        super().__init__(f"Appointment is already booked for time slot {time_slot_id}")


class RescheduleAcrossDoctorsError(DomainError):
    """Rescheduling changed the doctor, which is a transfer, not a reschedule.

    Not supported: a reschedule is "this patient, different time, same
    doctor". Moving between doctors touches consent, records-sharing and
    billing assumptions this codebase has not designed for -- see the
    single-calendar-per-doctor note elsewhere. Deliberately refused rather
    than silently allowed.
    """

    def __init__(self, from_doctor_id, to_doctor_id) -> None:
        self.from_doctor_id = from_doctor_id
        self.to_doctor_id = to_doctor_id
        super().__init__(
            f"Cannot reschedule from doctor {from_doctor_id} to doctor {to_doctor_id}: "
            "rescheduling across doctors is not supported"
        )


# --------------------------------------------------------------------------
# Phase 3: chatbot conversations
# --------------------------------------------------------------------------


class ConversationNotFoundError(DomainError):
    def __init__(self, conversation_id) -> None:
        self.conversation_id = conversation_id
        super().__init__(f"Conversation {conversation_id} not found")


class ConversationNotActiveError(DomainError):
    """The conversation is escalated, completed or expired.

    Terminal by design: once a conversation has been handed to a human,
    no tool may act on it again. Enforced by the service so that a model
    which keeps talking after escalation is merely talking.
    """

    def __init__(self, conversation_id, status) -> None:
        self.conversation_id = conversation_id
        self.status = status
        super().__init__(f"Conversation {conversation_id} is {status.value}, not active")


class IdentityNotVerifiedError(DomainError):
    """A patient-scoped operation was attempted before identity was proven.

    Raised by the SERVICE, not checked by the model. The model is not
    trusted to sequence verification before action -- see the guard in
    chatbot/tools.py.
    """

    def __init__(self, conversation_id) -> None:
        self.conversation_id = conversation_id
        super().__init__(f"Conversation {conversation_id} has no verified patient identity")


class ProposalNotFoundError(DomainError):
    """Unknown, expired, already used, or belongs to another conversation.

    ONE exception for all four, same reasoning as InvalidOAuthStateError:
    distinguishing them tells a caller whether an id they guessed exists.
    """

    def __init__(self) -> None:
        super().__init__("That option is no longer available")


class ProposalKindMismatchError(DomainError):
    """confirm_booking called on a cancellation proposal, or vice versa.

    A cross-check rather than a convenience: it catches a model that
    grabbed the wrong proposal id from its context, which is exactly the
    failure the two-phase design exists to make harmless.
    """

    def __init__(self, expected, actual) -> None:
        super().__init__(f"Expected a {expected.value} proposal, got {actual.value}")


class ConversationNotTerminalError(DomainError):
    """Reopen was attempted on a conversation that is not stuck.

    Reopening only makes sense for ESCALATED/COMPLETED conversations --
    an ACTIVE one is not blocked on anything, and EXPIRED conversations
    already get out of the way on their own (see
    get_or_create_conversation). Refusing this keeps "reopen" meaning
    exactly one thing: releasing a phone number stuck behind a terminal
    state.
    """

    def __init__(self, conversation_id, status) -> None:
        self.conversation_id = conversation_id
        self.status = status
        super().__init__(
            f"Conversation {conversation_id} is {status.value}, not a terminal state that needs reopening"
        )
