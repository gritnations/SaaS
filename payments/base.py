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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

ORDER_REFERENCE_PATTERN = r"^[A-Za-z0-9_-]{6,64}$"
"""The accepted shape of an order reference: 6 to 64 letters, digits, hyphens or underscores."""

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


@dataclass(frozen=True)
class CheckoutSession:
    """The result of starting a checkout.

    Attributes:
        provider: The name of the provider that produced the session.
        order_reference: The order the checkout is for.
        checkout_url: Where to send the payer, or ``None`` when the provider issues no URL.
        live: ``True`` only when real money can move. A stub always reports ``False``.
    """

    provider: str
    order_reference: str
    checkout_url: str | None
    live: bool


@dataclass(frozen=True)
class PaymentEvent:
    """A payment notification after the provider has parsed it.

    Attributes:
        provider: The name of the provider that sent the notification.
        order_reference: The order the notification is about.
        status: The reported outcome: ``"paid"``, ``"failed"``, ``"expired"``, or ``"ignored"`` for a
            verified notification that needs no action.
        live: ``True`` only when the event describes real money.
        checkout_id: The provider's checkout identifier, when the notification is about a checkout.
        account_id: The business's payment account the notification came from, if any.
        amount_minor: The amount paid, in minor units, as the provider reports it.
        currency: The currency paid, as an upper-case ISO 4217 code.
        payment_id: The provider's payment identifier, when known.
    """

    provider: str
    order_reference: str
    status: str
    live: bool
    checkout_id: str | None = None
    account_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    payment_id: str | None = None


@dataclass(frozen=True)
class CheckoutOrder:
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

    reference: str
    amount_minor: int
    currency: str
    description: str
    account_id: str
    customer_email: str
    success_url: str
    cancel_url: str
    expires_at: datetime


@dataclass(frozen=True)
class HostedCheckout:
    """A checkout page created by the provider.

    Attributes:
        checkout_id: The provider's identifier, stored with the booking.
        checkout_url: Where to send the payer.
    """

    checkout_id: str
    checkout_url: str


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
