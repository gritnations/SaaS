"""Email boundary for the platform: the only module that talks to the email provider.

The provider is Resend, reached over HTTPS with the standard library, so no extra dependency is needed.
The API key is read from the environment, sent only in the ``Authorization`` header, and never appears in
an exception, a log record or a return value.

Environment variables read:
    * ``RESEND_API_KEY``: the provider key. A secret. When absent, no email is sent.
    * ``EMAIL_FROM``: the sender, such as ``Bookings <bookings@mail.example.com>``.

Constraints:
    * Importing this module and constructing a mailer perform no network I/O.
"""

import base64
import json
import re
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Annotated, Protocol

from pydantic import AfterValidator, BaseModel, Field, InstanceOf, StringConstraints

from contracts import STRICT

RESEND_URL = "https://api.resend.com/emails"
API_KEY_ENV = "RESEND_API_KEY"
FROM_ENV = "EMAIL_FROM"
TIMEOUT_SECONDS = 8.0
_SENDER_PATTERN = re.compile(r"^[^<>@\r\n]*<[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+>$|^[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+$")


class MailConfigError(RuntimeError):
    """Raised when the email configuration is unusable. The message never contains the key."""


class MailError(Exception):
    """Raised when an email could not be sent. The message is a short status, never message content."""


MAX_ATTACHMENT_BYTES = 1_000_000
"""The largest file the platform attaches. Booking calendar files are a few hundred bytes."""


def has_controls(value: str) -> bool:
    """Check text for control characters, such as a line break, which must never reach a mail header.

    Args:
        value: The text.

    Returns:
        ``True`` when the text contains one.
    """
    return any(unicodedata.category(char) == "Cc" for char in value)


def _header_text(value: str) -> str:
    """Refuse header text that contains a control character.

    Args:
        value: The text.

    Returns:
        The text, unchanged.

    Raises:
        ValueError: If it contains a control character.
    """
    if has_controls(value):
        raise ValueError("header text must not contain control characters")
    return value


Address = Annotated[str, StringConstraints(pattern=r"^[^@\s]+@[^@\s]+$", max_length=254)]
"""An email address as a recipient or reply-to: one ``@`` and no spaces or line breaks."""

Subject = Annotated[str, StringConstraints(min_length=1, max_length=300), AfterValidator(_header_text)]
"""A subject line: 1 to 300 characters, no control characters."""


class Attachment(BaseModel):
    """A file attached to an email.

    Attributes:
        filename: The file name shown to the recipient.
        content: The file's bytes.
        content_type: Its media type.
    """

    model_config = STRICT

    filename: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,100}$")]
    content: Annotated[bytes, Field(min_length=1, max_length=MAX_ATTACHMENT_BYTES)]
    content_type: Annotated[str, StringConstraints(pattern=r"^[a-z]+/[a-z0-9.+-]+$")]


class Message(BaseModel):
    """One email to one recipient.

    Attributes:
        to: Recipient address.
        subject: Subject line.
        html: HTML body. Every interpolated value must already be escaped.
        text: Plain-text body.
        reply_to: Where replies go, if not the sender.
        attachments: Files to attach.
    """

    model_config = STRICT

    to: Address
    subject: Subject
    html: Annotated[str, StringConstraints(min_length=1)]
    text: Annotated[str, StringConstraints(min_length=1)]
    reply_to: Address | None = None
    attachments: tuple[InstanceOf[Attachment], ...] = ()


class Mailer(Protocol):
    """What the platform needs from an email provider. Tests substitute a fake."""

    def send(self, message: Message) -> str:
        """Send one email.

        Args:
            message: The email.

        Returns:
            The provider's message identifier.
        """
        ...


class ResendMailer:
    """Sends email through Resend's HTTP API."""

    def __init__(self, api_key: str, sender: str, timeout_seconds: float = TIMEOUT_SECONDS) -> None:
        """Create the mailer. Performs no I/O.

        Args:
            api_key: The provider key.
            sender: The ``From`` address.
            timeout_seconds: Network timeout per request.
        """
        self._api_key = api_key
        self._sender = sender
        self._timeout = timeout_seconds

    def payload(self, message: Message) -> dict[str, object]:
        """Build the request body for a message.

        Args:
            message: The email.

        Returns:
            The JSON-ready body.
        """
        body: dict[str, object] = {
            "from": self._sender,
            "to": [message.to],
            "subject": message.subject,
            "html": message.html,
            "text": message.text,
        }
        if message.reply_to:
            body["reply_to"] = message.reply_to
        if message.attachments:
            body["attachments"] = [
                {
                    "filename": item.filename,
                    "content": base64.b64encode(item.content).decode("ascii"),
                    "content_type": item.content_type,
                }
                for item in message.attachments
            ]
        return body

    def send(self, message: Message) -> str:
        """Send one email.

        Args:
            message: The email.

        Returns:
            The provider's message identifier.

        Raises:
            MailError: If the provider refuses the message or cannot be reached. The message names only
                the HTTP status or the error type.
        """
        request = urllib.request.Request(
            RESEND_URL,
            data=json.dumps(self.payload(message)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "platform-mailer/1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 (fixed https URL)
                answer = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise MailError(f"provider refused the message (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise MailError(f"provider unreachable ({type(exc).__name__})") from None
        identifier = answer.get("id") if isinstance(answer, dict) else None
        return str(identifier)[:100] if identifier else ""


def sender_domain(environ: Mapping[str, str]) -> str:
    """Read the domain of the ``EMAIL_FROM`` address.

    Args:
        environ: The mapping to read.

    Returns:
        The domain in lower case, or ``example.com`` when no address is configured.
    """
    match = re.search(r"@([A-Za-z0-9.-]+)>?\s*$", environ.get(FROM_ENV, "").strip())
    return match.group(1).lower() if match else "example.com"


def mailer_from_environment(environ: Mapping[str, str]) -> Mailer | None:
    """Build the mailer from the environment, or report that email is not configured.

    Args:
        environ: The mapping to read.

    Returns:
        The mailer, or ``None`` when ``RESEND_API_KEY`` is absent.

    Raises:
        MailConfigError: If the key is set but ``EMAIL_FROM`` is missing or not an address.
    """
    key = environ.get(API_KEY_ENV, "").strip()
    if not key:
        return None
    sender = environ.get(FROM_ENV, "").strip()
    if not _SENDER_PATTERN.match(sender):
        raise MailConfigError(f"{FROM_ENV} must be an address such as Bookings <bookings@mail.example.com>.")
    return ResendMailer(key, sender)
