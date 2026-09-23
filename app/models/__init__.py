"""Model package.

Importing every model here gives Alembic autogenerate a single import that
populates `Base.metadata`. A model that is never imported is invisible to
autogenerate, and the classic symptom is a migration that mysteriously drops
a table.
"""

from app.db.base import Base
from app.models.appointment import Appointment
from app.models.appointment_external_event import AppointmentExternalEvent
from app.models.booking_proposal import BookingProposal
from app.models.calendar_connection import CalendarConnection
from app.models.conversation import Conversation
from app.models.conversation_message import ConversationMessage
from app.models.doctor import Doctor
from app.models.enums import (
    AppointmentStatus,
    BookingChannel,
    CalendarConnectionState,
    CalendarProvider,
    CalendarPushState,
    ConflictResolution,
    ConversationChannel,
    ConversationStatus,
    MessageRole,
    ProposalKind,
    StaffRole,
    NotificationChannel,
    NotificationKind,
    NotificationStatus,
)
from app.models.external_busy_block import ExternalBusyBlock
from app.models.notification import Notification
from app.models.oauth_state import OAuthState
from app.models.patient import Patient
from app.models.schedule_conflict import ScheduleConflict
from app.models.sms_reply_job import SmsReplyJob
from app.models.staff_account import StaffAccount
from app.models.time_slot import TimeSlot
from app.models.voice_turn_job import VoiceTurnJob

__all__ = [
    "Base",
    # Phase 1
    "Appointment",
    "AppointmentStatus",
    "BookingChannel",
    "Doctor",
    "Patient",
    "TimeSlot",
    # Phase 2
    "AppointmentExternalEvent",
    "CalendarConnection",
    "CalendarConnectionState",
    "CalendarProvider",
    "CalendarPushState",
    "ConflictResolution",
    "ExternalBusyBlock",
    "Notification",
    "NotificationChannel",
    "NotificationKind",
    "NotificationStatus",
    "OAuthState",
    "ScheduleConflict",
    # Phase 3
    "BookingProposal",
    "Conversation",
    "ConversationChannel",
    "ConversationMessage",
    "ConversationStatus",
    "MessageRole",
    "ProposalKind",
    "SmsReplyJob",
    "StaffAccount",
    "StaffRole",
    # Phase 4
    "VoiceTurnJob",
]
