"""Tests for the booking idempotency key (migration 0018) against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. Most tests connect as the database owner and share one
transaction with the booking service, so nothing is committed and everything is rolled back afterwards. If
migration 0018 is not applied yet, its file is run inside that transaction first. The one concurrency test needs
real commits, so it is skipped until 0018 is applied to the database, and it removes everything it created.

What these tests establish: a first request with a key books and stores the key; the same key with the same
request returns that booking (without its manage code) and creates nothing; the same key with a different request
is refused; a different key, or no key, for a taken time is still refused as before; a replay shows the booking's
current state; the table refuses a malformed key and a key without its hash; and simultaneous repeats of one
request make exactly one booking. They do not establish how the page chooses its key.
"""

import os
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import cast

import psycopg
import pytest
from psycopg import errors
from test_app_api_role_postgres import owner_connection  # noqa: F401
from test_manage_postgres import URL_ENV
from test_migrations import FILES, number

from booking.service import (
    BookingRequest,
    BookingService,
    Confirmation,
    IdempotencyKeyReuseError,
    InvalidSlotError,
)
from postgres import Database, DatabaseSettings

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

KEY_FILE = next(path for path in FILES if number(path) == 18)
Connection = psycopg.Connection[tuple[object, ...]]


class OneTransaction:
    """Hands the booking service one open connection, with a savepoint around each use, and never commits it."""

    def __init__(self, connection: Connection) -> None:
        """Keep the connection.

        Args:
            connection: The test's open connection.
        """
        self._connection = connection

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        """Lend the connection, undoing the use if it raises, as the real boundary does.

        Yields:
            The test's connection.
        """
        with self._connection.transaction():
            yield self._connection


class World:
    """A business open every day 08:00-20:00 with a free one-hour service, inside the test's transaction.

    Attributes:
        service: The booking service.
        connection: The test's connection.
        slug: The business slug.
        day: A local date three days ahead.
    """

    def __init__(self, connection: Connection) -> None:
        """Create the business and its service.

        Args:
            connection: The owner's connection, rolled back after the test.
        """
        self.connection = connection
        self.slug = "idem-" + secrets.token_hex(5)
        business = connection.execute(
            "INSERT INTO businesses (slug, name, notice_minutes) VALUES (%s, 'Idem Test', 0) RETURNING id",
            (self.slug,),
        ).fetchone()
        assert business is not None
        self.business_id = str(business[0])
        connection.execute(
            "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor) "
            "VALUES (%s, 'hour', 'Hour', 60, 0)",
            (self.business_id,),
        )
        for weekday in range(7):
            connection.execute(
                "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) "
                "VALUES (%s, %s, '08:00', '20:00')",
                (self.business_id, weekday),
            )
        self.service = BookingService(cast(Database, OneTransaction(connection)))
        self.day: date = datetime.now(UTC).date() + timedelta(days=3)

    def request(self, time: str = "10:00", key: str | None = None) -> BookingRequest:
        """Build a booking request for the test day.

        Args:
            time: Local start time.
            key: The idempotency key, or ``None``.

        Returns:
            The request.
        """
        return BookingRequest(
            service="hour",
            date=self.day,
            time=time,
            name="Pat Customer",
            email="pat@example.com",
            phone=None,
            note=None,
            idempotency_key=key,
        )

    def count(self) -> int:
        """Count the business's bookings.

        Returns:
            The number of bookings.
        """
        found = self.connection.execute(
            "SELECT count(*) FROM bookings WHERE business_id = %s", (self.business_id,)
        ).fetchone()
        assert found is not None
        return cast(int, found[0])


@pytest.fixture
def world(owner_connection: Connection) -> World:  # noqa: F811
    """Make the key columns exist in the open transaction, then create the business.

    Args:
        owner_connection: The owner's connection, rolled back after the test.

    Returns:
        The prepared world.
    """
    exists = owner_connection.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'bookings' AND column_name = 'idempotency_key')"
    ).fetchone()
    if exists is not None and not exists[0]:
        owner_connection.execute(KEY_FILE.read_text(encoding="utf-8"))
    return World(owner_connection)


def test_a_first_request_with_a_key_books_and_stores_the_key(world: World) -> None:
    """The booking is made with its manage code, and its key and request hash are stored."""
    made = world.service.create_booking(world.slug, world.request(key="key-abcdefgh-1"))
    assert made.manage_token is not None and made.replayed is False
    stored = world.connection.execute(
        "SELECT idempotency_key, octet_length(request_hash) FROM bookings WHERE reference = %s", (made.reference,)
    ).fetchone()
    assert stored == ("key-abcdefgh-1", 32)


def test_the_same_key_and_request_returns_the_same_booking_and_creates_nothing(world: World) -> None:
    """A repeat gets the original reference, time and state, without the manage code, and no second booking."""
    first = world.service.create_booking(world.slug, world.request(key="key-abcdefgh-2"))
    again = world.service.create_booking(world.slug, world.request(key="key-abcdefgh-2"))
    assert again.replayed is True and again.manage_token is None and again.checkout_url is None
    assert (again.reference, again.status, again.payment_status) == (
        first.reference,
        first.status,
        first.payment_status,
    )
    assert (again.slot.time, again.slot.label, again.slot.starts_at) == (
        first.slot.time,
        first.slot.label,
        first.slot.starts_at,
    )
    assert again.service.slug == first.service.slug
    assert world.count() == 1


def test_the_same_key_for_a_different_request_is_refused(world: World) -> None:
    """A key cannot be reused for another time, and nothing is created."""
    world.service.create_booking(world.slug, world.request("10:00", key="key-abcdefgh-3"))
    with pytest.raises(IdempotencyKeyReuseError):
        world.service.create_booking(world.slug, world.request("12:00", key="key-abcdefgh-3"))
    assert world.count() == 1


def test_a_different_key_or_no_key_for_a_taken_time_is_still_refused(world: World) -> None:
    """Only the same key gets the first booking back; any other repeat meets the taken time as before."""
    world.service.create_booking(world.slug, world.request(key="key-abcdefgh-4"))
    with pytest.raises(InvalidSlotError):
        world.service.create_booking(world.slug, world.request(key="key-abcdefgh-5"))
    with pytest.raises(InvalidSlotError):
        world.service.create_booking(world.slug, world.request())
    assert world.count() == 1


def test_a_replay_shows_the_current_state_of_the_booking(world: World) -> None:
    """If the booking was cancelled since, the repeat reports it as cancelled and does not rebook."""
    first = world.service.create_booking(world.slug, world.request(key="key-abcdefgh-6"))
    world.connection.execute(
        "UPDATE bookings SET status = 'cancelled', cancelled_at = now() WHERE reference = %s", (first.reference,)
    )
    again = world.service.create_booking(world.slug, world.request(key="key-abcdefgh-6"))
    assert (again.replayed, again.status, again.reference) == (True, "cancelled", first.reference)
    assert world.count() == 1


def test_the_table_refuses_a_malformed_key_and_a_key_without_its_hash(world: World) -> None:
    """The columns carry their own rules, so a bad value cannot be stored by any write path."""
    made = world.service.create_booking(world.slug, world.request())
    for column, value in (("idempotency_key", "short"), ("idempotency_key", "has spaces in it")):
        with pytest.raises(errors.CheckViolation):
            with world.connection.transaction():
                world.connection.execute(
                    f"UPDATE bookings SET {column} = %s, request_hash = %s WHERE reference = %s",
                    (value, b"\x00" * 32, made.reference),
                )
    with pytest.raises(errors.CheckViolation):
        with world.connection.transaction():
            world.connection.execute(
                "UPDATE bookings SET idempotency_key = 'key-abcdefgh-7' WHERE reference = %s", (made.reference,)
            )


def test_simultaneous_repeats_of_one_request_make_exactly_one_booking() -> None:
    """Six threads send the same keyed request at once: one booking, and every answer names it."""
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    with database.connection() as connection:
        found = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'bookings' AND column_name = 'idempotency_key')"
        ).fetchone()
    if found is None or not found[0]:
        database.close()
        pytest.skip("migration 0018 is not applied to this database, and this test needs real commits")
    slug = "idemrace-" + secrets.token_hex(5)
    with database.connection() as connection:
        business = connection.execute(
            "INSERT INTO businesses (slug, name, notice_minutes) VALUES (%s, 'Idem Race', 0) RETURNING id::text",
            (slug,),
        ).fetchone()
        assert business is not None
        business_id = business[0]
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
    service = BookingService(database)
    request = BookingRequest(
        service="hour",
        date=datetime.now(UTC).date() + timedelta(days=3),
        time="10:00",
        name="Pat",
        email="pat@example.com",
        phone=None,
        note=None,
        idempotency_key="key-race-abcdefgh",
    )
    barrier = threading.Barrier(6)
    outcomes: list[object] = [None] * 6

    def attempt(index: int) -> None:
        """Wait for every thread, then send the request.

        Args:
            index: Which thread.
        """
        barrier.wait()
        try:
            outcomes[index] = service.create_booking(slug, request.model_copy())
        except Exception as exc:  # noqa: BLE001 - the outcome is the point of the test
            outcomes[index] = exc

    try:
        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        answers = [o for o in outcomes if isinstance(o, Confirmation)]
        assert len(answers) == 6, f"unexpected outcomes: {outcomes!r}"
        assert len({a.reference for a in answers}) == 1
        assert sum(1 for a in answers if not a.replayed) == 1
        with database.connection() as connection:
            count = connection.execute(
                "SELECT count(*) FROM bookings WHERE business_id = %s", (business_id,)
            ).fetchone()
        assert count is not None and count[0] == 1
    finally:
        with database.connection() as connection:
            for table in ("email_log", "bookings", "weekly_hours", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = %s", (business_id,))
            connection.execute("DELETE FROM businesses WHERE id = %s", (business_id,))
        database.close()
