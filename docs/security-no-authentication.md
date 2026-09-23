# Risk: the API has no authentication

**Status:** PARTIALLY CLOSED 2026-09-21. Staff/doctor authentication (JWT
bearer, `staff_accounts`) now gates every mutating and doctor-scoped
endpoint. What remains open is item 3 below (patient-facing access) and
the items in "Still open" — this is not a clean bill of health.
**Raised:** Phase 1 (booking endpoints) · **Widened:** Phase 2 (calendar endpoints)
· **Item 1–2 closed:** Phase 3 (staff/doctor auth), ahead of real patient
data arriving via the chatbot.

## The risk, in one paragraph

Every endpoint in this service is unauthenticated and unauthorized: there is no
login, no session, no API key, and no check that a caller is who they claim to
be or may act on the record they are touching. Anyone who can reach the port can
enumerate nothing (UUID primary keys are unguessable, which is the only thing
currently protecting patient data), but can read any appointment whose id they
obtain, cancel it, book appointments as any patient, and — since Phase 2 — mint
Google consent links for any doctor and disconnect a doctor's calendar at will.
UUIDs are an obstacle to enumeration, not an authorization control: an id that
leaks through a browser history, a shared link, a log aggregator or a support
ticket becomes a durable capability to read and cancel that patient's care.
The practical blast radius today is limited only by the service being bound to
localhost; the moment it is exposed — a cloud deploy, a tunnel for a Twilio
webhook, a colleague's laptop on the same network — it is an unauthenticated
system holding appointment records and live Google OAuth refresh tokens, which
is a reportable data-protection failure in most jurisdictions rather than merely
a bug.

## What is and is not mitigated

| | Status |
|---|---|
| Enumeration of patients/appointments | Mitigated by UUIDv4 primary keys |
| Reading a specific record given its id | Mitigated — requires a valid staff bearer token |
| Cancelling someone else's appointment | Mitigated for a *different doctor's* appointment (403). **NOT mitigated** for a patient's own — see "Still open" |
| Booking as an arbitrary patient | Requires a staff token, but any staff token — booking is not doctor-scoped (see below) |
| Rescheduling an appointment | Same scoping as cancel |
| Generating a Google consent link for any doctor | Mitigated — requires a valid staff token scoped to that doctor |
| Disconnecting a doctor's calendar | Mitigated, same as above |
| Stealing stored OAuth tokens via the API | Mitigated — tokens are never in a response model |
| Stealing stored OAuth tokens via the database | Mitigated at rest (Fernet), not against app compromise |
| A deactivated staff account still working | Mitigated — every request re-reads `is_active` from the DB, not from the JWT, so deactivation is effective on the next request, not at token expiry |
| A leaked staff JWT being revocable before it expires | **NOT mitigated** — see "Still open" |

### Still open

- **No patient identity at all.** Item 3 from the original list (a scoped,
  expiring capability token for patients) is unbuilt. Every current
  endpoint is staff/doctor-only; there is no notion of "this caller is
  patient X" yet. The chatbot (Phase 3, next) is what will need this —
  build it before the chatbot ships, not after.
- **Booking is not doctor-scoped.** `POST /appointments` accepts any valid
  staff token, front-desk or doctor, for any doctor's slot — see the
  docstring in `app/api/v1/appointments.py`. Deliberate simplification:
  the doctor is only known once the slot is resolved inside the service,
  and this endpoint's real audience is front-desk staff, who are
  unscoped by design. Revisit once doctor-role accounts are expected to
  book through this endpoint directly.
- **No token revocation. 12-hour leak window.** JWTs are stateless with a
  12-hour lifetime and no refresh/revocation store (see `core/config.py`).
  A leaked token is valid for up to 12 hours after leaking, full stop —
  deactivating the *account* takes effect immediately (see the table
  above), but the token itself cannot be individually killed.

  **Acceptable while all three hold:** staff-only (no patient tokens
  exist), single clinic, low headcount (a leaked token is one of two
  known credentials, not one of thousands). **Promotion criteria — build
  a JTI-based revocation list the moment ANY of these becomes true:**
    - a public deploy (the leak surface is no longer "someone with access
      to this machine"),
    - patient-facing tokens exist (item 3 below) — a 12-hour window on a
      *patient's* token is a different risk than on a clinic employee's,
    - a second clinic/tenant is onboarded (blast radius of one leaked
      token stops being "this one clinic").

  The fix, when triggered: a `token_id` (JTI) claim plus a small
  revoked-JTI table checked on every request — same shape as
  `oauth_states`, and a much smaller lift than it sounds like, but not
  worth building against a risk that doesn't exist yet.
- **No self-registration, on purpose.** Accounts are created via
  `scripts/create_staff_account.py` against direct DB access. Fine for
  one clinic bootstrapped by its developer; needs an actual admin flow
  before a second clinic or a non-technical owner is expected to add staff.
- **No rate limiting** on login or booking (item 4, still unbuilt).
- **Date of birth is a weak second factor, and is scoped to scheduling
  only.** The chatbot identifies a caller by the phone number the message
  arrived from and authenticates with date of birth (see
  services/conversation_service.py). DOB is knowable by family members,
  appears in breach corpora, and is not a secret in any meaningful sense.
  It is the strongest factor available over bare SMS without an
  enrollment flow, and it is a large improvement on caller ID alone —
  for **scheduling**, where the worst case is a family member moving an
  appointment.

  **PROMOTION CRITERION — blocking, not advisory:** if the chatbot is ever
  extended beyond scheduling — test results, prescriptions, clinical
  questions, anything a patient would not want a housemate to read — DOB
  **MUST be replaced before that scope lands, not after**. Retrofitting
  authentication onto a conversation surface that already handles
  clinical data means every conversation between the scope change and
  the fix is an unauthenticated disclosure. The replacement is a real
  enrollment flow (a PIN or one-time code established through a channel
  already trusted for that patient), not a second guessable fact.

  **PHASE 4 ADDENDUM (voice): the trigger above is unchanged by adding a
  second channel, but voice adds a risk this bullet did not originally
  cover.** Scope is still the thing that matters — a voice caller asking
  about their next appointment is exactly as low-stakes as a text asking
  the same thing, and the promotion criterion fires on CLINICAL SCOPE
  expanding, not on which channel is talking. What voice adds is
  independent of scope: a DOB **spoken aloud** can be overheard by anyone
  physically near the caller at that moment — a car, a waiting room, a
  speakerphone call — in a way a typed SMS is not. This is a live,
  real-time disclosure of a specific patient's actual DOB during the
  verification exchange itself, distinct from "DOB is knowable in the
  abstract" (family members, breach corpora) which is what the original
  paragraph above was about, and distinct from the STT-mishearing risk
  already handled by the read-back-confirm gate (see
  `docs/phase-4-dob-over-voice-decision.md`) — that gate protects
  against the SYSTEM mishearing the caller, not against a THIRD PARTY
  correctly hearing the caller. Not a blocking gap for a scheduling-only
  bot (the worst case is still "a family member moving an appointment",
  now via a slightly wider disclosure surface), but worth stating
  plainly rather than leaving implicit: if scope ever expands per the
  criterion above, this bullet's replacement enrollment flow (a PIN or
  one-time code) should also be evaluated for whether it is safe to
  speak aloud, not just safe to type.
- **Escalation is recorded but nothing watches for it, AND (as of the
  Phase 3 injection suite) there is no way back once it fires.**
  `conversation_service._escalate` marks the conversation ESCALATED,
  ends it, and logs a warning. No staff member is notified. The bot
  tells the caller a person will pick this up; at present, nobody is
  told to. Separately, the fix for injection-suite finding #10 (see
  `conversation_service.get_or_create_conversation`) made ESCALATED
  conversations permanently terminal for their phone number — correct,
  and necessary to close a lockout-bypass vulnerability, but it means an
  escalated caller now has no path back without a staff action that does
  not exist yet either.

  **These are now tracked together as a single blocking unit — see
  `docs/phase-3.5-pre-launch-blockers.md`, not this bullet — because
  shipping either half alone is actively worse than shipping neither.**
  Full reasoning for why notification-without-reopen and
  reopen-without-notification are both harmful, not just incomplete,
  lives there. This entry stays here as the historical record of the
  original finding; treat the other file as authoritative for current
  status and the definition of done.

  **PHASE 4 CHECK (voice): both halves confirmed to already cover the
  new channel correctly, mechanism unchanged.** `enqueue_escalation_
  notification` is keyed on `conversation_id`, not channel, so a voice
  escalation pages staff exactly like an SMS one. `conversation_service.
  reopen` likewise never references channel, and the next inbound
  contact on that phone number — whether it arrives as a text or a
  call — goes through the same `get_or_create_conversation` fall-through.
  No code change was needed for either. What voice DOES change is the
  cost of the still-open gap (`escalation_notify_phone` unset): an SMS
  caller left in an unmonitored queue is waiting on a thread they can
  reasonably expect to sit for a while; a voice caller was just told
  "someone will help you" **while live on the phone**, and the call then
  ends. If nobody is actually watching the queue, that is a worse
  experience than the SMS case, not a different risk — the requirement
  below was already unconditional ("before real patient traffic"), and
  voice does not add a new condition, only a sharper reason not to skip it.
- **`TWILIO_AUTH_TOKEN` rotation — hard date: 2026-11-01. Tracked
  separately from the credential list below, on purpose.**

  This is not one row among several outbound API keys. It is the key
  the inbound webhook uses to validate Twilio's signature (see
  `app/api/v1/webhooks.py`), which is the ENTIRE authentication
  boundary for every inbound SMS. Whoever holds it can construct a
  request this system accepts as genuinely from Twilio, with any
  `From` number they choose — which means they can impersonate *any
  patient's phone number* to the chatbot. That does not merely leak
  data: the phone-identifies half of chatbot identity (see
  `conversation_service.py`) is entirely built on the assumption that
  `From` was not attacker-controlled, and this token is the only thing
  making that assumption true. Injection-suite finding #10 already
  showed what a hole in that specific boundary costs (a lockout
  bypassable indefinitely) from a completely different angle — this is
  the same boundary, from the credential side instead of the logic
  side. Losing the SendGrid key costs a clinic its email sending
  capability; losing this one costs the identity guarantee the whole
  chatbot is built on. That difference in blast radius is why this
  gets a shorter, separately-tracked rotation cadence rather than
  sharing the general 2027-01-21 date below — bundling it in there
  undersold it once already and should not happen again. **Rotate this
  one FIRST, immediately, if there is ever reason to suspect any
  credential in `.env` leaked**, regardless of where this scheduled
  date stands.

- **Other live credentials with a hard rotation date: 2027-01-21.**
  Not "before production" — that phrase has no date attached to it and
  reliably becomes never. On that date, rotate everything in this list
  regardless of what else is true, then set a new date here.

  | Credential | Where | Why it is on this list |
  |---|---|---|
  | `frontdesk@clinic.example.com` password | `staff_accounts` | Created 2026-09-21 for auth verification; password shared in a chat transcript |
  | `rao@clinic.example.com` password | `staff_accounts` | Same |
  | `TWILIO_ACCOUNT_SID` | `.env` | Real; paired with the auth token above, but an account identifier, not a secret — low sensitivity on its own |
  | `GEMINI_API_KEY` | `.env` | Real Google Cloud key (project `gen-lang-client-0494458002`, confirmed 2026-09-23 via the AI Studio console) — see the callout below on why it isn't a rotation concern like the rest of this table |
  | `TOKEN_ENCRYPTION_KEYS` | `.env` | Dev Fernet key; rotating it requires re-encrypting stored OAuth tokens (prepend-new-key procedure in `db/types.py`) |
  | `JWT_SECRET_KEY` | `.env` | Dev signing key; rotating invalidates all issued staff tokens, which is the intended behaviour |

  > ⚠️ **`GEMINI_API_KEY` is real and does not need rotating; the model
  > pinned to it did need changing.** `gemini-3.8-flash` (the newest
  > model at the time it was picked) has a genuine, currently-documented
  > free tier of ~20 requests/day — not a misconfigured account, just a
  > brand-new model with a tiny free allocation. Attaching billing to
  > lift that would move the project to metered paid pricing, not raise
  > the free ceiling — wrong direction entirely for a project explicitly
  > built to stay on Gemini's free tier (see `gemini_model`'s docstring
  > in `core/config.py` for the same cost reasoning that drove the
  > original Anthropic → Gemini switch). Fixed 2026-09-23 by pinning to
  > `gemini-3.5-flash-lite` instead — 500 requests/day free, still $0.
  >
  > This finding took two wrong turns to land on the right one, worth
  > naming rather than erasing: first "upgrade to a paid tier" (right
  > instinct that something needed fixing, no evidence yet for HOW),
  > then "this probably isn't a real account" (real observations —
  > model name and quota didn't match this assessment's training data —
  > but the wrong conclusion, since the account was genuinely real and
  > billing genuinely was not the fix). The actual answer needed
  > checking the AI Studio console directly, not reasoning from what
  > "should" be true against either guess.

One thing that is *not* as bad as it looks: the OAuth flow cannot be used to
attach an attacker's calendar to a doctor. The `doctor_id` is bound into the
server-side state row when the link is minted, never taken from the callback, so
completing the flow requires that doctor's own Google credentials. The real OAuth
exposure is phishing — an attacker can generate consent links that genuinely
originate from this server.

## What fixing it requires

1. ~~Authentication for staff/doctor endpoints (session or bearer token).~~ **Done.**
2. ~~An authorization check per request — caller is that doctor, or is clinic staff.~~ **Done**, for read/cancel/reschedule. Booking remains unscoped — see "Still open".
3. Patient-facing access via a scoped, expiring capability token sent to the
   patient's verified phone/email, rather than a bare appointment UUID in a URL.
4. Rate limiting on booking and on OAuth link generation.

Item 3 is now the actual blocker for the chatbot: a Twilio-driven
conversation needs some notion of "this phone number is patient X" that
does not yet exist anywhere in this codebase.

## Recommendation

Design and build item 3 (patient identity/access) as part of the chatbot's
conversation-state work, not as an afterthought once booking flows already
exist without it — retrofitting patient auth onto endpoints already in use
is a migration; building it alongside is not. Keep the service bound to
localhost or behind a private network until item 3 lands and a public
deploy is actually intended.
