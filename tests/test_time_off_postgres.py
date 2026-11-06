"""Tests for days off against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied, including
``0011_time_off.sql``. Each test creates its own businesses with random slugs and deletes everything it created.

What these tests establish: a day off closes whole local days, both ends included, for the public availability, a
direct booking request and a customer move, and only for its own business; a booking that already exists is never
touched, and the owner is told how many fall inside new days off; entries can be added, listed, overlapped and
removed by the business's own owner only; the limits and the past-date rule hold; days are the business's own
local dates; and the Data API roles cannot read the table.
"""

import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from psycopg import errors

from booking.manage import ManageService
from booking.owner import MAX_TIME_OFF, InvalidChangeError, OwnerService
from booking.service import BookingRequest, BookingService, InvalidSlotError, NotFoundError
from postgres import Database, DatabaseSettings

URL_ENV = "BOOKING_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")


@dataclass
class World:
    """One temporary business with an owner, and the services bound to the database.

    Attributes:
        database: The database boundary.
        owners: The owner service.
        bookings: The booking service.
        manage: The customer self-service.
        slug: The business's slug.
        account: Its owner's account identifier.
        today: The business's local date now.
        ids: Every business created for the test, for cleanup.
    """

    database: Database
    owners: OwnerService
    bookings: BookingService
    manage: ManageService
    slug: str
    account: str
    today: date
    ids: list[str] = field(default_factory=list)


def make_business(database: Database, zone: str = "Europe/London") -> tuple[str, str, str]:
    """Create a business open every day 08:00-20:00 with a 60-minute service, and give it an owner.

    Args:
        database: The database boundary.
        zone: The business's IANA time zone.

    Returns:
        ``(slug, owner account, business id)``.
    """
    slug = "off-" + secrets.token_hex(6)
    account = str(uuid.uuid4())
    with database.connection() as connection:
        row = connection.execute(
            "INSERT INTO businesses (slug, name, time_zone, break_minutes, notice_minutes, window_days) "
            "VALUES (%s, 'Days off test', %s, 0, 0, 30) RETURNING id::text",
            (slug, zone),
        ).fetchone()
        assert row is not None
        business_id = row[0]
        connection.execute(
            "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor, sort_order) "
            "VALUES (%s, 'hour', 'Hour', 60, 0, 1)",
            (business_id,),
        )
        for weekday in range(7):
            connection.execute(
                "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) VALUES (%s, %s, '08:00', '20:00')",
                (business_id, weekday),
            )
        connection.execute(
            "INSERT INTO business_members (business_id, user_id, role) VALUES (%s, %s, 'owner')", (business_id, account)
        )
    return slug, account, business_id


@pytest.fixture
def world() -> Iterator[World]:
    """Create a London business with an owner, then remove it and anything else the test added.

    Yields:
        The business and the services.
    """
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    slug, account, business_id = make_business(database)
    made = World(
        database,
        OwnerService(database),
        BookingService(database),
        ManageService(database),
        slug,
        account,
        datetime.now(ZoneInfo("Europe/London")).date(),
        [business_id],
    )
    try:
        yield made
    finally:
        with database.connection() as connection:
            for table in ("time_off", "bookings", "business_members", "weekly_hours", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = ANY(%s::uuid[])", (made.ids,))
            connection.execute("DELETE FROM businesses WHERE id = ANY(%s::uuid[])", (made.ids,))
        database.close()


def open_days(world: World, first: date, count: int, slug: str | None = None) -> set[date]:
    """List the dates, from a run of dates, that still have bookable times.

    Args:
        world: The test business.
        first: The first date asked about.
        count: How many dates.
        slug: Another business, or ``None`` for the test business.

    Returns:
        The dates with at least one session.
    """
    _, _, days = world.bookings.availability(slug or world.slug, "hour", first, count)
    return {day.date for day in days}


def request(day: date, start: str = "10:00") -> BookingRequest:
    """Build a booking request for the hour service.

    Args:
        day: The local date.
        start: Local start time.

    Returns:
        The request.
    """
    return BookingRequest("hour", day, start, "Test Customer", "customer@example.com", None, None)


def test_a_closed_day_has_no_times_and_its_neighbours_are_unaffected(world: World) -> None:
    """One day off removes that day's sessions and no other day's."""
    day = world.today + timedelta(days=5)
    entries, affected = world.owners.add_time_off(world.account, world.slug, day, day, None)
    assert affected == 0 and [(e.first_day, e.last_day) for e in entries] == [(day, day)]
    assert open_days(world, day - timedelta(days=1), 3) == {day - timedelta(days=1), day + timedelta(days=1)}


def test_a_range_closes_both_its_first_and_last_day(world: World) -> None:
    """Five days off close exactly those five days: the days either side stay open."""
    first = world.today + timedelta(days=5)
    world.owners.add_time_off(world.account, world.slug, first, first + timedelta(days=4), "Leave")
    assert open_days(world, first - timedelta(days=1), 7) == {first - timedelta(days=1), first + timedelta(days=5)}


def test_a_direct_booking_request_for_a_closed_day_is_refused(world: World) -> None:
    """A request that skips the page is refused like any unavailable time; an open day still books."""
    closed, open_day = world.today + timedelta(days=5), world.today + timedelta(days=6)
    world.owners.add_time_off(world.account, world.slug, closed, closed, None)
    with pytest.raises(InvalidSlotError):
        world.bookings.create_booking(world.slug, request(closed))
    assert world.bookings.create_booking(world.slug, request(open_day)).reference


def test_a_customer_cannot_move_a_booking_onto_a_closed_day(world: World) -> None:
    """The move uses the same availability, so a closed day is refused and an open one still works."""
    booked, closed, open_day = (world.today + timedelta(days=n) for n in (4, 6, 7))
    made = world.bookings.create_booking(world.slug, request(booked))
    assert made.manage_token is not None
    world.owners.add_time_off(world.account, world.slug, closed, closed, None)
    with pytest.raises(InvalidSlotError):
        world.manage.move(world.slug, made.reference, made.manage_token, closed, "10:00")
    moved = world.manage.move(world.slug, made.reference, made.manage_token, open_day, "10:00")
    assert moved.starts_at.astimezone(ZoneInfo("Europe/London")).date() == open_day


def test_a_booking_inside_new_days_off_stays_and_is_counted(world: World) -> None:
    """The owner is told how many live bookings fall in the range; cancelled ones and others outside do not count."""
    day = world.today + timedelta(days=5)
    kept = world.bookings.create_booking(world.slug, request(day, "10:00"))
    cancelled = world.bookings.create_booking(world.slug, request(day, "13:00"))
    outside = world.bookings.create_booking(world.slug, request(day + timedelta(days=3), "10:00"))
    world.owners.cancel_booking(world.account, world.slug, cancelled.reference)
    _, affected = world.owners.add_time_off(world.account, world.slug, day, day + timedelta(days=1), None)
    assert affected == 1
    for reference in (kept.reference, outside.reference):
        _, found = world.owners.booking_by_reference(world.account, world.slug, reference)
        assert found.status == "confirmed"


def test_removing_a_day_off_brings_its_times_back(world: World) -> None:
    """Removal returns what is left, and the closed day opens again."""
    day = world.today + timedelta(days=5)
    entries, _ = world.owners.add_time_off(world.account, world.slug, day, day, None)
    assert day not in open_days(world, day, 1)
    assert world.owners.remove_time_off(world.account, world.slug, entries[0].id) == []
    assert day in open_days(world, day, 1)


def test_overlapping_entries_are_allowed_and_each_is_removed_on_its_own(world: World) -> None:
    """Two overlapping ranges close their union; removing one leaves the other closed."""
    start = world.today + timedelta(days=5)
    world.owners.add_time_off(world.account, world.slug, start, start + timedelta(days=2), None)
    entries, _ = world.owners.add_time_off(
        world.account, world.slug, start + timedelta(days=1), start + timedelta(days=4), None
    )
    assert len(entries) == 2
    assert open_days(world, start, 5) == set()
    first = entries[0]
    remaining = world.owners.remove_time_off(world.account, world.slug, first.id)
    assert len(remaining) == 1
    assert open_days(world, start, 5) == {start}


def test_unknown_and_malformed_entries_are_not_found(world: World) -> None:
    """Removing an identifier that is not one, or that does not exist, answers not found."""
    for entry in (str(uuid.uuid4()), "not-an-id"):
        with pytest.raises(NotFoundError):
            world.owners.remove_time_off(world.account, world.slug, entry)


def test_another_owner_cannot_see_add_or_remove_and_other_businesses_stay_open(world: World) -> None:
    """Days off are private to the business's own owner, and never close another business."""
    day = world.today + timedelta(days=5)
    entries, _ = world.owners.add_time_off(world.account, world.slug, day, day, "Private")
    other_slug, other_account, other_id = make_business(world.database)
    world.ids.append(other_id)
    for call in (
        lambda: world.owners.time_off(other_account, world.slug),
        lambda: world.owners.add_time_off(other_account, world.slug, day, day, None),
        lambda: world.owners.remove_time_off(other_account, world.slug, entries[0].id),
    ):
        with pytest.raises(NotFoundError):
            call()
    assert world.owners.time_off(other_account, other_slug) == []
    assert len(world.owners.time_off(world.account, world.slug)) == 1
    assert day in open_days(world, day, 1, other_slug)


def test_the_rules_refuse_past_days_backwards_ranges_and_too_much(world: World) -> None:
    """A past start, an end before the start, more than a year, a long note and too many entries are refused."""
    today = world.today
    with pytest.raises(InvalidChangeError):
        world.owners.add_time_off(world.account, world.slug, today - timedelta(days=1), today, None)
    with pytest.raises(InvalidChangeError):
        world.owners.add_time_off(world.account, world.slug, today + timedelta(days=5), today + timedelta(days=4), None)
    with pytest.raises(InvalidChangeError):
        world.owners.add_time_off(world.account, world.slug, today, today + timedelta(days=366), None)
    with pytest.raises(InvalidChangeError):
        world.owners.add_time_off(world.account, world.slug, today, today, "x" * 101)
    assert world.owners.time_off(world.account, world.slug) == []
    entries, _ = world.owners.add_time_off(world.account, world.slug, today, today + timedelta(days=365), "  A year  ")
    assert entries[0].note == "A year" and entries[0].first_day == today


def test_there_is_a_limit_on_how_many_entries_a_business_can_have(world: World) -> None:
    """The entry after the limit is refused; removing one makes room."""
    start = world.today + timedelta(days=1)
    with world.database.connection() as connection:
        connection.execute(
            "INSERT INTO time_off (business_id, first_day, last_day) "
            "SELECT b.id, %s::date + n, %s::date + n FROM businesses b, generate_series(0, %s) AS n WHERE b.slug = %s",
            (start, start, MAX_TIME_OFF - 2, world.slug),
        )
    day = start + timedelta(days=MAX_TIME_OFF - 1)
    entries, _ = world.owners.add_time_off(world.account, world.slug, day, day, None)
    assert len(entries) == MAX_TIME_OFF
    with pytest.raises(InvalidChangeError):
        world.owners.add_time_off(world.account, world.slug, day + timedelta(days=1), day + timedelta(days=1), None)
    world.owners.remove_time_off(world.account, world.slug, entries[0].id)
    world.owners.add_time_off(world.account, world.slug, day + timedelta(days=1), day + timedelta(days=1), None)


def test_past_entries_are_not_listed(world: World) -> None:
    """An entry that has ended is kept in the database but no longer shown."""
    with world.database.connection() as connection:
        connection.execute(
            "INSERT INTO time_off (business_id, first_day, last_day) "
            "SELECT id, %s, %s FROM businesses WHERE slug = %s",
            (world.today - timedelta(days=9), world.today - timedelta(days=8), world.slug),
        )
    assert world.owners.time_off(world.account, world.slug) == []


def test_days_are_the_business_s_own_local_dates(world: World) -> None:
    """A business in Riyadh closes its own calendar date, whatever the time zone of the owner or the visitor."""
    slug, account, business_id = make_business(world.database, "Asia/Riyadh")
    world.ids.append(business_id)
    local_today = datetime.now(ZoneInfo("Asia/Riyadh")).date()
    day = local_today + timedelta(days=5)
    assert day in open_days(world, day, 1, slug)
    world.owners.add_time_off(account, slug, day, day, None)
    assert open_days(world, day - timedelta(days=1), 3, slug) == {day - timedelta(days=1), day + timedelta(days=1)}


def test_the_database_refuses_a_backwards_range(world: World) -> None:
    """Even a direct insert cannot end a day off before it starts."""
    with pytest.raises(errors.CheckViolation):
        with world.database.connection() as connection:
            connection.execute(
                "INSERT INTO time_off (business_id, first_day, last_day) "
                "SELECT id, %s, %s FROM businesses WHERE slug = %s",
                (world.today + timedelta(days=5), world.today + timedelta(days=4), world.slug),
            )


def test_data_api_roles_cannot_read_days_off(world: World) -> None:
    """The ``anon`` role used by the Supabase Data API has no access to the days-off table."""
    with world.database.connection() as connection:
        if connection.execute("SELECT 1 FROM pg_roles WHERE rolname = 'anon'").fetchone() is None:
            pytest.skip("no anon role in this database")
    with pytest.raises(errors.InsufficientPrivilege):
        with world.database.connection() as connection:
            connection.execute("SET LOCAL ROLE anon")
            connection.execute("SELECT count(*) FROM time_off")
