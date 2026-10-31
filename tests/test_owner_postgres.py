"""Tests for the owner service against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration in
``migrations/`` applied. Each test creates two temporary businesses, each with its own owner account,
and deletes everything it created afterwards.

What these tests establish: an owner reaches only their own business (bookings, look-up by reference,
services, hours and settings), and an owner's changes to services and hours show on the public booking
side at once.
"""

import os
import secrets
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import pytest

from booking.owner import ConflictError, HoursBlock, OwnerService, OwnerServiceRecord
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
        slugs[1], BookingRequest("hour", day, "10:00", "B Customer", "b@example.com", None, None)
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
        lambda: pair.owners.services(owner, other),
        lambda: pair.owners.update_service(owner, other, "hour", {"active": False}),
        lambda: pair.owners.hours(owner, other),
        lambda: pair.owners.replace_hours(owner, other, []),
        lambda: pair.owners.settings(owner, other),
        lambda: pair.owners.update_settings(owner, other, {"window_days": 2}),
        lambda: pair.owners.create_service(owner, other, OwnerServiceRecord("x", "X", 30, 0, True, 0)),
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


def test_owner_changes_show_on_the_public_side(pair: Pair) -> None:
    """Switching a service off hides it from customers; closing every day leaves no sessions."""
    pair.owners.update_service(pair.owner_a, pair.a, "hour", {"active": False})
    _, public = pair.bookings.list_services(pair.a)
    assert public == []
    pair.owners.update_service(pair.owner_a, pair.a, "hour", {"active": True})
    pair.owners.replace_hours(pair.owner_a, pair.a, [])
    _, _, days = pair.bookings.availability(pair.a, "hour", datetime.now(UTC).date(), 7)
    assert days == []
    pair.owners.replace_hours(pair.owner_a, pair.a, [HoursBlock(d, time(9, 0), time(10, 0)) for d in range(7)])
    _, _, days = pair.bookings.availability(pair.a, "hour", datetime.now(UTC).date() + timedelta(days=1), 1)
    assert [slot.time for slot in days[0].slots] == ["09:00"]


def test_contact_details_saved_by_an_owner_reach_customers_in_the_same_fields(pair: Pair) -> None:
    """Address, phone and email saved in settings come back from the public side, each in its own field."""
    pair.owners.update_settings(
        pair.owner_a,
        pair.a,
        {
            "public_address": "1 High Street, London",
            "public_phone": "020 7946 0000",
            "public_email": "hello@example.com",
        },
    )
    business, _ = pair.bookings.list_services(pair.a)
    assert business.public_address == "1 High Street, London"
    assert business.public_phone == "020 7946 0000"
    assert business.public_email == "hello@example.com"
    assert business.payment_options == ("venue",)
    pair.owners.update_settings(
        pair.owner_a, pair.a, {"public_address": None, "public_phone": None, "public_email": None}
    )
    business, _ = pair.bookings.list_services(pair.a)
    assert (business.public_address, business.public_phone, business.public_email) == (None, None, None)


def test_duplicate_service_identifiers_are_a_conflict(pair: Pair) -> None:
    """A second service with an existing slug is refused."""
    with pytest.raises(ConflictError):
        pair.owners.create_service(pair.owner_a, pair.a, OwnerServiceRecord("hour", "Again", 30, 0, True, 0))
