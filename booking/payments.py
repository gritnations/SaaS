"""Online payments for bookings: each business's payment account, and the effect of verified payment events.

Trust path: ``api -> booking.payments -> postgres.Database -> PostgreSQL``, with the provider reached
through ``payments.base.AccountPayments``.

A payment event reaches :meth:`PaymentService.record` only after the provider adapter has verified its
signature. Even then it confirms a booking only when everything in it matches the database: the checkout,
the booking reference, the business's payment account, the amount and the currency. Recording is
idempotent, so a repeated notification changes nothing.

State transitions of a paid booking:
    * ``pending_payment`` + paid -> ``confirmed`` (payment ``paid``), and the confirmation emails are sent
      with a new manage link.
    * ``expired`` + paid -> ``confirmed`` if the time is still free; otherwise it stays ``expired`` with
      payment ``paid``, is logged and is listed to the owner, who refunds it from their provider dashboard.
    * ``pending_payment`` + expired or failed -> ``expired``, releasing the time.
"""

import logging
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from psycopg import errors

from booking.service import MANAGE_TOKEN_BYTES, BookingNotifier, NotFoundError, hash_token
from payments.base import AccountPayments, PaymentEvent, ProviderUnavailableError
from postgres import Database

LOGGER = logging.getLogger(__name__)

_BOOKING_QUERY = (
    "SELECT b.id::text, b.reference, b.status, b.payment_status, b.price_minor, z.currency, z.payment_account_id "
    "FROM bookings b JOIN businesses z ON z.id = b.business_id "
)


@dataclass(frozen=True)
class PaymentSettings:
    """A business's online payment set-up, as its owner sees it.

    Attributes:
        provider: The payment provider's name.
        connected: The business has a payment account with the provider.
        enabled: The account can take payments, so paid services can be booked online.
    """

    provider: str
    connected: bool
    enabled: bool


class PaymentsBackend(Protocol):
    """What the API needs from the payment service. Tests substitute a fake."""

    def settings(self, account: str, slug: str) -> PaymentSettings:
        """See :meth:`PaymentService.settings`.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The payment set-up.
        """
        ...

    def connect(self, account: str, slug: str) -> str:
        """See :meth:`PaymentService.connect`.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The provider's sign-up link.
        """
        ...

    def record(self, event: PaymentEvent) -> bool:
        """See :meth:`PaymentService.record`.

        Args:
            event: A verified payment event.

        Returns:
            Whether a booking changed.
        """
        ...


class PaymentService:
    """Payment accounts and payment events, backed by PostgreSQL and one payment provider."""

    def __init__(
        self,
        database: Database,
        provider: AccountPayments,
        site_url: str,
        notifier: BookingNotifier | None = None,
    ) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
            provider: The payment provider.
            site_url: The public site, without a trailing slash, that the provider sends owners back to.
            notifier: Sends the confirmation emails once a booking is paid. ``None`` sends nothing.
        """
        self._database = database
        self._provider = provider
        self._site_url = site_url
        self._notifier = notifier

    @staticmethod
    def _member_business(
        connection: psycopg.Connection[Any], account: str, slug: str, lock: bool = False
    ) -> tuple[str, str, str, str | None, str | None, bool]:
        """Load the payment fields of a business the account manages.

        Args:
            connection: An open connection.
            account: The verified account identifier.
            slug: The business's public identifier.
            lock: Lock the business row until the transaction ends.

        Returns:
            ``(id, name, currency, contact_email, payment_account_id, payments_enabled)``.

        Raises:
            NotFoundError: If the business does not exist or the account does not manage it.
        """
        row = connection.execute(
            "SELECT b.id::text, b.name, b.currency, b.contact_email, b.payment_account_id, b.payments_enabled "
            "FROM businesses b JOIN business_members m ON m.business_id = b.id "
            "WHERE b.slug = %s AND m.user_id = %s" + (" FOR UPDATE OF b" if lock else ""),
            (slug, account),
        ).fetchone()
        if row is None:
            raise NotFoundError("Business not found.")
        return row[0], row[1], row[2], row[3], row[4], bool(row[5])

    def settings(self, account: str, slug: str) -> PaymentSettings:
        """Get the business's payment set-up, asking the provider whether a connected account is ready.

        When the provider cannot be reached, the last known state is returned.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The payment set-up.

        Raises:
            NotFoundError: If the account does not manage the business.
            postgres.DatabaseUnavailableError: If the database cannot be reached.
        """
        with self._database.connection() as connection:
            business_id, _, _, _, payment_account, enabled = self._member_business(connection, account, slug)
        if payment_account is None:
            return PaymentSettings(self._provider.name, False, False)
        try:
            ready = self._provider.can_take_payments(payment_account)
        except ProviderUnavailableError:
            return PaymentSettings(self._provider.name, True, enabled)
        if ready != enabled:
            with self._database.connection() as connection:
                connection.execute("UPDATE businesses SET payments_enabled = %s WHERE id = %s", (ready, business_id))
        return PaymentSettings(self._provider.name, True, ready)

    def connect(self, account: str, slug: str) -> str:
        """Give the business a payment account if it has none, and a link to complete its sign-up there.

        The business row is locked while the account is created, so two clicks never create two accounts.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The provider's single-use sign-up link.

        Raises:
            NotFoundError: If the account does not manage the business.
            payments.base.ProviderUnavailableError: If the provider could not be reached.
            postgres.DatabaseUnavailableError: If the database cannot be reached.
        """
        with self._database.connection() as connection:
            business_id, name, currency, contact_email, payment_account, _ = self._member_business(
                connection, account, slug, lock=True
            )
            if payment_account is None:
                payment_account = self._provider.create_account(name, contact_email, currency)
                connection.execute(
                    "UPDATE businesses SET payment_account_id = %s WHERE id = %s", (payment_account, business_id)
                )
                LOGGER.info("payment account created business=%s", slug)
        return self._provider.onboarding_url(
            payment_account, f"{self._site_url}/#payments=done", f"{self._site_url}/#payments=retry"
        )

    def record(self, event: PaymentEvent) -> bool:
        """Apply a verified payment event to its booking.

        Args:
            event: An event whose signature the provider adapter has verified.

        Returns:
            ``True`` when a booking changed, ``False`` when the event needed nothing or did not match.

        Raises:
            postgres.DatabaseUnavailableError: If the database cannot be reached, so the provider retries.
        """
        if event.status not in ("paid", "expired", "failed") or not event.checkout_id:
            return False
        token: str | None = None
        with self._database.connection() as connection:
            row = connection.execute(
                _BOOKING_QUERY + "WHERE b.checkout_id = %s FOR UPDATE OF b", (event.checkout_id,)
            ).fetchone()
            if row is None and event.order_reference:
                # The notification can, in principle, overtake the storing of the checkout identifier.
                row = connection.execute(
                    _BOOKING_QUERY + "WHERE b.reference = %s AND b.checkout_id IS NULL FOR UPDATE OF b",
                    (event.order_reference,),
                ).fetchone()
            if row is None:
                LOGGER.warning("payment event for an unknown checkout provider=%s", event.provider)
                return False
            booking_id, reference, status, payment_status, price, currency, payment_account = row
            if event.order_reference != reference or event.account_id != payment_account:
                LOGGER.error("payment event does not match its booking reference=%s", reference)
                return False
            if event.status != "paid":
                result = connection.execute(
                    "UPDATE bookings SET status = 'expired', checkout_id = %s "
                    "WHERE id = %s AND status = 'pending_payment'",
                    (event.checkout_id, booking_id),
                )
                return result.rowcount == 1
            if event.amount_minor != price or event.currency != currency:
                LOGGER.error("payment amount or currency does not match reference=%s", reference)
                return False
            if payment_status == "paid" or status not in ("pending_payment", "expired"):
                return False
            token = secrets.token_hex(MANAGE_TOKEN_BYTES)
            try:
                with connection.transaction():
                    connection.execute(
                        "UPDATE bookings SET status = 'confirmed', payment_status = 'paid', checkout_id = %s, "
                        "payment_id = %s, manage_token_hash = %s WHERE id = %s",
                        (event.checkout_id, event.payment_id, hash_token(token), booking_id),
                    )
            except errors.ExclusionViolation:
                connection.execute(
                    "UPDATE bookings SET payment_status = 'paid', checkout_id = %s, payment_id = %s WHERE id = %s",
                    (event.checkout_id, event.payment_id, booking_id),
                )
                LOGGER.error(
                    "payment arrived after the hold ran out and the time was taken: refund reference=%s", reference
                )
                return True
        LOGGER.info("booking paid and confirmed reference=%s", reference)
        if self._notifier is not None:
            self._notifier.booking_event("confirmed", booking_id, token)
        return True
