"""Stripe adapter: each business takes card payments into its own Stripe account.

The business is the seller. Its customers pay it directly through a Stripe-hosted checkout created on the
business's own account. Stripe charges its fees to the business and Stripe is responsible for losses on
that account. The platform holds no money and takes no fee.

Separately, the platform bills each business for its plan: a monthly subscription on the platform's own
Stripe account, started from a :class:`payments.base.SubscriptionOrder` built on the server.

What this adapter does:
    * creates a business's account and the link where the business completes its sign-up with Stripe;
    * asks Stripe whether an account can take card payments;
    * creates a hosted checkout from a :class:`payments.base.CheckoutOrder` built on the server;
    * creates a subscription checkout and a customer-portal link for a business's plan;
    * verifies the signature of every notification before reading it. Notifications from businesses'
      accounts and from the platform's own account come from two webhook destinations with two secrets; each
      secret is trusted only for its own kind of notification.

What it does not do: it stores nothing and decides nothing about bookings. ``booking.payments`` records
verified events, checks the amount, currency and account against the database and moves the booking on.

Secrets: the API key and the webhook signing secret are read from the environment, are never logged and
never leave the server. Only test-mode keys are accepted until live payments are approved.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import stripe

from payments.base import (
    CheckoutOrder,
    CheckoutSession,
    HostedCheckout,
    PaymentEvent,
    ProviderNotLiveError,
    ProviderUnavailableError,
    SubscriptionOrder,
    WebhookRejectedError,
)

LOGGER = logging.getLogger(__name__)

SECRET_KEY_ENV = "STRIPE_SECRET_KEY"
WEBHOOK_SECRET_ENV = "STRIPE_WEBHOOK_SECRET"
BILLING_WEBHOOK_SECRET_ENV = "STRIPE_BILLING_WEBHOOK_SECRET"
TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")
"""Accepted API key prefixes. Live keys are refused until live payments are approved."""

COUNTRY_BY_CURRENCY = {"GBP": "gb"}
"""The country a new account is created in, by the business's currency. Others are asked during sign-up."""

_PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
_SUBSCRIPTION_STATUS = {
    "active": "subscription_active",
    "past_due": "subscription_past_due",
    "unpaid": "subscription_past_due",
    "canceled": "subscription_ended",
    "incomplete_expired": "subscription_ended",
}
"""How a Stripe subscription status maps to the boundary's statuses. Others need no action."""


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
    """The Stripe implementation of :class:`payments.base.PaymentProvider`, :class:`payments.base.AccountPayments`
    and :class:`payments.base.SubscriptionBilling`.

    Attributes:
        name: ``"stripe"``, used in API paths.
        live: ``False``: only test-mode keys are accepted.
    """

    name: str = "stripe"
    live: bool = False

    def __init__(
        self, client: stripe.StripeClient, webhook_secret: str, billing_webhook_secret: str | None = None
    ) -> None:
        """Create the adapter. Performs no I/O.

        Args:
            client: The Stripe client, holding the API key.
            webhook_secret: The signing secret of the destination for events from businesses' accounts.
            billing_webhook_secret: The signing secret of the destination for events from the platform's own
                account, which carries plan subscriptions. ``None`` means plans cannot be billed.
        """
        self._client = client
        self._webhook_secret = webhook_secret
        self._billing_webhook_secret = billing_webhook_secret
        self._prices: dict[str, str] = {}

    @property
    def bills_plans(self) -> bool:
        """Whether plan subscriptions can be billed: their notifications can be verified.

        Returns:
            ``True`` when the billing webhook secret is configured.
        """
        return self._billing_webhook_secret is not None

    def _plan_price(self, order: SubscriptionOrder) -> str:
        """Find, or create once, the monthly Stripe price for a plan at its current amount.

        The lookup key includes the amount and currency, so a changed price in code gets a new Stripe price.

        Args:
            order: The subscription order.

        Returns:
            The price identifier.

        Raises:
            stripe.StripeError: If Stripe refused or could not be reached.
        """
        key = f"gn_plan_{order.plan}_monthly_{order.currency.lower()}_{order.amount_minor}"
        if key in self._prices:
            return self._prices[key]
        found = self._client.v1.prices.list({"lookup_keys": [key], "active": True, "limit": 1})
        if found.data:
            price_id = str(found.data[0].id)
        else:
            created = self._client.v1.prices.create(
                {
                    "currency": order.currency.lower(),
                    "unit_amount": order.amount_minor,
                    "recurring": {"interval": "month"},
                    "lookup_key": key,
                    "product_data": {"name": f"{order.plan_name} plan"},
                    "metadata": {"plan": order.plan},
                }
            )
            price_id = str(created.id)
        self._prices[key] = price_id
        return price_id

    def start_subscription(self, order: SubscriptionOrder) -> HostedCheckout:
        """Create a Stripe-hosted checkout that starts the business's monthly plan subscription.

        Args:
            order: The order, built on the server.

        Returns:
            The checkout.

        Raises:
            ProviderUnavailableError: If Stripe refused, could not be reached or returned no URL.
        """
        marks = {"business_id": order.business_id, "plan": order.plan}
        try:
            params: dict[str, Any] = {
                "mode": "subscription",
                "line_items": [{"price": self._plan_price(order), "quantity": 1}],
                "client_reference_id": order.business_id,
                "metadata": marks,
                "subscription_data": {"metadata": marks},
                "success_url": order.success_url,
                "cancel_url": order.cancel_url,
            }
            if order.customer_id:
                params["customer"] = order.customer_id
            elif order.customer_email:
                params["customer_email"] = order.customer_email
            session = self._client.v1.checkout.sessions.create(params)  # type: ignore[arg-type]
        except stripe.StripeError as exc:
            raise self._unavailable("start subscription", exc) from None
        if not session.url:
            raise ProviderUnavailableError("Stripe returned a checkout without a URL.")
        return HostedCheckout(checkout_id=str(session.id), checkout_url=str(session.url))

    def billing_portal_url(self, customer_id: str, return_url: str) -> str:
        """Get a single-use link to Stripe's customer portal for the business's plan.

        Args:
            customer_id: The business's Stripe customer.
            return_url: Where Stripe sends the owner afterwards.

        Returns:
            The link.

        Raises:
            ProviderUnavailableError: If Stripe refused or could not be reached.
        """
        try:
            portal = self._client.v1.billing_portal.sessions.create({"customer": customer_id, "return_url": return_url})
        except stripe.StripeError as exc:
            raise self._unavailable("open billing portal", exc) from None
        return str(portal.url)

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
        signature = headers.get("stripe-signature")
        secrets = [(self._webhook_secret, True)]
        if self._billing_webhook_secret is not None:
            secrets.append((self._billing_webhook_secret, False))
        event = None
        for secret, from_businesses in secrets:
            try:
                event = self._client.construct_event(body, signature, secret)
            except (stripe.SignatureVerificationError, ValueError):
                continue
            break
        if event is None:
            raise WebhookRejectedError("signature or body rejected")
        live = bool(event.livemode)
        account_id = _text(getattr(event, "account", None))
        if (account_id is not None) != from_businesses:
            # Each destination's secret is trusted only for its own kind of notification.
            return PaymentEvent(provider=self.name, order_reference="", status="ignored", live=live)
        if not from_businesses:
            return self._subscription_event(event, live)
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

    def _subscription_event(self, event: Any, live: bool) -> PaymentEvent:
        """Read a plan subscription outcome from a verified notification from the platform's own account.

        Args:
            event: The verified event.
            live: Whether it describes real money.

        Returns:
            The event, or one with status ``"ignored"`` when it needs no action.
        """
        data = event.data.object
        item: dict[str, Any] = data.to_dict() if hasattr(data, "to_dict") else {}
        raw_marks = item.get("metadata")
        marks: dict[str, Any] = raw_marks if isinstance(raw_marks, dict) else {}
        status = "ignored"
        ends_at: datetime | None = None
        ends_known = False
        if event.type == "checkout.session.completed" and item.get("mode") == "subscription":
            if item.get("payment_status") in ("paid", "no_payment_required"):
                status = "subscription_active"
            subscription, customer = item.get("subscription"), item.get("customer")
            reference = _text(item.get("client_reference_id")) or _text(marks.get("business_id")) or ""
        elif event.type in ("customer.subscription.created", "customer.subscription.updated"):
            status = _SUBSCRIPTION_STATUS.get(str(item.get("status")), "ignored")
            subscription, customer = item.get("id"), item.get("customer")
            reference = _text(marks.get("business_id")) or ""
            ends_at, ends_known = self._subscription_end(item), True
        elif event.type == "customer.subscription.deleted":
            status = "subscription_ended"
            subscription, customer = item.get("id"), item.get("customer")
            reference = _text(marks.get("business_id")) or ""
        else:
            return PaymentEvent(provider=self.name, order_reference="", status="ignored", live=live)
        return PaymentEvent(
            provider=self.name,
            order_reference=reference,
            status=status,
            live=live,
            subscription_id=_text(subscription)
            or (_text(subscription.get("id")) if isinstance(subscription, dict) else None),
            customer_id=_text(customer) or (_text(customer.get("id")) if isinstance(customer, dict) else None),
            plan=_text(marks.get("plan")),
            ends_at=ends_at,
            ends_known=ends_known,
        )

    @staticmethod
    def _subscription_end(item: dict[str, Any]) -> datetime | None:
        """Read when a cancelled subscription will end.

        Args:
            item: The subscription object.

        Returns:
            ``cancel_at``, or the current period's end when it is set to cancel at period end, or ``None``.
        """
        moment = item.get("cancel_at")
        if not isinstance(moment, int) and item.get("cancel_at_period_end"):
            raw_items = item.get("items")
            items: dict[str, Any] = raw_items if isinstance(raw_items, dict) else {}
            data = items.get("data")
            first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
            moment = item.get("current_period_end") or first.get("current_period_end")
        return datetime.fromtimestamp(moment, UTC) if isinstance(moment, int) else None

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
        The adapter, or ``None`` when neither required setting is present. ``STRIPE_BILLING_WEBHOOK_SECRET``
        is optional; without it plans cannot be billed.

    Raises:
        StripeConfigError: If only one required setting is present, the key is not a test-mode key, or a
            webhook secret has the wrong shape.
    """
    key = environ.get(SECRET_KEY_ENV, "").strip()
    secret = environ.get(WEBHOOK_SECRET_ENV, "").strip()
    billing = environ.get(BILLING_WEBHOOK_SECRET_ENV, "").strip() or None
    if not key and not secret:
        return None
    if not key or not secret:
        raise StripeConfigError(f"{SECRET_KEY_ENV} and {WEBHOOK_SECRET_ENV} must be set together.")
    if not key.startswith(TEST_KEY_PREFIXES):
        raise StripeConfigError(f"{SECRET_KEY_ENV} must be a test-mode key until live payments are approved.")
    if not secret.startswith("whsec_"):
        raise StripeConfigError(f"{WEBHOOK_SECRET_ENV} must be a webhook signing secret.")
    if billing is not None and (not billing.startswith("whsec_") or billing == secret):
        raise StripeConfigError(f"{BILLING_WEBHOOK_SECRET_ENV} must be the platform destination's own signing secret.")
    if billing is not None and not key:
        raise StripeConfigError(f"{BILLING_WEBHOOK_SECRET_ENV} needs {SECRET_KEY_ENV}.")
    return StripeProvider(stripe.StripeClient(key, max_network_retries=2), secret, billing)
