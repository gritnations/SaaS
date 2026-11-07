"""Tests for cancelling a paid booking against a real PostgreSQL database. A fake refunder stands in for Stripe.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied, including
0012. Each test uses the temporary business from ``test_manage_postgres`` and deletes everything it created.

What these tests establish: a customer cancelling a paid booking in time is refunded first and then the booking
is cancelled and marked refunded; a failed refund leaves the booking confirmed, paid and unannounced so the
customer can try again; a repeat or a late cancel never refunds; bookings that were free or paid at the venue
are never refunded; and the emails say what happened to the money. They do not establish that Stripe accepts the
refund: that is checked by hand in Stripe test mode.
"""

import os

import pytest
from test_manage_postgres import URL_ENV, World, book, world  # noqa: F401

from booking.manage import ManageService, TooLateError
from booking.notify import Notifier
from booking.owner import OwnerService
from booking.refunds import RefundUnavailableError
from payments.base import ProviderUnavailableError

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

ACCOUNT = "acct_test_refund"
PAYMENT = "pi_test_refund"


class FakeRefunds:
    """Records refunds, or fails on demand.

    Attributes:
        calls: ``(account, payment, reference)`` for each refund asked for.
        fail: When ``True``, the next refund raises :class:`payments.base.ProviderUnavailableError`.
    """

    def __init__(self) -> None:
        """Create the recorder."""
        self.calls: list[tuple[str, str, str]] = []
        self.fail = False

    def refund_booking(self, account_id: str, payment_id: str, reference: str) -> None:
        """Record a refund.

        Args:
            account_id: The business's payment account.
            payment_id: The payment.
            reference: The booking reference.

        Raises:
            ProviderUnavailableError: When :attr:`fail` is set.
        """
        if self.fail:
            raise ProviderUnavailableError("Stripe could not refund the booking.")
        self.calls.append((account_id, payment_id, reference))


def manager(world: World, refunds: FakeRefunds | None) -> ManageService:
    """Build a manage service with the fake refunder and the test mailer.

    Args:
        world: The test business.
        refunds: The refunder, or ``None`` for none.

    Returns:
        The service.
    """
    return ManageService(
        world.database,
        notifier=Notifier(world.database, world.mailer, "https://book.example.com"),
        refunds={"stripe": refunds} if refunds is not None else None,
    )


def owner_manager(world: World, refunds: FakeRefunds | None) -> OwnerService:
    """Build an owner service with the fake refunder and the test mailer.

    Args:
        world: The test business.
        refunds: The refunder, or ``None`` for none.

    Returns:
        The service.
    """
    return OwnerService(
        world.database,
        notifier=Notifier(world.database, world.mailer, "https://book.example.com"),
        refunds={"stripe": refunds} if refunds is not None else None,
    )


def set_payment(world: World, reference: str, status: str, price: int = 1500, provider: str | None = "stripe") -> None:
    """Make a booking look paid (or otherwise) and give the business a payment account.

    Args:
        world: The test business.
        reference: The booking.
        status: The payment status to set.
        price: The booking's price in minor units.
        provider: The gateway recorded as having taken the payment.
    """
    online = status in ("paid", "refunded")
    with world.database.connection() as connection:
        connection.execute("UPDATE businesses SET payment_account_id = %s WHERE slug = %s", (ACCOUNT, world.slug))
        connection.execute(
            "UPDATE bookings SET payment_status = %s, price_minor = %s, payment_id = %s, payment_provider = %s "
            "WHERE reference = %s",
            (status, price, PAYMENT if online else None, provider if online else None, reference),
        )


def stored(world: World, reference: str) -> tuple[str, str]:
    """Read a booking's status and payment status.

    Args:
        world: The test business.
        reference: The booking.

    Returns:
        ``(status, payment_status)``.
    """
    with world.database.connection() as connection:
        row = connection.execute(
            "SELECT status, payment_status FROM bookings WHERE reference = %s", (reference,)
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_cancelling_a_paid_booking_refunds_it_then_cancels_and_says_so(world: World) -> None:
    """The refund is asked for once on the business's account, the booking becomes refunded, both emails say so."""
    reference, token = book(world)
    set_payment(world, reference, "paid")
    refunds = FakeRefunds()
    world.mailer.sent.clear()
    result = manager(world, refunds).cancel(world.slug, reference, token)
    assert refunds.calls == [(ACCOUNT, PAYMENT, reference)]
    assert (result.status, result.payment_status) == ("cancelled", "refunded")
    assert stored(world, reference) == ("cancelled", "refunded")
    customer, owner = world.mailer.sent
    assert "Your card payment of £15.00 has been refunded in full." in customer.text
    assert "card payment of £15.00 was refunded in full" in owner.text


def test_a_failed_refund_leaves_the_booking_untouched_and_a_retry_works(world: World) -> None:
    """Nothing is cancelled and no email is sent when the refund fails; trying again then succeeds."""
    reference, token = book(world)
    set_payment(world, reference, "paid")
    refunds = FakeRefunds()
    service = manager(world, refunds)
    world.mailer.sent.clear()
    refunds.fail = True
    with pytest.raises(ProviderUnavailableError):
        service.cancel(world.slug, reference, token)
    assert stored(world, reference) == ("confirmed", "paid")
    assert world.mailer.sent == [] and refunds.calls == []
    refunds.fail = False
    assert service.cancel(world.slug, reference, token).payment_status == "refunded"
    assert stored(world, reference) == ("cancelled", "refunded")
    assert len(refunds.calls) == 1


def test_cancelling_twice_refunds_once(world: World) -> None:
    """A second cancel of the same booking changes nothing and asks for no second refund."""
    reference, token = book(world)
    set_payment(world, reference, "paid")
    refunds = FakeRefunds()
    service = manager(world, refunds)
    service.cancel(world.slug, reference, token)
    world.mailer.sent.clear()
    assert service.cancel(world.slug, reference, token).payment_status == "refunded"
    assert len(refunds.calls) == 1 and world.mailer.sent == []


def test_a_paid_booking_past_the_cancel_deadline_is_not_refunded(world: World) -> None:
    """Too late to cancel online: no refund is asked for and the booking stays confirmed and paid."""
    reference, token = book(world)
    set_payment(world, reference, "paid")
    with world.database.connection() as connection:
        connection.execute("UPDATE businesses SET cancel_cutoff_minutes = 5760 WHERE slug = %s", (world.slug,))
    refunds = FakeRefunds()
    with pytest.raises(TooLateError):
        manager(world, refunds).cancel(world.slug, reference, token)
    assert refunds.calls == [] and stored(world, reference) == ("confirmed", "paid")


@pytest.mark.parametrize("status", ["not_required", "at_venue"])
def test_free_and_pay_at_venue_bookings_are_never_refunded(world: World, status: str) -> None:
    """Nothing was paid online, so nothing is refunded and the payment status is kept."""
    reference, token = book(world)
    set_payment(world, reference, status, price=0 if status == "not_required" else 1500)
    refunds = FakeRefunds()
    world.mailer.sent.clear()
    manager(world, refunds).cancel(world.slug, reference, token)
    assert refunds.calls == [] and stored(world, reference) == ("cancelled", status)
    assert all("refunded" not in message.text.lower() for message in world.mailer.sent)


def test_a_paid_booking_cannot_be_cancelled_online_without_a_refunder(world: World) -> None:
    """With no way to refund, the booking is left as it was rather than cancelled with the money kept."""
    reference, token = book(world)
    set_payment(world, reference, "paid")
    with pytest.raises(RefundUnavailableError):
        manager(world, None).cancel(world.slug, reference, token)
    assert stored(world, reference) == ("confirmed", "paid")


@pytest.mark.parametrize("provider", ["paymob", None])
def test_a_booking_paid_through_a_gateway_without_a_refund_adapter_is_left_alone(
    world: World, provider: str | None
) -> None:
    """Only the gateway that took the money can give it back: another gateway's booking is never sent to Stripe."""
    reference, token = book(world)
    set_payment(world, reference, "paid", provider=provider)
    refunds = FakeRefunds()
    with pytest.raises(RefundUnavailableError):
        manager(world, refunds).cancel(world.slug, reference, token)
    with pytest.raises(RefundUnavailableError):
        owner_manager(world, refunds).cancel_booking(world.owner, world.slug, reference)
    assert refunds.calls == [] and stored(world, reference) == ("confirmed", "paid")


def test_the_owner_cancelling_a_paid_booking_refunds_it_first_and_the_customer_is_told(world: World) -> None:
    """No deadline applies to the owner; the refund is asked for once and only the customer is emailed."""
    reference, _ = book(world)
    set_payment(world, reference, "paid")
    with world.database.connection() as connection:
        connection.execute("UPDATE businesses SET cancel_cutoff_minutes = 5760 WHERE slug = %s", (world.slug,))
    refunds = FakeRefunds()
    world.mailer.sent.clear()
    _, cancelled = owner_manager(world, refunds).cancel_booking(world.owner, world.slug, reference)
    assert refunds.calls == [(ACCOUNT, PAYMENT, reference)]
    assert (cancelled.status, cancelled.payment_status) == ("cancelled", "refunded")
    assert stored(world, reference) == ("cancelled", "refunded")
    assert [m.to for m in world.mailer.sent] == ["pat@example.com"]
    assert "cancelled by the business" in world.mailer.sent[0].text
    assert "Your card payment of £15.00 has been refunded in full." in world.mailer.sent[0].text


def test_a_failed_refund_leaves_the_owner_cancel_untouched_and_a_retry_works(world: World) -> None:
    """Nothing is cancelled and no email is sent when the refund fails; trying again then succeeds."""
    reference, _ = book(world)
    set_payment(world, reference, "paid")
    refunds = FakeRefunds()
    service = owner_manager(world, refunds)
    world.mailer.sent.clear()
    refunds.fail = True
    with pytest.raises(ProviderUnavailableError):
        service.cancel_booking(world.owner, world.slug, reference)
    assert stored(world, reference) == ("confirmed", "paid") and world.mailer.sent == []
    refunds.fail = False
    service.cancel_booking(world.owner, world.slug, reference)
    service.cancel_booking(world.owner, world.slug, reference)
    assert stored(world, reference) == ("cancelled", "refunded") and len(refunds.calls) == 1


@pytest.mark.parametrize("status", ["not_required", "at_venue"])
def test_the_owner_cancelling_an_unpaid_booking_asks_for_no_refund(world: World, status: str) -> None:
    """Nothing was paid online, so nothing is refunded and the payment status is kept."""
    reference, _ = book(world)
    set_payment(world, reference, status, price=0 if status == "not_required" else 1500)
    refunds = FakeRefunds()
    owner_manager(world, refunds).cancel_booking(world.owner, world.slug, reference)
    assert refunds.calls == [] and stored(world, reference) == ("cancelled", status)
