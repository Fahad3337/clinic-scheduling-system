# Phase 4 decision: DOB verification over voice

**Status: DECIDED 2026-09-22, before any Phase 4 code.** Independent of
`docs/phase-4-voice-transport-decision.md` — this holds regardless of
which transport option is chosen there, and is addressed separately on
purpose rather than folded into that write-up.

**Decision: keep the 3-strike threshold identical across channels; do
not weaken it for voice. Instead, add a mandatory read-back-confirm
step before a spoken DOB is ever CHECKED against a patient record or
counted as a guess.**

**IMPLEMENTATION CORRECTION (2026-09-22, when this was built): the claim
below that `conversation_service.verify_identity` needs zero changes did
not survive contact with implementation — see "Implementation note" at
the end of this file for what actually changed and why, and for why the
security property this decision cares about (the guess budget) is
unaffected regardless.**

## Why the SMS-era math does not just transfer

`MAX_IDENTITY_ATTEMPTS = 3` and the failure-counting in
`_register_failure` were built around SMS's failure mode: a caller
types a date of birth, and a wrong answer is either an actual wrong
guess (the caller doesn't know it, or is an attacker) or a plain typo.
Both are properly the caller's problem — the caller could see exactly
what they typed before sending, and a typo is comparable in kind to a
genuine wrong guess for the purpose of "was this attempt informative
about whether the caller knows the DOB."

Voice breaks that equivalence. The caller's spoken words pass through a
speech-to-text layer before anything in this system ever sees them.
STT misrecognizing digits or month names ("15" heard as "50", "March"
heard as "May") produces a WRONG value the caller never actually said
and had no chance to review — this is not a typo, it is a transport
failure that happens to look identical, from `verify_identity`'s
point of view, to a wrong guess. Counting it as a strike is counting
against the caller for a failure that is entirely the system's fault,
not theirs.

## What this actually threatens, precisely

**Not a new attacker capability.** The exact-match comparison in
`verify_identity` (`patient.date_of_birth != date_of_birth`) means a
misrecognized value would have to happen to land on the correct real
DOB to falsely succeed — vanishingly unlikely. An attacker's guessing
capability is unchanged by the existence of STT: they get exactly as
many genuine guesses as the strike count allows, on any channel. This
is NOT a confidentiality/authentication-bypass risk.

**An availability/escalation-cost risk instead.** A legitimate caller
who says the correct DOB but is misheard three times gets escalated
exactly like an attacker who genuinely doesn't know it — hitting the
same "permanently terminal until a staff member reopens it" path
Phase 3.5 built (`conversation_service.get_or_create_conversation`,
`reopen`). A meaningfully-nonzero STT digit-error rate applied across
every voice caller translates directly into inflated staff escalation
volume — landing on exactly the failure mode
`docs/phase-3.5-pre-launch-blockers.md` already warned about
("an unmonitored escalation queue is worse than no escalation path"),
except now self-inflicted by transcription noise rather than by actual
lockouts. This is an operational and UX cost, not a security hole, and
should be fixed as one.

## Options considered, and why the obvious one is wrong

**Loosen the strike count for voice** (e.g. more attempts before
escalation, because "a strike means less over voice"). Rejected: this
fixes a transcription-accuracy problem by weakening the actual security
property (how many independent guesses an attacker gets) — for an
attacker, phone and SMS present identical risk, so there is no
principled reason their guess budget should differ by channel. This
conflates two unrelated layers: the fix for a noisy input channel
belongs in that channel's transport, not in the authentication logic
that has to hold regardless of transport.

**Keep the threshold, fix the input layer instead.** The right target:
prevent a mis-transcription from ever being submitted as a guess in the
first place, so every attempt that DOES consume a strike is one the
caller actually confirmed as correct. Chosen approach below.

## The chosen mechanism: read-back-confirm before commit

After STT produces a candidate date, the voice transport layer speaks
it back and asks for explicit confirmation ("I heard March 15th,
1990 — is that right?") BEFORE calling `verify_identity` at all. A
caller who says "no" triggers a re-prompt and consumes **no strike** —
it never reached the service as a guess. Only a caller who confirms
"yes, that's correct" has the value passed to `verify_identity`, at
which point a mismatch is a real signal (a genuine wrong guess, or the
caller misremembering) worth counting exactly as it does today.

This is the same category of carve-out `conversation_service` already
has for `patient.date_of_birth is None` — not every failure to verify
is the caller's fault, and the existing code already distinguishes
that. This extends the same principle to a channel-specific failure
mode, at the layer that actually owns it.

**Where this lives, and why it must be structural, not prompted.**
Every other identity-security property in this codebase is enforced
structurally, never left to the model's discretion — no tool schema
accepts a `patient_id`, the phone number is read from the transport
layer and never from model output, attempt counting is server-side so
the model cannot be talked into believing a different count. Asking
Gemini to "please confirm the DOB by reading it back before calling
verify_identity" as a prompt instruction would be the odd one out: a
security-relevant guarantee resting on the model choosing to comply.
The read-back-confirm belongs in the deterministic voice IVR flow
itself (plain `<Say>`+`<Gather>` logic, comparable to the rest of the
call's scripted structure) — `verify_identity` is simply never invoked
until a caller has verbally confirmed the captured value, the same way
today's tool layer never lets the model supply a patient id. This
requires **zero changes to `conversation_service.verify_identity`** —
the fix is entirely in what the voice transport adapter passes in, and
when.

**Sanity check performed on this design** (per this project's standing
"verify a test's premise, not just its outcome" discipline): does
saying "no" to a read-back give an attacker extra guesses? No — the
read-back only confirms what the system HEARD, never whether it is
correct. An attacker gains the ability to make sure each of their
strikes matches their actual intended guess, not the ability to make
more strikes or to learn anything about the real DOB before committing
one. The guess budget is unchanged; only the fidelity between "what the
caller said" and "what gets checked" improves.

**One new bound needed, unrelated to the identity strike count:** the
confirm loop itself must be capped (e.g. 3 re-prompts) before falling
back to escalation on its own — a call-length/cost hygiene limit, not a
security limit, and tracked separately from `identity_attempts` so a
noisy phone line cannot hold a call open indefinitely.

## Compatibility with the transport decision

This mechanism does not depend on which option
`phase-4-voice-transport-decision.md` picks. Under webhook-per-turn
(Option A there), Twilio's `<Gather>` action webhook already returns
the transcribed `SpeechResult` text, so the read-back-confirm is built
entirely with ordinary `<Say>`/`<Gather>` TwiML in the handler — no
extra infrastructure. Under Media Streams (Option B there), the same
confirm-before-commit pattern applies at the application layer instead
of in TwiML. Built independently of the transport choice, as intended.

## Implementation note (2026-09-22): why a pure-TwiML gate wasn't possible

The plan above assumed the voice TRANSPORT layer could recognise "this
utterance is a spoken date of birth" on its own, deterministically,
before anything reaches the model — mirroring how the phone number is
read straight from Twilio's `From` field, never from the model. That
assumption doesn't hold. A caller states their DOB inside an ordinary
sentence ("it's March 14th, 1985"), and distinguishing that from an
unrelated date-shaped utterance ("do you have anything March 14th?",
a requested APPOINTMENT date) requires exactly the natural-language
understanding the model has and a plain `<Gather>` handler does not. A
regex/date-parser heuristic on raw `SpeechResult` text would misfire on
both kinds of sentence and had no clean way to know which one applied.

So the model still extracts the candidate DOB from the caller's speech
and still calls `verify_identity` itself, exactly as it does for SMS —
that part was never realistically avoidable. What moved into
`conversation_service.verify_identity` instead is a structural gate,
not a trust-the-model gate: a new `pending_dob_candidate` column on
`Conversation` (voice only; always NULL for SMS). The FIRST call with a
given value is never checked against a patient record and never
touches `identity_attempts` — it is only stored and echoed back via the
tool result. Only a SECOND call that restates the SAME value proceeds to
the real, strike-counted check. See `Conversation`'s module docstring
("PHASE 4: A THIRD GUARANTEE") and `verify_identity`'s own docstring for
the mechanism in full.

**Why this still satisfies "structural, not prompted", despite living
inside `verify_identity` now:** the property this decision actually
cares about — that a mis-transcription can never consume a strike or
falsely pass — is enforced by the SERVICE comparing two independent
tool calls, not by trusting the model to behave. What genuinely IS
prompted (and was always going to be, for the reason above) is the
CONVERSATIONAL half: getting the model to actually read the date back
and wait for a yes before re-calling. If the model skips that and
re-calls immediately, or never re-calls at all, the worst case is a
confused conversation or a caller who never gets verified — never an
extra guess, never a bypassed check, never a false accept. This is the
exact same trust boundary this codebase already accepts for
propose/confirm booking (the model is trusted to wait for real
agreement; only the resulting mutation is structurally single-use), not
a new or weaker one introduced for voice.

The strike-consumption question this file exists to answer is unchanged
by this correction: a "no" (in practice, the model calling back with a
DIFFERENT value than the pending one) still costs nothing, and only a
CONFIRMED, matching value is ever compared against the real record.
Live-verified against the real database during implementation, and
covered by `tests/test_voice_dob_confirm.py`.
