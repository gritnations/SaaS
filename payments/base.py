"""The provider-neutral payment interface.

Everything the API needs from a payment provider is declared here. Adapters implement
:class:`PaymentProvider`. The API looks them up by name in a :class:`PaymentRegistry`.

Constraints:
    * An amount is never taken from the caller. A checkout is started from an order reference only, so
      that the price is always decided on the server.
    * A provider that is not live must refuse to act. It must not pretend to succeed.

Providers that let each business take payments into its own account also implement
:class:`AccountPayments`. The booking layer, not the caller, builds every :class:`CheckoutOrder`, so the
amount always comes from the server.
"""

import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, StringConstraints

from contracts import ORDER_REFERENCE_PATTERN as ORDER_REFERENCE_PATTERN  # re-exported for the API
from contracts import (
    STRICT,
    AwareDatetime,
    Currency,
    OrderReference,
    PaidAmountMinor,
    ProviderId,
    ProviderName,
    ReportedAmountMinor,
    ReturnUrl,
    SecureUrl,
)

_ORDER_REFERENCE_RE = re.compile(ORDER_REFERENCE_PATTERN)


class PaymentError(Exception):
    """Base class for every error raised by the payment boundary."""


class UnknownProviderError(PaymentError):
    """Raised when no provider is registered under the requested name."""


class ProviderNotLiveError(PaymentError):
    """Raised when a provider exists structurally but is not yet able to process payments."""


class WebhookRejectedError(PaymentError):
    """Raised when a webhook payload is malformed or cannot be trusted."""


class ProviderUnavailableError(PaymentError):
    """Raised when the provider could not be reached or refused a request. The message holds no secret."""


def is_valid_order_reference(value: str) -> bool:
    """Check an order reference against :data:`ORDER_REFERENCE_PATTERN`.

    Args:
        value: The candidate reference.

    Returns:
        ``True`` if the reference has the accepted shape.
    """
    return bool(_ORDER_REFERENCE_RE.fullmatch(value))


class CheckoutSession(BaseModel):
    """The result of starting a checkout.

    Attributes:
        provider: The name of the provider that produced the session.
        order_reference: The order the checkout is for.
        checkout_url: Where to send the payer, or ``None`` when the provider issues no URL.
        live: ``True`` only when real money can move. A stub always reports ``False``.
    """

    model_config = STRICT

    provider: ProviderName
    order_reference: OrderReference
    checkout_url: SecureUrl | None
    live: bool


class BookingPaymentEvent(BaseModel):
    """A verified notification about a customer's payment for a booking.

    Only the provider, the outcome and whether it is live are always present. The other fields are checked
    against the booking in the database before anything changes, and an event that lacks what its outcome
    needs is left alone there.

    Attributes:
        provider: The provider that sent the notification.
        status: The outcome: ``"paid"``, ``"failed"``, ``"expired"``, or ``"refunded"`` (a payment was refunded
            in full).
        live: ``True`` only when the event describes real money.
        order_reference: The booking the notification names, when it names one.
        checkout_id: The provider's checkout identifier, when the notification is about a checkout.
        account_id: The business's payment account the notification came from, if any.
        amount_minor: The amount paid, in minor units, as the provider reports it.
        currency: The currency paid, as an upper-case ISO 4217 code.
        payment_id: The provider's payment identifier, when known.
        event_id: The provider's own identifier for this notification, used to recognise a repeat. ``None``
            when the provider sends none.
    """

    model_config = STRICT

    provider: ProviderName
    status: Literal["paid", "failed", "expired", "refunded"]
    live: bool
    order_reference: OrderReference | None = None
    checkout_id: ProviderId | None = None
    account_id: ProviderId | None = None
    amount_minor: ReportedAmountMinor | None = None
    currency: Currency | None = None
    payment_id: ProviderId | None = None
    event_id: ProviderId | None = None


class SubscriptionEvent(BaseModel):
    """A verified notification about a business's own subscription to a plan, billed to the platform.

    Attributes:
        provider: The provider that sent the notification.
        status: The outcome: ``"subscription_active"``, ``"subscription_past_due"`` or ``"subscription_ended"``.
        live: ``True`` only when the event describes real money.
        business_id: The business the subscription belongs to, as recorded when it was started.
        subscription_id: The provider's subscription identifier.
        customer_id: The provider's customer identifier, when known.
        plan: The plan the subscription is for, as recorded when it was started. Checked against the paid
            plans when the subscription becomes active.
        ends_at: When a cancelled subscription will end, or ``None`` when it is not set to end. Only events
            about the subscription itself carry it; ``ends_known`` says whether this one does.
        ends_known: Whether the event reports the subscription's end date (``ends_at`` may still be ``None``).
    """

    model_config = STRICT

    provider: ProviderName
    status: Literal["subscription_active", "subscription_past_due", "subscription_ended"]
    live: bool
    business_id: uuid.UUID
    subscription_id: ProviderId
    customer_id: ProviderId | None = None
    plan: str | None = None
    ends_at: AwareDatetime | None = None
    ends_known: bool = False


class IgnoredEvent(BaseModel):
    """A verified notification that needs no action, or one too incomplete to act on.

    Attributes:
        provider: The provider that sent the notification.
        live: ``True`` only when the event describes real money.
        status: Always ``"ignored"``, so that every event reports an outcome in the same way.
    """

    model_config = STRICT

    provider: ProviderName
    live: bool
    status: Literal["ignored"] = "ignored"


PaymentEvent = BookingPaymentEvent | SubscriptionEvent | IgnoredEvent
"""What a provider adapter returns for a verified notification. The record's type says who handles it."""


class SubscriptionOrder(BaseModel):
    """A business's subscription to a paid plan, billed to the platform. Every value is decided on the server.

    Attributes:
        business_id: The business, recorded on the subscription so that notifications can be matched.
        plan: The plan key, such as ``starter``.
        plan_name: The plan's display name.
        amount_minor: The monthly price in minor units.
        currency: Upper-case ISO 4217 code.
        customer_email: Prefilled when the business has no customer record yet.
        customer_id: The business's existing customer record with the provider, if any.
        success_url: Where the owner returns after subscribing.
        cancel_url: Where the owner returns after giving up.
    """

    model_config = STRICT

    business_id: str
    plan: Annotated[str, StringConstraints(pattern=r"^(starter|pro)$")]
    plan_name: str
    amount_minor: PaidAmountMinor
    currency: Currency
    customer_email: str | None
    customer_id: ProviderId | None
    success_url: ReturnUrl
    cancel_url: ReturnUrl


class CancellationQuote(BaseModel):
    """What cancelling a business's plan now refunds. Worked out on the server; a browser never sends it.

    Attributes:
        refund_minor: The refund in minor units, ``0`` when nothing is owed.
        currency: Upper-case ISO 4217 code.
        unused_days: Whole days of the paid period that are not used, for showing to the owner.
    """

    model_config = STRICT

    refund_minor: ReportedAmountMinor
    currency: Currency
    unused_days: Annotated[int, Field(ge=0)]


@runtime_checkable
class SubscriptionBilling(Protocol):
    """A provider through which the platform bills businesses for their plan.

    Attributes:
        name: The provider name, as in :class:`PaymentProvider`.
        live: Whether real money can move.
    """

    name: str
    live: bool

    def start_subscription(self, order: SubscriptionOrder) -> "HostedCheckout":
        """Create a hosted checkout that starts a monthly subscription on the platform's own account.

        Args:
            order: The order, built on the server.

        Returns:
            The checkout.

        Raises:
            ProviderUnavailableError: If the provider could not create it.
        """
        ...

    def billing_portal_url(self, customer_id: str, return_url: str) -> str:
        """Get a single-use link to the provider's page where a business manages its subscription.

        Args:
            customer_id: The business's customer record.
            return_url: Where the provider sends the owner afterwards.

        Returns:
            The link.

        Raises:
            ProviderUnavailableError: If the provider could not create it.
        """
        ...

    def quote_cancellation(self, subscription_id: str, now: datetime) -> CancellationQuote:
        """Work out what cancelling the subscription at ``now`` would refund, without changing anything.

        The refund is the unused share of the last paid period, rounded down and never more than was paid.

        Args:
            subscription_id: The business's subscription.
            now: The moment of cancellation.

        Returns:
            The quote.

        Raises:
            ProviderUnavailableError: If the provider could not be asked.
        """
        ...

    def cancel_with_refund(self, subscription_id: str, now: datetime) -> CancellationQuote:
        """Refund the unused part of the last paid period, then cancel the subscription at once.

        The refund is made first, so a failed refund never leaves a cancelled plan, and never twice for one
        subscription: a retry after a failed cancel finds the refund already made.

        Args:
            subscription_id: The business's subscription.
            now: The moment of cancellation.

        Returns:
            What was refunded.

        Raises:
            ProviderUnavailableError: If the refund or the cancellation failed.
        """
        ...


class CheckoutOrder(BaseModel):
    """A checkout the booking layer asks a provider to host. Every value is decided on the server.

    Attributes:
        reference: The booking reference the payment is for.
        amount_minor: The price in minor units, from the database.
        currency: Upper-case ISO 4217 code.
        description: What the customer sees they are paying for.
        account_id: The business's payment account. The payment goes to it directly.
        customer_email: Prefilled on the checkout page.
        success_url: Where the payer returns after paying.
        cancel_url: Where the payer returns after giving up.
        expires_at: When the checkout stops accepting payment.
    """

    model_config = STRICT

    reference: OrderReference
    amount_minor: PaidAmountMinor
    currency: Currency
    description: Annotated[str, StringConstraints(min_length=1)]
    account_id: ProviderId
    customer_email: str
    success_url: ReturnUrl
    cancel_url: ReturnUrl
    expires_at: AwareDatetime


class HostedCheckout(BaseModel):
    """A checkout page created by the provider.

    Attributes:
        checkout_id: The provider's identifier, stored with the booking.
        checkout_url: Where to send the payer.
    """

    model_config = STRICT

    checkout_id: ProviderId
    checkout_url: SecureUrl


@runtime_checkable
class BookingRefunds(Protocol):
    """A provider that can give a customer's booking payment back, on the business's own account."""

    def refund_booking(self, account_id: str, payment_id: str, reference: str) -> None:
        """Refund a booking's payment in full. Safe to repeat: a booking is never refunded twice.

        Args:
            account_id: The business's payment account, where the payment was taken.
            payment_id: The payment to refund.
            reference: The booking reference, recorded on the refund.

        Raises:
            ProviderUnavailableError: If the provider refused or could not be reached.
        """
        ...


@runtime_checkable
class AccountPayments(Protocol):
    """A provider through which each business takes payments into its own account.

    Attributes:
        name: The provider name, as in :class:`PaymentProvider`.
        live: Whether real money can move.
    """

    name: str
    live: bool

    def create_account(self, business_name: str, contact_email: str | None, currency: str) -> str:
        """Create a payment account for a business.

        Args:
            business_name: Shown to the business during sign-up with the provider.
            contact_email: The business's contact email, if known.
            currency: The business's upper-case ISO 4217 currency.

        Returns:
            The account identifier.

        Raises:
            ProviderUnavailableError: If the provider could not create it.
        """
        ...

    def onboarding_url(self, account_id: str, return_url: str, refresh_url: str) -> str:
        """Get a single-use link where the business completes its sign-up with the provider.

        Args:
            account_id: The account.
            return_url: Where the provider sends the business afterwards.
            refresh_url: Where the provider sends the business if the link has expired.

        Returns:
            The link.

        Raises:
            ProviderUnavailableError: If the provider could not create it.
        """
        ...

    def can_take_payments(self, account_id: str) -> bool:
        """Ask the provider whether the account can take card payments now.

        Args:
            account_id: The account.

        Returns:
            ``True`` when it can.

        Raises:
            ProviderUnavailableError: If the provider could not be asked.
        """
        ...

    def start_checkout(self, order: CheckoutOrder) -> HostedCheckout:
        """Create a hosted checkout for an order, on the business's own account.

        Args:
            order: The order, built on the server.

        Returns:
            The checkout.

        Raises:
            ProviderUnavailableError: If the provider could not create it.
        """
        ...


@runtime_checkable
class PaymentProvider(Protocol):
    """What every payment provider adapter must offer.

    Attributes:
        name: The short lowercase name used in API paths, for example ``"paymob"``.
        live: Whether the adapter can process real payments.
    """

    name: str
    live: bool

    def create_checkout(self, order_reference: str) -> CheckoutSession:
        """Start a checkout for an order.

        Args:
            order_reference: A reference that passes :func:`is_valid_order_reference`.

        Returns:
            The session describing where the payer should go.

        Raises:
            ProviderNotLiveError: If the provider cannot process payments yet.
        """
        ...

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> PaymentEvent:
        """Verify and parse a provider notification.

        Args:
            headers: The request headers, with lowercase names.
            body: The raw request body, exactly as received.

        Returns:
            The parsed event.

        Raises:
            ProviderNotLiveError: If the provider cannot process payments yet.
            WebhookRejectedError: If the payload is malformed or cannot be trusted.
        """
        ...


class PaymentRegistry:
    """The set of providers the API may use, looked up by name."""

    def __init__(self, providers: Iterable[PaymentProvider]) -> None:
        """Register the given providers.

        Args:
            providers: The adapters to make available. Names must be unique.

        Raises:
            ValueError: If two providers share a name.
        """
        self._providers: dict[str, PaymentProvider] = {}
        for provider in providers:
            if provider.name in self._providers:
                raise ValueError(f"duplicate payment provider name: {provider.name}")
            self._providers[provider.name] = provider

    def get(self, name: str) -> PaymentProvider:
        """Return the provider registered under a name.

        Args:
            name: The provider name from the request path.

        Returns:
            The matching adapter.

        Raises:
            UnknownProviderError: If no provider has that name.
        """
        try:
            return self._providers[name]
        except KeyError:
            raise UnknownProviderError(name) from None

    def names(self) -> list[str]:
        """List the registered provider names.

        Returns:
            The names in alphabetical order.
        """
        return sorted(self._providers)
