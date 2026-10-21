"""Tests for plans, trials and payment options against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied. Fake
providers stand in for Stripe. Each test creates its own businesses and deletes everything it created.

What these tests establish: a new business starts free with a 14-day trial that includes online payments,
its booking page open; an upgrade checkout is built from server values and only a matching verified
subscription event changes the plan, once; renewal failures, recovery and cancellation move the plan as
agreed; events for another subscription or an unknown business change nothing; online payments need a paid
plan or the trial; and each business chooses how its customers pay, with paying at the venue confirmed at
once. Online payments work on every plan for now; the plan rule is still tested with its switch on.
"""

import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from test_payments_postgres import FakeProvider

from booking.billing import BillingService
from booking.owner import InvalidChangeError, NewBusiness, OwnerService
from booking.payments import InvalidOptionsError, PaymentService, PlanRequiredError
from booking.service import (
    TRIAL_DAYS,
    BookingRequest,
    BookingService,
    PaymentUnavailableError,
)
from payments.base import HostedCheckout, PaymentEvent, SubscriptionOrder
from postgres import Database, DatabaseSettings

URL_ENV = "BOOKING_TEST_DATABASE_URL"
SITE = "https://book.example.com"

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")


@dataclass
class FakeBillingProvider:
    """Records subscription orders and answers without any network.

    Attributes:
        name: The provider name.
        live: Never live.
        orders: Every order received.
    """

    name: str = "stripe"
    live: bool = False
    orders: list[SubscriptionOrder] = field(default_factory=list)

    def start_subscription(self, order: SubscriptionOrder) -> HostedCheckout:
        """Record the order.

        Args:
            order: The order.

        Returns:
            A checkout.
        """
        self.orders.append(order)
        return HostedCheckout("cs_test_plan", f"https://checkout.example.com/{order.business_id}")

    def billing_portal_url(self, customer_id: str, return_url: str) -> str:
        """Give a portal link.

        Args:
            customer_id: The customer.
            return_url: Unused.

        Returns:
            The link.
        """
        return f"https://billing.example.com/{customer_id}"


@dataclass
class World:
    """Services bound to the test database.

    Attributes:
        database: The database boundary.
        provider: The fake billing provider.
        accounts: The fake payment-account provider.
        owners: The owner service.
        billing: The billing service.
        payments: The payment service.
        bookings: The booking service.
    """

    database: Database
    provider: FakeBillingProvider
    accounts: FakeProvider
    owners: OwnerService
    billing: BillingService
    payments: PaymentService
    bookings: BookingService


@pytest.fixture
def world() -> Iterator[World]:
    """Open the database and remove every business the test created.

    Yields:
        The services. Created slugs start with ``plantest-``.
    """
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    provider, accounts = FakeBillingProvider(), FakeProvider()
    try:
        yield World(
            database,
            provider,
            accounts,
            OwnerService(database),
            BillingService(database, provider, SITE),
            PaymentService(database, accounts, SITE),
            BookingService(database, payments=accounts, site_url=SITE),
        )
    finally:
        with database.connection() as connection:
            ids = [
                str(row[0])
                for row in connection.execute("SELECT id FROM businesses WHERE slug LIKE 'plantest-%'").fetchall()
            ]
            for table in ("email_log", "bookings", "business_members", "weekly_hours", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = ANY(%s::uuid[])", (ids,))
            connection.execute("DELETE FROM businesses WHERE id = ANY(%s::uuid[])", (ids,))
        database.close()


def sign_up(world: World) -> tuple[str, str, str]:
    """Create a business for a new account, with a paid service open every day.

    Args:
        world: The services.

    Returns:
        ``(account, slug, business_id)``.
    """
    account = str(uuid.uuid4())
    made = world.owners.create_business(
        account,
        NewBusiness(f"Plantest {secrets.token_hex(3)}", "salon", None, "Sample Owner", "free", "owner@example.com"),
    )
    with world.database.connection() as connection:
        row = connection.execute("SELECT id::text FROM businesses WHERE slug = %s", (made.slug,)).fetchone()
        assert row is not None
        connection.execute(
            "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor) "
            "VALUES (%s, 'cut', 'Haircut', 60, 2500)",
            (row[0],),
        )
        connection.execute("UPDATE businesses SET notice_minutes = 0 WHERE id = %s", (row[0],))
        for weekday in (0, 6):
            connection.execute(
                "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) VALUES (%s, %s, '09:00', '17:00')",
                (row[0], weekday),
            )
    return account, made.slug, row[0]


def plan_of(world: World, slug: str) -> tuple[str, str, str | None, str | None]:
    """Read a business's plan fields.

    Args:
        world: The services.
        slug: The business.

    Returns:
        ``(plan, plan_status, customer_id, subscription_id)``.
    """
    with world.database.connection() as connection:
        row = connection.execute(
            "SELECT plan, plan_status, billing_customer_id, billing_subscription_id FROM businesses WHERE slug = %s",
            (slug,),
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2], row[3]


def set_business(world: World, slug: str, assignments: str) -> None:
    """Change fields of a test business directly.

    Args:
        world: The services.
        slug: The business.
        assignments: SQL ``SET`` assignments, written by the test itself.
    """
    with world.database.connection() as connection:
        connection.execute(f"UPDATE businesses SET {assignments} WHERE slug = %s", (slug,))


def event(business_id: str, status: str, subscription: str = "sub_test_1", plan: str = "starter") -> PaymentEvent:
    """Build a verified subscription event.

    Args:
        business_id: The business.
        status: The subscription status.
        subscription: The subscription.
        plan: The plan.

    Returns:
        The event.
    """
    return PaymentEvent(
        provider="stripe",
        order_reference=business_id,
        status=status,
        live=False,
        subscription_id=subscription,
        customer_id="cus_test_1",
        plan=plan,
    )


def book(world: World, slug: str, start: str, pay: str | None = None) -> tuple[str, str]:
    """Book the paid service on the next Sunday or Saturday.

    Args:
        world: The services.
        slug: The business.
        start: Local start time.
        pay: The customer's choice.

    Returns:
        ``(status, payment_status)`` of the new booking.
    """
    day = datetime.now(UTC).date() + timedelta(days=2)
    while day.weekday() not in (5, 6):
        day += timedelta(days=1)
    made = world.bookings.create_booking(
        slug, BookingRequest("cut", day, start, "Test Customer", "customer@example.com", None, None, pay)
    )
    return made.status, made.payment_status


def test_a_new_business_starts_free_with_a_trial_and_is_open(world: World) -> None:
    """Free and active, a 14-day trial that includes online payments, and its booking page open."""
    account, slug, _ = sign_up(world)
    [membership] = world.owners.memberships(account)
    assert (membership.plan, membership.plan_status, membership.online_payments) == ("free", "active", True)
    assert membership.trial_ends_at is not None
    left = membership.trial_ends_at - datetime.now(UTC)
    assert timedelta(days=TRIAL_DAYS - 1) < left <= timedelta(days=TRIAL_DAYS)
    world.bookings.list_services(slug)


def test_an_upgrade_is_built_from_server_values_and_keeps_the_business_open(world: World) -> None:
    """The checkout carries the server price; the plan changes only when the subscription is confirmed."""
    account, slug, business_id = sign_up(world)
    assert world.billing.upgrade(account, slug, "pro") == f"https://checkout.example.com/{business_id}"
    order = world.provider.orders[0]
    assert (order.business_id, order.plan, order.amount_minor, order.currency) == (business_id, "pro", 2900, "GBP")
    assert plan_of(world, slug)[:2] == ("free", "active")
    world.bookings.list_services(slug)
    with pytest.raises(InvalidChangeError):
        world.billing.upgrade(account, slug, "enterprise")
    with pytest.raises(InvalidChangeError):
        world.billing.portal(account, slug)


def test_a_matching_subscription_activates_the_plan_once(world: World) -> None:
    """The plan becomes active with its subscription and customer; a repeat changes nothing; a second
    upgrade is sent to the portal."""
    account, slug, business_id = sign_up(world)
    assert world.billing.record(event(business_id, "subscription_active", plan="pro")) is True
    assert plan_of(world, slug) == ("pro", "active", "cus_test_1", "sub_test_1")
    assert world.billing.record(event(business_id, "subscription_active", plan="pro")) is False
    assert world.billing.portal(account, slug) == "https://billing.example.com/cus_test_1"
    with pytest.raises(InvalidChangeError):
        world.billing.upgrade(account, slug, "starter")


def test_renewal_failure_recovery_and_cancellation(world: World) -> None:
    """past_due on a failed renewal, active again when paid, back to free (customer kept) when it ends."""
    _, slug, business_id = sign_up(world)
    world.billing.record(event(business_id, "subscription_active"))
    assert world.billing.record(event(business_id, "subscription_past_due")) is True
    assert plan_of(world, slug)[:2] == ("starter", "past_due")
    assert world.billing.record(event(business_id, "subscription_active")) is True
    assert plan_of(world, slug)[:2] == ("starter", "active")
    assert world.billing.record(event(business_id, "subscription_ended")) is True
    assert plan_of(world, slug) == ("free", "active", "cus_test_1", None)


def test_events_that_do_not_match_change_nothing(world: World) -> None:
    """Another subscription, an unknown or malformed business, or an unknown plan is ignored."""
    _, slug, business_id = sign_up(world)
    world.billing.record(event(business_id, "subscription_active"))
    assert world.billing.record(event(business_id, "subscription_past_due", subscription="sub_other")) is False
    assert world.billing.record(event(business_id, "subscription_ended", subscription="sub_other")) is False
    assert world.billing.record(event(business_id, "subscription_active", subscription="sub_other")) is False
    assert world.billing.record(event(str(uuid.uuid4()), "subscription_active")) is False
    assert world.billing.record(event("not-a-business", "subscription_active")) is False
    _, other_slug, other_id = sign_up(world)
    assert world.billing.record(event(other_id, "subscription_active", plan="enterprise")) is False
    assert plan_of(world, slug)[:2] == ("starter", "active")
    assert plan_of(world, other_slug)[:2] == ("free", "active")


def test_a_new_business_takes_paid_bookings_at_the_venue(world: World) -> None:
    """By default a paid service is confirmed at once and marked pay at the venue; online is not offered."""
    _, slug, _ = sign_up(world)
    business, _ = world.bookings.list_services(slug)
    assert business.payment_options == ("venue",)
    assert book(world, slug, "09:00") == ("confirmed", "at_venue")
    with pytest.raises(PaymentUnavailableError):
        book(world, slug, "11:00", pay="online")


def test_online_payments_work_on_every_plan_for_now(world: World) -> None:
    """With the plan rule off, a free business whose trial has ended can still connect Stripe, switch
    online payment on and be paid online."""
    account, slug, _ = sign_up(world)
    set_business(world, slug, "trial_ends_at = now() - interval '1 minute'")
    world.payments.connect(account, slug)
    assert world.payments.settings(account, slug).enabled is True
    options = world.payments.set_options(account, slug, True, None)
    assert (options.pay_online, options.pay_at_venue, options.online_allowed) == (True, True, True)
    assert world.bookings.list_services(slug)[0].payment_options == ("online", "venue")
    assert book(world, slug, "09:00") == ("pending_payment", "awaiting")
    assert book(world, slug, "11:00", pay="venue") == ("confirmed", "at_venue")
    assert world.owners.memberships(account)[0].online_payments is True


def test_the_plan_rule_still_works_when_switched_on(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the switch on: the trial allows online payments; after it, connecting and switching online on
    are refused and an online choice stops counting until a paid plan restores it."""
    monkeypatch.setattr("booking.service.ONLINE_PAYMENTS_NEED_PAID_PLAN", True)
    account, slug, business_id = sign_up(world)
    world.payments.connect(account, slug)
    world.payments.settings(account, slug)
    world.payments.set_options(account, slug, True, None)
    assert world.bookings.list_services(slug)[0].payment_options == ("online", "venue")
    set_business(world, slug, "trial_ends_at = now() - interval '1 minute'")
    assert world.bookings.list_services(slug)[0].payment_options == ("venue",)
    assert book(world, slug, "13:00") == ("confirmed", "at_venue")
    with pytest.raises(PlanRequiredError):
        world.payments.set_options(account, slug, True, None)
    with pytest.raises(PlanRequiredError):
        world.payments.connect(account, slug)
    world.billing.record(event(business_id, "subscription_active"))
    assert world.bookings.list_services(slug)[0].payment_options == ("online", "venue")


def test_payment_options_must_leave_a_way_to_pay_and_need_a_ready_account(world: World) -> None:
    """Online cannot be switched on before Stripe is ready; switching everything off is refused; online
    only removes the venue option."""
    account, slug, _ = sign_up(world)
    with pytest.raises(InvalidOptionsError):
        world.payments.set_options(account, slug, True, None)
    with pytest.raises(InvalidOptionsError):
        world.payments.set_options(account, slug, None, False)
    world.payments.connect(account, slug)
    world.payments.settings(account, slug)
    world.payments.set_options(account, slug, True, False)
    assert world.bookings.list_services(slug)[0].payment_options == ("online",)
    with pytest.raises(PaymentUnavailableError):
        book(world, slug, "09:00", pay="venue")
