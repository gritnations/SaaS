"""Tests for the administration records and the owner text rules.

What these tests establish: the administration records (memberships, sign-up details, owner bookings, settings,
settings changes and payment settings) refuse values of the wrong type, unknown fields and values outside the
database's limits; the plan status and role lists equal the database's checks in ``migrations/``; owner-entered
text follows the text rules, at the API and in the input records alike, and refused text answers 422 with nothing
saved; records loaded from a row keep stored text as it is. They do not establish anything against a real
database: that is covered by the ``*_postgres.py`` tests when a test database is configured.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError
from test_owner_api import GOOD, FakeOwners, FakeVerifier, client_for

from booking.owner import Membership, NewBusiness, OwnerBooking, Settings, SettingsChanges
from booking.payments import PaymentSettings
from contracts import PlanStatus, Role, TextRefusedError, clean_business_name, from_row

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

ARABIC_NOOR = "".join(chr(c) for c in (0x0646, 0x0648, 0x0631))
ARABIC_SALON = "".join(chr(c) for c in (0x0635, 0x0627, 0x0644, 0x0648, 0x0646))
CYRILLIC_A = chr(0x0410)
FULL_WIDTH_S = chr(0xFF33)
GREEK_ALPHA = chr(0x03B1)
RIGHT_TO_LEFT_OVERRIDE = chr(0x202E)
ZERO_WIDTH_SPACE = chr(0x200B)

WHEN = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)

MEMBERSHIP: dict[str, Any] = {"slug": "example", "name": "Example Advisory", "role": "owner"}

NEW_BUSINESS: dict[str, Any] = {
    "name": "Fade & Co Barbers",
    "business_type": "barbershop",
    "owner_name": "Sample Owner",
    "contact_email": "owner@example.com",
}

BOOKING: dict[str, Any] = {
    "reference": "ZA2JBN2C5B",
    "status": "confirmed",
    "service_slug": "intro",
    "service_name": "Introductory call",
    "starts_at": WHEN,
    "ends_at": WHEN,
    "price_minor": 0,
    "customer_name": "Sample Customer",
    "customer_email": "customer@example.com",
    "customer_phone": None,
    "customer_note": None,
    "created_at": WHEN,
}

SETTINGS: dict[str, Any] = {
    "name": "Example Advisory",
    "time_zone": "Europe/London",
    "currency": "GBP",
    "break_minutes": 0,
    "notice_minutes": 240,
    "window_days": 30,
}

SIGNUP = {"name": "Fade & Co Barbers", "business_type": "barbershop", "owner_name": "Sample Owner", "plan": "free"}


def check_values(text: str) -> set[str]:
    """Read the quoted values out of a ``CHECK (column IN (...))`` clause.

    Args:
        text: The clause's list of values.

    Returns:
        The values.
    """
    return set(re.findall(r"'([^']*)'", text))


def latest_check(pattern: str) -> set[str]:
    """Find the last match of a check pattern across the migrations, in file order.

    Args:
        pattern: A regular expression whose first group is the check's list of values.

    Returns:
        The allowed values.
    """
    found: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        found += re.findall(pattern, path.read_text(encoding="utf-8"))
    assert found, f"no match for {pattern} in migrations"
    return check_values(found[-1])


def signed_in(owners: FakeOwners) -> Any:
    """Build a signed-in client around a fake owner service.

    Args:
        owners: The fake owner service.

    Returns:
        The client.
    """
    return client_for(owners, FakeVerifier())


def test_the_plan_status_list_equals_the_database_check() -> None:
    """A plan status added to the database without the code (or the reverse) fails here, in CI."""
    found = latest_check(r"ADD CONSTRAINT businesses_plan_status_check\s+CHECK \(plan_status IN \(([^)]*)\)\)")
    assert set(get_args(PlanStatus)) == found


def test_the_role_list_equals_the_database_check() -> None:
    """A role added to the database without the code (or the reverse) fails here, in CI."""
    assert set(get_args(Role)) == latest_check(r"\brole\s+text NOT NULL CHECK \(role IN \(([^)]*)\)\)")


def test_valid_administration_records_are_accepted() -> None:
    """The values the platform stores and accepts today pass."""
    Membership(**MEMBERSHIP)
    Membership(
        **MEMBERSHIP,
        display_name="Sample Owner",
        plan="pro",
        plan_status="past_due",
        trial_ends_at=WHEN,
        online_payments=True,
        plan_ends_at=WHEN,
        time_zone="Asia/Riyadh",
    )
    NewBusiness(**NEW_BUSINESS)
    NewBusiness(**{**NEW_BUSINESS, "business_type": "other", "business_type_other": "Dog grooming", "region": "sa"})
    OwnerBooking(**BOOKING)
    OwnerBooking(**{**BOOKING, "customer_phone": "+44 7700 900000", "customer_note": "Hi", "payment_status": "paid"})
    Settings(**SETTINGS)
    Settings(
        **SETTINGS,
        contact_email="owner@example.com",
        meeting_link="https://meet.example.com/x",
        public_address="1 High Street, London",
        public_phone="020 7946 0000",
        public_email="hello@example.com",
    )
    SettingsChanges()
    PaymentSettings(provider="stripe", connected=True, enabled=False)


@pytest.mark.parametrize(
    "change",
    [
        {"slug": "ab"},
        {"role": "admin"},
        {"plan": "enterprise"},
        {"plan_status": "awaiting_payment"},
        {"trial_ends_at": datetime(2026, 12, 1)},
        {"online_payments": 1},
        {"time_zone": ""},
        {"extra": True},
    ],
)
def test_a_membership_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Web name, role, plan and plan status from the shared lists, aware times, no conversion or extra fields."""
    with pytest.raises(ValidationError):
        Membership(**{**MEMBERSHIP, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"reference": "za2jbn2c5b"},
        {"status": "done"},
        {"payment_status": "card"},
        {"starts_at": datetime(2026, 11, 20, 9, 0)},
        {"price_minor": -1},
        {"customer_name": ""},
        {"customer_email": "x"},
        {"customer_note": "x" * 1001},
    ],
)
def test_an_owner_booking_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Reference shape, the status lists, aware times and the database's limits on the customer's details."""
    with pytest.raises(ValidationError):
        OwnerBooking(**{**BOOKING, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"currency": "gbp"},
        {"break_minutes": 241},
        {"window_days": 0},
        {"cancel_cutoff_minutes": 43_201},
        {"meeting_link": "http://meet.example.com"},
        {"public_address": "ab"},
        {"public_phone": "123"},
        {"window_days": "30"},
    ],
)
def test_settings_refuse_wrong_values(change: dict[str, Any]) -> None:
    """Settings within the database's limits, with no conversion."""
    with pytest.raises(ValidationError):
        Settings(**{**SETTINGS, **change})


@pytest.mark.parametrize(
    "fields",
    [
        {"provider": "other", "connected": True, "enabled": True},
        {"provider": "stripe", "connected": 1, "enabled": True},
    ],
)
def test_payment_settings_refuse_wrong_values(fields: dict[str, Any]) -> None:
    """A known provider and real true or false flags."""
    with pytest.raises(ValidationError):
        PaymentSettings(**fields)


@pytest.mark.parametrize(
    "change",
    [
        {"business_type": "bakery"},
        {"region": "mars"},
        {"plan": "enterprise"},
        {"name": ""},
        {"owner_name": "x" * 101},
        {"business_type_other": "x" * 61},
        {"contact_email": "not-an-email"},
    ],
)
def test_sign_up_details_refuse_wrong_values(change: dict[str, Any]) -> None:
    """Listed business types and regions, a known plan, and the owner text rules."""
    with pytest.raises(ValidationError):
        NewBusiness(**{**NEW_BUSINESS, **change})


def test_a_bilingual_business_name_is_accepted_and_a_mixed_word_is_refused() -> None:
    """Each word is in one script; different words may use different scripts."""
    bilingual = f"Noor Salon {ARABIC_SALON} {ARABIC_NOOR}"
    assert clean_business_name(bilingual) == bilingual
    for name in (f"{CYRILLIC_A}pple Store", f"Sal{GREEK_ALPHA}n"):
        with pytest.raises(TextRefusedError):
            clean_business_name(name)


def test_owner_text_is_normalised_before_it_is_used() -> None:
    """Full-width letters become ordinary ones and zero-width characters are removed."""
    new = NewBusiness(
        **{
            **NEW_BUSINESS,
            "name": f"{FULL_WIDTH_S}alon{ZERO_WIDTH_SPACE} One",
            "owner_name": f" Sam{ZERO_WIDTH_SPACE} ",
        }
    )
    assert (new.name, new.owner_name) == ("Salon One", "Sam")


@pytest.mark.parametrize(
    "change",
    [
        {"name": f"Salon{RIGHT_TO_LEFT_OVERRIDE}"},
        {"owner_name": "Sam\x00"},
        {"business_type_other": f"Dog{RIGHT_TO_LEFT_OVERRIDE}"},
        {"contact_email": f"{CYRILLIC_A}dmin@example.com"},
        {"contact_email": f"owner{ZERO_WIDTH_SPACE}@example.com"},
    ],
)
def test_sign_up_text_with_hidden_or_look_alike_characters_is_refused(change: dict[str, Any]) -> None:
    """Direction and control characters are refused in names; an email may not mix scripts or hide characters."""
    with pytest.raises(ValidationError):
        NewBusiness(**{**NEW_BUSINESS, **change})


def test_an_owner_name_may_mix_scripts() -> None:
    """Names written in two scripts are real, as for customers."""
    assert NewBusiness(**{**NEW_BUSINESS, "owner_name": f"Sam {ARABIC_NOOR}"}).owner_name == f"Sam {ARABIC_NOOR}"


def test_settings_changes_clear_only_what_may_be_cleared() -> None:
    """``None`` clears the contact fields; other settings cannot be cleared; fields left out are not changed."""
    changes = SettingsChanges(window_days=14, contact_email=None)
    assert changes.values() == {"window_days": 14, "contact_email": None}
    for field in ("name", "window_days", "cancel_cutoff_minutes"):
        with pytest.raises(ValidationError):
            SettingsChanges(**{field: None})


@pytest.mark.parametrize(
    "change",
    [
        {"name": f"{CYRILLIC_A}pple"},
        {"public_address": f"1 High St{RIGHT_TO_LEFT_OVERRIDE}"},
        {"public_address": f"a{ZERO_WIDTH_SPACE}b"},
        {"public_email": f"hello@ex{CYRILLIC_A}mple.com"},
        {"meeting_link": "https://a b.com/xyz"},
        {"public_phone": "123"},
    ],
)
def test_settings_changes_refuse_owner_text_that_breaks_the_rules(change: dict[str, Any]) -> None:
    """Business name, address and emails follow the text rules; links and phones keep their patterns."""
    with pytest.raises(ValidationError):
        SettingsChanges(**change)


def test_stored_text_is_loaded_as_it_is() -> None:
    """Rows saved before the text rules load unchanged, so an old value cannot turn a page into a server error."""
    old = f"Old{ZERO_WIDTH_SPACE} Name{RIGHT_TO_LEFT_OVERRIDE}"
    settings = from_row(Settings, (*{**SETTINGS, "name": old}.values(), 1440, 1440, None, None, f"{old} Rd"))
    membership = Membership(**{**MEMBERSHIP, "name": old, "display_name": old})
    booking = from_row(OwnerBooking, tuple({**BOOKING, "customer_name": old}.values()))
    assert settings.name == old and settings.public_address == f"{old} Rd"
    assert membership.name == old and membership.display_name == old
    assert booking.customer_name == old


@pytest.mark.parametrize(
    "body",
    [
        {**SIGNUP, "name": f"{CYRILLIC_A}pple Barbers"},
        {**SIGNUP, "owner_name": f"Sam{RIGHT_TO_LEFT_OVERRIDE}"},
        {**SIGNUP, "contact_email": f"{CYRILLIC_A}dmin@example.com"},
        {**SIGNUP, "business_type": "other", "business_type_other": f"Dogs{RIGHT_TO_LEFT_OVERRIDE}"},
    ],
)
def test_refused_sign_up_text_answers_422_and_creates_nothing(body: dict[str, Any]) -> None:
    """The API refuses what the sign-up record refuses, before the service is called."""
    owners = FakeOwners()
    response = signed_in(owners).post("/owner/businesses", headers=GOOD, json=body)
    assert response.status_code == 422
    assert owners.calls == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/owner/businesses/example/settings", {"name": f"Sal{GREEK_ALPHA}n"}),
        ("patch", "/owner/businesses/example/settings", {"public_address": f"1 High St{RIGHT_TO_LEFT_OVERRIDE}"}),
        ("patch", "/owner/businesses/example/settings", {"contact_email": f"{CYRILLIC_A}dmin@example.com"}),
        ("patch", "/owner/businesses/example/settings", {"public_email": f"hi{ZERO_WIDTH_SPACE}@example.com"}),
        (
            "post",
            "/owner/businesses/example/services",
            {"slug": "x1", "name": f"Cut{RIGHT_TO_LEFT_OVERRIDE}", "duration_minutes": 30},
        ),
        ("patch", "/owner/businesses/example/services/intro", {"name": "Cut\x07"}),
        (
            "post",
            "/owner/businesses/example/time-off",
            {"first_day": "2026-12-24", "note": f"Closed{RIGHT_TO_LEFT_OVERRIDE}"},
        ),
    ],
)
def test_refused_owner_text_answers_422_and_saves_nothing(method: str, path: str, body: dict[str, Any]) -> None:
    """Settings, services and days off refuse hidden or look-alike characters before the service is called."""
    owners = FakeOwners()
    response = signed_in(owners).request(method.upper(), path, headers=GOOD, json=body)
    assert response.status_code == 422
    assert owners.calls == []


def test_accepted_owner_text_reaches_the_service_normalised() -> None:
    """New owner text is normalised before it is saved, and an empty string still clears a contact field."""
    owners = FakeOwners()
    response = signed_in(owners).patch(
        "/owner/businesses/example/settings",
        headers=GOOD,
        json={"name": f"{FULL_WIDTH_S}alon{ZERO_WIDTH_SPACE} One", "public_address": "", "public_email": ""},
    )
    assert response.status_code == 200
    assert owners.calls[-1][1][2].values() == {"name": "Salon One", "public_address": None, "public_email": None}
