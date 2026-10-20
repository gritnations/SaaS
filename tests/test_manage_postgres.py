"""Tests for manage links, owner cancel and booking emails against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied.
Each test creates its own business and deletes everything it created. A recording mailer stands in for
the email provider, so no email leaves the machine.

What these tests establish: only the right code manages a booking and only its hash is stored; the
cancel and move deadlines hold; a move cannot land on a taken time; the owner can cancel; and each event
sends the expected emails and records them, with a provider failure leaving the booking change in place.
"""

import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from booking.manage import ManageService, TooLateError
from booking.notify import Notifier
from booking.owner import OwnerService
from booking.service import BookingRequest, BookingService, InvalidSlotError, NotFoundError, hash_token
from mail import MailError, Message
from postgres import Database, DatabaseSettings

URL_ENV = "BOOKING_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")


class RecordingMailer:
    """Records messages instead of sending them, or fails on demand.

    Attributes:
        sent: Messages handed over, in order.
        fail: When ``True``, every send raises :class:`mail.MailError`.
    """

    def __init__(self) -> None:
        """Create the recorder."""
        self.sent: list[Message] = []
        self.fail = False

    def send(self, message: Message) -> str:
        """Record a message.

        Args:
            message: The email.

        Returns:
            A fake provider identifier.

        Raises:
            MailError: When :attr:`fail` is set.
        """
        if self.fail:
            raise MailError("provider refused the message (HTTP 500)")
        self.sent.append(message)
        return f"fake-{len(self.sent)}"


@dataclass
class World:
    """A temporary business with services wired to a recording mailer.

    Attributes:
        database: The database boundary.
        mailer: The recording mailer.
        bookings: Public booking service.
        manage: Manage-link service.
        owners: Owner service.
        slug: Business slug.
        owner: The owner's account identifier.
        day: A local date three days ahead with working hours.
    """

    database: Database
    mailer: RecordingMailer
    bookings: BookingService
    manage: ManageService
    owners: OwnerService
    slug: str
    owner: str
    day: object


@pytest.fixture
def world() -> Iterator[World]:
    """Create a business open every day 08:00-20:00, no break, 24-hour deadlines, with a contact email.

    Yields:
        The services and the business.
    """
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    slug = "test-" + secrets.token_hex(6)
    owner = str(uuid.uuid4())
    with database.connection() as connection:
        business_id = str(
            connection.execute(
                "INSERT INTO businesses (slug, name, notice_minutes, contact_email) "
                "VALUES (%s, 'Test & Co', 0, 'owner@example.com') RETURNING id",
                (slug,),
            ).fetchone()[
                0
            ]  # type: ignore[index]
        )
        connection.execute(
            "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor) VALUES (%s, 'hour', 'Hour', 60, 0)",
            (business_id,),
        )
        for weekday in range(7):
            connection.execute(
                "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) VALUES (%s, %s, '08:00', '20:00')",
                (business_id, weekday),
            )
        connection.execute(
            "INSERT INTO business_members (business_id, user_id, role) VALUES (%s, %s, 'owner')", (business_id, owner)
        )
    mailer = RecordingMailer()
    notifier = Notifier(database, mailer, "https://book.example.com")
    try:
        yield World(
            database,
            mailer,
            BookingService(database, notifier=notifier),
            ManageService(database, notifier=notifier),
            OwnerService(database, notifier=notifier),
            slug,
            owner,
            datetime.now(UTC).date() + timedelta(days=3),
        )
    finally:
        with database.connection() as connection:
            for table in ("email_log", "bookings", "business_members", "weekly_hours", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = %s", (business_id,))
            connection.execute("DELETE FROM businesses WHERE id = %s", (business_id,))
        database.close()


def book(world: World, start: str = "10:00") -> tuple[str, str]:
    """Book the hour service on the test day.

    Args:
        world: The test business.
        start: Local start time.

    Returns:
        ``(reference, manage_token)``.
    """
    confirmed = world.bookings.create_booking(
        world.slug, BookingRequest("hour", world.day, start, "Pat <b>Customer</b>", "pat@example.com", None, None)  # type: ignore[arg-type]
    )
    assert confirmed.manage_token is not None
    return confirmed.reference, confirmed.manage_token


def log(world: World) -> list[tuple[str, str, str]]:
    """Read the email log for the test business.

    Args:
        world: The test business.

    Returns:
        ``(kind, recipient_role, status)`` in order.
    """
    with world.database.connection() as connection:
        rows = connection.execute(
            "SELECT l.kind, l.recipient_role, l.status FROM email_log l JOIN businesses b ON b.id = l.business_id "
            "WHERE b.slug = %s ORDER BY l.created_at, l.recipient_role",
            (world.slug,),
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def test_booking_sends_both_emails_and_stores_only_the_code_hash(world: World) -> None:
    """The customer gets a manage link, the business gets the details, and the database holds only a hash."""
    reference, token = book(world)
    customer, owner = world.mailer.sent
    assert customer.to == "pat@example.com" and f"#manage={world.slug}.{reference}.{token}" in customer.text
    assert owner.to == "owner@example.com" and token not in owner.text
    assert "<b>Customer</b>" not in customer.html
    assert log(world) == [("confirmed", "customer", "sent"), ("confirmed", "owner", "sent")]
    with world.database.connection() as connection:
        stored = connection.execute(
            "SELECT manage_token_hash FROM bookings WHERE reference = %s", (reference,)
        ).fetchone()
    assert stored is not None and bytes(stored[0]) == hash_token(token)
    assert token.encode() not in bytes(stored[0])


def test_only_the_right_code_manages_a_booking(world: World) -> None:
    """A wrong code, another reference or another business all give the same not-found."""
    reference, token = book(world)
    assert world.manage.view(world.slug, reference.lower(), token).status == "confirmed"
    other_ref, _ = book(world, "12:00")
    for slug, ref, code in [
        (world.slug, reference, secrets.token_hex(24)),
        (world.slug, other_ref, token),
        ("example", reference, token),
    ]:
        with pytest.raises(NotFoundError):
            world.manage.view(slug, ref, code)


def test_cancel_emails_both_sides_and_is_idempotent(world: World) -> None:
    """Cancelling notifies the customer and business once; cancelling again changes and sends nothing."""
    reference, token = book(world)
    world.mailer.sent.clear()
    assert world.manage.cancel(world.slug, reference, token).status == "cancelled"
    assert [m.to for m in world.mailer.sent] == ["pat@example.com", "owner@example.com"]
    assert world.manage.cancel(world.slug, reference, token).status == "cancelled"
    assert len(world.mailer.sent) == 2
    _, _, days = world.bookings.availability(world.slug, "hour", world.day, 1)  # type: ignore[arg-type]
    assert {s.time: s.available for s in days[0].slots}["10:00"] is True


def test_deadlines_block_late_changes(world: World) -> None:
    """With a 4-day deadline, a booking three days ahead can no longer be cancelled or moved online."""
    reference, token = book(world)
    with world.database.connection() as connection:
        connection.execute(
            "UPDATE businesses SET cancel_cutoff_minutes = 5760, move_cutoff_minutes = 5760 WHERE slug = %s",
            (world.slug,),
        )
    view = world.manage.view(world.slug, reference, token)
    assert (view.can_cancel, view.can_move) == (False, False)
    with pytest.raises(TooLateError):
        world.manage.cancel(world.slug, reference, token)
    with pytest.raises(TooLateError):
        world.manage.move(world.slug, reference, token, world.day, "14:00")  # type: ignore[arg-type]


def test_move_to_a_free_time_works_and_to_a_taken_time_is_refused(world: World) -> None:
    """A move frees the old time, refuses a taken one and its own time, and updates the calendar sequence."""
    reference, token = book(world, "10:00")
    book(world, "15:00")
    with pytest.raises(InvalidSlotError):
        world.manage.move(world.slug, reference, token, world.day, "15:00")  # type: ignore[arg-type]
    with pytest.raises(InvalidSlotError):
        world.manage.move(world.slug, reference, token, world.day, "10:00")  # type: ignore[arg-type]
    world.mailer.sent.clear()
    moved = world.manage.move(world.slug, reference, token, world.day, "11:00")  # type: ignore[arg-type]
    assert moved.starts_at.astimezone(moved.business.zone).strftime("%H:%M") == "11:00"
    customer = world.mailer.sent[0]
    assert "Booking moved" in customer.subject and token in customer.text
    assert b"SEQUENCE:1" in customer.attachments[0].content


def test_owner_cancel_emails_only_the_customer(world: World) -> None:
    """The business cancelling its own booking emails the customer, not itself; another owner cannot."""
    reference, _ = book(world)
    with pytest.raises(NotFoundError):
        world.owners.cancel_booking(str(uuid.uuid4()), world.slug, reference)
    world.mailer.sent.clear()
    _, cancelled = world.owners.cancel_booking(world.owner, world.slug, reference)
    assert cancelled.status == "cancelled"
    assert [m.to for m in world.mailer.sent] == ["pat@example.com"]
    assert "by the business" in world.mailer.sent[0].text


def test_a_failed_email_never_undoes_the_booking(world: World) -> None:
    """When the provider fails, the booking still exists and the failure is logged."""
    world.mailer.fail = True
    reference, token = book(world)
    assert world.manage.view(world.slug, reference, token).status == "confirmed"
    assert log(world) == [("confirmed", "customer", "failed"), ("confirmed", "owner", "failed")]
