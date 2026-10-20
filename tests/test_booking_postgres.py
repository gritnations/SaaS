"""Tests for the booking service against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set, so the default test run needs no database and no
credential. The database must already have the migrations in ``migrations/`` applied. Each test creates
its own business with a random slug and deletes everything it created afterwards.

What these tests establish: the database refuses overlapping bookings even when requests race, the
service books only real, free, available sessions, and the Supabase Data API roles cannot reach the
booking tables. What they do not establish: behaviour under a transaction-mode pooler or at scale.
"""

import os
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from psycopg import errors

from booking.service import (
    BookingRequest,
    BookingService,
    InvalidSlotError,
    PaymentUnavailableError,
    SlotTakenError,
)
from postgres import Database, DatabaseSettings

URL_ENV = "BOOKING_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")


@dataclass
class World:
    """One temporary business and the service bound to it.

    Attributes:
        database: The database boundary.
        service: The booking service.
        slug: The temporary business's slug.
        day: A local date inside the booking window with working hours.
    """

    database: Database
    service: BookingService
    slug: str
    day: date


@pytest.fixture
def world() -> Iterator[World]:
    """Create a temporary business open every day 08:00-20:00 with a 15-minute break, then remove it.

    Yields:
        The temporary business and a booking service.
    """
    settings = DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV], "DATABASE_POOL_MAX_SIZE": "8"})
    database = Database(settings)
    slug = "test-" + secrets.token_hex(6)
    with database.connection() as connection:
        business_id = connection.execute(
            "INSERT INTO businesses (slug, name, break_minutes, notice_minutes, window_days) "
            "VALUES (%s, 'Test business', 15, 0, 30) RETURNING id",
            (slug,),
        ).fetchone()[
            0
        ]  # type: ignore[index]
        connection.execute(
            "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor, sort_order) VALUES "
            "(%s, 'hour', 'Hour', 60, 0, 1), (%s, 'half', 'Half hour', 30, 0, 2), (%s, 'paid', 'Paid', 60, 5000, 3)",
            (business_id, business_id, business_id),
        )
        for weekday in range(7):
            connection.execute(
                "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) VALUES (%s, %s, '08:00', '20:00')",
                (business_id, weekday),
            )
    try:
        yield World(database, BookingService(database), slug, datetime.now(UTC).date() + timedelta(days=3))
    finally:
        with database.connection() as connection:
            for table in ("bookings", "weekly_hours", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = %s", (business_id,))
            connection.execute("DELETE FROM businesses WHERE id = %s", (business_id,))
        database.close()


def request(world: World, service: str, start: str) -> BookingRequest:
    """Build a booking request on the test day.

    Args:
        world: The temporary business.
        service: Service slug.
        start: Local start time.

    Returns:
        The request.
    """
    return BookingRequest(service, world.day, start, "Test Customer", "customer@example.com", None, None)


def test_racing_requests_for_one_session_produce_exactly_one_booking(world: World) -> None:
    """Eight simultaneous requests for the same session: one is confirmed, the rest are refused."""
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        """Wait for every thread, then try to book 10:30."""
        barrier.wait()
        try:
            world.service.create_booking(world.slug, request(world, "hour", "10:30"))
            result = "booked"
        except (SlotTakenError, InvalidSlotError):
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("booked") == 1
    assert outcomes.count("refused") == 7


def test_a_booking_blocks_overlaps_of_other_durations_and_its_break(world: World) -> None:
    """After 10:30-11:30 (+15 break), half-hours overlapping it are taken; those clear of it are not.

    Half-hour sessions with a 15-minute break start every 45 minutes from 08:00: 09:30, 10:15, 11:00, 11:45.
    """
    world.service.create_booking(world.slug, request(world, "hour", "10:30"))
    with pytest.raises(InvalidSlotError):
        world.service.create_booking(world.slug, request(world, "half", "11:00"))
    _, _, days = world.service.availability(world.slug, "half", world.day, 1)
    taken = {slot.time: slot.available for slot in days[0].slots}
    assert taken["10:15"] is False
    assert taken["11:00"] is False
    assert taken["09:30"] is True
    assert taken["11:45"] is True


def test_the_database_itself_refuses_an_overlapping_row(world: World) -> None:
    """Even a direct insert that skips the service cannot overlap a confirmed booking."""
    confirmed = world.service.create_booking(world.slug, request(world, "hour", "13:00"))
    with pytest.raises(errors.ExclusionViolation):
        with world.database.connection() as connection:
            connection.execute(
                "INSERT INTO bookings (business_id, service_id, reference, status, starts_at, ends_at, blocked_until, "
                "price_minor, customer_name, customer_email) "
                "SELECT business_id, service_id, 'OVERLAP234', 'confirmed', starts_at + interval '30 minutes', "
                "ends_at + interval '30 minutes', blocked_until + interval '30 minutes', 0, 'X', 'x@example.com' "
                "FROM bookings WHERE reference = %s",
                (confirmed.reference,),
            )


def test_paid_services_and_off_grid_times_are_refused(world: World) -> None:
    """A paid service needs a way to pay, and a time off the session grid is not a session."""
    with world.database.connection() as connection:
        connection.execute("UPDATE businesses SET pay_at_venue = false WHERE slug = %s", (world.slug,))
    with pytest.raises(PaymentUnavailableError):
        world.service.create_booking(world.slug, request(world, "paid", "10:30"))
    with pytest.raises(InvalidSlotError):
        world.service.create_booking(world.slug, request(world, "hour", "10:10"))


def test_dates_outside_the_booking_window_are_refused(world: World) -> None:
    """Yesterday and a date beyond the window have no sessions."""
    for day in (datetime.now(UTC).date() - timedelta(days=1), datetime.now(UTC).date() + timedelta(days=60)):
        with pytest.raises(InvalidSlotError):
            world.service.create_booking(
                world.slug, BookingRequest("hour", day, "10:30", "Test", "t@example.com", None, None)
            )


def test_data_api_roles_cannot_read_bookings(world: World) -> None:
    """The ``anon`` role used by the Supabase Data API has no access to the booking tables."""
    with world.database.connection() as connection:
        if connection.execute("SELECT 1 FROM pg_roles WHERE rolname = 'anon'").fetchone() is None:
            pytest.skip("no anon role in this database")
    with pytest.raises(errors.InsufficientPrivilege):
        with world.database.connection() as connection:
            connection.execute("SET LOCAL ROLE anon")
            connection.execute("SELECT count(*) FROM bookings")
