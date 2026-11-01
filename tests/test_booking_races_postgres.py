"""Tests for simultaneous requests against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied. Each test
creates its own business with a random slug and deletes everything it created afterwards. Every race starts all
its requests at the same instant, from separate threads, each on its own pooled connection.

What these tests establish: whatever the mix of services and start times that race, no two live bookings ever
overlap and the winners are exactly the bookings that were stored; a customer move and a new booking racing for
one time give it to exactly one of them and leave the other where it was; a cancel racing moves of the same
booking always ends cancelled, with no live booking left; and one session raced for twelve rounds is booked
exactly once each round. The single-session race itself is in ``test_booking_postgres.py``; a race for a paid
hold is in ``test_payments_postgres.py``.

What they do not establish: behaviour behind a transaction-mode connection pooler, or at a scale beyond a dozen
simultaneous requests.
"""

import functools
import os
import secrets
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking.manage import ManagedBooking, ManageService, TooLateError
from booking.service import BookingRequest, BookingService, Confirmation, InvalidSlotError, SlotTakenError
from postgres import Database, DatabaseSettings

URL_ENV = "BOOKING_TEST_DATABASE_URL"
THREADS = 12
ZONE = ZoneInfo("Europe/London")
REFUSED = (SlotTakenError, InvalidSlotError)

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")


@dataclass
class World:
    """One temporary business and the services bound to it.

    Attributes:
        database: The database boundary.
        bookings: The booking service.
        manage: The customer self-service.
        slug: The temporary business's slug.
        day: A local date inside the booking window with working hours.
    """

    database: Database
    bookings: BookingService
    manage: ManageService
    slug: str
    day: date


@pytest.fixture
def world() -> Iterator[World]:
    """Create a business open every day 08:00-20:00, no break, with 60 and 30 minute services, then remove it.

    Yields:
        The temporary business, with a connection pool large enough for every racing thread.
    """
    settings = DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV], "DATABASE_POOL_MAX_SIZE": str(THREADS)})
    database = Database(settings)
    slug = "race-" + secrets.token_hex(6)
    with database.connection() as connection:
        row = connection.execute(
            "INSERT INTO businesses (slug, name, break_minutes, notice_minutes, window_days) "
            "VALUES (%s, 'Race test', 0, 0, 30) RETURNING id",
            (slug,),
        ).fetchone()
        assert row is not None
        business_id = row[0]
        connection.execute(
            "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor, sort_order) VALUES "
            "(%s, 'hour', 'Hour', 60, 0, 1), (%s, 'half', 'Half hour', 30, 0, 2)",
            (business_id, business_id),
        )
        for weekday in range(7):
            connection.execute(
                "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) VALUES (%s, %s, '08:00', '20:00')",
                (business_id, weekday),
            )
    try:
        yield World(
            database,
            BookingService(database),
            ManageService(database),
            slug,
            datetime.now(UTC).date() + timedelta(days=3),
        )
    finally:
        with database.connection() as connection:
            for table in ("bookings", "weekly_hours", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = %s", (business_id,))
            connection.execute("DELETE FROM businesses WHERE id = %s", (business_id,))
        database.close()


def race(actions: Sequence[Callable[[], object]]) -> list[object]:
    """Run every action at the same instant, each in its own thread, and collect what each one did.

    Args:
        actions: The actions to run.

    Returns:
        For each action, in order, the value it returned or the exception it raised.
    """
    barrier = threading.Barrier(len(actions))
    outcomes: list[object] = [None] * len(actions)

    def run(index: int) -> None:
        """Wait for every thread, then run one action.

        Args:
            index: Which action.
        """
        barrier.wait()
        try:
            outcomes[index] = actions[index]()
        except Exception as exc:  # noqa: BLE001 - the outcome is the point of the test
            outcomes[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(actions))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


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


def live_starts(world: World) -> list[str]:
    """List the local start times of the business's live bookings.

    Args:
        world: The temporary business.

    Returns:
        ``HH:MM`` for each live booking, in order.
    """
    with world.database.connection() as connection:
        rows = connection.execute(
            "SELECT b.starts_at FROM bookings b JOIN businesses s ON s.id = b.business_id "
            "WHERE s.slug = %s AND b.status IN ('confirmed', 'pending_payment') ORDER BY b.starts_at",
            (world.slug,),
        ).fetchall()
    return [row[0].astimezone(ZONE).strftime("%H:%M") for row in rows]


def overlapping_pairs(world: World) -> int:
    """Count pairs of live bookings of the business that overlap, including any break.

    Args:
        world: The temporary business.

    Returns:
        The number of overlapping pairs: always ``0`` if the rule holds.
    """
    with world.database.connection() as connection:
        row = connection.execute(
            "SELECT count(*) FROM bookings a JOIN bookings b ON b.business_id = a.business_id AND a.id < b.id "
            "JOIN businesses s ON s.id = a.business_id "
            "WHERE s.slug = %s AND a.status IN ('confirmed', 'pending_payment') "
            "AND b.status IN ('confirmed', 'pending_payment') "
            "AND tstzrange(a.starts_at, a.blocked_until, '[)') && tstzrange(b.starts_at, b.blocked_until, '[)')",
            (world.slug,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def only_known(outcomes: list[object], allowed: tuple[type, ...]) -> None:
    """Fail when a racing request ended in anything other than a result or an expected refusal.

    Args:
        outcomes: What each racing request did.
        allowed: The result types and exception types that are acceptable.
    """
    unexpected = [repr(o) for o in outcomes if not isinstance(o, allowed)]
    assert not unexpected, f"unexpected outcomes: {unexpected}"


def test_racing_overlapping_sessions_never_leave_two_live_bookings_that_overlap(world: World) -> None:
    """Hour and half-hour sessions at overlapping times race: the winners are exactly the stored bookings, and
    none of them overlap another."""
    mix = [("hour", "10:30"), ("half", "10:30"), ("half", "11:00"), ("hour", "11:00")] * 3
    actions: list[Callable[[], object]] = [
        functools.partial(world.bookings.create_booking, world.slug, request(world, service, start))
        for service, start in mix
    ]
    outcomes = race(actions)
    only_known(outcomes, (Confirmation, *REFUSED))
    booked = sum(isinstance(o, Confirmation) for o in outcomes)
    assert booked >= 1
    assert len(live_starts(world)) == booked
    assert overlapping_pairs(world) == 0


def test_a_move_and_new_bookings_racing_for_one_time_give_it_to_exactly_one(world: World) -> None:
    """A customer moving to 15:00 while five others book 15:00: one gets it, and a lost move stays where it was."""
    for hour in range(5):
        old, target = f"{8 + hour:02d}:00", f"{14 + hour:02d}:00"
        made = world.bookings.create_booking(world.slug, request(world, "hour", old))
        assert made.manage_token is not None
        token = made.manage_token
        actions: list[Callable[[], object]] = [
            lambda: world.manage.move(world.slug, made.reference, token, world.day, target)
        ]
        actions += [lambda: world.bookings.create_booking(world.slug, request(world, "hour", target))] * 5
        outcomes = race(actions)
        only_known(outcomes, (ManagedBooking, Confirmation, *REFUSED, TooLateError))
        moved = isinstance(outcomes[0], ManagedBooking)
        new = sum(isinstance(o, Confirmation) for o in outcomes[1:])
        assert int(moved) + new == 1
        starts = live_starts(world)
        assert starts.count(target) == 1
        assert (old in starts) is (not moved)
    assert overlapping_pairs(world) == 0


def test_a_cancel_racing_moves_of_the_same_booking_always_ends_cancelled(world: World) -> None:
    """Three cancels and three moves of one booking at once: no live booking is left, and every cancel answers."""
    for hour in range(5):
        old, target = f"{8 + hour:02d}:00", f"{14 + hour:02d}:00"
        made = world.bookings.create_booking(world.slug, request(world, "hour", old))
        assert made.manage_token is not None
        token = made.manage_token
        cancels = [lambda: world.manage.cancel(world.slug, made.reference, token)] * 3
        moves = [lambda: world.manage.move(world.slug, made.reference, token, world.day, target)] * 3
        outcomes = race(cancels + moves)
        only_known(outcomes, (ManagedBooking, *REFUSED, TooLateError))
        assert all(isinstance(o, ManagedBooking) for o in outcomes[:3]), f"a cancel failed: {outcomes[:3]!r}"
        assert old not in live_starts(world) and target not in live_starts(world)
    assert live_starts(world) == []


def test_the_same_session_raced_twelve_times_is_booked_exactly_once_each_time(world: World) -> None:
    """Twelve rounds of twelve simultaneous requests, each round for a different hour of the day."""
    for hour in range(12):
        start = f"{8 + hour:02d}:00"
        outcomes = race([lambda: world.bookings.create_booking(world.slug, request(world, "hour", start))] * THREADS)
        only_known(outcomes, (Confirmation, *REFUSED))
        assert sum(isinstance(o, Confirmation) for o in outcomes) == 1, f"round {start}: {outcomes!r}"
    assert live_starts(world) == [f"{8 + hour:02d}:00" for hour in range(12)]
    assert overlapping_pairs(world) == 0
