"""Tests for reminder emails against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied. Each test
creates its own business and deletes everything it created. A recording mailer stands in for the email provider,
so no email leaves the machine. A run claims due bookings across every business, so the checks look only at this
test's bookings and at messages sent to this test's customers.

What these tests establish: a due booking gets one reminder and only one; bookings that are too far off, booked
late, cancelled, unpaid or already started get none; a moved booking is reminded for its new time; a failed send,
or a message that cannot be built, is retried up to the limit; a booking another run holds is skipped, not claimed
twice; email that is not configured is recorded once and not retried; a real move records its time; and the run
works with only the API role's privileges.
"""

import os
import secrets
import string
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from test_app_api_role_postgres import LimitedDatabase
from test_manage_postgres import URL_ENV, RecordingMailer, World, book, world  # noqa: F401

from booking import notify
from booking.notify import Notifier
from booking.reminders import _CLAIM, BATCH_SIZE, MAX_ATTEMPTS, REMINDER_LEAD, ReminderService
from postgres import Database, DatabaseSettings

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


@dataclass
class Shop:
    """A temporary business with one free service, and a reminder service on a recording mailer.

    Attributes:
        database: The database boundary.
        mailer: The recording mailer.
        reminders: The reminder service.
        business_id: The business.
        service_id: Its service.
        emails: Customer addresses used by this test's bookings.
    """

    database: Database
    mailer: RecordingMailer
    reminders: ReminderService
    business_id: str
    service_id: str
    emails: set[str]


@pytest.fixture
def shop() -> Iterator[Shop]:
    """Create a business with a meeting link, a contact email and one free service.

    Yields:
        The business and its reminder service.
    """
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    with database.connection() as connection:
        business_id = str(
            connection.execute(
                "INSERT INTO businesses (slug, name, contact_email, meeting_link) "
                "VALUES (%s, 'Test & Co', 'owner@example.com', 'https://meet.example.com/room') RETURNING id",
                ("test-" + secrets.token_hex(6),),
            ).fetchone()[
                0
            ]  # type: ignore[index]
        )
        service_id = str(
            connection.execute(
                "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor) "
                "VALUES (%s, 'hour', 'Hour', 60, 0) RETURNING id",
                (business_id,),
            ).fetchone()[
                0
            ]  # type: ignore[index]
        )
    mailer = RecordingMailer()
    try:
        yield Shop(
            database,
            mailer,
            ReminderService(database, Notifier(database, mailer, "https://book.example.com")),
            business_id,
            service_id,
            set(),
        )
    finally:
        with database.connection() as connection:
            for table in ("email_log", "bookings", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = %s", (business_id,))
            connection.execute("DELETE FROM businesses WHERE id = %s", (business_id,))
        database.close()


def add(shop: Shop, starts_in: timedelta, booked_ago: timedelta = 3 * DAY, status: str = "confirmed") -> str:
    """Insert a one-hour booking directly.

    Args:
        shop: The test business.
        starts_in: How far from now it starts (negative for the past).
        booked_ago: How long ago it was made.
        status: ``confirmed``, ``cancelled`` or ``pending_payment``.

    Returns:
        The booking's id.
    """
    now = datetime.now(UTC)
    start = now + starts_in
    email = f"customer-{secrets.token_hex(4)}@example.com"
    shop.emails.add(email)
    reference = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    hold = status == "pending_payment"
    with shop.database.connection() as connection:
        row = connection.execute(
            "INSERT INTO bookings (business_id, service_id, reference, status, starts_at, ends_at, blocked_until, "
            "price_minor, customer_name, customer_email, created_at, cancelled_at, cancelled_by, payment_status, "
            "hold_expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 'Pat <b>Customer</b>', %s, %s, %s, %s, %s, %s) "
            "RETURNING id::text",
            (
                shop.business_id,
                shop.service_id,
                reference,
                status,
                start,
                start + HOUR,
                start + HOUR,
                email,
                now - booked_ago,
                now if status == "cancelled" else None,
                "customer" if status == "cancelled" else None,
                "awaiting" if hold else "not_required",
                now + HOUR if hold else None,
            ),
        ).fetchone()
    assert row is not None
    return str(row[0])


def reminded(shop: Shop) -> list[str]:
    """List the addresses of this test's customers that received a reminder, in order.

    Args:
        shop: The test business.

    Returns:
        Recipient addresses.
    """
    return [m.to for m in shop.mailer.sent if m.to in shop.emails and m.subject.startswith("Reminder:")]


def state(shop: Shop, booking_id: str) -> tuple[object, int]:
    """Read a booking's reminder claim.

    Args:
        shop: The test business.
        booking_id: The booking.

    Returns:
        ``(reminder_starts_at, reminder_attempts)``.
    """
    with shop.database.connection() as connection:
        row = connection.execute(
            "SELECT reminder_starts_at, reminder_attempts FROM bookings WHERE id = %s", (booking_id,)
        ).fetchone()
    assert row is not None
    return row[0], int(row[1])


def logged(shop: Shop) -> list[tuple[str, str, str]]:
    """Read the email log for the test business.

    Args:
        shop: The test business.

    Returns:
        ``(kind, recipient_role, status)`` in order.
    """
    with shop.database.connection() as connection:
        rows = connection.execute(
            "SELECT kind, recipient_role, status FROM email_log WHERE business_id = %s ORDER BY created_at",
            (shop.business_id,),
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def test_a_due_booking_gets_one_reminder_and_only_one(shop: Shop) -> None:
    """A booking 23 hours away, made days ago, is reminded once; later runs send nothing more."""
    booking = add(shop, 23 * HOUR)
    shop.reminders.run()
    shop.reminders.run()
    assert len(reminded(shop)) == 1
    message = next(m for m in shop.mailer.sent if m.to in shop.emails)
    assert message.subject == "Reminder: Hour with Test & Co"
    assert message.reply_to == "owner@example.com"
    assert "https://meet.example.com/room" in message.text
    assert "confirmation email" in message.text
    assert "#manage" not in message.text and "#manage" not in message.html
    assert "Pat &lt;b&gt;Customer&lt;/b&gt;" in message.html
    assert message.attachments == ()
    starts_at, attempts = state(shop, booking)
    assert starts_at is not None and attempts == 1
    assert logged(shop) == [("reminder", "customer", "sent")]


def test_bookings_that_are_not_due_get_no_reminder(shop: Shop) -> None:
    """Too far off, booked late, cancelled, unpaid or already started: no reminder."""
    add(shop, 30 * HOUR)
    add(shop, 20 * HOUR, booked_ago=2 * HOUR)
    add(shop, 19 * HOUR, status="cancelled")
    add(shop, 18 * HOUR, status="pending_payment")
    add(shop, -2 * HOUR)
    shop.reminders.run()
    assert reminded(shop) == []
    assert logged(shop) == []


def test_a_moved_booking_is_reminded_for_its_new_time(shop: Shop) -> None:
    """After a reminder, a move to another time a day ahead brings a second reminder; a late move does not."""
    early = add(shop, 23 * HOUR)
    late = add(shop, 20 * HOUR)
    shop.reminders.run()
    assert len(reminded(shop)) == 2
    now = datetime.now(UTC)
    with shop.database.connection() as connection:
        for booking, moved_ago in ((early, 2 * DAY), (late, HOUR)):
            connection.execute(
                "UPDATE bookings SET starts_at = starts_at - interval '5 hours', ends_at = ends_at - interval "
                "'5 hours', blocked_until = blocked_until - interval '5 hours', moved_at = %s, reminder_attempts = 0 "
                "WHERE id = %s",
                (now - moved_ago, booking),
            )
    shop.reminders.run()
    assert len(reminded(shop)) == 3
    with shop.database.connection() as connection:
        rows = dict(
            connection.execute(
                "SELECT id::text, reminder_starts_at = starts_at FROM bookings WHERE id IN (%s, %s)", (early, late)
            ).fetchall()
        )
    assert rows == {early: True, late: False}


def test_a_failed_send_is_retried_up_to_the_limit(shop: Shop) -> None:
    """Each failure clears the claim; after the limit the booking is left alone."""
    booking = add(shop, 23 * HOUR)
    shop.mailer.fail = True
    for attempt in range(1, MAX_ATTEMPTS + 1):
        shop.reminders.run()
        assert state(shop, booking) == (None, attempt)
    shop.mailer.fail = False
    shop.reminders.run()
    assert reminded(shop) == []
    assert logged(shop) == [("reminder", "customer", "failed")] * MAX_ATTEMPTS


def test_a_retry_after_one_failure_sends_the_reminder(shop: Shop) -> None:
    """One failed send, then a working provider: the next run sends it."""
    booking = add(shop, 23 * HOUR)
    shop.mailer.fail = True
    shop.reminders.run()
    shop.mailer.fail = False
    shop.reminders.run()
    assert len(reminded(shop)) == 1
    assert state(shop, booking)[1] == 2
    assert logged(shop) == [("reminder", "customer", "failed"), ("reminder", "customer", "sent")]


def test_a_booking_another_run_holds_is_not_claimed_twice(shop: Shop) -> None:
    """While one run holds its claim uncommitted, another skips the booking; once committed, it stays claimed."""
    booking = add(shop, 23 * HOUR)
    holder = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    try:
        with holder.connection() as connection:
            held = connection.execute(
                _CLAIM, {"lead": REMINDER_LEAD, "attempts": MAX_ATTEMPTS, "limit": BATCH_SIZE}
            ).fetchall()
            assert booking in {row[0] for row in held}
            shop.reminders.run()
            assert reminded(shop) == []
        shop.reminders.run()
        assert reminded(shop) == []
        assert state(shop, booking)[1] == 1
    finally:
        holder.close()


def test_unconfigured_email_is_recorded_once_and_not_retried() -> None:
    """With no email provider the reminder is logged as skipped, and the booking is not claimed again."""
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    shop = Shop(database, RecordingMailer(), ReminderService(database, Notifier(database, None, None)), "", "", set())
    with database.connection() as connection:
        shop.business_id = str(
            connection.execute(
                "INSERT INTO businesses (slug, name) VALUES (%s, 'Quiet') RETURNING id",
                ("test-" + secrets.token_hex(6),),
            ).fetchone()[
                0
            ]  # type: ignore[index]
        )
        shop.service_id = str(
            connection.execute(
                "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor) "
                "VALUES (%s, 'hour', 'Hour', 60, 0) RETURNING id",
                (shop.business_id,),
            ).fetchone()[
                0
            ]  # type: ignore[index]
        )
    try:
        booking = add(shop, 23 * HOUR)
        shop.reminders.run()
        shop.reminders.run()
        assert logged(shop) == [("reminder", "customer", "skipped")]
        assert state(shop, booking)[1] == 1
    finally:
        with database.connection() as connection:
            for table in ("email_log", "bookings", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = %s", (shop.business_id,))
            connection.execute("DELETE FROM businesses WHERE id = %s", (shop.business_id,))
        database.close()


def test_a_real_move_records_when_it_happened(world: World) -> None:  # noqa: F811
    """Moving through a manage link sets the move time and resets the reminder attempts."""
    reference, token = book(world)
    with world.database.connection() as connection:
        connection.execute("UPDATE bookings SET reminder_attempts = 2 WHERE reference = %s", (reference,))
    world.manage.move(world.slug, reference, token, world.day, "12:00")  # type: ignore[arg-type]
    with world.database.connection() as connection:
        row = connection.execute(
            "SELECT moved_at > now() - interval '1 minute', reminder_attempts FROM bookings WHERE reference = %s",
            (reference,),
        ).fetchone()
    assert row is not None and row[0] is True and row[1] == 0


def test_the_run_works_as_the_limited_role(shop: Shop) -> None:
    """Claiming, sending, recording and releasing all work with only the API role's privileges."""
    sent = add(shop, 23 * HOUR)
    limited = LimitedDatabase(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    try:
        service = ReminderService(limited, Notifier(limited, shop.mailer, None))
        service.run()
        assert len(reminded(shop)) == 1
        failed = add(shop, 20 * HOUR)
        shop.mailer.fail = True
        service.run()
        assert state(shop, failed) == (None, 1)
        assert state(shop, sent)[1] == 1
    finally:
        limited.close()


def test_a_refused_message_is_retried_and_sent_once_it_builds(shop: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    """A message that cannot be built from what is stored is a failure, retried, not given up on at once."""
    booking = add(shop, 23 * HOUR)

    def refuse(context: object) -> None:
        """Stand in for a message that cannot be built.

        Args:
            context: Ignored.

        Raises:
            ValueError: Always.
        """
        raise ValueError("refused")

    monkeypatch.setattr(notify, "reminder_message", refuse)
    first = shop.reminders.run()
    assert first.failed >= 1 and first.dropped == 0
    assert state(shop, booking) == (None, 1)
    monkeypatch.undo()
    shop.reminders.run()
    assert len(reminded(shop)) == 1
    assert logged(shop) == [("reminder", "customer", "failed"), ("reminder", "customer", "sent")]
