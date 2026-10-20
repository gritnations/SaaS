"""Tests for paid bookings and payment events against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied. A
fake provider stands in for Stripe. Each test creates its own business and deletes everything it created.

What these tests establish: a paid booking is a hold that blocks its time and carries a checkout built from
database values; only a matching verified payment confirms it, once; a mismatched amount, account or
reference changes nothing; an expired or abandoned hold frees its time; a payment that arrives after its
time was taken is kept for a refund instead of double-booking; a failed checkout releases the hold at once;
and a business gets exactly one payment account.
"""

import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from booking.owner import OwnerService
from booking.payments import PaymentService
from booking.service import (
    CHECKOUT_MINUTES,
    HOLD_MINUTES,
    BookingRequest,
    BookingService,
    InvalidSlotError,
    PaymentUnavailableError,
)
from payments.base import CheckoutOrder, HostedCheckout, PaymentEvent, ProviderUnavailableError
from postgres import Database, DatabaseSettings

URL_ENV = "BOOKING_TEST_DATABASE_URL"
SITE = "https://book.example.com"
ACCOUNT = "acct_1TestBusiness"

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")


@dataclass
class FakeProvider:
    """A payment provider that records what it is asked and answers without any network.

    Attributes:
        name: The provider name.
        live: Never live.
        orders: Every checkout order received.
        created: Every account created.
        ready: The readiness answer.
        fail_checkout: Whether starting a checkout fails.
    """

    name: str = "stripe"
    live: bool = False
    orders: list[CheckoutOrder] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    ready: bool = True
    fail_checkout: bool = False

    def create_account(self, business_name: str, contact_email: str | None, currency: str) -> str:
        """Create an account.

        Args:
            business_name: The business.
            contact_email: Unused.
            currency: Unused.

        Returns:
            A new account identifier.
        """
        self.created.append(business_name)
        return f"acct_new{secrets.token_hex(4)}"

    def onboarding_url(self, account_id: str, return_url: str, refresh_url: str) -> str:
        """Give a link.

        Args:
            account_id: The account.
            return_url: Unused.
            refresh_url: Unused.

        Returns:
            The link.
        """
        return f"https://connect.example.com/{account_id}"

    def can_take_payments(self, account_id: str) -> bool:
        """Report readiness.

        Args:
            account_id: Unused.

        Returns:
            :attr:`ready`.
        """
        return self.ready

    def start_checkout(self, order: CheckoutOrder) -> HostedCheckout:
        """Create a checkout, or fail.

        Args:
            order: The order.

        Returns:
            The checkout.

        Raises:
            ProviderUnavailableError: When :attr:`fail_checkout` is set.
        """
        if self.fail_checkout:
            raise ProviderUnavailableError("Stripe could not create checkout.")
        self.orders.append(order)
        return HostedCheckout(f"cs_test_{order.reference}", f"https://checkout.example.com/{order.reference}")


@dataclass
class RecordingNotifier:
    """Records each booking event instead of sending email.

    Attributes:
        events: ``(kind, booking_id, token)`` for each event.
    """

    events: list[tuple[str, str, str | None]] = field(default_factory=list)

    def booking_event(self, kind: str, booking_id: str, manage_token: str | None) -> None:
        """Record the event.

        Args:
            kind: The event.
            booking_id: The booking.
            manage_token: The manage code.
        """
        self.events.append((kind, booking_id, manage_token))


@dataclass
class World:
    """One temporary business that takes payments, and the services bound to it.

    Attributes:
        database: The database boundary.
        provider: The fake provider.
        notifier: The recording notifier.
        bookings: The booking service.
        payments: The payment service.
        slug: The business slug.
        owner: The owner's account.
        day: A bookable local date.
    """

    database: Database
    provider: FakeProvider
    notifier: RecordingNotifier
    bookings: BookingService
    payments: PaymentService
    slug: str
    owner: str
    day: date


@pytest.fixture
def world() -> Iterator[World]:
    """Create a business open every day 08:00-20:00 with a paid 60-minute service and a payment account.

    Yields:
        The temporary business.
    """
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    slug = "paytest-" + secrets.token_hex(5)
    owner = str(uuid.uuid4())
    with database.connection() as connection:
        row = connection.execute(
            "INSERT INTO businesses (slug, name, notice_minutes, payment_account_id, payments_enabled, pay_online, "
            "trial_ends_at) VALUES (%s, 'Pay test', 0, %s, true, true, now() + interval '14 days') RETURNING id::text",
            (slug, ACCOUNT + secrets.token_hex(4)),
        ).fetchone()
        assert row is not None
        business_id = row[0]
        connection.execute(
            "INSERT INTO business_members (business_id, user_id, role) VALUES (%s, %s, 'owner')",
            (business_id, owner),
        )
        connection.execute(
            "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor) VALUES "
            "(%s, 'cut', 'Haircut', 60, 2500), (%s, 'free', 'Chat', 60, 0)",
            (business_id, business_id),
        )
        for weekday in range(7):
            connection.execute(
                "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) VALUES (%s, %s, '08:00', '20:00')",
                (business_id, weekday),
            )
    provider, notifier = FakeProvider(), RecordingNotifier()
    try:
        yield World(
            database,
            provider,
            notifier,
            BookingService(database, notifier=notifier, payments=provider, site_url=SITE),
            PaymentService(database, provider, SITE, notifier=notifier),
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


def book(world: World, start: str = "10:00", service: str = "cut") -> tuple[str, str | None]:
    """Book a session on the test day.

    Args:
        world: The temporary business.
        start: Local start time.
        service: Service slug.

    Returns:
        The reference and the checkout link.
    """
    request = BookingRequest(service, world.day, start, "Test Customer", "customer@example.com", None, None)
    made = world.bookings.create_booking(world.slug, request)
    return made.reference, made.checkout_url


def row(world: World, reference: str) -> tuple[str, str, str | None, bool]:
    """Read a booking's state.

    Args:
        world: The temporary business.
        reference: The booking.

    Returns:
        ``(status, payment_status, checkout_id, has_manage_hash)``.
    """
    with world.database.connection() as connection:
        found = connection.execute(
            "SELECT status, payment_status, checkout_id, manage_token_hash IS NOT NULL FROM bookings WHERE reference = %s",
            (reference,),
        ).fetchone()
    assert found is not None
    return found[0], found[1], found[2], found[3]


def account(world: World) -> str:
    """Read the business's payment account.

    Args:
        world: The temporary business.

    Returns:
        The account identifier.
    """
    with world.database.connection() as connection:
        found = connection.execute(
            "SELECT payment_account_id FROM businesses WHERE slug = %s", (world.slug,)
        ).fetchone()
    assert found is not None and found[0]
    return str(found[0])


def paid(world: World, reference: str, **change: object) -> PaymentEvent:
    """Build the verified paid event for a booking.

    Args:
        world: The temporary business.
        reference: The booking.
        **change: Fields to alter.

    Returns:
        The event.
    """
    base = PaymentEvent(
        provider="stripe",
        order_reference=reference,
        status="paid",
        live=False,
        checkout_id=f"cs_test_{reference}",
        account_id=account(world),
        amount_minor=2500,
        currency="GBP",
        payment_id="pi_test_1",
    )
    return replace(base, **change)  # type: ignore[arg-type]


def free_times(world: World) -> set[str]:
    """List the available times of the paid service on the test day.

    Args:
        world: The temporary business.

    Returns:
        The available start times.
    """
    _, _, days = world.bookings.availability(world.slug, "cut", world.day, 1)
    return {slot.time for slot in days[0].slots if slot.available}


def test_a_paid_booking_is_a_hold_with_a_checkout_built_from_the_database(world: World) -> None:
    """The hold blocks its time; the order's amount, currency, account and expiry come from the server."""
    reference, link = book(world)
    assert link == f"https://checkout.example.com/{reference}"
    assert row(world, reference) == ("pending_payment", "awaiting", f"cs_test_{reference}", False)
    order = world.provider.orders[0]
    assert (order.amount_minor, order.currency, order.account_id) == (2500, "GBP", account(world))
    assert order.success_url == f"{SITE}/?b={world.slug}#paid={reference}"
    assert (
        timedelta(minutes=CHECKOUT_MINUTES - 1)
        < order.expires_at - datetime.now(UTC)
        < timedelta(minutes=CHECKOUT_MINUTES + 1)
    )
    with world.database.connection() as connection:
        held = connection.execute("SELECT hold_expires_at FROM bookings WHERE reference = %s", (reference,)).fetchone()
    assert held is not None and held[0] - order.expires_at == timedelta(minutes=HOLD_MINUTES - CHECKOUT_MINUTES)
    assert "10:00" not in free_times(world)
    with pytest.raises(InvalidSlotError):
        book(world)
    assert world.notifier.events == []


def test_a_matching_payment_confirms_the_booking_once(world: World) -> None:
    """The booking is confirmed with a new manage code and emailed; a repeat notification does nothing."""
    reference, _ = book(world)
    assert world.payments.record(paid(world, reference)) is True
    assert row(world, reference)[:2] == ("confirmed", "paid")
    assert row(world, reference)[3] is True
    [(kind, _, token)] = world.notifier.events
    assert kind == "confirmed" and token is not None and len(token) == 48
    assert world.payments.record(paid(world, reference)) is False
    assert len(world.notifier.events) == 1


@pytest.mark.parametrize(
    "change",
    [{"amount_minor": 100}, {"currency": "EUR"}, {"account_id": "acct_someoneelse"}, {"order_reference": "ZZZZZZZZZZ"}],
)
def test_a_mismatched_payment_changes_nothing(world: World, change: dict[str, object]) -> None:
    """A wrong amount, currency, account or reference never confirms a booking."""
    reference, _ = book(world)
    assert world.payments.record(paid(world, reference, **change)) is False
    assert row(world, reference)[:2] == ("pending_payment", "awaiting")
    assert world.notifier.events == []


def test_an_expired_or_failed_checkout_frees_the_time(world: World) -> None:
    """The provider's expiry or a failed payment releases the hold."""
    first, _ = book(world, "10:00")
    second, _ = book(world, "12:00")
    assert world.payments.record(paid(world, first, status="expired")) is True
    assert world.payments.record(paid(world, second, status="failed")) is True
    assert row(world, first)[0] == row(world, second)[0] == "expired"
    assert {"10:00", "12:00"} <= free_times(world)


def test_a_hold_that_ran_out_frees_its_time_and_a_late_payment_is_kept_for_refund(world: World) -> None:
    """Once the hold ends another customer can take the time; the first customer's late payment then
    leaves their booking expired, marked paid and listed to the owner, never double-booked."""
    late, _ = book(world)
    with world.database.connection() as connection:
        connection.execute(
            "UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE reference = %s", (late,)
        )
    assert "10:00" in free_times(world)
    taker, _ = book(world, service="free")
    assert row(world, late)[0] == "expired"
    assert world.payments.record(paid(world, late)) is True
    assert row(world, late)[:2] == ("expired", "paid")
    assert row(world, taker)[0] == "confirmed"
    _, listed = OwnerService(world.database).bookings(world.owner, world.slug, world.day, 1)
    assert {(b.reference, b.status, b.payment_status) for b in listed} == {
        (late, "expired", "paid"),
        (taker, "confirmed", "not_required"),
    }


def test_a_late_payment_still_confirms_when_the_time_is_free(world: World) -> None:
    """A hold that ran out but whose time nobody took is confirmed by its payment."""
    reference, _ = book(world)
    with world.database.connection() as connection:
        connection.execute(
            "UPDATE bookings SET status = 'expired', hold_expires_at = now() - interval '1 minute' "
            "WHERE reference = %s",
            (reference,),
        )
    assert world.payments.record(paid(world, reference)) is True
    assert row(world, reference)[:2] == ("confirmed", "paid")


def test_a_failed_checkout_releases_the_hold_at_once(world: World) -> None:
    """If the checkout cannot be created the customer is told and the time is free again."""
    world.provider.fail_checkout = True
    with pytest.raises(PaymentUnavailableError):
        book(world)
    assert "10:00" in free_times(world)


def test_paid_services_need_a_business_that_takes_payments(world: World) -> None:
    """With the account not ready a paid service is paid at the venue; with no venue option either it
    cannot be booked; a free one always can."""
    with world.database.connection() as connection:
        connection.execute("UPDATE businesses SET payments_enabled = false WHERE slug = %s", (world.slug,))
    reference, link = book(world)
    assert link is None and row(world, reference)[:2] == ("confirmed", "at_venue")
    with world.database.connection() as connection:
        connection.execute("UPDATE businesses SET pay_at_venue = false WHERE slug = %s", (world.slug,))
    with pytest.raises(PaymentUnavailableError):
        book(world, "12:00")
    reference, link = book(world, "14:00", service="free")
    assert link is None and row(world, reference)[:2] == ("confirmed", "not_required")


def test_a_business_gets_one_payment_account_and_readiness_is_refreshed(world: World) -> None:
    """Connecting twice creates one account; the owner's set-up follows the provider's readiness."""
    with world.database.connection() as connection:
        connection.execute(
            "UPDATE businesses SET payment_account_id = NULL, payments_enabled = false WHERE slug = %s", (world.slug,)
        )
    assert world.payments.settings(world.owner, world.slug).connected is False
    first = world.payments.connect(world.owner, world.slug)
    second = world.payments.connect(world.owner, world.slug)
    assert first == second and len(world.provider.created) == 1
    world.provider.ready = False
    assert world.payments.settings(world.owner, world.slug).enabled is False
    world.provider.ready = True
    assert world.payments.settings(world.owner, world.slug).enabled is True
    with pytest.raises(Exception):
        world.payments.connect(str(uuid.uuid4()), world.slug)
