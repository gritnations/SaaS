"""Tests for plan billing against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied. A
fake provider stands in for Stripe. Each test creates its own businesses and deletes everything it created.

What these tests establish: a business on a paid plan waits for payment with its booking page closed;
the checkout is built from server values; only a matching verified subscription event activates it, once;
renewal failures, recovery and cancellation move the plan as agreed; events for another subscription or an
unknown business change nothing; and only an unpaid paid plan can be switched to free from the dashboard.
"""

import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from booking.billing import BillingService
from booking.owner import InvalidChangeError, NewBusiness, OwnerService
from booking.service import BookingService, NotFoundError
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
        provider: The fake provider.
        owners: The owner service.
        billing: The billing service.
        bookings: The booking service.
    """

    database: Database
    provider: FakeBillingProvider
    owners: OwnerService
    billing: BillingService
    bookings: BookingService


@pytest.fixture
def world() -> Iterator[World]:
    """Open the database and remove every business the test created.

    Yields:
        The services. Created slugs start with ``plantest-``.
    """
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    provider = FakeBillingProvider()
    try:
        yield World(
            database,
            provider,
            OwnerService(database),
            BillingService(database, provider, SITE),
            BookingService(database),
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


def sign_up(world: World, plan: str) -> tuple[str, str, str]:
    """Create a business on a plan for a new account.

    Args:
        world: The services.
        plan: The plan.

    Returns:
        ``(account, slug, business_id)``.
    """
    account = str(uuid.uuid4())
    made = world.owners.create_business(
        account,
        NewBusiness(f"Plantest {secrets.token_hex(3)}", "salon", None, "Sample Owner", plan, "owner@example.com"),
    )
    with world.database.connection() as connection:
        row = connection.execute("SELECT id::text FROM businesses WHERE slug = %s", (made.slug,)).fetchone()
    assert row is not None
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


def test_a_paid_plan_waits_for_payment_with_its_booking_page_closed(world: World) -> None:
    """The membership says so, the public services are not found, and the checkout uses server values."""
    account, slug, business_id = sign_up(world, "starter")
    assert [(m.plan, m.plan_status) for m in world.owners.memberships(account)] == [("starter", "awaiting_payment")]
    with pytest.raises(NotFoundError):
        world.bookings.list_services(slug)
    assert world.billing.start_checkout(account, slug) == f"https://checkout.example.com/{business_id}"
    order = world.provider.orders[0]
    assert (order.business_id, order.plan, order.amount_minor, order.currency, order.customer_email) == (
        business_id,
        "starter",
        900,
        "GBP",
        "owner@example.com",
    )
    assert order.success_url == f"{SITE}/#billing=done"


def test_a_free_business_is_open_and_has_nothing_to_pay(world: World) -> None:
    """Free is active at once, its page is open, and no checkout or portal is offered."""
    account, slug, _ = sign_up(world, "free")
    assert plan_of(world, slug)[:2] == ("free", "active")
    world.bookings.list_services(slug)
    with pytest.raises(InvalidChangeError):
        world.billing.start_checkout(account, slug)
    with pytest.raises(InvalidChangeError):
        world.billing.portal(account, slug)


def test_a_matching_subscription_activates_the_plan_once(world: World) -> None:
    """The plan becomes active with its subscription and customer; a repeat changes nothing."""
    account, slug, business_id = sign_up(world, "pro")
    assert world.billing.record(event(business_id, "subscription_active", plan="pro")) is True
    assert plan_of(world, slug) == ("pro", "active", "cus_test_1", "sub_test_1")
    world.bookings.list_services(slug)
    assert world.billing.record(event(business_id, "subscription_active", plan="pro")) is False
    assert world.billing.portal(account, slug) == "https://billing.example.com/cus_test_1"


def test_renewal_failure_recovery_and_cancellation(world: World) -> None:
    """past_due on a failed renewal, active again when paid, back to free (customer kept) when it ends."""
    _, slug, business_id = sign_up(world, "starter")
    world.billing.record(event(business_id, "subscription_active"))
    assert world.billing.record(event(business_id, "subscription_past_due")) is True
    assert plan_of(world, slug)[:2] == ("starter", "past_due")
    world.bookings.list_services(slug)
    assert world.billing.record(event(business_id, "subscription_active")) is True
    assert plan_of(world, slug)[:2] == ("starter", "active")
    assert world.billing.record(event(business_id, "subscription_ended")) is True
    assert plan_of(world, slug) == ("free", "active", "cus_test_1", None)


def test_events_that_do_not_match_change_nothing(world: World) -> None:
    """Another subscription, an unknown or malformed business, or an unknown plan is ignored."""
    _, slug, business_id = sign_up(world, "starter")
    world.billing.record(event(business_id, "subscription_active"))
    assert world.billing.record(event(business_id, "subscription_past_due", subscription="sub_other")) is False
    assert world.billing.record(event(business_id, "subscription_ended", subscription="sub_other")) is False
    assert world.billing.record(event(business_id, "subscription_active", subscription="sub_other")) is False
    assert world.billing.record(event(str(uuid.uuid4()), "subscription_active")) is False
    assert world.billing.record(event("not-a-business", "subscription_active")) is False
    _, other_slug, other_id = sign_up(world, "starter")
    assert world.billing.record(event(other_id, "subscription_active", plan="enterprise")) is False
    assert plan_of(world, slug)[:2] == ("starter", "active")
    assert plan_of(world, other_slug)[:2] == ("starter", "awaiting_payment")


def test_only_an_unpaid_plan_can_switch_to_free_from_the_dashboard(world: World) -> None:
    """Awaiting payment can choose free; a paid plan must be cancelled through the portal; others get 404."""
    account, slug, business_id = sign_up(world, "pro")
    with pytest.raises(NotFoundError):
        world.billing.choose_free(str(uuid.uuid4()), slug)
    assert world.billing.choose_free(account, slug).plan == "free"
    assert plan_of(world, slug)[:2] == ("free", "active")
    paid_account, paid_slug, paid_id = sign_up(world, "pro")
    world.billing.record(event(paid_id, "subscription_active", plan="pro"))
    with pytest.raises(InvalidChangeError):
        world.billing.choose_free(paid_account, paid_slug)
