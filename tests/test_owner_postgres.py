"""Tests for the owner service against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration in
``migrations/`` applied. Each test creates two temporary businesses, each with its own owner account,
and deletes everything it created afterwards.

What these tests establish: an owner reaches only their own business (bookings, look-up by reference,
services, hours and settings), an owner's changes to services and hours show on the public booking side
at once, and a search finds, orders and limits bookings as typed, treating wildcard characters as
ordinary text.
"""

import os
import secrets
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from psycopg import Connection

from booking.owner import ConflictError, HoursBlock, OwnerService, OwnerServiceRecord, ServiceChanges, SettingsChanges
from booking.service import BookingRequest, BookingService, NotFoundError
from postgres import Database, DatabaseSettings

URL_ENV = "BOOKING_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")


@dataclass
class Pair:
    """Two temporary businesses with one owner each.

    Attributes:
        owners: The owner service.
        bookings: The public booking service.
        a: Slug of business A.
        b: Slug of business B.
        owner_a: Account that owns A.
        owner_b: Account that owns B.
        ref_b: Reference of a booking at B.
    """

    owners: OwnerService
    bookings: BookingService
    a: str
    b: str
    owner_a: str
    owner_b: str
    ref_b: str


@contextmanager
def pair_database() -> Iterator[Connection[Any]]:
    """Open a connection to the test database for rows a test writes itself.

    Yields:
        The connection; its changes are kept when the block ends without an error.
    """
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    try:
        with database.connection() as connection:
            yield connection
    finally:
        database.close()


@pytest.fixture
def pair() -> Iterator[Pair]:
    """Create businesses A and B, open every day 08:00-20:00, each with a free service and one owner.

    Yields:
        The two businesses, their owners and a booking at B.
    """
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    slugs = ["test-" + secrets.token_hex(6), "test-" + secrets.token_hex(6)]
    accounts = [str(uuid.uuid4()), str(uuid.uuid4())]
    ids: list[str] = []
    with database.connection() as connection:
        for slug, account in zip(slugs, accounts):
            business_id = str(
                connection.execute(
                    "INSERT INTO businesses (slug, name, notice_minutes) VALUES (%s, 'Test', 0) RETURNING id",
                    (slug,),
                ).fetchone()[
                    0
                ]  # type: ignore[index]
            )
            ids.append(business_id)
            connection.execute(
                "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor) "
                "VALUES (%s, 'hour', 'Hour', 60, 0)",
                (business_id,),
            )
            for weekday in range(7):
                connection.execute(
                    "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) "
                    "VALUES (%s, %s, '08:00', '20:00')",
                    (business_id, weekday),
                )
            connection.execute(
                "INSERT INTO business_members (business_id, user_id, role) VALUES (%s, %s, 'owner')",
                (business_id, account),
            )
    bookings = BookingService(database)
    day = datetime.now(UTC).date() + timedelta(days=3)
    confirmed = bookings.create_booking(
        slugs[1],
        BookingRequest(
            service="hour", date=day, time="10:00", name="B Customer", email="b@example.com", phone=None, note=None
        ),
    )
    try:
        yield Pair(OwnerService(database), bookings, slugs[0], slugs[1], accounts[0], accounts[1], confirmed.reference)
    finally:
        with database.connection() as connection:
            for table in ("bookings", "business_members", "weekly_hours", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = ANY(%s::uuid[])", (ids,))
            connection.execute("DELETE FROM businesses WHERE id = ANY(%s::uuid[])", (ids,))
        database.close()


def test_an_owner_sees_only_their_own_businesses(pair: Pair) -> None:
    """Each account lists exactly its own business; an unknown account lists none."""
    assert [m.slug for m in pair.owners.memberships(pair.owner_a)] == [pair.a]
    assert [m.slug for m in pair.owners.memberships(pair.owner_b)] == [pair.b]
    assert pair.owners.memberships(str(uuid.uuid4())) == []


def test_an_owner_cannot_reach_another_business(pair: Pair) -> None:
    """Every owner operation on B by A's owner answers not found, and changes nothing."""
    owner, other = pair.owner_a, pair.b
    attempts: list[Callable[[], object]] = [
        lambda: pair.owners.bookings(owner, other, datetime.now(UTC).date(), 7),
        lambda: pair.owners.booking_by_reference(owner, other, pair.ref_b),
        lambda: pair.owners.search_bookings(owner, other, pair.ref_b),
        lambda: pair.owners.services(owner, other),
        lambda: pair.owners.update_service(owner, other, "hour", ServiceChanges(active=False)),
        lambda: pair.owners.hours(owner, other),
        lambda: pair.owners.replace_hours(owner, other, []),
        lambda: pair.owners.settings(owner, other),
        lambda: pair.owners.update_settings(owner, other, SettingsChanges(window_days=2)),
        lambda: pair.owners.create_service(
            owner,
            other,
            OwnerServiceRecord(slug="xx", name="X", duration_minutes=30, price_minor=0, active=True, sort_order=0),
        ),
    ]
    for attempt in attempts:
        with pytest.raises(NotFoundError):
            attempt()
    assert len(pair.owners.hours(pair.owner_b, other)) == 7
    assert pair.owners.settings(pair.owner_b, other).window_days == 30


def test_a_reference_is_found_only_in_its_own_business(pair: Pair) -> None:
    """B's owner finds the booking with its customer details; A's owner cannot, even with the reference."""
    _, found = pair.owners.booking_by_reference(pair.owner_b, pair.b, pair.ref_b.lower())
    assert (found.reference, found.customer_email) == (pair.ref_b, "b@example.com")
    with pytest.raises(NotFoundError):
        pair.owners.booking_by_reference(pair.owner_a, pair.a, pair.ref_b)


@pytest.mark.parametrize(
    "query",
    [
        "B Customer",
        "custom",
        "b cust*",
        "B@EXAMPLE.COM",
        "@example",
        "hour",
        "list all hours",
        "confirmed upcoming",
        "show all bookings",
    ],
)
def test_a_search_finds_the_booking(pair: Pair, query: str) -> None:
    """Name, start of name, email, service, status and an empty condition each find B's booking."""
    _, result = pair.owners.search_bookings(pair.owner_b, pair.b, query)
    assert [found.reference for found in result.bookings] == [pair.ref_b]
    assert result.more is False


@pytest.mark.parametrize(
    "query",
    [
        "nobody",
        "Customer*",
        "other@example.com",
        "cancelled",
        "past",
        "yesterday",
        "B_Customer",
        "B%",
        "' OR 1=1 --",
    ],
)
def test_a_search_leaves_out_what_does_not_match(pair: Pair, query: str) -> None:
    """Wildcard characters and quotes are ordinary text, and each condition narrows."""
    _, result = pair.owners.search_bookings(pair.owner_b, pair.b, query)
    assert result.bookings == ()


def test_a_search_by_date_and_reference(pair: Pair) -> None:
    """The booking is found on its day and by its reference, and never by the other business."""
    day = datetime.now(UTC).date() + timedelta(days=3)
    for query in (f"{day.day}/{day.month}/{day.year}", pair.ref_b.lower()):
        _, result = pair.owners.search_bookings(pair.owner_b, pair.b, query)
        assert [found.reference for found in result.bookings] == [pair.ref_b]
        _, elsewhere = pair.owners.search_bookings(pair.owner_a, pair.a, query)
        assert elsewhere.bookings == ()


def test_search_results_put_upcoming_first_and_stop_at_the_limit(pair: Pair) -> None:
    """Soonest upcoming, then the latest past; beyond fifty, the answer says there are more."""
    with pair_database() as connection:
        business_id, service_id = connection.execute(
            "SELECT b.id, s.id FROM businesses b JOIN services s ON s.business_id = b.id WHERE b.slug = %s",
            (pair.a,),
        ).fetchone()  # type: ignore[misc]
        for number, offset in enumerate([-2, -1, 2, 1, *range(10, 60)]):
            start = datetime.now(UTC) + timedelta(days=offset)
            connection.execute(
                "INSERT INTO bookings (business_id, service_id, reference, status, starts_at, ends_at, blocked_until, "
                "price_minor, customer_name, customer_email) "
                "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, 0, 'Order Check', 'order@example.com')",
                (
                    business_id,
                    service_id,
                    f"ORDER{number:05d}",
                    start,
                    start + timedelta(hours=1),
                    start + timedelta(hours=1),
                ),
            )
    _, result = pair.owners.search_bookings(pair.owner_a, pair.a, "Order Check")
    assert result.more is True
    assert len(result.bookings) == 50
    assert [found.reference for found in result.bookings[:2]] == ["ORDER00003", "ORDER00002"]
    _, past = pair.owners.search_bookings(pair.owner_a, pair.a, "past order check")
    assert [found.reference for found in past.bookings] == ["ORDER00001", "ORDER00000"]


def test_owner_changes_show_on_the_public_side(pair: Pair) -> None:
    """Switching a service off hides it from customers; closing every day leaves no sessions."""
    pair.owners.update_service(pair.owner_a, pair.a, "hour", ServiceChanges(active=False))
    _, public = pair.bookings.list_services(pair.a)
    assert public == []
    pair.owners.update_service(pair.owner_a, pair.a, "hour", ServiceChanges(active=True))
    pair.owners.replace_hours(pair.owner_a, pair.a, [])
    _, _, days = pair.bookings.availability(pair.a, "hour", datetime.now(UTC).date(), 7)
    assert days == []
    pair.owners.replace_hours(
        pair.owner_a, pair.a, [HoursBlock(weekday=d, start=time(9, 0), end=time(10, 0)) for d in range(7)]
    )
    _, _, days = pair.bookings.availability(pair.a, "hour", datetime.now(UTC).date() + timedelta(days=1), 1)
    assert [slot.time for slot in days[0].slots] == ["09:00"]


def test_contact_details_saved_by_an_owner_reach_customers_in_the_same_fields(pair: Pair) -> None:
    """Address, phone and email saved in settings come back from the public side, each in its own field."""
    pair.owners.update_settings(
        pair.owner_a,
        pair.a,
        SettingsChanges(
            public_address="1 High Street, London", public_phone="020 7946 0000", public_email="hello@example.com"
        ),
    )
    business, _ = pair.bookings.list_services(pair.a)
    assert business.public_address == "1 High Street, London"
    assert business.public_phone == "020 7946 0000"
    assert business.public_email == "hello@example.com"
    assert business.payment_options == ("venue",)
    pair.owners.update_settings(
        pair.owner_a, pair.a, SettingsChanges(public_address=None, public_phone=None, public_email=None)
    )
    business, _ = pair.bookings.list_services(pair.a)
    assert (business.public_address, business.public_phone, business.public_email) == (None, None, None)


def test_duplicate_service_identifiers_are_a_conflict(pair: Pair) -> None:
    """A second service with an existing slug is refused."""
    with pytest.raises(ConflictError):
        pair.owners.create_service(
            pair.owner_a,
            pair.a,
            OwnerServiceRecord(
                slug="hour", name="Again", duration_minutes=30, price_minor=0, active=True, sort_order=0
            ),
        )
