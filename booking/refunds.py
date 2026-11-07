"""Giving a customer's card payment back when a paid booking is cancelled.

Trust path: ``api -> booking.manage / booking.owner -> booking.refunds -> payments (provider adapter)``.

Each booking records the gateway that took its payment. A refund goes to the adapter registered for that
gateway, on the business's own account there. A gateway without a refund adapter cannot refund automatically:
the booking is then left as it was, never cancelled with the customer's money kept.
"""

from collections.abc import Mapping

from booking.service import BookingError
from payments.base import BookingRefunds


class RefundUnavailableError(BookingError):
    """A paid booking cannot be refunded automatically, so it was not cancelled."""


def refund_paid_booking(
    refunds: Mapping[str, BookingRefunds] | None,
    provider: str | None,
    account: str | None,
    payment_id: str | None,
    reference: str,
) -> None:
    """Refund a paid booking in full through the gateway that took the payment.

    Args:
        refunds: Refund adapters by gateway name. ``None`` means none.
        provider: The gateway that took the payment, as recorded on the booking.
        account: The business's account with that gateway.
        payment_id: The gateway's identifier for the payment.
        reference: The booking reference, recorded on the refund.

    Raises:
        RefundUnavailableError: If the gateway is unknown, has no refund adapter, or the payment is not on record.
        payments.base.ProviderUnavailableError: If the gateway refused or could not be reached. Nothing was
            refunded unless the gateway says so; a repeat is safe.
    """
    adapter = refunds.get(provider) if refunds is not None and provider else None
    if adapter is None or not account or not payment_id:
        raise RefundUnavailableError("This booking was paid online and cannot be refunded automatically.")
    adapter.refund_booking(account, payment_id, reference)
