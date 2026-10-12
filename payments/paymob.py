"""Paymob adapter. Structural only: it is not live.

Paymob is the first real provider the platform will support. This module fixes where its code lives and
which interface it implements. It makes no network call and reads no credential.

It must stay non-live until all of the following are implemented and verified against Paymob's current
published documentation and a real test account: webhook signature verification, replay protection,
idempotency, server-side amount verification, explicit payment-state transitions and secret isolation.
The existing Node adapter under ``schedule-booking/`` is a reference only. Its calls were written from
documentation and never verified against a real account.
"""

from collections.abc import Mapping

from payments.base import CheckoutSession, PaymentEvent, ProviderNotLiveError


class PaymobProvider:
    """The Paymob implementation of :class:`payments.base.PaymentProvider`.

    Attributes:
        name: ``"paymob"``, used in API paths.
        live: ``False``. Every operation refuses to run.
    """

    name: str = "paymob"
    live: bool = False

    def create_checkout(self, order_reference: str) -> CheckoutSession:
        """Refuse to start a checkout, because the adapter is not live.

        Args:
            order_reference: The order the checkout would be for. Unused.

        Returns:
            Never returns.

        Raises:
            ProviderNotLiveError: Always.
        """
        raise ProviderNotLiveError(self.name)

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> PaymentEvent:
        """Refuse to accept a notification, because no signature check exists yet.

        Args:
            headers: The request headers. Unused.
            body: The raw request body. Unused.

        Returns:
            Never returns.

        Raises:
            ProviderNotLiveError: Always. An unverified notification must never be treated as payment.
        """
        raise ProviderNotLiveError(self.name)
