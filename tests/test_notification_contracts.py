"""Tests for the notification records: the email context, the email message and its attachment.

What these tests establish: the records refuse values of the wrong type, unknown fields and values outside the
database's limits; the event kind and ``cancelled_by`` lists equal the database's checks in ``migrations/``; a
context at the database's limits loads, and stored text the input rules would now refuse still produces both
emails; a subject line never carries a control character; and a message the record refuses never breaks the
booking change. They do not establish how a mail client shows the emails, or anything about the provider.
"""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from booking.notify import Context, Notifier, customer_message, owner_message, subject_line
from contracts import CancelledBy, EventKind
from mail import MAX_ATTACHMENT_BYTES, Attachment, Message

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

RIGHT_TO_LEFT_OVERRIDE = chr(0x202E)
ZERO_WIDTH_SPACE = chr(0x200B)

WHEN = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)

CONTEXT: dict[str, Any] = {
    "kind": "confirmed",
    "business_name": "Example Advisory",
    "business_slug": "example",
    "time_zone": "Europe/London",
    "contact_email": "owner@example.com",
    "meeting_link": None,
    "reference": "ZA2JBN2C5B",
    "service_name": "Introductory call",
    "starts_at": WHEN,
    "ends_at": WHEN,
    "customer_name": "Sample Customer",
    "customer_email": "customer@example.com",
    "customer_phone": None,
    "customer_note": None,
    "cancelled_by": None,
    "sequence": 0,
    "manage_url": None,
}

MESSAGE: dict[str, Any] = {
    "to": "customer@example.com",
    "subject": "Booking confirmed",
    "html": "<p>Hi</p>",
    "text": "Hi",
}

ATTACHMENT: dict[str, Any] = {
    "filename": "booking-ZA2JBN2C5B.ics",
    "content": b"BEGIN:VCALENDAR",
    "content_type": "text/calendar",
}


def latest_check(pattern: str) -> set[str]:
    """Find the last match of a check pattern across the migrations, in file order, and read its values.

    Args:
        pattern: A regular expression whose first group is the check's list of values.

    Returns:
        The allowed values.
    """
    found: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        found += re.findall(pattern, path.read_text(encoding="utf-8"))
    assert found, f"no match for {pattern} in migrations"
    return set(re.findall(r"'([^']*)'", found[-1]))


class FakeMailer:
    """Records what would be sent."""

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.sent: list[Message] = []

    def send(self, message: Message) -> str:
        """Record a message.

        Args:
            message: The email.

        Returns:
            A provider identifier.
        """
        self.sent.append(message)
        return "id-1"


def test_the_event_kind_list_equals_the_database_check() -> None:
    """An event kind added to the email log without the code (or the reverse) fails here, in CI."""
    assert set(get_args(EventKind)) == latest_check(r"\bkind\s+text NOT NULL CHECK \(kind IN \(([^)]*)\)\)")


def test_the_cancelled_by_list_equals_the_database_check() -> None:
    """Who may cancel matches the database's check on ``bookings.cancelled_by``."""
    found = latest_check(r"cancelled_by IS NULL OR cancelled_by IN \(([^)]*)\)")
    assert set(get_args(CancelledBy)) == found


def test_a_context_at_the_database_limits_loads() -> None:
    """Every value the database accepts loads, so a stored booking always gets its emails."""
    Context(
        **{
            **CONTEXT,
            "business_name": "b" * 120,
            "service_name": "s" * 120,
            "customer_name": "c" * 100,
            "customer_email": "e" * 250 + "@x.y",
            "customer_phone": "0" * 32,
            "customer_note": "n" * 1000,
            "contact_email": "a@b",
            "meeting_link": "https://" + "m" * 492,
            "cancelled_by": "owner",
            "sequence": 10_000,
            "payment_status": "refunded",
            "price_minor": 2_000_000_000,
            "currency": "SAR",
            "manage_url": "http://localhost:8080/manage/#example.ZA2JBN2C5B." + "a" * 48,
        }
    )


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "reminder"},
        {"business_slug": "Example"},
        {"reference": "za2jbn2c5b"},
        {"starts_at": datetime(2026, 12, 1, 9, 0)},
        {"customer_name": ""},
        {"customer_note": "n" * 1001},
        {"cancelled_by": "system"},
        {"sequence": -1},
        {"sequence": "0"},
        {"payment_status": "card"},
        {"price_minor": -1},
        {"currency": "gbp"},
        {"manage_url": "ftp://example.com/x"},
        {"extra": True},
    ],
)
def test_a_context_refuses_wrong_values(change: dict[str, Any]) -> None:
    """The event and status lists, shapes and limits as stored, aware times, no conversion or extra fields."""
    with pytest.raises(ValidationError):
        Context(**{**CONTEXT, **change})


def test_stored_text_the_input_rules_refuse_still_produces_both_emails() -> None:
    """Names saved before the text rules load unchanged and are sent, so no booking loses its emails."""
    old = f"Old{ZERO_WIDTH_SPACE} Name{RIGHT_TO_LEFT_OVERRIDE}"
    context = Context(**{**CONTEXT, "business_name": old, "service_name": old, "customer_name": old})
    owner = owner_message(context)
    assert customer_message(context).to == "customer@example.com"
    assert owner is not None and owner.to == "owner@example.com"


@pytest.mark.parametrize("field", ["business_name", "service_name"])
def test_a_subject_built_from_a_stored_line_break_carries_a_space_instead(field: str) -> None:
    """Control characters in a stored name become spaces in the subject; the email is still built."""
    context = Context(**{**CONTEXT, field: "Line\r\nbreak\tname"})
    owner = owner_message(context)
    assert owner is not None
    for message in (customer_message(context), owner):
        assert "\r" not in message.subject and "\n" not in message.subject and "\t" not in message.subject
    assert "Line  break name" in customer_message(context).subject


def test_subject_line_leaves_clean_text_unchanged() -> None:
    """Only control characters are replaced; other text, including non-Latin letters, stays as it is."""
    text = "Booking confirmed: " + "".join(chr(c) for c in (0x0635, 0x0627, 0x0644, 0x0648, 0x0646))
    assert subject_line(text) == text
    assert subject_line("a\x00b\x7fc") == "a b c"


@pytest.mark.parametrize(
    "change",
    [
        {"to": "customer @example.com"},
        {"to": "customer@example.com\nBcc: x@example.com"},
        {"to": "no-at-sign"},
        {"reply_to": "a\r@example.com"},
        {"subject": ""},
        {"subject": "Booking\nconfirmed"},
        {"subject": "s" * 301},
        {"html": ""},
        {"text": ""},
        {"attachments": [Attachment(**ATTACHMENT)]},
        {"attachments": ({"filename": "x.ics", "content": b"x", "content_type": "text/calendar"},)},
        {"cc": "x@example.com"},
    ],
)
def test_a_message_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Addresses without spaces or line breaks, a clean subject, non-empty bodies and real attachment records."""
    with pytest.raises(ValidationError):
        Message(**{**MESSAGE, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"filename": "../booking.ics"},
        {"filename": ""},
        {"content": b""},
        {"content": b"x" * (MAX_ATTACHMENT_BYTES + 1)},
        {"content": "BEGIN:VCALENDAR"},
        {"content_type": "Text/Calendar"},
    ],
)
def test_an_attachment_refuses_wrong_values(change: dict[str, Any]) -> None:
    """A plain file name, 1 byte to 1 MB of bytes, and a media type."""
    with pytest.raises(ValidationError):
        Attachment(**{**ATTACHMENT, **change})


def test_valid_messages_are_accepted() -> None:
    """A message with a reply-to address and an attachment at the size limit passes."""
    big = Attachment(**{**ATTACHMENT, "content": b"x" * MAX_ATTACHMENT_BYTES})
    Message(**MESSAGE, reply_to="owner@example.com", attachments=(big,))


def test_a_refused_message_never_breaks_the_booking_change(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stored address the message record refuses: nothing is sent, the server log says why, nothing is raised."""
    mailer = FakeMailer()
    notifier = Notifier(database=None, mailer=mailer, site_url=None)  # type: ignore[arg-type]
    context = Context(**{**CONTEXT, "customer_email": "old address@example.com"})
    monkeypatch.setattr(notifier, "_context", lambda kind, booking_id, token: ("business-id", context))
    with caplog.at_level(logging.WARNING, logger="booking.notify"):
        notifier.booking_event("confirmed", "booking-id", None)
    assert mailer.sent == []
    assert [r.getMessage() for r in caplog.records] == ["email message refused (ValidationError)"]
