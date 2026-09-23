"""Application settings, loaded from environment variables.

WHY pydantic-settings rather than reading os.environ inline: config becomes a
typed, validated object that fails loudly at startup if something is missing,
instead of failing at 2am on the one code path that reads a missing key.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "clinic-scheduling-api"
    debug: bool = False

    # Must use the asyncpg driver: postgresql+asyncpg://user:pass@host:5432/db
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://clinic:clinic@localhost:5432/clinic",
    )

    # Pool sizing: asyncpg connections are cheap but Postgres' max_connections
    # is not. pool_size * number_of_workers must stay under it.
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # How long a booking transaction may hold its row lock before Postgres
    # kills it. WHY set this at all: a hung transaction holding FOR UPDATE on
    # a popular slot would block every other booker for that slot
    # indefinitely. Failing fast is better than a queue that never drains.
    statement_timeout_ms: int = 5_000
    lock_timeout_ms: int = 3_000

    # ------------------------------------------------------------------ #
    # Phase 2: secrets
    #
    # WHY SecretStr for every credential: its __repr__/__str__ render as
    # "**********". That means an accidental `print(settings)`, a FastAPI
    # debug page, or a Sentry frame capture cannot leak a key. You must call
    # .get_secret_value() to use it, which makes every real use greppable in
    # review.
    #
    # All default to empty rather than being required, so the API and the
    # test suite still boot without third-party credentials configured. The
    # services that need them fail loudly at call time instead. FLAGGED as a
    # trade-off: it moves a class of misconfiguration from startup to first
    # use. A startup self-check that warns about missing keys for ENABLED
    # features is the better answer once feature flags exist.
    # ------------------------------------------------------------------ #

    # Comma-separated Fernet keys; the FIRST is used for new encryption and
    # the rest only for decryption, which is what makes rotation possible.
    # Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_keys: SecretStr = SecretStr("")

    # --- Google Calendar OAuth ---
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    # Must EXACTLY match an authorized redirect URI in the Google Cloud
    # console, including scheme, port and trailing slash, or the consent
    # screen returns redirect_uri_mismatch.
    google_oauth_redirect_uri: str = "http://localhost:8000/api/v1/calendar/oauth/callback"

    # --- Twilio (SMS + Voice, Phase 4) ---
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_from_number: str = ""
    # How many times a voice turn may re-prompt on silence (no SpeechResult
    # at all -- the caller said nothing, or Twilio's STT produced nothing
    # usable) before giving up and ending the call. Deliberately NOT the
    # same counter as Conversation.identity_attempts: this bounds a bad
    # phone line holding a call open, not a wrong guess -- see
    # docs/phase-4-dob-over-voice-decision.md on why these two kinds of
    # "failed attempt" must stay separate, a principle this setting also
    # applies outside the DOB step specifically.
    voice_gather_max_silence_retries: int = 2

    # SUPERSEDED, 2026-09-22: this project briefly had a
    # `voice_turn_deadline_seconds` here that wrapped handle_message() in
    # asyncio.wait_for inside the live Gather webhook. Live measurement
    # showed why that could never work as scoped: a SINGLE Gemini call
    # took 12.6s, and a normal turn needs at least TWO sequential calls
    # (identity, then the answer) -- so no deadline value could both fit
    # under Twilio's ~15s webhook budget AND leave room for a real
    # response. That was the exact trigger condition
    # docs/phase-4-voice-transport-decision.md named up front for the
    # poll/redirect hold pattern, so that was built instead -- see
    # models/voice_turn_job.py. The settings below replace it.

    # How often the worker drains pending voice turns. Tighter than SMS's
    # sms_reply_scan_interval_seconds (3s) on purpose: a voice caller is
    # actively holding on a live call, unlike an SMS recipient who is not
    # watching a clock.
    voice_turn_scan_interval_seconds: int = 2

    # The Gather webhook's own "please hold" loop: how long each
    # <Pause> is, and how many times it may redirect back into itself
    # before giving up. Together they bound the TOTAL wall-clock time a
    # caller may be held waiting for a background turn -- a budget this
    # application controls, unlike a single request's Twilio-imposed
    # ceiling.
    #
    # RETUNED 2026-09-23 against a clean steady-state reading (the
    # original 20-cycle/40s value was sized off a demand-spike-inflated
    # 12.6s/call measurement -- see the retraction in
    # docs/phase-4-voice-transport-decision.md). With billing genuinely
    # unblocked and gemini-3.5-flash-lite in place (see gemini_model's
    # docstring), a real two-call turn (verify_identity -> pending
    # confirmation, then the model's own read-back reply) measured
    # 1.51s + 1.43s = 2.99s total wall time; a single-call turn measured
    # 1.29s. 10 x 2s = 20s keeps roughly 6-7x margin over that measured
    # two-call turn -- comfortable without being the old, now-known-to-be
    # oversized budget. STILL A THIN SAMPLE (n=2 successful turns): revisit
    # again with real production traffic before treating 20s as precisely
    # right rather than a reasonable, evidence-adjusted estimate.
    voice_hold_poll_interval_seconds: int = 2
    voice_hold_poll_max_cycles: int = 10

    # --- SendGrid email ---
    sendgrid_api_key: SecretStr = SecretStr("")
    sendgrid_from_email: str = "noreply@example-clinic.test"

    # ------------------------------------------------------------------ #
    # Phase 2: job scheduling knobs
    # ------------------------------------------------------------------ #
    calendar_sync_interval_minutes: int = 5
    # How far ahead we mirror the doctor's calendar. Must comfortably exceed
    # how far ahead patients can book, or bookings could be made beyond the
    # synced horizon and never checked against the doctor's real calendar.
    calendar_sync_window_days: int = 90
    # How often to abandon the incremental cursor and re-read the whole
    # window. REQUIRED, not an optimization: a sync token pins the time
    # window from the full sync that created it, so without re-baselining
    # the horizon never moves forward and events beyond the original
    # timeMax are never seen -- availability silently stops being checked
    # past that date.
    calendar_full_resync_days: int = 7

    # How often the notification sender drains the outbox. Short, because
    # booking confirmations are supposed to feel immediate.
    notification_scan_interval_seconds: int = 15
    reminder_lead_hours: int = 24
    # If the worker was down and a reminder is now this far overdue, skip it
    # rather than sending a "reminder" for an appointment that already
    # happened or is imminent. Policy value -- confirm with the clinic.
    reminder_staleness_cutoff_hours: int = 6
    # A row CLAIMED longer than this is assumed to be from a crashed worker
    # and gets moved to UNRESOLVED for human/reconciliation review.
    notification_claim_timeout_minutes: int = 10

    # ------------------------------------------------------------------ #
    # Phase 3.5: escalation paging
    # ------------------------------------------------------------------ #
    #
    # SMS destination for "a conversation escalated to a human". Reused
    # infrastructure (TwilioSmsSender, the same outbox as patient
    # reminders) rather than email: an escalation means a patient is
    # mid-conversation waiting, and SMS gets read faster than an inbox.
    #
    # SIMPLIFICATION, flagged: ONE destination, matching the single-
    # clinic/single-doctor assumption already pervasive in this codebase.
    # A real clinic with a rotating front-desk shift needs this to be a
    # roster (query staff_accounts for on-duty front_desk numbers) rather
    # than a static config value -- revisit alongside the multi-doctor
    # generalization already flagged elsewhere.
    #
    # DELIBERATELY EMPTY BY DEFAULT, and NOT treated as "fine, just skip
    # it" the way a patient's missing email is -- see
    # notification_service.enqueue_escalation_notification. An
    # unconfigured destination here means every future escalation is
    # silently unmonitored, which is precisely the harm Phase 3.5 exists
    # to close; it is logged as an ERROR, not info, when it is empty at
    # the moment an escalation actually fires.
    escalation_notify_phone: str = ""

    # How long a doctor has to finish Google's consent screen before the
    # state row expires. Short enough that a leaked state is stale almost
    # immediately, long enough for someone to find their password and do 2FA.
    oauth_state_ttl_minutes: int = 15

    # ------------------------------------------------------------------ #
    # Phase 3: authentication
    # ------------------------------------------------------------------ #
    #
    # Symmetric HS256 signing key for staff JWTs. Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    #
    # WHY HS256 (shared secret) rather than RS256 (public/private key pair):
    # this API is both the issuer and the only verifier -- nothing else
    # needs to check a token's signature without holding the secret. RS256
    # earns its complexity when a THIRD PARTY must verify tokens without
    # being trusted to mint them; that is not this system.
    jwt_secret_key: SecretStr = SecretStr("")
    jwt_algorithm: str = "HS256"

    # Deliberately SHORT-lived, and deliberately NO REFRESH TOKEN in this
    # phase. A stateless JWT cannot be revoked before it expires -- there is
    # no row to delete, no state to flip. Keeping the lifetime short (a
    # single clinic shift) bounds how long a leaked token stays dangerous.
    # A staff member logs in again the next day. FLAGGED: this is the
    # honest tradeoff of shipping auth without also shipping a revocation
    # store; a refresh-token table (issued once, checked and revocable on
    # every refresh, same shape as oauth_states) is the natural upgrade
    # once "staff has to re-login every 12h" becomes an actual complaint.
    jwt_access_token_expire_minutes: int = 720  # 12 hours

    # ------------------------------------------------------------------ #
    # Phase 3: chatbot conversations
    # ------------------------------------------------------------------ #
    #
    # How long a verified identity stays valid within a conversation. An
    # SMS thread is long-lived and phones get handed to family members; a
    # verification from this morning should not still authorize a
    # cancellation this evening. Short enough to bound that, long enough
    # that a normal back-and-forth booking never re-prompts.
    # POLICY VALUE -- confirm with the clinic.
    conversation_identity_ttl_minutes: int = 60

    # How long a proposed booking/cancellation stays confirmable. An offer
    # is stale well before the slot is: the patient has moved on, and the
    # slot may have gone to someone else.
    proposal_ttl_minutes: int = 15

    # Idle timeout for a whole conversation.
    conversation_ttl_minutes: int = 1440  # 24h

    # ------------------------------------------------------------------ #
    # Phase 3: Twilio inbound webhook
    # ------------------------------------------------------------------ #
    #
    # The PUBLIC url Twilio used, which is what it signed. This must be
    # configured explicitly and cannot be derived from the request:
    # behind a tunnel, proxy or load balancer the app sees a different
    # scheme, host or port than Twilio called, and the signature is
    # computed over the URL STRING. Deriving it from request.url is the
    # single most common reason Twilio signature validation "randomly"
    # fails in one environment and works in another.
    twilio_webhook_base_url: str = "http://localhost:8000"

    # How often the worker drains inbound SMS jobs. Much shorter than the
    # notification scan: this is a person waiting for a reply in a live
    # conversation, not a reminder due tomorrow. The query is a partial
    # index lookup, so polling this often is cheap.
    #
    # NOTE what replaced what here: an earlier version ran the whole
    # conversation loop inside the webhook with a deadline below
    # Twilio's ~15s, falling back to a canned message when it overran.
    # That traded "the patient gets nothing" for "the patient sometimes
    # gets a useless answer", which is better but still bad. Moving the
    # work to the worker removes the deadline from the request path
    # entirely -- see models/sms_reply_job.py.
    sms_reply_scan_interval_seconds: int = 3

    # ------------------------------------------------------------------ #
    # Phase 3: the model
    # ------------------------------------------------------------------ #
    gemini_api_key: SecretStr = SecretStr("")
    # Pin it: a model id that floats means the bot's behaviour changes
    # under you without a deploy, which for a system that books medical
    # appointments is not a convenience.
    #
    # CHANGED 2026-09-23, deliberately, for cost -- same reasoning as the
    # original Anthropic -> Gemini switch. `gemini-3.8-flash` (the
    # newest model at the time) has a real free-tier cap of ~20
    # requests/day, which a single multi-turn conversation can exhaust
    # outright -- confirmed live during Phase 4 verification. Attaching
    # billing to move past that would switch the project to metered
    # paid pricing, not just raise the free ceiling -- the opposite of
    # what "stay free" means. `gemini-3.5-flash-lite` has a documented
    # 500 requests/day free tier instead -- 25x the headroom, still
    # $0. FLAGGED, not fully verified yet: "Lite" trades some capability
    # for cost/speed, and this bot leans on reliable function-calling
    # (verify_identity, propose_/confirm_ tools) for its whole safety
    # model. Re-run the injection suite and the DOB-confirm suite's
    # tool-payload assertions against this model specifically once quota
    # allows, rather than assuming Flash-tier tool-calling quality
    # carries over unchanged.
    gemini_model: str = "gemini-3.5-flash-lite"
    # A patient is waiting on an SMS reply. Long enough for a real
    # response, short enough that a hung provider produces a fallback
    # rather than a Twilio webhook timeout.
    gemini_timeout_seconds: float = 20.0

    # How many past messages to replay. A cap rather than summarisation --
    # see the note in chatbot/loop.py on why truncation is safe here.
    conversation_history_limit: int = 40

    # Tool calls the assistant may make while answering ONE inbound
    # message. A model that exceeds this is going in circles, which is an
    # escalation rather than something to keep paying for.
    max_tool_calls_per_turn: int = 5

    @property
    def token_encryption_key_list(self) -> list[str]:
        """Parsed encryption keys, newest first."""
        raw = self.token_encryption_keys.get_secret_value()
        return [k.strip() for k in raw.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is parsed once per process."""
    return Settings()
