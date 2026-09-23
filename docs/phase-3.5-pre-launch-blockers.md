# Phase 3.5 — pre-launch blockers

**Status: CLOSED 2026-09-22. All four items complete and verified.**
Phase 3 (chatbot conversation loop, tool layer, safety model, Twilio
adapter) is **closed** — approved 2026-09-22 after the injection suite.
This is a distinct, smaller unit that sits between Phase 3 closing and
the first real patient message: four items, one of them a matched pair
that must ship together, none of them optional.

Kept as its own named file rather than folded into the general
carry-forward list (poll staggering, `oauth_states` cleanup, the
OAuth re-authorize UX, etc.) specifically so it cannot be lost among
lower-priority deferred items. Those stay deferred indefinitely and
nobody is harmed by that. These four block launch.

## The matched pair: escalation notification + staff reopen action

These two ship together or not at all — each is actively harmful
without the other, not merely incomplete:

- **Shipping notification without reopen**: staff learn a conversation
  escalated, and can do nothing about it except by hand-editing the
  database. The notification becomes a nag with no resolution path.
- **Shipping reopen without notification**: the mechanism to un-stick a
  conversation exists, but nothing tells staff a conversation needs it.
  An escalated phone number sits dead until someone happens to go
  looking — which, per the fix below, is now *forever* rather than
  until the next message, so nobody will happen to go looking.

### 1. Escalation notification to staff

`conversation_service._escalate` marks a conversation `ESCALATED`, ends
it, and logs a warning server-side. No person is told. The bot tells
the caller "someone will follow up" — at present, nobody is told to.

Full reasoning and the "an unmonitored escalation queue is worse than
no escalation path" argument: `docs/security-no-authentication.md`,
under "Escalation is recorded but nothing watches for it."

**Implementation, small, pieces already exist:** add
`NotificationKind.ESCALATION`, enqueue through the existing notification
outbox (inherits send-once, retries, audit trail for free), add a staff
destination to config.

### 2. Staff-facing reopen action for terminal conversations

New as of the Phase 3 injection suite (finding #10,
`test_10_escalated_conversation_cannot_be_reactivated_by_asking`,
`test_11_lockout_survives_continued_messages_after_escalation`).

The fix for that finding — `conversation_service.get_or_create_conversation`
now treats `ESCALATED`/`COMPLETED` as **permanently terminal** for a
phone number, never superseded by a new conversation row — closed a real
lockout-bypass vulnerability (see the function's docstring for the
mechanism). It also means an escalated phone number now has **no path
back** into the chatbot. Before the fix, continuing to text created a
fresh conversation, which was the bug; there is currently nothing that
correctly replaces that path for a legitimately-resolved escalation
(a patient who spoke to staff and is now allowed to book again).

**Done.** `PATCH /api/v1/conversations/{id}/reopen`
(`app/api/v1/conversations.py`), staff-authenticated via the existing
`get_current_staff` dependency, no doctor-scoping (conversations have no
doctor_id). `conversation_service.reopen` (`app/services/
conversation_service.py`) sets the conversation's status to `EXPIRED` —
routing the next inbound message through the exact same fall-through
path in `get_or_create_conversation` that an ordinary idle timeout
already takes — plus audit-only metadata (`reopened_at`,
`reopened_by_staff_id`, `reopen_reason`; migration `0010_conversation_
reopen`, CHECK `reopened_iff_staff_recorded`). It writes exactly those
four columns and structurally cannot touch `patient_id`,
`identity_verified_at`, or `identity_attempts` — there is no parameter
for a patient id anywhere in the request schema, service function, or
endpoint. Refuses anything not currently `ESCALATED`/`COMPLETED`
(`ConversationNotTerminalError`, 409). Tested in
`tests/test_conversation_reopen.py` (15 tests: audit fields, the CHECK
constraint both directions, cold identity map, unauthenticated/wrong-
role rejection, 404/409 paths, and — the property that actually
matters — that a conversation escalated *after* successful identity
verification reopens into a brand-new row with `patient_id`,
`identity_verified_at` all `NULL` and `identity_attempts = 0`, never
inherited from the old row). Live-verified over real HTTP against the
running stack: login → unauthenticated reopen (401) → authenticated
reopen (200, `status: expired`) → repeat reopen (409) → next inbound
message on the same phone number produces a new conversation id with
zero identity state.

### 3. Async delivery confirmation at scale — already covered

The SMS reply outbox (`sms_reply_service.py`) is built and tested for
exactly the failure mode the live Twilio 400 (error 21408, trial-account
region restriction) surfaced during Phase 3 verification: permanent vs.
transient send failures are classified correctly (`abandoned` vs.
`failed`+retry), the two-phase `reply_body` marker means a retry never
re-runs the model, and `reap_stuck_claims` distinguishes a job that never
reached the provider (safely requeued) from one that did and is
genuinely ambiguous (`UNRESOLVED`, needs reconciliation). Covered by
`tests/test_sms_reply_outbox.py`, including the concurrency and
crash-mid-send cases. **No further work identified** — listed here so
this unit is a complete checklist, not because anything is outstanding.

### 4. Authentication scope — already covered

Staff/doctor JWT auth (Phase 3, `staff_accounts`, doctor-scoping on
read/cancel/reschedule) is built and tested. The chatbot's own patient
identity (phone + DOB) is a separate, weaker mechanism by design, scoped
to scheduling only — see `docs/security-no-authentication.md` for its
explicit promotion criterion (must be replaced before any clinical
scope expansion). **No further work identified for launch** — the
patient-identity limitations are known, documented, and accepted for a
scheduling-only chatbot; listed here for completeness, not as new work.

## Definition of done for this unit

- [x] `NotificationKind.ESCALATION` enqueued on every escalation, staff
      destination configured, delivered through the existing outbox.
- [x] A staff-authenticated reopen action exists, is tested, and a
      reopened conversation's next inbound message is verifiably NOT
      treated as terminal by `get_or_create_conversation`.
- [x] Item 3 — no action needed, verified complete.
- [x] Item 4 — no action needed, verified complete.

All four items closed 2026-09-22. `escalation_notify_phone` must still
be set to a real destination before real patient traffic — the code
path works and is tested, but ships with an empty default (logs loudly
rather than silently dropping; see `core/config.py`). Retained here
rather than deleted so the verification trail (what was checked, how)
survives past the immediate work.

## Phase 4 addendum (voice, 2026-09-22)

Checked whether adding a second channel reopened anything this unit
closed. It did not: both `enqueue_escalation_notification` and
`conversation_service.reopen` are keyed on `conversation_id`/status,
never on channel, so a voice conversation escalates and reopens through
the exact same code paths as SMS, verified rather than assumed — no
voice-specific branch exists in either function to have gotten wrong.
See `docs/security-no-authentication.md`'s matching addendum for the one
thing voice DOES change: the same unconfigured-`escalation_notify_phone`
gap is a worse failure mode for a caller who was live on the phone than
for an SMS thread sitting unanswered. Not a new requirement — the
requirement was already unconditional — just a sharper reason to close
it before real traffic on either channel.
