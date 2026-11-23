"""Tests for plans, trials and payment options against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied. Fake
providers stand in for Stripe. Each test creates its own businesses and deletes everything it created.

What these tests establish: a new business starts free with a 14-day trial that includes online payments,
its booking page open; an upgrade checkout is built from server values and only a matching verified
subscription event changes the plan, once; renewal failures, recovery and cancellation move the plan as
agreed; events for another subscription or an unknown business change nothing; each notice is applied from
the subscription's current state, read from the fake provider with the business locked, so a late or
reversed notice, a repeat, or a subscription started for another business changes nothing, and a failed
read changes nothing and can be retried; online payments need a paid plan or the trial; and each business
chooses how its customers pay, with paying at the venue confirmed at once. Online payments work on every plan for now; the plan rule is still tested with its switch on.
"""

import os
import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from test_payments_postgres import FakeProvider

from booking.billing import BillingService
from booking.owner import InvalidChangeError, NewBusiness, OwnerService
from booking.payments import InvalidOptionsError, PaymentService, PlanRequiredError
from booking.service import (
    TRIAL_DAYS,
    BookingRequest,
    BookingService,
    NotFoundError,
    PaymentUnavailableError,
)
from payments.base import (
    CancellationQuote,
    HostedCheckout,
    ProviderUnavailableError,
    SubscriptionEvent,
    SubscriptionOrder,
    SubscriptionState,
)
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
        quote: What every cancellation refunds.
        cancelled: The subscriptions cancelled.
        fail: If set, a cancellation raises it.
        states: Each subscription's current state, as Stripe would report it now.
        reads: The subscriptions whose state was read.
        read_fail: If set, a read raises it.
        gate: If set, a read takes the state, then waits for this before answering.
        reading: Set when a read is waiting at the gate.
    """

    name: str = "stripe"
    live: bool = False
    orders: list[SubscriptionOrder] = field(default_factory=list)
    quote: CancellationQuote = CancellationQuote(refund_minor=450, currency="GBP", unused_days=15)
    cancelled: list[str] = field(default_factory=list)
    fail: Exception | None = None
    states: dict[str, SubscriptionState] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    read_fail: Exception | None = None
    gate: threading.Event | None = None
    reading: threading.Event = field(default_factory=threading.Event)

    def start_subscription(self, order: SubscriptionOrder) -> HostedCheckout:
        """Record the order.

        Args:
            order: The order.

        Returns:
            A checkout.
        """
        self.orders.append(order)
        return HostedCheckout(
            checkout_id="cs_test_plan", checkout_url=f"https://checkout.example.com/{order.business_id}"
        )

    def billing_portal_url(self, customer_id: str, return_url: str) -> str:
        """Give a portal link.

        Args:
            customer_id: The customer.
            return_url: Unused.

        Returns:
            The link.
        """
        return f"https://billing.example.com/{customer_id}"

    def quote_cancellation(self, subscription_id: str, now: datetime) -> CancellationQuote:
        """Give the fixed quote.

        Args:
            subscription_id: The subscription.
            now: Unused.

        Returns:
            The quote.
        """
        return self.quote

    def cancel_with_refund(self, subscription_id: str, now: datetime) -> CancellationQuote:
        """Record the cancellation, or raise the configured failure.

        Args:
            subscription_id: The subscription.
            now: Unused.

        Returns:
            The refund made.

        Raises:
            Exception: The configured failure.
        """
        if self.fail is not None:
            raise self.fail
        self.cancelled.append(subscription_id)
        if subscription_id in self.states:
            self.states[subscription_id] = self.states[subscription_id].model_copy(
                update={"status": "subscription_ended", "ends_at": None}
            )
        return self.quote

    def subscription_state(self, subscription_id: str) -> SubscriptionState:
        """Report the subscription's state now, or raise the configured failure.

        Args:
            subscription_id: The subscription.

        Returns:
            Its state. An unknown subscription has no status.

        Raises:
            Exception: The configured failure.
        """
        self.reads.append(subscription_id)
        if self.read_fail is not None:
            raise self.read_fail
        state = self.states.get(subscription_id, SubscriptionState(status=None))
        if self.gate is not None:
            self.reading.set()
            self.gate.wait(timeout=10)
        return state


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
            connection.execute("DELETE FROM payment_events WHERE event_id LIKE 'evt_plantest_%'")
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
        NewBusiness(
            name=f"Plantest {secrets.token_hex(3)}",
            business_type="salon",
            owner_name="Sample Owner",
            contact_email="owner@example.com",
        ),
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


def test_a_plan_is_billed_in_pounds_for_a_business_outside_the_uk(world: World) -> None:
    """Plan prices are set in pounds only, so a Saudi business is billed in pounds, not riyals."""
    account = str(uuid.uuid4())
    made = world.owners.create_business(
        account,
        NewBusiness(
            name=f"Plantest {secrets.token_hex(3)}",
            business_type="salon",
            owner_name="Sample Owner",
            contact_email="owner@example.com",
            region="sa",
        ),
    )
    world.billing.upgrade(account, made.slug, "starter")
    order = world.provider.orders[-1]
    assert (order.amount_minor, order.currency) == (900, "GBP")


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


Status = Literal["subscription_active", "subscription_past_due", "subscription_ended"]


def event(
    world: World,
    business_id: str,
    status: Status,
    subscription: str = "sub_test_1",
    plan: str = "starter",
    ends_at: datetime | None = None,
    stale: bool = False,
    event_id: str | None = None,
) -> SubscriptionEvent:
    """Build a verified subscription notice and, unless it is stale, make Stripe's current state match it.

    Args:
        world: The services.
        business_id: The business.
        status: The subscription status the notice reports.
        subscription: The subscription.
        plan: The plan.
        ends_at: When the subscription is set to end.
        stale: ``True`` for a notice that arrives late: Stripe's current state is left as it is.
        event_id: Stripe's identifier for the notice, or ``None``.

    Returns:
        The notice.
    """
    if not stale:
        world.provider.states[subscription] = SubscriptionState(
            status=status, business_id=uuid.UUID(business_id), plan=plan, ends_at=ends_at
        )
    return SubscriptionEvent(
        provider="stripe",
        business_id=uuid.UUID(business_id),
        status=status,
        live=False,
        subscription_id=subscription,
        customer_id="cus_test_1",
        plan=plan,
        ends_at=ends_at,
        ends_known=True,
        event_id=event_id,
    )


def event_id() -> str:
    """Make a notice identifier that the test fixture removes afterwards.

    Returns:
        The identifier.
    """
    return f"evt_plantest_{secrets.token_hex(6)}"


def ends_of(world: World, slug: str) -> datetime | None:
    """Read when a business's plan is set to end.

    Args:
        world: The services.
        slug: The business.

    Returns:
        The end, or ``None``.
    """
    with world.database.connection() as connection:
        row = connection.execute("SELECT plan_ends_at FROM businesses WHERE slug = %s", (slug,)).fetchone()
    assert row is not None
    return row[0] if row[0] is None else row[0].astimezone(UTC)


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
        slug,
        BookingRequest(
            service="cut",
            date=day,
            time=start,
            name="Test Customer",
            email="customer@example.com",
            phone=None,
            note=None,
            pay=pay,
        ),
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
    assert world.billing.record(event(world, business_id, "subscription_active", plan="pro")) is True
    assert plan_of(world, slug) == ("pro", "active", "cus_test_1", "sub_test_1")
    assert world.billing.record(event(world, business_id, "subscription_active", plan="pro")) is False
    assert world.billing.portal(account, slug) == "https://billing.example.com/cus_test_1"
    with pytest.raises(InvalidChangeError):
        world.billing.upgrade(account, slug, "starter")


def test_cancelling_a_paid_plan_refunds_and_moves_the_business_to_free(world: World) -> None:
    """The owner sees the refund first; cancelling refunds through the provider, ends the plan at once and
    clears the subscription, so the provider's later 'ended' notice changes nothing."""
    account, slug, business_id = sign_up(world)
    world.billing.record(event(world, business_id, "subscription_active", plan="pro"))
    assert world.billing.cancel_quote(account, slug) == world.provider.quote
    assert world.billing.cancel_plan(account, slug) == world.provider.quote
    assert world.provider.cancelled == ["sub_test_1"]
    assert plan_of(world, slug) == ("free", "active", "cus_test_1", None)
    with world.database.connection() as connection:
        row = connection.execute("SELECT plan_ends_at FROM businesses WHERE slug = %s", (slug,)).fetchone()
    assert row is not None and row[0] is None
    assert world.billing.record(event(world, business_id, "subscription_ended")) is False
    with pytest.raises(InvalidChangeError):
        world.billing.cancel_plan(account, slug)


def test_only_the_owner_of_a_paid_plan_can_cancel_it(world: World) -> None:
    """A free business has nothing to cancel; another account cannot reach the business; neither reaches Stripe."""
    account, slug, business_id = sign_up(world)
    with pytest.raises(InvalidChangeError):
        world.billing.cancel_quote(account, slug)
    with pytest.raises(InvalidChangeError):
        world.billing.cancel_plan(account, slug)
    world.billing.record(event(world, business_id, "subscription_active"))
    with pytest.raises(NotFoundError):
        world.billing.cancel_plan(str(uuid.uuid4()), slug)
    with pytest.raises(NotFoundError):
        world.billing.cancel_quote(str(uuid.uuid4()), slug)
    assert world.provider.cancelled == [] and plan_of(world, slug)[:2] == ("starter", "active")


def test_a_failed_cancellation_leaves_the_plan_unchanged(world: World) -> None:
    """If the provider cannot refund or cancel, the business keeps its plan and subscription, and can retry."""
    account, slug, business_id = sign_up(world)
    world.billing.record(event(world, business_id, "subscription_active"))
    world.provider.fail = ProviderUnavailableError("Stripe could not refund.")
    with pytest.raises(ProviderUnavailableError):
        world.billing.cancel_plan(account, slug)
    assert plan_of(world, slug) == ("starter", "active", "cus_test_1", "sub_test_1")
    world.provider.fail = None
    world.billing.cancel_plan(account, slug)
    assert plan_of(world, slug)[0] == "free"


def test_renewal_failure_recovery_and_cancellation(world: World) -> None:
    """past_due on a failed renewal, active again when paid, back to free (customer kept) when it ends."""
    _, slug, business_id = sign_up(world)
    world.billing.record(event(world, business_id, "subscription_active"))
    assert world.billing.record(event(world, business_id, "subscription_past_due")) is True
    assert plan_of(world, slug)[:2] == ("starter", "past_due")
    assert world.billing.record(event(world, business_id, "subscription_active")) is True
    assert plan_of(world, slug)[:2] == ("starter", "active")
    assert world.billing.record(event(world, business_id, "subscription_ended")) is True
    assert plan_of(world, slug) == ("free", "active", "cus_test_1", None)


def test_events_that_do_not_match_change_nothing(world: World) -> None:
    """Another subscription, an unknown or malformed business, or an unknown plan is ignored."""
    _, slug, business_id = sign_up(world)
    world.billing.record(event(world, business_id, "subscription_active"))
    assert world.billing.record(event(world, business_id, "subscription_past_due", subscription="sub_other")) is False
    assert world.billing.record(event(world, business_id, "subscription_ended", subscription="sub_other")) is False
    assert world.billing.record(event(world, business_id, "subscription_active", subscription="sub_other")) is False
    assert world.billing.record(event(world, str(uuid.uuid4()), "subscription_active")) is False
    _, other_slug, other_id = sign_up(world)
    assert world.billing.record(event(world, other_id, "subscription_active", plan="enterprise")) is False
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
    world.billing.record(event(world, business_id, "subscription_active"))
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


def test_a_cancelled_plan_keeps_its_plan_and_reports_when_it_ends(world: World) -> None:
    """Cancelling at period end keeps the plan and records the date; undoing it clears the date; the end
    returns the business to Free and clears it too."""
    account, slug, business_id = sign_up(world)
    world.billing.record(event(world, business_id, "subscription_active"))
    ends = datetime(2026, 11, 20, tzinfo=UTC)
    assert world.billing.record(event(world, business_id, "subscription_active", ends_at=ends)) is True
    [membership] = world.owners.memberships(account)
    assert (membership.plan, membership.plan_status, membership.plan_ends_at) == ("starter", "active", ends)
    assert world.billing.record(event(world, business_id, "subscription_active", ends_at=ends)) is False
    assert world.billing.record(event(world, business_id, "subscription_active")) is True
    assert world.owners.memberships(account)[0].plan_ends_at is None
    world.billing.record(event(world, business_id, "subscription_active", ends_at=ends))
    world.billing.record(event(world, business_id, "subscription_ended"))
    [membership] = world.owners.memberships(account)
    assert (membership.plan, membership.plan_ends_at) == ("free", None)


def test_a_late_active_notice_after_the_owner_cancels_changes_nothing(world: World) -> None:
    """The owner cancels from the Plan screen; a delayed 'active' notice for the old subscription is read
    against Stripe, which says it has ended, so the business stays on Free."""
    account, slug, business_id = sign_up(world)
    assert world.billing.record(event(world, business_id, "subscription_active", plan="pro")) is True
    world.billing.cancel_plan(account, slug)
    late = event(world, business_id, "subscription_active", plan="pro", stale=True, event_id=event_id())
    assert world.billing.record(late) is False
    assert plan_of(world, slug) == ("free", "active", "cus_test_1", None)


def test_a_late_active_notice_after_the_subscription_ended_changes_nothing(world: World) -> None:
    """Stripe ends the subscription; a delayed 'active' notice for it changes nothing."""
    _, slug, business_id = sign_up(world)
    world.billing.record(event(world, business_id, "subscription_active"))
    assert world.billing.record(event(world, business_id, "subscription_ended")) is True
    late = event(world, business_id, "subscription_active", stale=True, event_id=event_id())
    assert world.billing.record(late) is False
    assert plan_of(world, slug) == ("free", "active", "cus_test_1", None)


def test_end_dates_follow_stripe_whatever_order_the_notices_arrive_in(world: World) -> None:
    """The owner sets the plan to cancel, then resumes it, and the 'set to cancel' notice arrives last: no
    end date is shown, because Stripe says the plan is not ending. When Stripe says it is ending, any
    notice brings the date in."""
    _, slug, business_id = sign_up(world)
    world.billing.record(event(world, business_id, "subscription_active"))
    ends = datetime(2026, 12, 20, tzinfo=UTC)
    cancelling = event(world, business_id, "subscription_active", ends_at=ends, stale=True, event_id=event_id())
    resumed = event(world, business_id, "subscription_active", event_id=event_id())
    assert world.billing.record(resumed) is False
    assert world.billing.record(cancelling) is False
    assert ends_of(world, slug) is None
    set_to_end(world, ends)
    assert world.billing.record(resumed) is True
    assert ends_of(world, slug) == ends
    assert plan_of(world, slug)[:2] == ("starter", "active")


def test_a_notice_that_changed_the_plan_is_recorded_and_a_repeat_is_ignored(world: World) -> None:
    """A repeat of a notice that changed the plan is ignored before Stripe is asked. A notice that changed
    nothing is not recorded, so a later delivery of it still applies."""
    _, slug, business_id = sign_up(world)
    first = event(world, business_id, "subscription_active", event_id=event_id())
    assert world.billing.record(first) is True
    reads = len(world.provider.reads)
    assert world.billing.record(first) is False
    assert len(world.provider.reads) == reads
    unchanged = event(world, business_id, "subscription_active", event_id=event_id())
    assert world.billing.record(unchanged) is False
    event(world, business_id, "subscription_past_due")
    assert world.billing.record(unchanged) is True
    assert plan_of(world, slug)[:2] == ("starter", "past_due")
    ids = [first.event_id, unchanged.event_id]
    with world.database.connection() as connection:
        rows = connection.execute("SELECT event_id FROM payment_events WHERE event_id = ANY(%s)", (ids,)).fetchall()
    assert {row[0] for row in rows} == set(ids)


def test_when_stripe_cannot_be_asked_nothing_changes_and_the_notice_can_be_retried(world: World) -> None:
    """The read fails: the error reaches the webhook (which answers 503), the plan is untouched and the
    notice is not recorded, so Stripe's retry applies it."""
    _, slug, business_id = sign_up(world)
    notice = event(world, business_id, "subscription_active", event_id=event_id())
    world.provider.read_fail = ProviderUnavailableError("Stripe could not read the subscription.")
    with pytest.raises(ProviderUnavailableError):
        world.billing.record(notice)
    assert plan_of(world, slug) == ("free", "active", None, None)
    world.provider.read_fail = None
    assert world.billing.record(notice) is True
    assert plan_of(world, slug)[:2] == ("starter", "active")


def test_a_subscription_that_belongs_to_another_business_changes_nothing(world: World) -> None:
    """The notice names this business, but Stripe's subscription was started for another one."""
    _, slug, business_id = sign_up(world)
    notice = event(world, business_id, "subscription_active")
    event(world, str(uuid.uuid4()), "subscription_active")
    assert world.billing.record(notice) is False
    assert plan_of(world, slug) == ("free", "active", None, None)


def test_two_notices_for_one_business_are_applied_one_after_the_other(world: World) -> None:
    """An 'active' notice has read Stripe and not yet applied it when the subscription ends and the 'ended'
    notice arrives. The second waits for the first, then reads Stripe again: the business ends on Free."""
    _, slug, business_id = sign_up(world)
    gate = threading.Event()
    world.provider.gate = gate
    outcomes: dict[str, object] = {}

    def run(name: str, notice: SubscriptionEvent) -> None:
        """Record a notice in a thread and keep its outcome.

        Args:
            name: The outcome's key.
            notice: The notice.
        """
        try:
            outcomes[name] = world.billing.record(notice)
        except Exception as exc:  # the test reports whatever happened
            outcomes[name] = exc

    first = threading.Thread(target=run, args=("active", event(world, business_id, "subscription_active")))
    first.start()
    try:
        assert world.provider.reading.wait(timeout=10)
        second = threading.Thread(target=run, args=("ended", event(world, business_id, "subscription_ended")))
        second.start()
        assert waiting_for_a_business(world), "the second notice did not wait for the first"
        assert world.provider.reads == ["sub_test_1"]
    finally:
        gate.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert outcomes == {"active": True, "ended": True}
    assert world.provider.reads == ["sub_test_1", "sub_test_1"]
    assert plan_of(world, slug) == ("free", "active", "cus_test_1", None)


def set_to_end(world: World, ends: datetime) -> None:
    """Make Stripe's current state say the subscription ends at ``ends``.

    Args:
        world: The services.
        ends: The end.
    """
    world.provider.states["sub_test_1"] = world.provider.states["sub_test_1"].model_copy(update={"ends_at": ends})


def waiting_for_a_business(world: World) -> bool:
    """Wait until a session is blocked reading a business's plan fields for update.

    Args:
        world: The services.

    Returns:
        Whether one was seen within ten seconds.
    """
    deadline = time.monotonic() + 10
    with world.database.connection() as connection:
        while time.monotonic() < deadline:
            connection.execute("SELECT pg_stat_clear_snapshot()")
            row = connection.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0 "
                "AND query LIKE %s",
                ("%billing_subscription_id, plan_ends_at FROM businesses%",),
            ).fetchone()
            if row is not None and row[0] > 0:
                return True
            time.sleep(0.05)
    return False
