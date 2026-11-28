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

For the earlier reminder: only a paid plan or a trial gets it; it follows the business's switch and timing; it is not
sent once the booking is close to the 24-hour one; each reminder is claimed, retried and recorded separately; the
business's note is in the email; and an owner cannot change the earlier reminder on a plan without it.
"""

import os
import secrets
import string
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from test_app_api_role_postgres import LimitedDatabase
from test_manage_postgres import URL_ENV, RecordingMailer, World, book, world  # noqa: F401

from booking import notify
from booking.notify import Notifier
from booking.owner import InvalidChangeError, OwnerService, ReminderChanges
from booking.reminders import _CLAIM, BATCH_SIZE, MAX_ATTEMPTS, REMINDER_LEAD, ReminderService
from booking.service import NotFoundError
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


def set_business(shop: Shop, **values: object) -> None:
    """Change columns of the test business.

    Args:
        shop: The test business.
        **values: Column names and their new values.
    """
    assignments = ", ".join(f"{name} = %s" for name in values)
    with shop.database.connection() as connection:
        connection.execute(f"UPDATE businesses SET {assignments} WHERE id = %s", (*values.values(), shop.business_id))


def early(shop: Shop, booking_id: str) -> tuple[bool, int, bool, bool]:
    """Read both reminders' state for a booking.

    Args:
        shop: The test business.
        booking_id: The booking.

    Returns:
        Whether the earlier reminder is claimed, its attempts, and whether each of the earlier and the 24-hour
        reminders is recorded as sent.
    """
    with shop.database.connection() as connection:
        row = connection.execute(
            "SELECT reminder_early_starts_at IS NOT NULL, reminder_early_attempts, "
            "reminder_early_sent_at IS NOT NULL, reminder_sent_at IS NOT NULL FROM bookings WHERE id = %s",
            (booking_id,),
        ).fetchone()
    assert row is not None
    return bool(row[0]), int(row[1]), bool(row[2]), bool(row[3])


def test_a_free_business_gets_only_the_24_hour_reminder(shop: Shop) -> None:
    """The earlier reminder is switched on by default but the free plan does not include it."""
    soon = add(shop, 23 * HOUR, booked_ago=10 * DAY)
    later = add(shop, 71 * HOUR, booked_ago=10 * DAY)
    shop.reminders.run()
    assert len(reminded(shop)) == 1
    assert early(shop, soon) == (False, 0, False, True)
    assert early(shop, later) == (False, 0, False, False)


@pytest.mark.parametrize("plan", ["starter", "pro", "trial"])
def test_a_paid_plan_or_trial_gets_the_earlier_reminder_once(shop: Shop, plan: str) -> None:
    """A booking just under 72 hours away is reminded once, and recorded as sent."""
    if plan == "trial":
        set_business(shop, trial_ends_at=datetime.now(UTC) + DAY)
    else:
        set_business(shop, plan=plan)
    booking = add(shop, 71 * HOUR, booked_ago=10 * DAY)
    shop.reminders.run()
    shop.reminders.run()
    assert len(reminded(shop)) == 1
    assert early(shop, booking) == (True, 1, True, False)
    assert logged(shop) == [("reminder_early", "customer", "sent")]


def test_an_ended_trial_stops_the_earlier_reminder(shop: Shop) -> None:
    """Once the trial is over, a free business gets no earlier reminder."""
    set_business(shop, trial_ends_at=datetime.now(UTC) - HOUR)
    add(shop, 71 * HOUR, booked_ago=10 * DAY)
    shop.reminders.run()
    assert reminded(shop) == []


@pytest.mark.parametrize(
    ("hours", "due", "not_due"),
    [
        (48, [47, 37], [49, 35]),
        (72, [71, 49], [73, 47]),
        (168, [167, 97], [169, 95]),
    ],
)
def test_the_earlier_reminder_is_sent_only_in_its_window(
    shop: Shop, hours: int, due: list[int], not_due: list[int]
) -> None:
    """From the lead time down to halfway to the 24-hour reminder."""
    set_business(shop, plan="pro", reminder_early_hours=hours)
    wanted = [add(shop, offset * HOUR, booked_ago=10 * DAY) for offset in due]
    others = [add(shop, offset * HOUR, booked_ago=10 * DAY) for offset in not_due]
    shop.reminders.run()
    assert len(reminded(shop)) == len(wanted)
    assert all(early(shop, booking)[0] for booking in wanted)
    assert not any(early(shop, booking)[0] for booking in others)


def test_a_booking_made_inside_the_lead_time_gets_no_earlier_reminder(shop: Shop) -> None:
    """Booked 60 hours ahead: the confirmation has just gone out, so only the 24-hour reminder will follow."""
    set_business(shop, plan="pro")
    add(shop, 60 * HOUR, booked_ago=timedelta(minutes=5))
    shop.reminders.run()
    assert reminded(shop) == []


@pytest.mark.parametrize(("column", "offset"), [("reminder_day_enabled", 23), ("reminder_early_enabled", 71)])
def test_a_switched_off_reminder_is_not_sent(shop: Shop, column: str, offset: int) -> None:
    """Each switch stops its own reminder."""
    set_business(shop, plan="pro", **{column: False})
    add(shop, offset * HOUR, booked_ago=10 * DAY)
    shop.reminders.run()
    assert reminded(shop) == []


def test_both_reminders_reach_the_same_booking(shop: Shop) -> None:
    """After the earlier reminder, the same booking a day away also gets the 24-hour one."""
    set_business(shop, plan="pro")
    booking = add(shop, 71 * HOUR, booked_ago=10 * DAY)
    shop.reminders.run()
    with shop.database.connection() as connection:
        connection.execute(
            "UPDATE bookings SET starts_at = starts_at - interval '48 hours', ends_at = ends_at - interval "
            "'48 hours', blocked_until = blocked_until - interval '48 hours' WHERE id = %s",
            (booking,),
        )
    shop.reminders.run()
    assert len(reminded(shop)) == 2
    assert early(shop, booking)[2:] == (True, True)
    assert logged(shop) == [("reminder_early", "customer", "sent"), ("reminder", "customer", "sent")]


def test_a_failed_earlier_reminder_is_retried_on_its_own(shop: Shop) -> None:
    """A failure clears only the earlier claim, is not recorded as sent, and a later run sends it."""
    set_business(shop, plan="pro")
    booking = add(shop, 71 * HOUR, booked_ago=10 * DAY)
    shop.mailer.fail = True
    shop.reminders.run()
    assert early(shop, booking) == (False, 1, False, False)
    shop.mailer.fail = False
    shop.reminders.run()
    assert early(shop, booking) == (True, 2, True, False)


def test_the_note_is_in_the_reminder_as_text(shop: Shop) -> None:
    """The business's note is included, and markup in it is shown, not run."""
    set_business(shop, reminder_note="Bring <your> ID")
    add(shop, 23 * HOUR)
    shop.reminders.run()
    message = next(m for m in shop.mailer.sent if m.to in shop.emails)
    assert "Note from Test & Co" in message.text and "Bring <your> ID" in message.text
    assert "Bring &lt;your&gt; ID" in message.html


def test_a_real_move_resets_the_earlier_reminder_attempts(world: World) -> None:  # noqa: F811
    """Moving through a manage link lets the earlier reminder be tried afresh for the new time."""
    reference, token = book(world)
    with world.database.connection() as connection:
        connection.execute("UPDATE bookings SET reminder_early_attempts = 3 WHERE reference = %s", (reference,))
    world.manage.move(world.slug, reference, token, world.day, "12:00")  # type: ignore[arg-type]
    with world.database.connection() as connection:
        row = connection.execute(
            "SELECT reminder_early_attempts FROM bookings WHERE reference = %s", (reference,)
        ).fetchone()
    assert row is not None and row[0] == 0


def test_owners_change_reminders_within_their_plan(shop: Shop) -> None:
    """A free owner changes the 24-hour reminder and the note only; a paid owner changes everything."""
    account = str(uuid.uuid4())
    with shop.database.connection() as connection:
        slug = connection.execute("SELECT slug FROM businesses WHERE id = %s", (shop.business_id,)).fetchone()
        assert slug is not None
        connection.execute(
            "INSERT INTO business_members (business_id, user_id, role) VALUES (%s, %s, 'owner')",
            (shop.business_id, account),
        )
    limited = LimitedDatabase(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    try:
        owners = OwnerService(limited)
        start = owners.reminder_settings(account, slug[0])
        assert (start.early_allowed, start.day_enabled, start.early_enabled, start.early_hours, start.note) == (
            False,
            True,
            True,
            72,
            None,
        )
        changed = owners.update_reminder_settings(account, slug[0], ReminderChanges(day_enabled=False, note="Hi"))
        assert (changed.day_enabled, changed.note) == (False, "Hi")
        for refused in (ReminderChanges(early_enabled=False), ReminderChanges(early_hours=48, note="No")):
            with pytest.raises(InvalidChangeError):
                owners.update_reminder_settings(account, slug[0], refused)
        assert owners.reminder_settings(account, slug[0]).note == "Hi"
        set_business(shop, plan="starter")
        paid = owners.update_reminder_settings(account, slug[0], ReminderChanges(early_hours=168, note=None))
        assert (paid.early_allowed, paid.early_hours, paid.note) == (True, 168, None)
        with pytest.raises(NotFoundError):
            owners.reminder_settings(str(uuid.uuid4()), slug[0])
        with pytest.raises(NotFoundError):
            owners.update_reminder_settings(str(uuid.uuid4()), slug[0], ReminderChanges(day_enabled=True))
    finally:
        limited.close()
        with shop.database.connection() as connection:
            connection.execute("DELETE FROM business_members WHERE business_id = %s", (shop.business_id,))
