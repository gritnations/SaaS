"""Secondary payment provider: a stub.

Its only purpose is to prove that the API depends on the payment interface and not on Paymob. It moves no
money, contacts no service and holds no secret.

Security constraint:
    The stub accepts notifications without a signature. It must therefore never be registered in
    production. ``api/main.py`` registers it only outside production.
"""

import json
from collections.abc import Mapping

from payments.base import CheckoutSession, PaymentEvent, WebhookRejectedError, is_valid_order_reference

_STATUSES = frozenset({"paid", "failed"})


class StubProvider:
    """A provider that answers with fixed, clearly non-live results.

    Attributes:
        name: ``"stub"``, used in API paths.
        live: ``False``. Nothing it returns represents real money.
    """

    name: str = "stub"
    live: bool = False

    def create_checkout(self, order_reference: str) -> CheckoutSession:
        """Return a non-live session for the order.

        Args:
            order_reference: The order the checkout is for.

        Returns:
            A session with no checkout URL and ``live`` set to ``False``.
        """
        return CheckoutSession(provider=self.name, order_reference=order_reference, checkout_url=None, live=False)

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> PaymentEvent:
        """Parse a stub notification.

        The body must be a JSON object with exactly an ``order_reference`` and a ``status`` of ``"paid"``
        or ``"failed"``.

        Args:
            headers: The request headers. Unused, because the stub has no signature.
            body: The raw request body.

        Returns:
            The parsed event, with ``live`` set to ``False``.

        Raises:
            WebhookRejectedError: If the body is not that JSON object.
        """
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise WebhookRejectedError("body is not valid JSON") from None
        if not isinstance(payload, dict) or set(payload) != {"order_reference", "status"}:
            raise WebhookRejectedError("unexpected fields")
        reference, status = payload["order_reference"], payload["status"]
        if not isinstance(reference, str) or not is_valid_order_reference(reference):
            raise WebhookRejectedError("invalid order reference")
        if status not in _STATUSES:
            raise WebhookRejectedError("invalid status")
        return PaymentEvent(provider=self.name, order_reference=reference, status=str(status), live=False)
