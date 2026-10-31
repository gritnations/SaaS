"""Tests for creating a business at sign-up against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied.
Each test uses fresh account identifiers and deletes every business it created.

What these tests establish: a new business gets its defaults and a unique slug, an account can have only
one business, the owner sees only their own business, customers can book the new business at once, and a
business outside the UK gets its own time zone and currency, with its opening hours read in that zone.
"""

import os
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking.owner import ConflictError, InvalidChangeError, NewBusiness, OwnerService, slugify
from booking.service import BookingRequest, BookingService, NotFoundError
from postgres import Database, DatabaseSettings

URL_ENV = "BOOKING_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")


@pytest.fixture
def database() -> Iterator[Database]:
    """Open the database and remove every business created by the test afterwards.

    Yields:
        The database boundary. Created slugs start with ``signup-test-``.
    """
    db = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    try:
        yield db
    finally:
        with db.connection() as connection:
            ids = [
                str(row[0])
                for row in connection.execute("SELECT id FROM businesses WHERE slug LIKE 'signup-test-%'").fetchall()
            ]
            for table in ("email_log", "bookings", "business_members", "weekly_hours", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = ANY(%s::uuid[])", (ids,))
            connection.execute("DELETE FROM businesses WHERE id = ANY(%s::uuid[])", (ids,))
        db.close()


def new(name: str, business_type: str = "barbershop", other: str | None = None, region: str = "uk") -> NewBusiness:
    """Build sign-up details.

    Args:
        name: Company name.
        business_type: Type.
        other: Description for ``other``.
        region: Where the business is.

    Returns:
        The details.
    """
    return NewBusiness(name, business_type, other, "Sample Owner", "free", "owner@example.com", region)


def test_a_new_business_gets_its_defaults(database: Database) -> None:
    """Slug from the name, owner membership with name, one free service and weekday hours."""
    owners = OwnerService(database)
    account = str(uuid.uuid4())
    tag = secrets.token_hex(3)
    made = owners.create_business(account, new(f"Signup Test {tag} Barbers"))
    assert made.slug == f"signup-test-{tag}-barbers"
    assert [(m.slug, m.display_name) for m in owners.memberships(account)] == [(made.slug, "Sample Owner")]
    services = owners.services(account, made.slug)
    assert [(s.slug, s.duration_minutes, s.price_minor) for s in services] == [("intro", 30, 0)]
    hours = owners.hours(account, made.slug)
    assert sorted({h.weekday for h in hours}) == [1, 2, 3, 4, 5]
    settings = owners.settings(account, made.slug)
    assert (settings.currency, settings.time_zone, settings.contact_email) == (
        "GBP",
        "Europe/London",
        "owner@example.com",
    )


def test_one_business_per_account(database: Database) -> None:
    """A second business for the same account is refused and nothing extra is created."""
    owners = OwnerService(database)
    account = str(uuid.uuid4())
    tag = secrets.token_hex(3)
    owners.create_business(account, new(f"Signup Test {tag} One"))
    with pytest.raises(ConflictError):
        owners.create_business(account, new(f"Signup Test {tag} Two"))
    assert len(owners.memberships(account)) == 1


def test_the_same_name_gets_a_distinct_slug(database: Database) -> None:
    """Two businesses with the same name get different slugs."""
    owners = OwnerService(database)
    name = f"Signup Test {secrets.token_hex(3)} Spa"
    first = owners.create_business(str(uuid.uuid4()), new(name, "spa"))
    second = owners.create_business(str(uuid.uuid4()), new(name, "spa"))
    assert first.slug != second.slug and second.slug == first.slug + "-2"


def test_other_type_needs_a_description(database: Database) -> None:
    """An other business must say what it is; a described one is accepted."""
    owners = OwnerService(database)
    tag = secrets.token_hex(3)
    with pytest.raises(InvalidChangeError):
        owners.create_business(str(uuid.uuid4()), new(f"Signup Test {tag} Mystery", "other"))
    made = owners.create_business(str(uuid.uuid4()), new(f"Signup Test {tag} Kennels", "other", "Dog grooming"))
    assert made.slug.startswith("signup-test-")


def test_owners_are_isolated_and_customers_can_book_at_once(database: Database) -> None:
    """Another owner cannot reach the new business; a customer can book its free service straight away."""
    owners = OwnerService(database)
    account = str(uuid.uuid4())
    made = owners.create_business(account, new(f"Signup Test {secrets.token_hex(3)} Clinic", "clinic"))
    with pytest.raises(NotFoundError):
        owners.settings(str(uuid.uuid4()), made.slug)
    bookings = BookingService(database)
    day = datetime.now(UTC).date() + timedelta(days=7)
    while day.weekday() > 4:
        day += timedelta(days=1)
    confirmed = bookings.create_booking(
        made.slug, BookingRequest("intro", day, "10:00", "Test Customer", "customer@example.com", None, None)
    )
    _, listed = owners.bookings(account, made.slug, day, 1)
    assert [b.reference for b in listed] == [confirmed.reference]


def test_a_business_outside_the_uk_gets_its_own_zone_and_currency(database: Database) -> None:
    """A Saudi business is in Riyadh time and riyals, and a 10:00 booking there is 10:00 Riyadh time."""
    owners = OwnerService(database)
    account = str(uuid.uuid4())
    made = owners.create_business(
        account, new(f"Signup Test {secrets.token_hex(3)} Advisory", "other", "Advisory", "sa")
    )
    assert made.time_zone == "Asia/Riyadh"
    assert [m.time_zone for m in owners.memberships(account)] == ["Asia/Riyadh"]
    settings = owners.settings(account, made.slug)
    assert (settings.time_zone, settings.currency) == ("Asia/Riyadh", "SAR")
    bookings = BookingService(database)
    business, _ = bookings.list_services(made.slug)
    assert (business.time_zone, business.currency) == ("Asia/Riyadh", "SAR")
    riyadh = ZoneInfo("Asia/Riyadh")
    day = datetime.now(riyadh).date() + timedelta(days=7)
    while day.weekday() > 4:
        day += timedelta(days=1)
    bookings.create_booking(
        made.slug, BookingRequest("intro", day, "10:00", "Test Customer", "customer@example.com", None, None)
    )
    _, [listed] = owners.bookings(account, made.slug, day, 1)
    assert listed.starts_at == datetime(day.year, day.month, day.day, 10, 0, tzinfo=riyadh)
    assert listed.starts_at.astimezone(UTC).hour == 7


def test_each_region_sets_its_zone_and_currency_and_an_unknown_one_is_refused(database: Database) -> None:
    """UK, UAE, Saudi Arabia and Qatar each get their own pair; anything else creates nothing."""
    owners = OwnerService(database)
    tag = secrets.token_hex(3)
    expected = {
        "uk": ("Europe/London", "GBP"),
        "ae": ("Asia/Dubai", "AED"),
        "sa": ("Asia/Riyadh", "SAR"),
        "qa": ("Asia/Qatar", "QAR"),
    }
    for region, pair in expected.items():
        account = str(uuid.uuid4())
        made = owners.create_business(account, new(f"Signup Test {tag} {region}", region=region))
        settings = owners.settings(account, made.slug)
        assert (settings.time_zone, settings.currency) == pair
    account = str(uuid.uuid4())
    with pytest.raises(InvalidChangeError):
        owners.create_business(account, new(f"Signup Test {tag} Nowhere", region="mars"))
    assert owners.memberships(account) == []


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Fade & Co Barbers", "fade-co-barbers"),
        ("  ---  ", "business"),
        ("AB", "business"),
        ("Café Ünïque", "caf-n-que"),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    """Names become safe slugs, with a fallback for names that give too little."""
    assert slugify(name) == slug
