"""Tests for cancelling a plan with a refund of the unused days, in the Stripe adapter. No network: a fake client
stands in for Stripe's API.

What these tests establish: the refund is the unused share of the last paid period, rounded down and never more
than was paid; nothing is refunded when the last invoice is unpaid or the subscription has already ended; the
refund is made before the subscription is cancelled, without proration, and never twice for one subscription,
even when a cancel is retried; and a failed refund leaves the subscription running. They do not establish that
Stripe accepts these requests: that is checked by hand in Stripe test mode.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import stripe
from test_stripe import SECRET
from test_stripe_billing import BILLING_SECRET

from payments.base import CancellationQuote, ProviderUnavailableError
from payments.stripe import StripeProvider

START = datetime(2026, 11, 1, tzinfo=UTC)
END = datetime(2026, 12, 1, tzinfo=UTC)
SUBSCRIPTION = "sub_test_1"


class FakeCancelClient:
    """Answers subscription, refund-list and refund requests with fixed objects and records every call.

    Attributes:
        calls: The request names, in order.
        refunds: Refunds made, as ``(params, options)``.
        cancelled: Cancel requests, as ``(subscription, params)``.
    """

    def __init__(
        self,
        *,
        status: str = "active",
        invoice_status: str = "paid",
        paid: int = 900,
        payment_type: str = "payment_intent",
        fail_refund: Exception | None = None,
        fail_cancel: Exception | None = None,
    ) -> None:
        """Create the fake.

        Args:
            status: The subscription's status.
            invoice_status: The latest invoice's status.
            paid: Minor units paid on the latest invoice.
            payment_type: ``payment_intent`` or ``charge``.
            fail_refund: An error for the refund request to raise, once.
            fail_cancel: An error for the cancel request to raise, once.
        """
        self.calls: list[str] = []
        self.refunds: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        self.cancelled: list[tuple[str, dict[str, Any] | None]] = []
        self._existing: list[dict[str, Any]] = []
        self._status = status
        self._fail_refund = fail_refund
        self._fail_cancel = fail_cancel
        payment = {
            "type": payment_type,
            "payment_intent": "pi_test_1" if payment_type == "payment_intent" else None,
            "charge": "ch_test_1" if payment_type == "charge" else None,
        }
        invoice: dict[str, Any] = {
            "status": invoice_status,
            "amount_paid": paid if invoice_status == "paid" else 0,
            "currency": "gbp",
            "payments": {"data": [{"status": "paid", "amount_paid": paid, "payment": payment}]},
        }
        item = {"current_period_start": int(START.timestamp()), "current_period_end": int(END.timestamp())}
        self._subscription = stripe.Subscription.construct_from(
            {
                "id": SUBSCRIPTION,
                "object": "subscription",
                "status": status,
                "items": {"object": "list", "data": [{"object": "subscription_item", **item}]},
                "latest_invoice": {
                    "object": "invoice",
                    **invoice,
                    "payments": {
                        "object": "list",
                        "data": [{"object": "invoice_payment", **entry} for entry in invoice["payments"]["data"]],
                    },
                },
            },
            "sk_test_placeholder",
        )
        self.v1 = SimpleNamespace(
            subscriptions=SimpleNamespace(retrieve=self._retrieve, cancel=self._cancel),
            refunds=SimpleNamespace(list=self._list, create=self._create),
        )

    def _retrieve(self, subscription: str, params: dict[str, Any] | None = None) -> stripe.Subscription:
        """Answer a subscription request.

        Args:
            subscription: The subscription identifier.
            params: The request parameters.

        Returns:
            The subscription.
        """
        self.calls.append("retrieve")
        assert subscription == SUBSCRIPTION
        return self._subscription

    def _list(self, params: dict[str, Any]) -> stripe.ListObject[stripe.Refund]:
        """Answer a refund-list request with the refunds made so far.

        Args:
            params: The request parameters.

        Returns:
            The list.
        """
        self.calls.append("list_refunds")
        return stripe.ListObject.construct_from(
            {"object": "list", "data": [{"object": "refund", **entry} for entry in self._existing]},
            "sk_test_placeholder",
        )

    def _create(self, params: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a refund, or raise the configured failure.

        Args:
            params: The refund parameters.
            options: The request options.

        Returns:
            The refund.

        Raises:
            Exception: The configured failure, once.
        """
        self.calls.append("create_refund")
        if self._fail_refund is not None:
            failure, self._fail_refund = self._fail_refund, None
            raise failure
        self.refunds.append((params, options))
        refund = {"id": f"re_test_{len(self.refunds)}", "amount": params["amount"], "metadata": params["metadata"]}
        self._existing.append(refund)
        return refund

    def _cancel(self, subscription: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Cancel the subscription, or raise the configured failure.

        Args:
            subscription: The subscription identifier.
            params: The request parameters.

        Returns:
            The cancelled subscription.

        Raises:
            Exception: The configured failure, once.
        """
        self.calls.append("cancel")
        if self._fail_cancel is not None:
            failure, self._fail_cancel = self._fail_cancel, None
            raise failure
        self.cancelled.append((subscription, params))
        return {"id": subscription, "status": "canceled"}


def provider(client: FakeCancelClient) -> StripeProvider:
    """Build the adapter around a fake client.

    Args:
        client: The fake.

    Returns:
        The adapter.
    """
    return StripeProvider(cast(Any, client), SECRET, BILLING_SECRET)


def network_down() -> stripe.StripeError:
    """Build a Stripe error as a dropped connection would raise.

    Returns:
        The error.
    """
    return stripe.APIConnectionError("network down")  # type: ignore[no-untyped-call]


def test_the_refund_is_the_unused_share_of_the_paid_period() -> None:
    """Halfway through a 30-day period, half of 900 pence and 15 unused days."""
    quote = provider(FakeCancelClient()).quote_cancellation(SUBSCRIPTION, START + timedelta(days=15))
    assert quote == CancellationQuote(refund_minor=450, currency="GBP", unused_days=15)


@pytest.mark.parametrize(
    ("moment", "refund", "days"),
    [
        (START, 900, 30),
        (START + timedelta(days=1, hours=1), 868, 28),
        (END - timedelta(seconds=1), 0, 0),
        (END, 0, 0),
        (END + timedelta(days=3), 0, 0),
        (START - timedelta(days=2), 900, 30),
    ],
)
def test_the_refund_is_rounded_down_and_never_more_than_was_paid(moment: datetime, refund: int, days: int) -> None:
    """Whole pence round down; a time outside the period gives all or nothing, never more than 900."""
    quote = provider(FakeCancelClient()).quote_cancellation(SUBSCRIPTION, moment)
    assert (quote.refund_minor, quote.unused_days) == (refund, days)


def test_nothing_is_refunded_when_the_last_invoice_is_unpaid() -> None:
    """A failed renewal means no money was taken for the new period."""
    client = FakeCancelClient(status="past_due", invoice_status="open")
    quote = provider(client).quote_cancellation(SUBSCRIPTION, START + timedelta(days=10))
    assert quote.refund_minor == 0


def test_nothing_is_refunded_for_a_subscription_that_has_already_ended() -> None:
    """An ended subscription has nothing left to refund."""
    quote = provider(FakeCancelClient(status="canceled")).quote_cancellation(SUBSCRIPTION, START + timedelta(days=10))
    assert quote.refund_minor == 0


def test_the_refund_is_made_before_the_subscription_is_cancelled_without_proration() -> None:
    """Refund first, so a failed refund never leaves a cancelled plan; no proration invoice is created."""
    client = FakeCancelClient()
    result = provider(client).cancel_with_refund(SUBSCRIPTION, START + timedelta(days=15))
    assert result == CancellationQuote(refund_minor=450, currency="GBP", unused_days=15)
    assert client.calls == ["retrieve", "list_refunds", "create_refund", "cancel"]
    params, options = client.refunds[0]
    assert params == {"payment_intent": "pi_test_1", "amount": 450, "metadata": {"plan_cancel": SUBSCRIPTION}}
    assert options is not None and SUBSCRIPTION in options["idempotency_key"]
    assert client.cancelled == [(SUBSCRIPTION, {"prorate": False, "invoice_now": False})]


def test_a_charge_payment_is_refunded_by_charge() -> None:
    """When the invoice was paid by a charge rather than a payment intent, the charge is refunded."""
    client = FakeCancelClient(payment_type="charge")
    provider(client).cancel_with_refund(SUBSCRIPTION, START + timedelta(days=15))
    assert client.refunds[0][0]["charge"] == "ch_test_1" and "payment_intent" not in client.refunds[0][0]


def test_a_failed_refund_leaves_the_subscription_running() -> None:
    """If the refund cannot be made, the plan is not cancelled and the error is the boundary's."""
    client = FakeCancelClient(fail_refund=network_down())
    with pytest.raises(ProviderUnavailableError):
        provider(client).cancel_with_refund(SUBSCRIPTION, START + timedelta(days=15))
    assert client.cancelled == []


def test_a_retried_cancel_never_refunds_twice() -> None:
    """The cancel fails after the refund; the retry finds the refund, reports it, and only cancels."""
    client = FakeCancelClient(fail_cancel=network_down())
    adapter = provider(client)
    with pytest.raises(ProviderUnavailableError):
        adapter.cancel_with_refund(SUBSCRIPTION, START + timedelta(days=15))
    retried = adapter.cancel_with_refund(SUBSCRIPTION, START + timedelta(days=16))
    assert len(client.refunds) == 1
    assert retried.refund_minor == 450 and client.cancelled == [
        (SUBSCRIPTION, {"prorate": False, "invoice_now": False})
    ]


def test_an_unpaid_plan_is_still_cancelled_with_no_refund() -> None:
    """With nothing to refund the subscription is still cancelled."""
    client = FakeCancelClient(status="past_due", invoice_status="open")
    result = provider(client).cancel_with_refund(SUBSCRIPTION, START + timedelta(days=10))
    assert result.refund_minor == 0 and client.refunds == [] and len(client.cancelled) == 1


def test_an_already_ended_subscription_is_left_alone() -> None:
    """Nothing is refunded and no cancel request is sent for a subscription Stripe has already ended."""
    client = FakeCancelClient(status="canceled")
    result = provider(client).cancel_with_refund(SUBSCRIPTION, START + timedelta(days=10))
    assert result.refund_minor == 0 and client.refunds == [] and client.cancelled == []
