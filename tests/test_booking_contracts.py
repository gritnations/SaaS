"""Tests for the booking records and the hidden- and look-alike-character rules.

What these tests establish: the booking records refuse values of the wrong type, unknown fields and badly formed
references, codes, links and times; the status lists equal the latest database checks in ``migrations/``; names,
emails and notes are normalised or refused as agreed, and each refusal is logged by field and reason without the
value. They do not establish anything about data already stored, which these rules never change.
"""

import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, get_args
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from booking.availability import Slot
from booking.links import CustomerLinks
from booking.manage import ManagedBooking
from booking.service import BookingRequest, Business, Confirmation, Service
from contracts import (
    BUSINESS_SLUG_PATTERN,
    SERVICE_SLUG_PATTERN,
    BookingStatus,
    PaymentStatus,
    TextRefusedError,
    clean_email,
    clean_name,
    clean_note,
)

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
LONDON = ZoneInfo("Europe/London")
BUSINESS = Business(
    id="internal-business-id",
    slug="example",
    name="Example Advisory",
    time_zone="Europe/London",
    currency="GBP",
    break_minutes=0,
    notice_minutes=240,
    window_days=30,
)
SERVICE = Service(id="internal-service-id", slug="intro", name="Introductory call", duration_minutes=30, price_minor=0)
SLOT = Slot(
    "09:00",
    "9:00 – 9:30 AM",
    datetime(2026, 11, 3, 9, 0, tzinfo=LONDON),
    datetime(2026, 11, 3, 9, 30, tzinfo=LONDON),
    True,
)

REQUEST: dict[str, Any] = {
    "service": "intro",
    "date": date(2026, 11, 3),
    "time": "09:00",
    "name": "Sample Customer",
    "email": "customer@example.com",
    "phone": None,
    "note": None,
}

MANAGED: dict[str, Any] = {
    "reference": "ABCDEFGH23",
    "status": "confirmed",
    "business": BUSINESS,
    "service": SERVICE,
    "starts_at": SLOT.starts_at,
    "ends_at": SLOT.ends_at,
    "can_cancel": True,
    "can_move": True,
    "cancel_deadline": SLOT.starts_at,
    "move_deadline": SLOT.starts_at,
}


def latest_check(constraint: str, column: str) -> set[str]:
    """Read the values in the last ``ADD CONSTRAINT`` with this name across the migrations, in file order.

    Args:
        constraint: The constraint name, such as ``bookings_status_check``.
        column: The column the check lists values for.

    Returns:
        The allowed values.
    """
    pattern = re.compile(rf"ADD CONSTRAINT {constraint}\s+CHECK \({column} IN \(([^)]*)\)\)")
    found: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        found += pattern.findall(path.read_text(encoding="utf-8"))
    assert found, f"no {constraint} found in migrations"
    return set(re.findall(r"'([^']*)'", found[-1]))


def test_the_status_lists_equal_the_database_checks() -> None:
    """A status added to the database without the code (or the reverse) fails here, in CI, without a database."""
    assert set(get_args(BookingStatus)) == latest_check("bookings_status_check", "status")
    assert set(get_args(PaymentStatus)) == latest_check("bookings_payment_status_check", "payment_status")


def test_the_business_slug_rule_equals_the_database_check() -> None:
    """The business and service web-name rules are copied from the database's checks, character for character."""
    first = (MIGRATIONS / "0001_booking.sql").read_text(encoding="utf-8")
    assert re.findall(r"CHECK \(slug ~ '([^']*)'\)", first) == [BUSINESS_SLUG_PATTERN, SERVICE_SLUG_PATTERN]
    assert re.match(BUSINESS_SLUG_PATTERN, "abc") and not re.match(BUSINESS_SLUG_PATTERN, "ab")
    assert re.match(SERVICE_SLUG_PATTERN, "ab")


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("\uff2d\uff41\uff52\uff59 Smith", "Mary Smith"),
        ("Mary\u200b Smith", "Mary Smith"),
        ("  Mary Smith  ", "Mary Smith"),
        ("\u0645\u062d\u0645\u062f \u0639\u0644\u064a", "\u0645\u062d\u0645\u062f \u0639\u0644\u064a"),
        ("Mary \u0645\u062d\u0645\u062f", "Mary \u0645\u062d\u0645\u062f"),
        ("Pat <b>Customer</b>", "Pat <b>Customer</b>"),
    ],
)
def test_names_are_normalised_and_mixed_scripts_are_allowed(typed: str, stored: str) -> None:
    """Full-width letters become ordinary ones, invisible characters go, and two-script names are kept."""
    assert clean_name(typed) == stored


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Mary\u202eSmith", "direction controls"),
        ("Mary\u2066Smith", "direction controls"),
        ("Mary\x00Smith", "control characters"),
        ("Mary\nSmith", "control characters"),
        ("\u200b\u200b", "empty"),
        ("x" * 101, "too long"),
    ],
)
def test_names_with_hidden_characters_are_refused(value: str, reason: str) -> None:
    """Direction overrides and control characters are refused, and a name that is only invisible is empty."""
    with pytest.raises(TextRefusedError, match=reason):
        clean_name(value)


def test_emails_are_normalised_and_single_script_addresses_are_accepted() -> None:
    """A full-width address becomes ordinary, and an address written wholly in Arabic is accepted."""
    assert clean_email("\uff43\uff55\uff53\uff54\uff4f\uff4d\uff45\uff52@example.com") == "customer@example.com"
    arabic = "\u0645\u062d\u0645\u062f@\u0645\u062b\u0627\u0644.\u0627\u0644\u0633\u0639\u0648\u062f\u064a\u0629"
    assert clean_email(arabic) == arabic
    assert clean_email("first.last+tag_1@example.co.uk") == "first.last+tag_1@example.co.uk"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("m\u0430ry@example.com", "mixed scripts"),
        ("mary@\u0435xample.com", "mixed scripts"),
        ("mary\u200b@example.com", "invisible characters"),
        ("mary@example.com\u202e", "invisible characters"),
        ("mary\x00@example.com", "invisible characters"),
        ("not-an-email", "not an email address"),
        ("a" * 250 + "@example.com", "not an email address"),
    ],
)
def test_emails_with_look_alike_or_hidden_characters_are_refused(value: str, reason: str) -> None:
    """A Cyrillic letter in a Latin address, and any invisible or direction character, is refused."""
    with pytest.raises(TextRefusedError, match=reason):
        clean_email(value)


def test_notes_keep_emoji_new_lines_and_formatting() -> None:
    """Notes are not normalised: emoji (including joined ones), new lines and tabs stay as typed."""
    note = "Hi \U0001f468\u200d\U0001f469\u200d\U0001f467\n\tThanks \uff01"
    assert clean_note(note) == note


@pytest.mark.parametrize(
    ("value", "reason"),
    [("hi\x00", "control characters"), ("hi\x1b[31m", "control characters"), ("hi\u202e", "direction controls")],
)
def test_notes_with_hidden_characters_are_refused(value: str, reason: str) -> None:
    """Control characters other than new lines and tabs, and direction controls, are refused in notes."""
    with pytest.raises(TextRefusedError, match=reason):
        clean_note(value)


def test_a_refusal_is_logged_by_field_and_reason_never_by_value(caplog: pytest.LogCaptureFixture) -> None:
    """The log shows which rule real customers hit, without recording what they typed."""
    with caplog.at_level(logging.WARNING, logger="contracts"):
        with pytest.raises(TextRefusedError) as refused:
            clean_email("secret-m\u0430ry@example.com")
    assert [r.getMessage() for r in caplog.records] == ["text refused field=email reason=mixed_scripts"]
    assert "secret" not in caplog.text and "secret" not in str(refused.value)


def test_a_booking_request_accepts_what_the_api_accepts_and_normalises_it() -> None:
    """The internal request applies the same rules as the API, so it never refuses what the API let through."""
    request = BookingRequest(
        **{**REQUEST, "name": "\uff2d\uff41\uff52\uff59", "note": "Hi\n", "pay": "venue", "idempotency_key": "k" * 8}
    )
    assert request.name == "Mary"
    assert BookingRequest(**{**REQUEST, "service": "ab"}).service == "ab"


@pytest.mark.parametrize(
    "change",
    [
        {"date": "2026-11-03"},
        {"date": datetime(2026, 11, 3, tzinfo=UTC)},
        {"time": "9am"},
        {"service": "Bad_Slug"},
        {"name": "Mary\u202e"},
        {"email": "m\u0430ry@example.com"},
        {"phone": "call me"},
        {"note": "x" * 1001},
        {"pay": "card"},
        {"idempotency_key": "short"},
        {"price_minor": 0},
    ],
)
def test_a_booking_request_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Strict: no conversion from text, no unknown fields, and the text rules apply inside as well."""
    with pytest.raises(ValidationError):
        BookingRequest(**{**REQUEST, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"reference": "abcdefgh23"},
        {"service": {"id": "x", "slug": "intro", "name": "x", "duration_minutes": 30, "price_minor": 0}},
        {"slot": None},
        {"status": "done"},
        {"payment_status": "free"},
        {"manage_token": "0" * 47},
        {"checkout_url": "http://checkout.example.com/"},
        {"replayed": "yes"},
    ],
)
def test_a_confirmation_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Only real service and session records, a well-formed reference and code, a secure link and known statuses."""
    with pytest.raises(ValidationError):
        Confirmation(**{"reference": "ABCDEFGH23", "service": SERVICE, "slot": SLOT, **change})


def test_a_replayed_confirmation_accepts_every_stored_status() -> None:
    """A repeat reports the booking as it is now, which may be any status the database allows."""
    for status in get_args(BookingStatus):
        for payment in get_args(PaymentStatus):
            Confirmation(
                reference="ABCDEFGH23", service=SERVICE, slot=SLOT, status=status, payment_status=payment, replayed=True
            )


@pytest.mark.parametrize(
    "change",
    [
        {"starts_at": datetime(2026, 11, 3, 9, 0)},
        {"move_deadline": "2026-11-03T09:00:00Z"},
        {"status": "done"},
        {"payment_status": "free"},
        {"business": {}},
        {"reference": "ABC"},
        {"can_cancel": "true"},
    ],
)
def test_a_managed_booking_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Times need a time zone, statuses come from the shared lists, and nested records are real ones."""
    ManagedBooking(**MANAGED)
    with pytest.raises(ValidationError):
        ManagedBooking(**{**MANAGED, **change})


@pytest.mark.parametrize(
    "base",
    ["http://book.example.com", "https://book.example.com/?x=1", "javascript:alert(1)", "https://book.example.com/#x"],
)
def test_customer_links_refuse_an_insecure_or_unusual_site(base: str) -> None:
    """Links sent to customers start from ``https://``, or ``http://localhost`` in development."""
    CustomerLinks(base="http://localhost:5500")
    with pytest.raises(ValidationError):
        CustomerLinks(base=base)


def test_booking_records_cannot_be_changed_after_they_are_built() -> None:
    """Frozen, as the dataclasses they replace were."""
    request = BookingRequest(**REQUEST)
    with pytest.raises(ValidationError):
        request.name = "Someone else"
