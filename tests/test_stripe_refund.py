"""Tests for refunding a booking's card payment, in the Stripe adapter. No network: a fake client stands in for
Stripe's API and answers with real SDK objects.

What these tests establish: the refund is a full refund of the booking's payment, made on the business's own
account, once per booking even when repeated; an existing refund for the booking is found rather than repeated;
and a Stripe failure becomes the boundary's error without a secret. They do not establish that Stripe accepts
these requests: that is checked by hand in Stripe test mode.
"""

from types import SimpleNamespace
from typing import Any, cast

import pytest
import stripe
from test_stripe import SECRET
from test_stripe_billing import BILLING_SECRET

from payments.base import BookingRefunds, ProviderUnavailableError
from payments.stripe import StripeProvider

ACCOUNT = "acct_test_1"
PAYMENT = "pi_test_1"
REFERENCE = "ZA2JBN2C5B"


class FakeRefundClient:
    """Answers refund-list and refund requests and records every call.

    Attributes:
        listed: Refund-list requests, as ``(params, options)``.
        created: Refunds made, as ``(params, options)``.
    """

    def __init__(
        self, existing: list[dict[str, Any]] | None = None, fail_with: Exception | None = None, fail_on: str = "create"
    ) -> None:
        """Create the fake.

        Args:
            existing: Refunds that already exist on the payment.
            fail_with: An error for one request to raise.
            fail_on: Which request fails: ``list`` or ``create``.
        """
        self.listed: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        self.created: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        self._existing = existing or []
        self._fail_with = fail_with
        self._fail_on = fail_on
        self.v1 = SimpleNamespace(refunds=SimpleNamespace(list=self._list, create=self._create))

    def _list(self, params: dict[str, Any], options: dict[str, Any] | None = None) -> stripe.ListObject[stripe.Refund]:
        """Answer a refund-list request.

        Args:
            params: The request parameters.
            options: The request options.

        Returns:
            The list.

        Raises:
            Exception: The configured failure.
        """
        self.listed.append((params, options))
        if self._fail_with is not None and self._fail_on == "list":
            raise self._fail_with
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
            Exception: The configured failure.
        """
        if self._fail_with is not None and self._fail_on == "create":
            raise self._fail_with
        self.created.append((params, options))
        return {"id": "re_test_1"}


def provider(client: FakeRefundClient) -> StripeProvider:
    """Build the adapter around a fake client.

    Args:
        client: The fake.

    Returns:
        The adapter.
    """
    return StripeProvider(cast(Any, client), SECRET, BILLING_SECRET)


def test_the_adapter_can_refund_bookings() -> None:
    """The adapter satisfies the booking refund boundary."""
    assert isinstance(provider(FakeRefundClient()), BookingRefunds)


def test_the_whole_payment_is_refunded_on_the_business_account() -> None:
    """No amount is sent, so Stripe refunds the whole payment; the key makes a repeat harmless."""
    client = FakeRefundClient()
    provider(client).refund_booking(ACCOUNT, PAYMENT, REFERENCE)
    assert client.listed == [({"payment_intent": PAYMENT, "limit": 20}, {"stripe_account": ACCOUNT})]
    assert client.created == [
        (
            {"payment_intent": PAYMENT, "metadata": {"booking_reference": REFERENCE}},
            {"stripe_account": ACCOUNT, "idempotency_key": f"booking-refund-{REFERENCE}"},
        )
    ]
    assert "amount" not in client.created[0][0]


def test_a_refund_already_made_for_the_booking_is_not_repeated() -> None:
    """Found by its booking reference, so a retry after the idempotency window still refunds once."""
    client = FakeRefundClient(existing=[{"id": "re_old", "metadata": {"booking_reference": REFERENCE}}])
    provider(client).refund_booking(ACCOUNT, PAYMENT, REFERENCE)
    assert client.created == []


def test_a_refund_for_another_booking_does_not_count() -> None:
    """Only a refund tagged with this booking's reference stops a new one."""
    client = FakeRefundClient(existing=[{"id": "re_old", "metadata": {"booking_reference": "OTHER00000"}}])
    provider(client).refund_booking(ACCOUNT, PAYMENT, REFERENCE)
    assert len(client.created) == 1


@pytest.mark.parametrize("fail_on", ["list", "create"])
def test_a_stripe_failure_becomes_the_boundary_error_without_a_secret(fail_on: str) -> None:
    """The caller sees one error type, and nothing sensitive in its message."""
    failure = stripe.APIConnectionError("network down")  # type: ignore[no-untyped-call]
    client = FakeRefundClient(fail_with=failure, fail_on=fail_on)
    with pytest.raises(ProviderUnavailableError) as raised:
        provider(client).refund_booking(ACCOUNT, PAYMENT, REFERENCE)
    assert SECRET not in str(raised.value) and "network down" not in str(raised.value)
