"""Tests for the shared contract types and the payment records built on them.

What these tests establish: the payment records refuse values of the wrong type (no silent conversion), unknown
fields, out-of-range amounts, badly formed currencies and identifiers, insecure links and times without a time
zone, and cannot be changed after they are built. They do not establish anything about the database, which keeps
its own checks.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from booking.owner import PLAN_PRICES
from payments.base import (
    BookingPaymentEvent,
    CancellationQuote,
    CheckoutOrder,
    CheckoutSession,
    HostedCheckout,
    IgnoredEvent,
    SubscriptionEvent,
    SubscriptionOrder,
)

ORDER: dict[str, Any] = {
    "reference": "ABCDEFGH23",
    "amount_minor": 2500,
    "currency": "GBP",
    "description": "Haircut, Fade & Co",
    "account_id": "acct_test_1",
    "customer_email": "customer@example.com",
    "success_url": "https://book.example.com/?b=fade#paid=ABCDEFGH23",
    "cancel_url": "https://book.example.com/?b=fade#unpaid=ABCDEFGH23",
    "expires_at": datetime(2026, 10, 20, 12, 31, tzinfo=UTC),
}

PLAN_ORDER: dict[str, Any] = {
    "business_id": "0b6c3f0e-6a8e-4d3e-9a51-0f3c1f2d9a10",
    "plan": "starter",
    "plan_name": "Starter",
    "amount_minor": 900,
    "currency": "GBP",
    "customer_email": None,
    "customer_id": None,
    "success_url": "https://book.example.com/#billing=done",
    "cancel_url": "https://book.example.com/#billing=cancelled",
}

BUSINESS = "0b6c3f0e-6a8e-4d3e-9a51-0f3c1f2d9a10"


def test_valid_records_are_accepted_including_local_development_links() -> None:
    """The values the platform builds today pass, and a localhost return link is allowed for development."""
    CheckoutOrder(**ORDER)
    CheckoutOrder(**{**ORDER, "success_url": "http://localhost:8000/book/fade#paid=ABCDEFGH23"})
    SubscriptionOrder(**PLAN_ORDER)
    HostedCheckout(checkout_id="cs_test_1", checkout_url="https://checkout.stripe.com/c/pay/cs_test_1")
    CancellationQuote(refund_minor=0, currency="GBP", unused_days=0)
    CheckoutSession(provider="stub", order_reference="order-123", checkout_url=None, live=False)


@pytest.mark.parametrize(
    "change",
    [
        {"amount_minor": "2500"},
        {"amount_minor": 0},
        {"amount_minor": 10_000_001},
        {"amount_minor": True},
        {"currency": "gbp"},
        {"currency": "GBPX"},
        {"reference": "x"},
        {"account_id": "acct test"},
        {"description": ""},
        {"success_url": "http://book.example.com/"},
        {"cancel_url": "javascript:alert(1)"},
        {"expires_at": datetime(2026, 10, 20, 12, 31)},
        {"expires_at": "2026-10-20T12:31:00Z"},
        {"amount": 1},
    ],
)
def test_a_checkout_order_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Strict: no conversion, no unknown fields, money always moves, links are secure, times are aware."""
    with pytest.raises(ValidationError):
        CheckoutOrder(**{**ORDER, **change})


@pytest.mark.parametrize(
    "change",
    [{"plan": "free"}, {"plan": "enterprise"}, {"amount_minor": 0}, {"customer_id": "cus test"}],
)
def test_a_plan_order_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Only a paid plan, at a price, can be ordered."""
    with pytest.raises(ValidationError):
        SubscriptionOrder(**{**PLAN_ORDER, **change})


def test_every_paid_plan_can_be_ordered() -> None:
    """The plan order accepts exactly the paid plans the platform sells."""
    for plan in PLAN_PRICES:
        SubscriptionOrder(**{**PLAN_ORDER, "plan": plan})


def test_records_cannot_be_changed_after_they_are_built() -> None:
    """Frozen, as the dataclasses they replace were."""
    order = CheckoutOrder(**ORDER)
    with pytest.raises(ValidationError):
        order.amount_minor = 1


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        (HostedCheckout, {"checkout_id": "cs_test_1", "checkout_url": "http://checkout.example.com/"}),
        (HostedCheckout, {"checkout_id": "", "checkout_url": "https://checkout.example.com/"}),
        (CancellationQuote, {"refund_minor": -1, "currency": "GBP", "unused_days": 0}),
        (CancellationQuote, {"refund_minor": 0, "currency": "GBP", "unused_days": -1}),
        (BookingPaymentEvent, {"provider": "stripe", "status": "ignored", "live": False}),
        (BookingPaymentEvent, {"provider": "stripe", "status": "subscription_active", "live": False}),
        (BookingPaymentEvent, {"provider": "other", "status": "paid", "live": False}),
        (BookingPaymentEvent, {"provider": "stripe", "status": "paid", "live": "false"}),
        (BookingPaymentEvent, {"provider": "stripe", "status": "paid", "live": False, "amount_minor": -1}),
        (BookingPaymentEvent, {"provider": "stripe", "status": "paid", "live": False, "currency": "gbp"}),
        (BookingPaymentEvent, {"provider": "stripe", "status": "paid", "live": False, "order_reference": ""}),
        (BookingPaymentEvent, {"provider": "stripe", "status": "paid", "live": False, "plan": "starter"}),
        (SubscriptionEvent, {"provider": "stripe", "status": "subscription_active", "live": False}),
        (
            SubscriptionEvent,
            {
                "provider": "stripe",
                "status": "subscription_active",
                "live": False,
                "business_id": BUSINESS,
                "subscription_id": "sub_1",
            },
        ),
        (IgnoredEvent, {"provider": "stripe", "live": False, "status": "paid"}),
    ],
)
def test_payment_records_refuse_wrong_values(kind: type[Any], fields: dict[str, Any]) -> None:
    """Each record holds only its own outcomes and fields, in the right shape; a subscription event needs its
    business and subscription, with the business as a real identifier rather than text."""
    with pytest.raises(ValidationError):
        kind(**fields)
