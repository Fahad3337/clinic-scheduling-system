"""Google OAuth2 authorization-code flow, with PKCE.

WHY THIS TALKS TO GOOGLE'S ENDPOINTS DIRECTLY INSTEAD OF USING
google-auth-oauthlib's Flow HELPER
---------------------------------------------------------------
The authorization-code exchange is one HTTPS POST with six form fields. The
library wraps that in a stateful `Flow` object whose behaviour around PKCE,
scope normalization and redirect handling varies between versions, and which
is awkward to fake in tests (it wants to own the HTTP layer).

Doing it explicitly here means: every parameter we send is visible in this
file, the whole thing is one injectable async function, and tests can
substitute a fake client without monkeypatching a third-party internal. For
a codebase whose stated goal is explaining WHY, thirty lines of legible HTTP
beats a black box.

`google-auth` is still the right tool for signing API requests later (the
Google client wrapper step) -- this is specifically about the auth dance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# openid+email identify WHICH Google account was linked, so staff can see it
# and so a re-authorization with a different account can be detected.
# calendar (read/write) is needed for both directions of sync: reading busy
# blocks and pushing our appointments as events.
#
# ASSUMPTION: we request full `calendar` scope. `calendar.events` would be
# narrower and is probably sufficient -- worth tightening before any real
# security review, since OAuth scopes should always be the minimum that
# works. Left broad for now because the sync service is not written yet and
# narrowing it blind risks a mid-development re-consent cycle. FLAGGED.
REQUIRED_SCOPES = ("https://www.googleapis.com/auth/calendar",)
REQUESTED_SCOPES = ("openid", "email", *REQUIRED_SCOPES)


class GoogleOAuthError(RuntimeError):
    """Any failure during the authorization-code exchange."""


class GoogleOAuthConfigError(GoogleOAuthError):
    """Client id/secret missing -- a deployment problem, not a user problem."""


@dataclass(frozen=True)
class PKCEChallenge:
    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> "PKCEChallenge":
        """RFC 7636 S256 challenge.

        WHY PKCE even though this is a confidential client with a secret:
        it binds the authorization code to the specific request that started
        the flow. If a code leaks (referer header, shared browser history, a
        malicious extension, a misconfigured proxy), it is useless without
        the verifier, which never left our server. OAuth 2.1 makes PKCE
        mandatory for all clients for exactly this reason.
        """
        # 32 random bytes -> 43 chars base64url, comfortably inside the
        # 43..128 range the RFC allows.
        verifier = secrets.token_urlsafe(32)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return cls(verifier=verifier, challenge=challenge)


@dataclass(frozen=True)
class GoogleTokens:
    access_token: str
    expires_at: datetime
    scopes: tuple[str, ...]
    account_email: str
    # None when Google declines to issue one -- see the big warning in
    # exchange_code(). The caller MUST NOT overwrite a stored token with None.
    refresh_token: str | None


def _decode_id_token_payload(id_token: str) -> dict:
    """Read the claims out of an id_token WITHOUT verifying its signature.

    WHY THAT IS SAFE HERE, AND ONLY HERE: this token came back in the body of
    a TLS-authenticated POST that we made directly to Google's token
    endpoint. The transport already proves both origin and integrity, and
    Google's own documentation says verification may be skipped when the
    token is received directly from Google over HTTPS in exactly this way.

    It is NOT safe anywhere else. If an id_token ever arrives from a browser,
    a mobile client, or any other party, it must be verified properly
    (signature against Google's JWKS, plus issuer, audience and expiry
    checks) -- at that point use google.oauth2.id_token.verify_oauth2_token
    rather than this function.
    """
    try:
        payload_segment = id_token.split(".")[1]
        # JWT uses base64url without padding; restore it before decoding.
        padding = "=" * (-len(payload_segment) % 4)
        decoded = base64.urlsafe_b64decode(payload_segment + padding)
        return json.loads(decoded)
    except (IndexError, ValueError) as exc:
        raise GoogleOAuthError("Could not read id_token payload") from exc


class GoogleOAuthClient:
    """Thin, injectable wrapper around Google's OAuth2 endpoints."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.client_id = client_id if client_id is not None else settings.google_client_id
        self.client_secret = (
            client_secret
            if client_secret is not None
            else settings.google_client_secret.get_secret_value()
        )
        self.redirect_uri = redirect_uri or settings.google_oauth_redirect_uri
        self._http = http_client

    def _require_config(self) -> None:
        if not self.client_id or not self.client_secret:
            raise GoogleOAuthConfigError(
                "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not configured"
            )

    def build_authorization_url(self, *, state: str, code_challenge: str) -> str:
        """The URL to send the doctor's browser to."""
        self._require_config()
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(REQUESTED_SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # access_type=offline is what makes Google issue a refresh token
            # at all. Without it we get a one-hour access token and no way to
            # ever sync again -- the single most common Google Calendar
            # integration bug.
            "access_type": "offline",
            # prompt=consent forces the consent screen even on re-auth.
            # WHY force it: Google issues a refresh token only on the FIRST
            # authorization for a given user+client. A doctor reconnecting
            # would otherwise complete the flow successfully and hand us NO
            # refresh token, and if we stored that we would silently lose the
            # ability to sync. Forcing consent makes a token reliably arrive.
            # Cost: the doctor sees the permission screen every reconnect,
            # which is a fair price for not breaking.
            "prompt": "consent",
            # Ask Google to reject rather than silently narrow the grant if
            # it cannot honour the full scope set.
            "include_granted_scopes": "false",
        }
        return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> GoogleTokens:
        """Trade an authorization code for tokens."""
        self._require_config()
        form = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }

        owns_client = self._http is None
        http = self._http or httpx.AsyncClient(timeout=15.0)
        try:
            response = await http.post(GOOGLE_TOKEN_ENDPOINT, data=form)
        except httpx.HTTPError as exc:
            raise GoogleOAuthError(f"Token endpoint unreachable: {exc}") from exc
        finally:
            if owns_client:
                await http.aclose()

        if response.status_code != 200:
            # Google returns a JSON body with `error` and `error_description`.
            # Surface it, but never echo the request -- it contains the
            # client_secret and the code.
            detail = ""
            try:
                body = response.json()
                detail = f"{body.get('error')}: {body.get('error_description')}"
            except ValueError:
                detail = response.text[:200]
            raise GoogleOAuthError(f"Token exchange failed ({response.status_code}) {detail}")

        payload = response.json()

        id_token = payload.get("id_token")
        if not id_token:
            raise GoogleOAuthError("Token response contained no id_token; cannot identify the account")
        claims = _decode_id_token_payload(id_token)
        email = claims.get("email")
        if not email:
            raise GoogleOAuthError("id_token contained no email claim")

        # expires_in is seconds. Convert to an absolute instant immediately --
        # storing a relative duration means every later comparison has to
        # remember when it was issued, and someone eventually forgets.
        expires_in = int(payload.get("expires_in", 3600))
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        granted = tuple(payload.get("scope", "").split()) if payload.get("scope") else ()

        return GoogleTokens(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=expires_at,
            scopes=granted,
            account_email=email,
        )
