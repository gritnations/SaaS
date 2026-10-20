"""Tests for the email boundary. No network: the HTTP call is replaced.

What these tests establish: configuration is validated, the request carries the expected body and the
key only in its header, attachments are base64-encoded, and failures never expose the key or the body.
What they do not establish: delivery through the real provider.
"""

import base64
import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from mail import Attachment, MailConfigError, MailError, Message, ResendMailer, mailer_from_environment, sender_domain

KEY = "re_test_secret_key_value_123"
MESSAGE = Message(
    to="customer@example.com",
    subject="Booking confirmed",
    html="<p>Hi</p>",
    text="Hi",
    reply_to="owner@example.com",
    attachments=[Attachment("booking.ics", b"BEGIN:VCALENDAR", "text/calendar")],
)


class FakeResponse(io.BytesIO):
    """A minimal stand-in for an HTTP response that works as a context manager."""

    def __enter__(self) -> "FakeResponse":
        """Enter the context.

        Returns:
            Itself.
        """
        return self

    def __exit__(self, *args: object) -> None:
        """Leave the context.

        Args:
            *args: Ignored.
        """


def test_no_key_means_no_mailer() -> None:
    """Without a key, email is off rather than broken."""
    assert mailer_from_environment({}) is None


@pytest.mark.parametrize("sender", ["", "bookings", "Bookings <not-an-address>", "a@b"])
def test_a_key_without_a_valid_sender_is_refused(sender: str) -> None:
    """A key needs a real From address, and the error never shows the key."""
    with pytest.raises(MailConfigError) as caught:
        mailer_from_environment({"RESEND_API_KEY": KEY, "EMAIL_FROM": sender})
    assert KEY not in str(caught.value)


def test_the_request_carries_the_message_and_the_key_only_in_its_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body fields, reply-to and attachment are sent; the key is in Authorization, not the body."""
    seen: dict[str, Any] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
        """Capture the request.

        Args:
            request: The request.
            timeout: The timeout.

        Returns:
            A provider answer.
        """
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data)  # type: ignore[arg-type]
        seen["timeout"] = timeout
        return FakeResponse(b'{"id": "msg_123"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    mailer = mailer_from_environment({"RESEND_API_KEY": KEY, "EMAIL_FROM": "Bookings <bookings@mail.example.com>"})
    assert mailer is not None
    assert mailer.send(MESSAGE) == "msg_123"
    assert seen["url"] == "https://api.resend.com/emails"
    assert seen["auth"] == f"Bearer {KEY}"
    assert KEY not in json.dumps(seen["body"])
    assert seen["body"]["to"] == ["customer@example.com"]
    assert seen["body"]["reply_to"] == "owner@example.com"
    assert base64.b64decode(seen["body"]["attachments"][0]["content"]) == b"BEGIN:VCALENDAR"
    assert seen["timeout"] > 0


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError("https://api.resend.com/emails", 422, "Unprocessable", {}, io.BytesIO(b"secret body")),  # type: ignore[arg-type]
        urllib.error.URLError("no route"),
        TimeoutError(),
    ],
)
def test_failures_become_a_short_status_without_secrets(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    """Refusals and network errors raise MailError naming only the status or error type."""

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
        """Fail.

        Args:
            request: The request.
            timeout: The timeout.

        Raises:
            Exception: The chosen failure.
        """
        raise failure

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(MailError) as caught:
        ResendMailer(KEY, "bookings@mail.example.com").send(MESSAGE)
    assert KEY not in str(caught.value)
    assert "secret body" not in str(caught.value)
    assert "customer@example.com" not in str(caught.value)


@pytest.mark.parametrize(
    ("value", "domain"),
    [
        ("Bookings <bookings@Mail.Example.com>", "mail.example.com"),
        ("bookings@example.org", "example.org"),
        ("", "example.com"),
    ],
)
def test_sender_domain_is_read_from_the_from_address(value: str, domain: str) -> None:
    """The sending domain comes from EMAIL_FROM, lower-cased, with a neutral fallback."""
    assert sender_domain({"EMAIL_FROM": value}) == domain
