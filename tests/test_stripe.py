"""Tests for the Stripe adapter. No network: a fake client stands in for Stripe's API, and webhook bodies
are signed here with Stripe's published signature scheme.

What these tests establish: only test-mode settings are accepted; a notification is read only after its
signature verifies; checkout, account and sign-up-link requests carry the agreed values (business as the
seller, Stripe collecting fees and responsible for losses, the amount from the order, no payment method
list); and Stripe failures surface as the boundary's own error. They do not establish that Stripe accepts
these requests: that is checked by hand in Stripe test mode.
"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
import stripe

from payments.base import (
    AccountPayments,
    CheckoutOrder,
    PaymentProvider,
    ProviderNotLiveError,
    ProviderUnavailableError,
    WebhookRejectedError,
)
from payments.stripe import StripeConfigError, StripeProvider, stripe_from_environment

SECRET = "whsec_test_secret"
ACCOUNT = "acct_1TestBusiness"


def sign(body: bytes, secret: str = SECRET, at: int | None = None) -> dict[str, str]:
    """Sign a webhook body the way Stripe does.

    Args:
        body: The raw body.
        secret: The endpoint's signing secret.
        at: The signing time. ``None`` means now.

    Returns:
        The headers carrying the signature.
    """
    stamp = int(time.time()) if at is None else at
    digest = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={stamp},v1={digest}"}


def event(kind: str, session: dict[str, Any], account: str | None = ACCOUNT) -> bytes:
    """Build a Stripe event body about a checkout.

    Args:
        kind: The event type.
        session: The checkout object.
        account: The connected account it came from.

    Returns:
        The JSON body.
    """
    payload = {
        "id": "evt_test_1",
        "object": "event",
        "type": kind,
        "livemode": False,
        "created": int(time.time()),
        "api_version": "2026-08-26.dahlia",
        "account": account,
        "data": {"object": {"object": "checkout.session", **session}},
    }
    return json.dumps(payload).encode()


PAID_SESSION = {
    "id": "cs_test_1",
    "client_reference_id": "ABCDEFGH23",
    "payment_status": "paid",
    "amount_total": 2500,
    "currency": "gbp",
    "payment_intent": "pi_test_1",
}


class FakeClient:
    """Records the requests the adapter makes and answers with fixed objects.

    Attributes:
        calls: ``(name, args)`` for each request.
        fail: If set, every request raises it.
        card_status: The card payments status reported for an account.
    """

    def __init__(self, fail: Exception | None = None, card_status: str = "active") -> None:
        """Create the fake.

        Args:
            fail: An error for every request to raise.
            card_status: The capability status to report.
        """
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = fail
        self.card_status = card_status
        self.v1 = SimpleNamespace(checkout=SimpleNamespace(sessions=SimpleNamespace(create=self._session)))
        self.v2 = SimpleNamespace(
            core=SimpleNamespace(
                accounts=SimpleNamespace(create=self._account, retrieve=self._retrieve),
                account_links=SimpleNamespace(create=self._link),
            )
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

    def _session(self, params: dict[str, Any], options: dict[str, Any]) -> SimpleNamespace:
        """Answer a checkout request.

        Args:
            params: The request parameters.
            options: The request options.

        Returns:
            A checkout.
        """
        self._call("checkout", params, options)
        return SimpleNamespace(id="cs_test_1", url="https://checkout.stripe.com/c/pay/cs_test_1")

    def _account(self, params: dict[str, Any]) -> SimpleNamespace:
        """Answer an account creation.

        Args:
            params: The request parameters.

        Returns:
            An account.
        """
        self._call("account", params)
        return SimpleNamespace(id=ACCOUNT)

    def _retrieve(self, account: str, params: dict[str, Any]) -> SimpleNamespace:
        """Answer an account retrieval.

        Args:
            account: The account.
            params: The request parameters.

        Returns:
            The account with its card payments status.
        """
        self._call("retrieve", account, params)
        card = SimpleNamespace(status=self.card_status)
        merchant = SimpleNamespace(capabilities=SimpleNamespace(card_payments=card))
        return SimpleNamespace(configuration=SimpleNamespace(merchant=merchant))

    def _link(self, params: dict[str, Any]) -> SimpleNamespace:
        """Answer an account link request.

        Args:
            params: The request parameters.

        Returns:
            The link.
        """
        self._call("link", params)
        return SimpleNamespace(url="https://connect.stripe.com/setup/test")


def provider(client: FakeClient | None = None) -> StripeProvider:
    """Build the adapter around a fake client, or a real client that is never used for a request.

    Args:
        client: The fake, or ``None`` for a real client with a placeholder key.

    Returns:
        The adapter.
    """
    real = stripe.StripeClient("sk_test_placeholder")
    return StripeProvider(cast(Any, client) if client is not None else real, SECRET)


ORDER = CheckoutOrder(
    reference="ABCDEFGH23",
    amount_minor=2500,
    currency="GBP",
    description="Haircut, Fade & Co",
    account_id=ACCOUNT,
    customer_email="customer@example.com",
    success_url="https://book.example.com/?b=fade#paid=ABCDEFGH23",
    cancel_url="https://book.example.com/?b=fade#unpaid=ABCDEFGH23",
    expires_at=datetime(2026, 10, 20, 12, 31, tzinfo=UTC),
)


def test_the_adapter_satisfies_both_interfaces_and_is_not_live() -> None:
    """Stripe is a provider and an account-payments provider, and is never live with test keys."""
    adapter = provider()
    assert isinstance(adapter, PaymentProvider) and isinstance(adapter, AccountPayments)
    assert adapter.live is False
    with pytest.raises(ProviderNotLiveError):
        adapter.create_checkout("order-123")


@pytest.mark.parametrize(
    "environ",
    [
        {"STRIPE_SECRET_KEY": "sk_test_x"},
        {"STRIPE_WEBHOOK_SECRET": SECRET},
        {"STRIPE_SECRET_KEY": "sk_live_x", "STRIPE_WEBHOOK_SECRET": SECRET},
        {"STRIPE_SECRET_KEY": "rk_live_x", "STRIPE_WEBHOOK_SECRET": SECRET},
        {"STRIPE_SECRET_KEY": "pk_test_x", "STRIPE_WEBHOOK_SECRET": SECRET},
        {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": "not-a-secret"},
    ],
)
def test_incomplete_live_or_malformed_settings_are_refused(environ: dict[str, str]) -> None:
    """Half a configuration, a live or publishable key, or a malformed secret stops the API from starting."""
    with pytest.raises(StripeConfigError) as raised:
        stripe_from_environment(environ)
    assert "sk_" not in str(raised.value) and SECRET not in str(raised.value)


def test_absent_settings_mean_no_stripe_and_test_keys_are_accepted() -> None:
    """No settings means no adapter; a secret or restricted test key works."""
    assert stripe_from_environment({}) is None
    for key in ("sk_test_x", "rk_test_x"):
        assert isinstance(
            stripe_from_environment({"STRIPE_SECRET_KEY": key, "STRIPE_WEBHOOK_SECRET": SECRET}), StripeProvider
        )


def test_a_paid_checkout_notification_is_read_after_its_signature_verifies() -> None:
    """Every field the booking layer checks comes from the verified event."""
    body = event("checkout.session.completed", PAID_SESSION)
    parsed = provider().parse_webhook(sign(body), body)
    assert (parsed.provider, parsed.status, parsed.live, parsed.order_reference) == (
        "stripe",
        "paid",
        False,
        "ABCDEFGH23",
    )
    assert (parsed.checkout_id, parsed.account_id, parsed.amount_minor, parsed.currency, parsed.payment_id) == (
        "cs_test_1",
        ACCOUNT,
        2500,
        "GBP",
        "pi_test_1",
    )


@pytest.mark.parametrize(
    ("kind", "change", "status"),
    [
        ("checkout.session.async_payment_succeeded", {}, "paid"),
        ("checkout.session.completed", {"payment_status": "unpaid"}, "ignored"),
        ("checkout.session.async_payment_failed", {"payment_status": "unpaid"}, "failed"),
        ("checkout.session.expired", {"payment_status": "unpaid"}, "expired"),
        ("payment_intent.created", {}, "ignored"),
    ],
)
def test_each_checkout_outcome_maps_to_one_status(kind: str, change: dict[str, Any], status: str) -> None:
    """Only a completed, paid checkout is ``paid``; an unpaid completion waits for its async outcome."""
    body = event(kind, {**PAID_SESSION, **change})
    assert provider().parse_webhook(sign(body), body).status == status


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"stripe-signature": "t=1,v1=abc"},
        sign(b"different body"),
        sign(event("checkout.session.completed", PAID_SESSION), secret="whsec_other"),
        sign(event("checkout.session.completed", PAID_SESSION), at=int(time.time()) - 3600),
    ],
)
def test_unsigned_wrongly_signed_or_stale_notifications_are_rejected(headers: dict[str, str]) -> None:
    """A missing, wrong, foreign or replayed-late signature is rejected before the body is read."""
    body = event("checkout.session.completed", PAID_SESSION)
    with pytest.raises(WebhookRejectedError):
        provider().parse_webhook(headers, body)


def test_a_malformed_but_signed_body_is_rejected() -> None:
    """A correctly signed body that is not JSON is still rejected."""
    with pytest.raises(WebhookRejectedError):
        provider().parse_webhook(sign(b"not json"), b"not json")


def test_a_checkout_is_a_direct_charge_on_the_business_account() -> None:
    """The amount, currency and account come from the order; the request is idempotent per booking and
    lists no payment methods."""
    client = FakeClient()
    checkout = provider(client).start_checkout(ORDER)
    assert (checkout.checkout_id, checkout.checkout_url) == ("cs_test_1", "https://checkout.stripe.com/c/pay/cs_test_1")
    _, (params, options) = client.calls[0]
    assert options == {"stripe_account": ACCOUNT, "idempotency_key": "booking-checkout-ABCDEFGH23"}
    assert params["line_items"] == [
        {
            "quantity": 1,
            "price_data": {"currency": "gbp", "unit_amount": 2500, "product_data": {"name": "Haircut, Fade & Co"}},
        }
    ]
    assert (params["mode"], params["client_reference_id"], params["expires_at"]) == (
        "payment",
        "ABCDEFGH23",
        int(ORDER.expires_at.timestamp()),
    )
    assert "payment_method_types" not in params and "application_fee_amount" not in str(params)


def test_a_new_account_lets_stripe_collect_fees_and_carry_losses() -> None:
    """Full Stripe Dashboard, Stripe as fee and loss collector, card payments requested, UK for GBP."""
    client = FakeClient()
    assert provider(client).create_account("Fade & Co", "owner@example.com", "GBP") == ACCOUNT
    _, (params,) = client.calls[0]
    assert params["dashboard"] == "full"
    assert params["defaults"] == {
        "currency": "gbp",
        "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
    }
    assert params["configuration"] == {"merchant": {"capabilities": {"card_payments": {"requested": True}}}}
    assert (params["identity"], params["contact_email"], params["display_name"]) == (
        {"country": "gb"},
        "owner@example.com",
        "Fade & Co",
    )


def test_the_sign_up_link_and_readiness_check() -> None:
    """The link is for onboarding the merchant configuration; readiness means card payments are active."""
    client = FakeClient()
    adapter = provider(client)
    assert adapter.onboarding_url(ACCOUNT, "https://book.example.com/#payments=done", "https://r") == (
        "https://connect.stripe.com/setup/test"
    )
    _, (params,) = client.calls[0]
    assert params["use_case"]["type"] == "account_onboarding"
    assert params["use_case"]["account_onboarding"]["configurations"] == ["merchant"]
    assert adapter.can_take_payments(ACCOUNT) is True
    assert provider(FakeClient(card_status="pending")).can_take_payments(ACCOUNT) is False


@pytest.mark.parametrize("call", ["checkout", "account", "link", "ready"])
def test_stripe_failures_become_the_boundary_error(call: str) -> None:
    """Any Stripe error surfaces as ProviderUnavailableError with no detail from Stripe."""
    failure = stripe.APIConnectionError("network down sk_test_secret")  # type: ignore[no-untyped-call]
    adapter = provider(FakeClient(fail=failure))
    with pytest.raises(ProviderUnavailableError) as raised:
        if call == "checkout":
            adapter.start_checkout(ORDER)
        elif call == "account":
            adapter.create_account("Fade & Co", None, "GBP")
        elif call == "link":
            adapter.onboarding_url(ACCOUNT, "https://a", "https://b")
        else:
            adapter.can_take_payments(ACCOUNT)
    assert "sk_test" not in str(raised.value)
