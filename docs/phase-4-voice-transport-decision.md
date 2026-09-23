# Phase 4 decision: voice transport shape

**Status: DECIDED 2026-09-22, before any Phase 4 code.** Same rigor as
the Phase 3 Anthropic-vs-Gemini and raw-loop-vs-Tool-Runner decisions —
written up and settled before touching the codebase, not discovered
mid-implementation.

**Decision: webhook-per-turn using Twilio's own `<Gather input="speech">`
+ `<Say>`, not a Media Streams / custom STT-TTS pipeline.** Reasoning
below. Treated entirely separately from the DOB-over-voice question —
see `docs/phase-4-dob-over-voice-decision.md` — that decision does not
depend on which option is picked here.

## The two shapes, and why they are not a spectrum

**Option A — webhook-per-turn (Twilio-hosted STT/TTS).** A call comes
in, Twilio hits a webhook, the response is TwiML: `<Say>` to speak a
prompt, `<Gather input="speech">` to collect the caller's reply. Twilio
runs its own speech-to-text, then POSTs the transcript to an action URL
as a normal HTTP request. That handler runs one turn (equivalent to one
`handle_message` call today) and returns new TwiML — another
`<Say>`+`<Gather>`, or `<Hangup>`. Structurally this is the SMS webhook
shape with the transport swapped: synchronous request in, synchronous
response out, one turn per HTTP round-trip.

**Option B — Media Streams (`<Connect><Stream>`), own STT/TTS.** The
call is handed to a persistent bidirectional WebSocket carrying raw
audio. The application runs (or proxies to) a streaming STT provider
for near-real-time partial transcripts, feeds them to the model,
streams synthesized speech back over the same socket as the response is
generated. This is a genuinely different application shape: a live,
stateful, long-running connection per call, not a stateless request.

These are not "basic vs. advanced settings on the same feature" — B
requires an architecture this codebase does not have anywhere yet
(long-lived per-call server-side state, a new external audio provider,
interruption/barge-in handling), while A reuses the request/response
model every other transport in this project already uses.

## Structural cost of each, point by point

### Does it need a reply outbox?

**A: no, not for the turn itself — and this is the one place the SMS
pattern does NOT port over unchanged.** The SMS outbox
(`sms_reply_service.py`) exists because Twilio's inbound SMS webhook
can be acknowledged immediately and the real reply sent later, at any
time, via a separate outbound REST call — the texter is not on a live
connection waiting. A phone call has no equivalent "ack now, reply
whenever" primitive: the caller is on the line, and Twilio is waiting
on THIS webhook's TwiML response to know what happens next on the call.
There is no way to "enqueue the reply and let a background worker send
it later" the way `send_sms_replies` does, because there is nothing to
send it TO independent of the live call's TwiML flow.

If a turn's processing time becomes a real problem (slow tool calls,
Gemini latency), the fallback is a poll/redirect pattern — acknowledge
with a short `<Play>`/`<Say>` "please hold" and a `<Redirect>` to a
polling endpoint that checks if a background-computed reply is ready,
looping on short redirects until it is. This is a genuinely new
mechanism, not a reuse of `sms_reply_service`'s outbox, and it has its
own UX cost (audible dead air / hold loops). **Not building this up
front** — see Decision below.

**B: no REST outbox either, but for the opposite reason.** The "reply"
is a continuous audio stream generated as the model's response is
produced, sent directly over the open socket. There is no delivery step
to make crash-safe the way SMS delivery is, but there IS new state to
manage that doesn't exist today: per-call session state (is TTS
currently playing, did the caller start talking again mid-playback and
need it interrupted, which tool calls are in flight). None of that is
an outbox, but all of it is new.

### What auth shape?

**A: direct reuse.** Twilio signs every webhook request
(`X-Twilio-Signature`) the same way for voice as for SMS. The existing
`RequestValidator`-based check in `app/api/v1/webhooks.py` — fails
closed, no bypass flag — applies to the new voice webhook routes
unchanged in mechanism; only the URL and payload shape differ.

**B: not a direct reuse.** A Media Streams connection is a WebSocket
upgrade, not a signed POST per turn. Twilio does authenticate the
initial handshake, but validating a WebSocket upgrade is a different
code path from validating a signed form-encoded POST body, and needs
its own review rather than inheriting the existing helper's guarantees
by assumption. **FLAGGED, not verified**: the exact signature mechanism
Twilio uses for the Stream upgrade request needs to be checked against
current Twilio docs before Option B is ever built — do not assume it is
the same `X-Twilio-Signature` check bolted onto a new URL.

### How real-time can it actually be?

**A: turn-based, with real gaps.** Each turn costs one full HTTP
round-trip plus whatever the handler does (model call, tool execution,
DB reads/writes) before Twilio will speak the next prompt. No
interruption/barge-in without additional Twilio-side configuration on
top of the basic `<Gather>` flow. This reads to the caller like a
capable IVR with natural-language input, not a fluid conversation.
**FLAGGED, not verified**: Twilio's webhook response timeout for TwiML
is a real, finite budget — check the current documented value before
Phase 4 implementation, since it bounds how much synchronous work a
turn handler can safely do before the poll/redirect fallback above
becomes necessary rather than optional.

**B: genuinely real-time.** Streaming partial transcripts, sub-second
TTS start, real interruption support — the only option that produces
an actually natural-feeling phone conversation. This is the entire
reason Media Streams exists; option A cannot approximate it no matter
how the webhook handler is tuned, because the fundamental unit of
interaction is "one full turn," not "a continuous stream."

## Recommendation

**Build Option A first.** Four reasons, in order of weight:

1. **Reuses the existing identity/tool/loop layer almost unchanged.**
   `conversation_service`, `proposal_service`, `chatbot/tools.py`, and
   `chatbot/loop.py` are already transport-agnostic — one turn in, one
   turn out — and `ConversationChannel.VOICE` already exists in the
   enum. Option A's webhook handler is a new adapter around that
   existing core, the same relationship the SMS webhook already has to
   it. Option B would need that core adapted to a streaming/partial-
   input model it was never designed for.
2. **Reuses the proven auth mechanism.** No new security review needed
   for the inbound-request authentication boundary — the exact thing
   Twilio-signature validation exists to protect (see the
   `TWILIO_AUTH_TOKEN` callout in `docs/security-no-authentication.md`)
   is unchanged in kind.
3. **Avoids taking on a new PHI-adjacent third-party dependency in the
   same phase as first getting voice working at all.** Option B
   requires selecting, integrating, and reviewing a streaming STT/TTS
   vendor — a new place patient speech leaves this system, with its own
   data-handling questions analogous to the `store: false` requirement
   already imposed on Gemini. That is a real, separate piece of work
   and deserves its own decision when (if) it is actually needed, not
   bundled into "make voice work at all."
4. **Matches this project's standing convention**: ship the simpler
   correct thing, flag the upgrade path explicitly, do not build for a
   hypothetical future requirement. The caller-experience cost of A
   (turn-based, brief gaps) is a real, honestly-disclosed trade-off —
   not a hidden one — and is the right cost to accept for a first cut.

**Within Option A: start fully synchronous, do not build the
poll/redirect hold-loop pattern up front.** Build it only if real
testing against the actual Twilio webhook timeout shows turns routinely
running long — the same "don't build for hypothetical future
requirements" reasoning applies one level down.

**Option B is the explicit, named upgrade path** if a genuinely
natural-feeling, interruptible phone conversation ever becomes a real
requirement rather than a nice-to-have. Not attempted now.

## Implementation note (2026-09-22): the poll/redirect trigger was hit, not just risked

"Build it only if real testing shows turns routinely running long" was
the deferral above. Live verification during implementation supplied
that evidence directly: a SINGLE Gemini call measured 12.6s against the
real API. Twilio's webhook budget is ~15s. A normal turn needs at least
two sequential calls (identity, then the answer) before it can reply —
so no in-request deadline could fit under Twilio's budget and still
leave room for a real response; the first cut (synchronous
`asyncio.wait_for` inside the Gather webhook) would have apologized and
hung up on essentially every real conversation. That is not "routinely
running long", that is "cannot work as scoped, ever, under measured
conditions" — a stronger trigger than the one this doc named, hit
sooner than expected.

The poll/redirect hold pattern was built in response — see
`app/models/voice_turn_job.py` for the mechanism (a fourth outbox,
simpler than `SmsReplyJob` since voice has no separate delivery step)
and `app/api/v1/webhooks.py`'s module docstring for the full before/
after. In brief: the Gather webhook now does only the fast part
(validate, load/create the conversation, enqueue a job) and replies
with "please hold" TwiML; the worker runs the model with no Twilio
deadline at all, exactly like SMS's worker; a poll loop with its own
bounded cycle count (`voice_hold_poll_max_cycles` x
`voice_hold_poll_interval_seconds`, ~40s) reads the result back. Twilio's
per-request budget is no longer coupled to how long the model takes at
all — the cumulative wait is a budget this application controls.

One caveat was recorded rather than glossed over at the time: the 12.6s
measurement was taken during, or immediately adjacent to, a real Gemini
high-demand window that also produced live 503s, and further attempts
to get a clean steady-state number were blocked by quota exhaustion
during that same verification session. That caveat is now resolved.

## Follow-up (2026-09-23): clean measurement obtained, settings retuned

Two things were fixed together, not sequentially: the project's Gemini
key turned out to be on a real Google Cloud project with billing never
attached (not, as first suspected, a non-production key — see the
retraction trail in `docs/security-no-authentication.md`'s
`GEMINI_API_KEY` entry), and the configured model (`gemini-3.8-flash`,
the newest at the time) turned out to have a real ~20-requests/day free
allocation, tiny enough that it -- not an overload event -- was the
actual cause of the exhaustion during the original measurement session.
Attaching billing was rejected as the fix (it would have moved the
project to metered paid pricing, directly against this project's
explicit goal of staying free — the same reasoning behind the original
Anthropic → Gemini switch). The model was repinned to
`gemini-3.5-flash-lite` instead: 500 requests/day free, still $0.

With that in place, a clean reading was taken: a real two-call turn
(`verify_identity` → `pending_confirmation` → the model's own natural
read-back reply, unscripted — the first time the voice DOB-confirm gate
was observed working end-to-end against a real model rather than a
test double) measured **1.51s + 1.43s = 2.99s total**; a single-call
turn measured **1.29s**. Both a fraction of the original 12.6s figure,
confirming that number was indeed demand-spike-inflated as suspected,
not representative of steady state.

`voice_hold_poll_max_cycles` was retuned from 20 to 10 (10 x 2s = 20s
total hold budget) — roughly 6-7x margin over the measured two-call
turn, comfortable without carrying the old, now-confirmed-oversized
40s ceiling. Still a thin sample (n=2 successful turns): treat 20s as a
reasonable evidence-adjusted estimate, not a precisely-tuned constant,
and revisit again once real production traffic provides more data. The
pattern itself remains correctly justified regardless of the exact
number — Twilio's per-request budget still cannot depend on model
latency at all, fast or slow — this follow-up only replaces a
provisional constant with a better-supported one.
