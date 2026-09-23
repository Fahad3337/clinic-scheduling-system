"""SMS and email senders.

WHY httpx against the REST APIs instead of the twilio / sendgrid SDKs:
both official SDKs are synchronous. Calling one from an async worker either
blocks the event loop or needs a thread pool we otherwise do not want, and
neither offers an async client. The surface we need is one POST each.

Both senders return the PROVIDER'S message id. That id is the only handle
for answering "did this actually go out?" later -- it is what turns an
UNRESOLVED notification from a permanent mystery into something a
reconciliation job can settle.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from app.core.config import get_settings

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
SENDGRID_SEND_ENDPOINT = "https://api.sendgrid.com/v3/mail/send"


class MessageSendError(RuntimeError):
    """Delivery failed. `permanent` decides retry vs give up."""

    def __init__(self, message: str, *, permanent: bool = False, status_code: int | None = None) -> None:
        self.permanent = permanent
        self.status_code = status_code
        super().__init__(message)


@runtime_checkable
class MessageSender(Protocol):
    """What the notification service needs from any channel.

    A Protocol rather than a base class so tests can pass a plain object
    with a `send` method, and so a future WhatsApp or voice channel needs
    no inheritance from our code.
    """

    async def send(self, *, recipient: str, body: str, subject: str | None = None) -> str:
        """Deliver, returning the provider's message id."""
        ...


class TwilioSmsSender:
    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client

    async def send(self, *, recipient: str, body: str, subject: str | None = None) -> str:
        settings = get_settings()
        sid = settings.twilio_account_sid
        token = settings.twilio_auth_token.get_secret_value()
        if not sid or not token or not settings.twilio_from_number:
            raise MessageSendError("Twilio is not configured", permanent=True)

        owns = self._http is None
        http = self._http or httpx.AsyncClient(timeout=20.0)
        try:
            response = await http.post(
                f"{TWILIO_API_BASE}/Accounts/{sid}/Messages.json",
                data={"From": settings.twilio_from_number, "To": recipient, "Body": body},
                auth=(sid, token),
            )
        except httpx.HTTPError as exc:
            raise MessageSendError(f"network error: {exc}") from exc
        finally:
            if owns:
                await http.aclose()

        if response.status_code in (200, 201):
            return response.json().get("sid", "")

        # 4xx from Twilio is almost always a bad number or a blocked
        # recipient -- retrying an unreachable number three times just
        # wastes attempts and delays giving up. 429 is the exception.
        permanent = 400 <= response.status_code < 500 and response.status_code != 429
        raise MessageSendError(
            f"twilio {response.status_code}: {response.text[:200]}",
            permanent=permanent,
            status_code=response.status_code,
        )


class SendGridEmailSender:
    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client

    async def send(self, *, recipient: str, body: str, subject: str | None = None) -> str:
        settings = get_settings()
        api_key = settings.sendgrid_api_key.get_secret_value()
        if not api_key:
            raise MessageSendError("SendGrid is not configured", permanent=True)

        owns = self._http is None
        http = self._http or httpx.AsyncClient(timeout=20.0)
        try:
            response = await http.post(
                SENDGRID_SEND_ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "personalizations": [{"to": [{"email": recipient}]}],
                    "from": {"email": settings.sendgrid_from_email},
                    "subject": subject or "Appointment update",
                    "content": [{"type": "text/plain", "value": body}],
                },
            )
        except httpx.HTTPError as exc:
            raise MessageSendError(f"network error: {exc}") from exc
        finally:
            if owns:
                await http.aclose()

        # SendGrid returns 202 Accepted with an EMPTY body; the id is in a
        # header. Code that parses the body for an id gets nothing and
        # looks like a failure.
        if response.status_code == 202:
            return response.headers.get("X-Message-Id", "")

        permanent = 400 <= response.status_code < 500 and response.status_code != 429
        raise MessageSendError(
            f"sendgrid {response.status_code}: {response.text[:200]}",
            permanent=permanent,
            status_code=response.status_code,
        )
