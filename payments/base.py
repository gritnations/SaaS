"""The provider-neutral payment interface.

Everything the API needs from a payment provider is declared here. Adapters implement
:class:`PaymentProvider`. The API looks them up by name in a :class:`PaymentRegistry`.

Constraints:
    * An amount is never taken from the caller. A checkout is started from an order reference only, so
      that the price is always decided on the server.
    * A provider that is not live must refuse to act. It must not pretend to succeed.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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
        status: The reported outcome, ``"paid"`` or ``"failed"``.
        live: ``True`` only when the event describes real money.
    """

    provider: str
    order_reference: str
    status: str
    live: bool


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
