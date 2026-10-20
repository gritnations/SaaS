"""Tests for the booking email builders. Pure: no database, no network.

What these tests establish: who receives which email, that customer-supplied text is escaped, that the
manage link is present only where it should be and carries the code after ``#``, that times are shown
in the business's time zone, and that the calendar file is well formed. What they do not establish:
how a real mail client renders the messages.
"""

import dataclasses
from datetime import UTC, datetime

import pytest

from booking.notify import (
    Context,
    NotifyConfigError,
    calendar_file,
    customer_message,
    manage_url,
    owner_message,
    site_url_from_environment,
    when_text,
)

TOKEN = "a" * 48
BASE = Context(
    kind="confirmed",
    business_name="Example Advisory",
    business_slug="example",
    time_zone="Europe/London",
    contact_email="owner@example.com",
    meeting_link="https://meet.example.com/room",
    reference="ZA2JBN2C5B",
    service_name="Introductory call",
    starts_at=datetime(2026, 7, 7, 8, 30, tzinfo=UTC),
    ends_at=datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
    customer_name="Sample <script>alert(1)</script> Customer",
    customer_email="customer@example.com",
    customer_phone="+44 7700 900000",
    customer_note="Bring the <b>figures</b> & notes",
    cancelled_by=None,
    sequence=0,
    manage_url=manage_url("https://book.example.com", "example", "ZA2JBN2C5B", TOKEN),
)


def test_manage_link_carries_the_code_after_the_hash() -> None:
    """The code is in the fragment, which browsers never send to a server."""
    assert BASE.manage_url == f"https://book.example.com/#manage=example.ZA2JBN2C5B.{TOKEN}"


def test_times_are_shown_in_the_business_time_zone() -> None:
    """08:30 UTC in July is 09:30 in London."""
    assert when_text(BASE) == "Tue 7 Jul 2026, 09:30 to 10:00 (UK time)"


def test_customer_confirmation_has_details_link_and_calendar_file() -> None:
    """The confirmation goes to the customer, replies go to the business, and the link and file are included."""
    message = customer_message(BASE, "mail.example.com")
    assert message.to == "customer@example.com"
    assert message.reply_to == "owner@example.com"
    assert "ZA2JBN2C5B" in message.text
    assert TOKEN in message.html and TOKEN in message.text
    assert "https://meet.example.com/room" in message.text
    assert message.attachments[0].filename == "booking-ZA2JBN2C5B.ics"


def test_customer_text_is_escaped_in_html() -> None:
    """Markup typed by a customer is shown as text, never run."""
    for message in (customer_message(BASE), owner_message(BASE)):
        assert message is not None
        assert "<script>" not in message.html
        assert "&lt;script&gt;" in message.html
    owner = owner_message(BASE)
    assert owner is not None
    assert "<b>figures</b>" not in owner.html


def test_owner_gets_the_customer_details_and_can_reply_to_the_customer() -> None:
    """The business email names the customer and replies go to them; it carries no manage link."""
    message = owner_message(BASE)
    assert message is not None
    assert message.to == "owner@example.com"
    assert message.reply_to == "customer@example.com"
    assert "customer@example.com" in message.text and "+44 7700 900000" in message.text
    assert TOKEN not in message.html and TOKEN not in message.text


def test_no_owner_email_without_a_contact_address_or_when_the_owner_cancelled() -> None:
    """Without a contact email, or for the owner's own cancellation, the business is not emailed."""
    assert owner_message(dataclasses.replace(BASE, contact_email=None)) is None
    assert owner_message(dataclasses.replace(BASE, kind="cancelled", cancelled_by="owner")) is None
    assert owner_message(dataclasses.replace(BASE, kind="cancelled", cancelled_by="customer")) is not None


def test_cancellation_email_has_no_manage_link_and_says_who_cancelled() -> None:
    """A cancelled booking cannot be managed, so no link; the wording follows who cancelled."""
    by_owner = customer_message(dataclasses.replace(BASE, kind="cancelled", cancelled_by="owner"))
    assert TOKEN not in by_owner.html
    assert "by the business" in by_owner.text
    assert "as you asked" in customer_message(dataclasses.replace(BASE, kind="cancelled", cancelled_by="customer")).text


def test_calendar_file_is_well_formed_and_escaped() -> None:
    """CRLF lines, UTC times, a stable identifier, and escaped commas in the summary."""
    data = calendar_file(dataclasses.replace(BASE, business_name="Smith, Jones; Partners")).decode()
    lines = data.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR" and "END:VCALENDAR" in lines
    assert "DTSTART:20260707T083000Z" in lines
    assert "UID:ZA2JBN2C5B@example.com" in lines
    assert any(line.startswith("SUMMARY:") and "Smith\\, Jones\\; Partners" in line for line in lines)


def test_a_moved_booking_updates_the_same_calendar_event() -> None:
    """The identifier stays the same and the sequence rises, so calendars update rather than duplicate."""
    data = calendar_file(dataclasses.replace(BASE, kind="moved", sequence=2)).decode()
    assert "UID:ZA2JBN2C5B@example.com" in data and "SEQUENCE:2" in data
    cancel = calendar_file(dataclasses.replace(BASE, kind="cancelled", sequence=2)).decode()
    assert "METHOD:CANCEL" in cancel and "STATUS:CANCELLED" in cancel


@pytest.mark.parametrize("url", ["http://book.example.com", "book.example.com", "https://", "javascript:alert(1)"])
def test_an_unsafe_site_url_is_refused(url: str) -> None:
    """Manage links only point at https sites (or localhost while developing)."""
    with pytest.raises(NotifyConfigError):
        site_url_from_environment({"PUBLIC_SITE_URL": url})


def test_site_url_accepts_https_and_localhost() -> None:
    """A trailing slash is trimmed."""
    assert (
        site_url_from_environment({"PUBLIC_SITE_URL": "https://preview.example.com/"}) == "https://preview.example.com"
    )
    assert site_url_from_environment({"PUBLIC_SITE_URL": "http://localhost:5500"}) == "http://localhost:5500"
    assert site_url_from_environment({}) is None


def test_the_calendar_identifier_uses_the_sending_domain() -> None:
    """The event identifier follows the configured sender, not a fixed domain."""
    message = customer_message(BASE, "mail.example.com")
    assert b"UID:ZA2JBN2C5B@mail.example.com" in message.attachments[0].content
