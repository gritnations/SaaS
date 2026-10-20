"""Stripe adapter: each business takes card payments into its own Stripe account.

The business is the seller. Its customers pay it directly through a Stripe-hosted checkout created on the
business's own account. Stripe charges its fees to the business and Stripe is responsible for losses on
that account. The platform holds no money and takes no fee.

What this adapter does:
    * creates a business's account and the link where the business completes its sign-up with Stripe;
    * asks Stripe whether an account can take card payments;
    * creates a hosted checkout from a :class:`payments.base.CheckoutOrder` built on the server;
    * verifies the signature of every notification before reading it.

What it does not do: it stores nothing and decides nothing about bookings. ``booking.payments`` records
verified events, checks the amount, currency and account against the database and moves the booking on.

Secrets: the API key and the webhook signing secret are read from the environment, are never logged and
never leave the server. Only test-mode keys are accepted until live payments are approved.
"""

import logging
from collections.abc import Mapping
from typing import Any

import stripe

from payments.base import (
    CheckoutOrder,
    CheckoutSession,
    HostedCheckout,
    PaymentEvent,
    ProviderNotLiveError,
    ProviderUnavailableError,
    WebhookRejectedError,
)

LOGGER = logging.getLogger(__name__)

SECRET_KEY_ENV = "STRIPE_SECRET_KEY"
WEBHOOK_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"
TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")
"""Accepted API key prefixes. Live keys are refused until live payments are approved."""

COUNTRY_BY_CURRENCY = {"GBP": "gb"}
"""The country a new account is created in, by the business's currency. Others are asked during sign-up."""

_PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")


class StripeConfigError(RuntimeError):
    """Raised when the Stripe settings are present but unusable. The message never contains a secret."""


def _text(value: object) -> str | None:
    """Return a value as text when it is a non-empty string.

    Args:
        value: Any value read from a Stripe object.

    Returns:
        The string, or ``None``.
    """
    return value if isinstance(value, str) and value else None


class StripeProvider:
    """The Stripe implementation of :class:`payments.base.PaymentProvider` and :class:`payments.base.AccountPayments`.

    Attributes:
        name: ``"stripe"``, used in API paths.
        live: ``False``: only test-mode keys are accepted.
    """

    name: str = "stripe"
    live: bool = False

    def __init__(self, client: stripe.StripeClient, webhook_secret: str) -> None:
        """Create the adapter. Performs no I/O.

        Args:
            client: The Stripe client, holding the API key.
            webhook_secret: The signing secret of the webhook endpoint.
        """
        self._client = client
        self._webhook_secret = webhook_secret

    def create_checkout(self, order_reference: str) -> CheckoutSession:
        """Refuse a checkout started from a bare order reference.

        A Stripe checkout needs an amount, a currency and the business's account, which only the booking
        layer can supply. It is started through :meth:`start_checkout` when a paid booking is made.

        Args:
            order_reference: Unused.

        Returns:
            Never returns.

        Raises:
            ProviderNotLiveError: Always.
        """
        raise ProviderNotLiveError(self.name)

    def create_account(self, business_name: str, contact_email: str | None, currency: str) -> str:
        """Create a Stripe account for a business: full Stripe Dashboard, Stripe collects its fees from the
        business and is responsible for losses, card payments requested.

        Args:
            business_name: Shown to the business during sign-up with Stripe.
            contact_email: The business's contact email, if known.
            currency: The business's upper-case ISO 4217 currency.

        Returns:
            The account identifier.

        Raises:
            ProviderUnavailableError: If Stripe refused or could not be reached.
        """
        params: dict[str, Any] = {
            "display_name": business_name,
            "dashboard": "full",
            "defaults": {
                "currency": currency.lower(),
                "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
            },
            "configuration": {"merchant": {"capabilities": {"card_payments": {"requested": True}}}},
        }
        if contact_email:
            params["contact_email"] = contact_email
        country = COUNTRY_BY_CURRENCY.get(currency.upper())
        if country is not None:
            params["identity"] = {"country": country}
        try:
            account = self._client.v2.core.accounts.create(params)  # type: ignore[arg-type]
        except stripe.StripeError as exc:
            raise self._unavailable("create account", exc) from None
        return str(account.id)

    def onboarding_url(self, account_id: str, return_url: str, refresh_url: str) -> str:
        """Get a single-use Stripe sign-up link for the account.

        Args:
            account_id: The account.
            return_url: Where Stripe sends the business afterwards.
            refresh_url: Where Stripe sends the business if the link has expired.

        Returns:
            The link.

        Raises:
            ProviderUnavailableError: If Stripe refused or could not be reached.
        """
        try:
            link = self._client.v2.core.account_links.create(
                {
                    "account": account_id,
                    "use_case": {
                        "type": "account_onboarding",
                        "account_onboarding": {
                            "configurations": ["merchant"],
                            "refresh_url": refresh_url,
                            "return_url": return_url,
                        },
                    },
                }
            )
        except stripe.StripeError as exc:
            raise self._unavailable("create onboarding link", exc) from None
        return str(link.url)

    def can_take_payments(self, account_id: str) -> bool:
        """Ask Stripe whether the account's card payments capability is active.

        Args:
            account_id: The account.

        Returns:
            ``True`` when card payments are active.

        Raises:
            ProviderUnavailableError: If Stripe refused or could not be reached.
        """
        try:
            account = self._client.v2.core.accounts.retrieve(account_id, {"include": ["configuration.merchant"]})
        except stripe.StripeError as exc:
            raise self._unavailable("retrieve account", exc) from None
        merchant = getattr(account.configuration, "merchant", None) if account.configuration else None
        capabilities = getattr(merchant, "capabilities", None)
        card_payments = getattr(capabilities, "card_payments", None)
        return getattr(card_payments, "status", None) == "active"

    def start_checkout(self, order: CheckoutOrder) -> HostedCheckout:
        """Create a Stripe-hosted checkout on the business's account (a direct charge).

        The request is idempotent per booking reference, so a retry never creates a second checkout.

        Args:
            order: The order, built on the server.

        Returns:
            The checkout.

        Raises:
            ProviderUnavailableError: If Stripe refused, could not be reached or returned no URL.
        """
        try:
            session = self._client.v1.checkout.sessions.create(
                {
                    "mode": "payment",
                    "line_items": [
                        {
                            "quantity": 1,
                            "price_data": {
                                "currency": order.currency.lower(),
                                "unit_amount": order.amount_minor,
                                "product_data": {"name": order.description},
                            },
                        }
                    ],
                    "client_reference_id": order.reference,
                    "metadata": {"booking_reference": order.reference},
                    "payment_intent_data": {"metadata": {"booking_reference": order.reference}},
                    "customer_email": order.customer_email,
                    "success_url": order.success_url,
                    "cancel_url": order.cancel_url,
                    "expires_at": int(order.expires_at.timestamp()),
                },
                {"stripe_account": order.account_id, "idempotency_key": f"booking-checkout-{order.reference}"},
            )
        except stripe.StripeError as exc:
            raise self._unavailable("create checkout", exc) from None
        if not session.url:
            raise ProviderUnavailableError("Stripe returned a checkout without a URL.")
        return HostedCheckout(checkout_id=str(session.id), checkout_url=str(session.url))

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> PaymentEvent:
        """Verify a Stripe notification's signature and read the checkout outcome from it.

        Args:
            headers: The request headers, with lowercase names. ``stripe-signature`` is required.
            body: The raw request body, exactly as received.

        Returns:
            The event. Notifications that need no action are returned with status ``"ignored"``.

        Raises:
            WebhookRejectedError: If the signature is missing, wrong or too old, or the body is malformed.
        """
        try:
            event = self._client.construct_event(body, headers.get("stripe-signature"), self._webhook_secret)
        except (stripe.SignatureVerificationError, ValueError):
            raise WebhookRejectedError("signature or body rejected") from None
        live = bool(event.livemode)
        account_id = _text(getattr(event, "account", None))
        if event.type not in (*_PAID_EVENTS, "checkout.session.async_payment_failed", "checkout.session.expired"):
            return PaymentEvent(provider=self.name, order_reference="", status="ignored", live=live)
        data = event.data.object
        session: dict[str, Any] = data.to_dict() if hasattr(data, "to_dict") else {}
        reference = _text(session.get("client_reference_id")) or ""
        if event.type in _PAID_EVENTS:
            status = "paid" if session.get("payment_status") == "paid" else "ignored"
        elif event.type == "checkout.session.expired":
            status = "expired"
        else:
            status = "failed"
        amount = session.get("amount_total")
        currency = _text(session.get("currency"))
        payment = session.get("payment_intent")
        return PaymentEvent(
            provider=self.name,
            order_reference=reference,
            status=status,
            live=live,
            checkout_id=_text(session.get("id")),
            account_id=account_id,
            amount_minor=amount if isinstance(amount, int) else None,
            currency=currency.upper() if currency else None,
            payment_id=_text(payment) or (_text(payment.get("id")) if isinstance(payment, dict) else None),
        )

    @staticmethod
    def _unavailable(action: str, exc: stripe.StripeError) -> ProviderUnavailableError:
        """Log a Stripe failure without any secret and turn it into the boundary's error.

        Args:
            action: What was being attempted.
            exc: The Stripe error.

        Returns:
            The error to raise.
        """
        LOGGER.error("stripe %s failed: %s code=%s", action, type(exc).__name__, getattr(exc, "code", None))
        return ProviderUnavailableError(f"Stripe could not {action}.")


def stripe_from_environment(environ: Mapping[str, str]) -> StripeProvider | None:
    """Build the Stripe adapter from the environment, or report that it is not configured.

    Args:
        environ: The mapping to read.

    Returns:
        The adapter, or ``None`` when neither setting is present.

    Raises:
        StripeConfigError: If only one setting is present, the key is not a test-mode key, or the
            webhook secret has the wrong shape.
    """
    key = environ.get(SECRET_KEY_ENV, "").strip()
    secret = environ.get(WEBHOOK_SECRET_ENV, "").strip()
    if not key and not secret:
        return None
    if not key or not secret:
        raise StripeConfigError(f"{SECRET_KEY_ENV} and {WEBHOOK_SECRET_ENV} must be set together.")
    if not key.startswith(TEST_KEY_PREFIXES):
        raise StripeConfigError(f"{SECRET_KEY_ENV} must be a test-mode key until live payments are approved.")
    if not secret.startswith("whsec_"):
        raise StripeConfigError(f"{WEBHOOK_SECRET_ENV} must be a webhook signing secret.")
    return StripeProvider(stripe.StripeClient(key, max_network_retries=2), secret)
