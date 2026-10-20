"""Tests for plan billing in the Stripe adapter. No network: a fake client stands in for Stripe's API, and
webhook bodies are signed here with Stripe's published signature scheme.

What these tests establish: the billing secret is optional, must be distinct and well formed; a plan
checkout is a monthly subscription at the price decided on the server, reusing one Stripe price per plan
and amount; the portal link is for the business's customer; subscription notifications are read only when
signed by the platform destination's secret, and each secret is trusted only for its own kind of event.
They do not establish that Stripe accepts these requests: that is checked by hand in Stripe test mode.
"""

import json
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
import stripe
from test_stripe import ACCOUNT, PAID_SESSION, SECRET, event, sign

from payments.base import ProviderUnavailableError, SubscriptionBilling, SubscriptionOrder, WebhookRejectedError
from payments.stripe import StripeConfigError, StripeProvider, stripe_from_environment

BILLING_SECRET = "whsec_billing_secret"
BUSINESS = "0b6c3f0e-6a8e-4d3e-9a51-0f3c1f2d9a10"
ORDER = SubscriptionOrder(
    business_id=BUSINESS,
    plan="starter",
    plan_name="Starter",
    amount_minor=900,
    currency="GBP",
    customer_email="owner@example.com",
    customer_id=None,
    success_url="https://book.example.com/#billing=done",
    cancel_url="https://book.example.com/#billing=cancelled",
)


def platform_event(kind: str, item: dict[str, Any]) -> bytes:
    """Build a Stripe event body from the platform's own account (no connected account).

    Args:
        kind: The event type.
        item: The event's object.

    Returns:
        The JSON body.
    """
    payload = {
        "id": "evt_billing_1",
        "object": "event",
        "type": kind,
        "livemode": False,
        "created": int(time.time()),
        "api_version": "2026-08-26.dahlia",
        "data": {"object": item},
    }
    return json.dumps(payload).encode()


MARKS = {"business_id": BUSINESS, "plan": "starter"}
SUBSCRIBED = {
    "object": "checkout.session",
    "id": "cs_test_plan",
    "mode": "subscription",
    "payment_status": "paid",
    "client_reference_id": BUSINESS,
    "metadata": MARKS,
    "subscription": "sub_test_1",
    "customer": "cus_test_1",
}


class FakeBillingClient:
    """Records price, checkout and portal requests and answers with fixed objects.

    Attributes:
        calls: ``(name, args)`` for each request.
        existing: Price identifiers Stripe already holds, by lookup key.
        fail: If set, every request raises it.
    """

    def __init__(self, existing: dict[str, str] | None = None, fail: Exception | None = None) -> None:
        """Create the fake.

        Args:
            existing: Prices Stripe already has.
            fail: An error for every request to raise.
        """
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.existing = existing or {}
        self.fail = fail
        self.v1 = SimpleNamespace(
            prices=SimpleNamespace(list=self._list, create=self._create),
            checkout=SimpleNamespace(sessions=SimpleNamespace(create=self._session)),
            billing_portal=SimpleNamespace(sessions=SimpleNamespace(create=self._portal)),
        )

    def _call(self, name: str, *args: Any) -> None:
        """Record a request and raise the configured failure.

        Args:
            name: The request.
            *args: Its arguments.

        Raises:
            Exception: The configured failure, if any.
        """
        self.calls.append((name, args))
        if self.fail is not None:
            raise self.fail

    def _list(self, params: dict[str, Any]) -> SimpleNamespace:
        """Answer a price lookup.

        Args:
            params: The request parameters.

        Returns:
            The matching prices.
        """
        self._call("list", params)
        key = params["lookup_keys"][0]
        return SimpleNamespace(data=[SimpleNamespace(id=self.existing[key])] if key in self.existing else [])

    def _create(self, params: dict[str, Any]) -> SimpleNamespace:
        """Answer a price creation.

        Args:
            params: The request parameters.

        Returns:
            The new price.
        """
        self._call("create", params)
        return SimpleNamespace(id="price_new")

    def _session(self, params: dict[str, Any]) -> SimpleNamespace:
        """Answer a checkout request.

        Args:
            params: The request parameters.

        Returns:
            A checkout.
        """
        self._call("checkout", params)
        return SimpleNamespace(id="cs_test_plan", url="https://checkout.stripe.com/c/pay/cs_test_plan")

    def _portal(self, params: dict[str, Any]) -> SimpleNamespace:
        """Answer a portal request.

        Args:
            params: The request parameters.

        Returns:
            The portal session.
        """
        self._call("portal", params)
        return SimpleNamespace(url="https://billing.stripe.com/p/session/test")


def billing_provider(client: FakeBillingClient | None = None, billing: str | None = BILLING_SECRET) -> StripeProvider:
    """Build the adapter with both secrets.

    Args:
        client: The fake, or ``None`` for a real client that is never used for a request.
        billing: The billing webhook secret.

    Returns:
        The adapter.
    """
    real = stripe.StripeClient("sk_test_placeholder")
    return StripeProvider(cast(Any, client) if client is not None else real, SECRET, billing)


def test_the_billing_secret_is_optional_distinct_and_well_formed() -> None:
    """Without it plans cannot be billed; with it they can; a reused or malformed secret is refused."""
    base = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": SECRET}
    plain = stripe_from_environment(base)
    assert plain is not None and plain.bills_plans is False
    full = stripe_from_environment({**base, "STRIPE_BILLING_WEBHOOK_SECRET": BILLING_SECRET})
    assert full is not None and full.bills_plans is True and isinstance(full, SubscriptionBilling)
    for bad in (SECRET, "not-a-secret"):
        with pytest.raises(StripeConfigError):
            stripe_from_environment({**base, "STRIPE_BILLING_WEBHOOK_SECRET": bad})


def test_a_plan_checkout_is_a_monthly_subscription_at_the_server_price() -> None:
    """A new price is created once with the plan's lookup key; the checkout carries the business."""
    client = FakeBillingClient()
    adapter = billing_provider(client)
    checkout = adapter.start_subscription(ORDER)
    assert checkout.checkout_url == "https://checkout.stripe.com/c/pay/cs_test_plan"
    (_, (listed,)), (_, (created,)), (_, (params,)) = client.calls
    assert listed["lookup_keys"] == ["gn_plan_starter_monthly_gbp_900"]
    assert (created["unit_amount"], created["currency"], created["recurring"]) == (900, "gbp", {"interval": "month"})
    assert params["mode"] == "subscription"
    assert params["line_items"] == [{"price": "price_new", "quantity": 1}]
    assert params["metadata"] == params["subscription_data"]["metadata"] == MARKS
    assert (params["client_reference_id"], params["customer_email"]) == (BUSINESS, "owner@example.com")
    assert "customer" not in params and "payment_method_types" not in params
    adapter.start_subscription(ORDER)
    assert [name for name, _ in client.calls] == ["list", "create", "checkout", "checkout"]


def test_an_existing_price_and_customer_are_reused() -> None:
    """A price Stripe already has is not created again; a known customer is passed instead of an email."""
    client = FakeBillingClient(existing={"gn_plan_starter_monthly_gbp_900": "price_old"})
    order = SubscriptionOrder(**{**ORDER.__dict__, "customer_id": "cus_test_1"})
    billing_provider(client).start_subscription(order)
    (_, _), (_, (params,)) = client.calls
    assert params["line_items"][0]["price"] == "price_old"
    assert params["customer"] == "cus_test_1" and "customer_email" not in params


def test_the_portal_link_is_for_the_business_customer() -> None:
    """The portal session is opened for the stored customer and returns to the site."""
    client = FakeBillingClient()
    url = billing_provider(client).billing_portal_url("cus_test_1", "https://book.example.com/#billing=portal")
    assert url == "https://billing.stripe.com/p/session/test"
    assert client.calls[0][1][0] == {"customer": "cus_test_1", "return_url": "https://book.example.com/#billing=portal"}


@pytest.mark.parametrize("call", ["subscription", "portal"])
def test_stripe_billing_failures_become_the_boundary_error(call: str) -> None:
    """Any Stripe error surfaces as ProviderUnavailableError."""
    failure = stripe.APIConnectionError("network down")  # type: ignore[no-untyped-call]
    adapter = billing_provider(FakeBillingClient(fail=failure))
    with pytest.raises(ProviderUnavailableError):
        if call == "subscription":
            adapter.start_subscription(ORDER)
        else:
            adapter.billing_portal_url("cus_test_1", "https://a")


@pytest.mark.parametrize(
    ("kind", "item", "status"),
    [
        ("checkout.session.completed", SUBSCRIBED, "subscription_active"),
        ("checkout.session.completed", {**SUBSCRIBED, "payment_status": "unpaid"}, "ignored"),
        (
            "customer.subscription.updated",
            {
                "object": "subscription",
                "id": "sub_test_1",
                "customer": "cus_test_1",
                "status": "active",
                "metadata": MARKS,
            },
            "subscription_active",
        ),
        (
            "customer.subscription.updated",
            {
                "object": "subscription",
                "id": "sub_test_1",
                "customer": "cus_test_1",
                "status": "past_due",
                "metadata": MARKS,
            },
            "subscription_past_due",
        ),
        (
            "customer.subscription.updated",
            {
                "object": "subscription",
                "id": "sub_test_1",
                "customer": "cus_test_1",
                "status": "incomplete",
                "metadata": MARKS,
            },
            "ignored",
        ),
        (
            "customer.subscription.deleted",
            {
                "object": "subscription",
                "id": "sub_test_1",
                "customer": "cus_test_1",
                "status": "canceled",
                "metadata": MARKS,
            },
            "subscription_ended",
        ),
        ("invoice.paid", {"object": "invoice", "id": "in_1"}, "ignored"),
    ],
)
def test_subscription_notifications_map_to_one_status(kind: str, item: dict[str, Any], status: str) -> None:
    """Platform notifications signed with the billing secret carry the business, plan and subscription."""
    body = platform_event(kind, item)
    parsed = billing_provider().parse_webhook(sign(body, BILLING_SECRET), body)
    assert parsed.status == status
    if status != "ignored":
        assert (parsed.order_reference, parsed.subscription_id, parsed.customer_id, parsed.plan) == (
            BUSINESS,
            "sub_test_1",
            "cus_test_1",
            "starter",
        )


def test_each_secret_is_trusted_only_for_its_own_kind_of_notification() -> None:
    """A platform event signed with the businesses' secret, or a business event signed with the billing
    secret, is ignored; a platform event without a billing secret configured is rejected."""
    platform = platform_event("checkout.session.completed", SUBSCRIBED)
    assert billing_provider().parse_webhook(sign(platform, SECRET), platform).status == "ignored"
    business = event("checkout.session.completed", PAID_SESSION, account=ACCOUNT)
    assert billing_provider().parse_webhook(sign(business, BILLING_SECRET), business).status == "ignored"
    assert billing_provider().parse_webhook(sign(business, SECRET), business).status == "paid"

    with pytest.raises(WebhookRejectedError):
        billing_provider(billing=None).parse_webhook(sign(platform, BILLING_SECRET), platform)
