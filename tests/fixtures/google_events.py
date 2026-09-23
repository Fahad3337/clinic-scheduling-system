"""Recorded Google Calendar API event payloads.

Modelled on the documented `Events` resource from Google's Calendar API v3
reference, rather than on the minimal dicts our own tests would naturally
produce. That difference is the point: hand-written fixtures only contain
the fields the author already thought about, so they cannot catch "the real
payload has a shape we did not anticipate".

VERIFY BEFORE TRUSTING FOR PRODUCTION DECISIONS: these are transcribed from
documentation, not captured from a live account. They are accurate in
structure and field names, but a specific default (see ALL_DAY_EVENT below)
should be confirmed against a real calendar before the clinic relies on it.
Recording real payloads once, with identifying details scrubbed, would be
strictly better and is worth doing when a real doctor's calendar exists.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------- #
# 1. A single timed event -- the ordinary case.
# --------------------------------------------------------------------- #
TIMED_EVENT: dict[str, Any] = {
    "kind": "calendar#event",
    "etag": '"3181161784712000"',
    "id": "7f8a9b0c1d2e3f4g",
    "status": "confirmed",
    "htmlLink": "https://www.google.com/calendar/event?eid=N2Y4YTliMGM",
    "created": "2026-09-15T10:00:00.000Z",
    "updated": "2026-09-15T10:30:00.000Z",
    "summary": "Dentist",
    "creator": {"email": "doctor@example.com", "self": True},
    "organizer": {"email": "doctor@example.com", "self": True},
    "start": {"dateTime": "2026-09-21T10:00:00-04:00", "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-09-21T11:00:00-04:00", "timeZone": "America/New_York"},
    "iCalUID": "7f8a9b0c1d2e3f4g@google.com",
    "sequence": 0,
    "reminders": {"useDefault": True},
    "eventType": "default",
}

# --------------------------------------------------------------------- #
# 2. One INSTANCE of a recurring event, as returned with singleEvents=true.
#
# Note the id: "{recurringEventId}_{UTC start}". Each occurrence carries its
# own distinct id, which is exactly what our
# UNIQUE(connection_id, external_event_id) needs -- occurrences become
# separate rows blocking separate slots. If we had used recurringEventId as
# the key instead, every occurrence of a weekly meeting would collapse into
# one row and only one week would ever be blocked.
# --------------------------------------------------------------------- #
RECURRING_INSTANCE: dict[str, Any] = {
    "kind": "calendar#event",
    "etag": '"3181161784713000"',
    "id": "a1b2c3d4e5f6g7h8_20260922T140000Z",
    "status": "confirmed",
    "created": "2026-01-05T09:00:00.000Z",
    "updated": "2026-09-01T09:00:00.000Z",
    "summary": "Weekly practice meeting",
    "creator": {"email": "doctor@example.com", "self": True},
    "organizer": {"email": "doctor@example.com", "self": True},
    "start": {"dateTime": "2026-09-22T10:00:00-04:00", "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-09-22T10:30:00-04:00", "timeZone": "America/New_York"},
    "recurringEventId": "a1b2c3d4e5f6g7h8",
    "originalStartTime": {
        "dateTime": "2026-09-22T10:00:00-04:00",
        "timeZone": "America/New_York",
    },
    "iCalUID": "a1b2c3d4e5f6g7h8@google.com",
    "sequence": 0,
    "eventType": "default",
}

SECOND_RECURRING_INSTANCE: dict[str, Any] = {
    **RECURRING_INSTANCE,
    "id": "a1b2c3d4e5f6g7h8_20260929T140000Z",
    "start": {"dateTime": "2026-09-29T10:00:00-04:00", "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-09-29T10:30:00-04:00", "timeZone": "America/New_York"},
    "originalStartTime": {
        "dateTime": "2026-09-29T10:00:00-04:00",
        "timeZone": "America/New_York",
    },
}

# --------------------------------------------------------------------- #
# 3. An all-day event spanning three days.
#
# end.date IS EXCLUSIVE: 23rd -> 26th is three days (23, 24, 25).
#
# ASSUMPTION THAT NEEDS CONFIRMING: all-day events created through the
# Google Calendar UI are widely reported to default to "Free"
# (transparency == "transparent"), which under our rules means they do NOT
# block. If that is true, a doctor's all-day "Annual leave" would leave
# their slots bookable -- patients booking through someone's holiday is a
# serious, very visible failure. This fixture deliberately OMITS
# transparency (the API omits it when the event is busy) so the happy path
# is covered; TRANSPARENT_ALL_DAY_EVENT below covers the other case.
# --------------------------------------------------------------------- #
ALL_DAY_EVENT: dict[str, Any] = {
    "kind": "calendar#event",
    "etag": '"3181161784714000"',
    "id": "i9j0k1l2m3n4o5p6",
    "status": "confirmed",
    "created": "2026-08-01T12:00:00.000Z",
    "updated": "2026-08-01T12:00:00.000Z",
    "summary": "Annual leave",
    "creator": {"email": "doctor@example.com", "self": True},
    "organizer": {"email": "doctor@example.com", "self": True},
    "start": {"date": "2026-09-23"},
    "end": {"date": "2026-09-26"},
    "iCalUID": "i9j0k1l2m3n4o5p6@google.com",
    "sequence": 0,
    "eventType": "default",
}

TRANSPARENT_ALL_DAY_EVENT: dict[str, Any] = {
    **ALL_DAY_EVENT,
    "id": "transparent-all-day-1",
    "summary": "Someone's birthday",
    "transparency": "transparent",
}

# --------------------------------------------------------------------- #
# 4. A deletion, as it arrives during INCREMENTAL sync.
#
# Note how little there is: no start, no end, no summary. Code that reaches
# for event["start"] before checking status crashes on every deletion.
# --------------------------------------------------------------------- #
CANCELLED_EVENT: dict[str, Any] = {
    "kind": "calendar#event",
    "etag": '"3181161784715000"',
    "id": "7f8a9b0c1d2e3f4g",
    "status": "cancelled",
}

CANCELLED_RECURRING_INSTANCE: dict[str, Any] = {
    "kind": "calendar#event",
    "etag": '"3181161784716000"',
    "id": "a1b2c3d4e5f6g7h8_20260929T140000Z",
    "status": "cancelled",
    "recurringEventId": "a1b2c3d4e5f6g7h8",
    "originalStartTime": {
        "dateTime": "2026-09-29T10:00:00-04:00",
        "timeZone": "America/New_York",
    },
}

# --------------------------------------------------------------------- #
# 5. A working-location event. Spans the whole working day and says only
#    WHERE someone is, not that they are unavailable. Treating it as busy
#    would zero out a doctor's entire availability every day they set one.
# --------------------------------------------------------------------- #
WORKING_LOCATION_EVENT: dict[str, Any] = {
    "kind": "calendar#event",
    "etag": '"3181161784717000"',
    "id": "q7r8s9t0u1v2w3x4",
    "status": "confirmed",
    "summary": "Clinic",
    "start": {"date": "2026-09-21"},
    "end": {"date": "2026-09-22"},
    "eventType": "workingLocation",
    "workingLocationProperties": {
        "type": "officeLocation",
        "officeLocation": {"label": "Main clinic"},
    },
    "transparency": "transparent",
    "visibility": "public",
    "sequence": 0,
}

# --------------------------------------------------------------------- #
# 6. An out-of-office event. Google's explicit "I am unavailable" type.
#    It MUST block even though such events are often transparent.
# --------------------------------------------------------------------- #
OUT_OF_OFFICE_EVENT: dict[str, Any] = {
    "kind": "calendar#event",
    "etag": '"3181161784718000"',
    "id": "y5z6a7b8c9d0e1f2",
    "status": "confirmed",
    "summary": "Out of office",
    "start": {"dateTime": "2026-09-24T09:00:00-04:00", "timeZone": "America/New_York"},
    "end": {"dateTime": "2026-09-24T17:00:00-04:00", "timeZone": "America/New_York"},
    "eventType": "outOfOffice",
    "outOfOfficeProperties": {
        "autoDeclineMode": "declineOnlyNewConflictingInvitations",
        "declineMessage": "Declined because I am not working",
    },
    "transparency": "transparent",
    "sequence": 0,
}
