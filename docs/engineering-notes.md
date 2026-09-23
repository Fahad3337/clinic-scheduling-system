# Engineering notes: process, not just outcomes

Most of this project's documentation (`docs/security-no-authentication.md`,
`docs/phase-3.5-pre-launch-blockers.md`, the two Phase 4 decision docs)
records *what was decided and why*. This file records something narrower
and, in its way, more revealing: a case study of the project's own
verification discipline being applied to the AI collaboration process
itself, in real time, under actual pressure — not to the code, to a
*claim about the code*.

## The Gemini verification episode (2026-09-23)

**The setup.** Phase 4 voice work had been blocked by `GEMINI_API_KEY`
hitting a 20-requests/day cap during live testing. The user asked for
two things: confirm whether "paid tier" meant simple billing or a
different rate-limit shape, and get a clean latency reading once
unblocked. Straightforward, or so it seemed.

**Claim 1: "this probably isn't a real Google account."** The reasoning
looked solid at the time: the configured model name (`gemini-3.8-flash`)
matched nothing in the assessment's training data, the observed 20/day
cap didn't match Google's documented free-tier figures for known Flash
models (~1,500/day), and the API path (`/v1beta/interactions`) didn't
match the well-known REST shape (`/v1beta/models/{model}:generateContent`).
Three data points, one direction, stated as a conclusion. The user was
asked to confirm — and did, initially agreeing the key looked
provisioned for the environment rather than a personal account.

**Claim 1, falsified.** The user then produced a screenshot: a real
Google AI Studio console, a real project (`gen-lang-client-0494458002`),
the key in question, sitting on "Free tier" with a "Set up billing"
control right next to it. Not a theory — a screenshot of a real console
session. Claim 1 was wrong, and wrong in a way that a moment of
"let me actually check the account" would have caught before ever being
stated as fact.

**Claim 2: "attach billing."** This followed immediately from the
screenshot, without stepping back to ask whether it fit what the user
had actually told the project days earlier: this system runs on Gemini
*specifically because* the user switched off Anthropic for cost reasons
and wants to stay free. "Attach billing" solves the rate limit and
breaks the actual requirement. The user caught this contradiction
directly — not by finding a technical flaw, but by naming the pattern:
*two fluent, confident, mutually contradictory stories in a row, and I'm
not accepting a third one.*

**The instruction that mattered:** stop producing narratives, produce
raw evidence. Specifically: a verbatim, unformatted HTTP response
including headers; a live fetch of Google's own documentation with a
direct quote; or an honest "I don't know and can't verify" if neither
was obtainable. No fourth story.

**What raw evidence actually showed:**
- A real `curl -v` request to `generativelanguage.googleapis.com`,
  pasted unedited: a TLS certificate chain issued by Google Trust
  Services matching `*.googleapis.com`, real Google infrastructure
  response headers, and a genuine 400 error body (from a deliberately
  malformed test request) listing dozens of real, internally-consistent
  step-type values.
- A live fetch of `ai.google.dev/gemini-api/docs/pricing`, quoted
  directly: both `gemini-3.8-flash` and `gemini-3.5-flash-lite` appear
  verbatim, alongside three dozen other real, dated model identifiers.
- A live fetch of `ai.google.dev/gemini-api/docs/text-generation`,
  quoted directly: the documented basic-text-generation request shape
  uses the exact same endpoint (`/v1beta/interactions`) and the exact
  same typed-input-array convention this project's own
  `app/integrations/gemini.py` already sends.

**Resolution.** The account is real. The model names are real — simply
newer than the assessment's training data, which is a knowledge-cutoff
problem, not a signal that anything else was fake. The API surface is
Google's real, documented basic chat-completion contract, not an
adjacent or undocumented one. The actual fix was neither of the first
two guesses: pin the model to `gemini-3.5-flash-lite` (500 requests/day
free, genuinely $0) instead of `gemini-3.8-flash` (~20/day free), and
leave billing untouched.

## Why this belongs in the repo, not just the chat transcript

Every other correction in this project's history — the DOB-over-voice
read-back-confirm design (`docs/phase-4-dob-over-voice-decision.md`),
the discovery that Gemini's real API uses a `steps` field and typed
step objects rather than the shape first assumed (`app/integrations/
gemini.py`'s `ModelTurn.raw_steps` docstring), the retracted premise
that SMS's outbox pattern implied a "10s-under-15s" deadline convention
that never actually existed (`docs/phase-4-voice-transport-decision.md`)
— is a case of the same underlying discipline: a claim about the
*system* was checked against the *real system* rather than trusted
because it sounded coherent. Migration `0009`'s docstring records
autogenerate missing a CHECK constraint for the fifth time; `for-update
-needs-populate-existing` was found because a test's premise was
checked independently rather than trusted because the test passed.

This episode is the same discipline pointed at a different target: not
"does the code do what I think it does", but "does what I just told you
about an external system match what that system actually says about
itself." The distinction matters because the second kind of claim is
*more* persuasive when wrong, not less — a fluent, well-reasoned,
internally-consistent explanation is exactly what a plausible-sounding
mistake looks like, and nothing about sounding confident correlates
with being correct. The project's standing conventions (verify a test's
premise, not just its outcome; assert state, not text; audit every
config surface, not just the ones already known to matter) exist to
catch that same failure mode in code. Here the failure mode showed up
in the collaboration itself, mid-conversation, and held up only because
one party refused to accept a third confident story and asked for
something unverifiable-in-good-faith to fabricate: a raw HTTP response
and a direct quote from a live-fetched page.

That is the actual test of whether a stated engineering discipline is
real: not whether it appears in code comments, but whether it survives
being turned on the person applying it.

## The "push and verify CI is green" episode (2026-09-23)

Publishing this repository meant, for the first time, running the test
suite against a genuinely clean checkout — no local `.env`, no
Dockerfile build-order accidents, nothing carried over from a long-lived
development session. That turned out to be its own adversarial
condition, distinct from anything local dev or Docker had ever actually
exercised, and it found two real bugs on the very first push.

**Bug 1: `pip install -e ".[dev]"` failed** with setuptools refusing to
guess between `app/`, `alembic/`, `scripts/` and `tests/` all sitting at
the repo root. Never caught before because the Dockerfile runs
`pip install -e .` *before* `COPY . .` — the ambiguity a full checkout
creates was simply never present at that point in any Docker build this
project had ever run.

**Bug 2, the more interesting one: 12 auth tests failed** with
`jwt.exceptions.InvalidKeyError: HMAC key must not be empty`.
`tests/conftest.py` carried a comment claiming its `JWT_SECRET_KEY`
fallback was set "before any `app.*` import" — but the actual import
order in the file put `from app.db.session import build_engine, ...`
*first*, which calls the `@lru_cache`-decorated `get_settings()` at
module level and caches an empty key before the fallback line ever
runs. Locally, and in every Docker run all project, the real dev `.env`
always supplied a real `JWT_SECRET_KEY` before Python even started, so
the cached value was never actually empty — the ordering bug was true
but silent. A clean CI checkout, with no `.env` at all, was the first
environment honest enough to expose it.

**This is the SAME finding as `for-update-needs-populate-existing`, from
a different direction.** Both are a comment asserting a guarantee ("the
lock is held", "this runs before any app import") that the code next to
it does not actually provide. In both cases the comment was not
decorative — someone had reasoned about the requirement correctly and
then written code that didn't implement it, and nothing forced a
re-check. The standing conclusion holds across both: a comment claiming
an invariant is evidence someone once cared about it, not evidence
anything currently enforces it. Audit the category this describes, not
just the one instance that already bit you — that's twice now, in two
unrelated parts of the codebase, caught by two different mechanisms (a
concurrency stress test; a clean-environment CI run).

**Clean-environment CI is now understood as a fifth verification
surface** this project deliberately checks, alongside cold sessions
(does code survive a fresh identity map, not a warm cache), recorded
fixtures (does code survive the actual shape a real provider sends, not
the shape assumed), and the config audit (does test config match
production config, not just the three settings that already caused a
problem). Each surface exists because something passed everywhere else
and failed there first. None of them are redundant with the others —
this project's whole track record is that they keep finding different
bugs, not the same bug twice.
