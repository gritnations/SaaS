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

Online payments need a paid plan or the trial (:func:`booking.service.online_payments_allowed`): without
one, a business cannot connect a payment account or switch online payment on, and an existing choice stops
counting until the plan includes it again.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import psycopg
from psycopg import errors

from booking.service import (
    MANAGE_TOKEN_BYTES,
    BookingError,
    BookingNotifier,
    NotFoundError,
    hash_token,
    online_payments_allowed,
)
from payments.base import AccountPayments, PaymentEvent, ProviderUnavailableError
from postgres import Database

LOGGER = logging.getLogger(__name__)

_BOOKING_QUERY = (
    "SELECT b.id::text, b.reference, b.status, b.payment_status, b.price_minor, z.currency, z.payment_account_id "
    "FROM bookings b JOIN businesses z ON z.id = b.business_id "
)


class PlanRequiredError(BookingError):
    """The change needs a plan that includes online payments."""


class InvalidOptionsError(BookingError):
    """The payment options asked for are not allowed, such as offering no way to pay."""


@dataclass(frozen=True)
class PaymentSettings:
    """A business's payment set-up, as its owner sees it.

    Attributes:
        provider: The payment provider's name.
        connected: The business has a payment account with the provider.
        enabled: The account can take payments.
        online_allowed: The business's plan, or its trial, includes online payments.
        pay_online: The business offers online payment.
        pay_at_venue: The business offers payment at the venue.
    """

    provider: str
    connected: bool
    enabled: bool
    online_allowed: bool = False
    pay_online: bool = False
    pay_at_venue: bool = True


@dataclass(frozen=True)
class _PaymentRow:
    """The payment fields of a business, loaded for one operation.

    Attributes:
        id: Internal identifier.
        name: Display name.
        currency: ISO 4217 code.
        contact_email: Contact email, or ``None``.
        payment_account_id: The provider account, or ``None``.
        payments_enabled: The account can take payments.
        plan: The plan.
        trial_ends_at: When the trial ends, or ``None``.
        pay_online: The business offers online payment.
        pay_at_venue: The business offers payment at the venue.
    """

    id: str
    name: str
    currency: str
    contact_email: str | None
    payment_account_id: str | None
    payments_enabled: bool
    plan: str
    trial_ends_at: datetime | None
    pay_online: bool
    pay_at_venue: bool

    @property
    def online_allowed(self) -> bool:
        """Whether the plan, or the trial, includes online payments now.

        Returns:
            ``True`` when it does.
        """
        return online_payments_allowed(self.plan, self.trial_ends_at, datetime.now(UTC))


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

    def set_options(
        self, account: str, slug: str, pay_online: bool | None, pay_at_venue: bool | None
    ) -> PaymentSettings:
        """See :meth:`PaymentService.set_options`.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            pay_online: Offer online payment, or ``None`` to leave it.
            pay_at_venue: Offer payment at the venue, or ``None`` to leave it.

        Returns:
            The payment set-up.
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
    ) -> _PaymentRow:
        """Load the payment fields of a business the account manages.

        Args:
            connection: An open connection.
            account: The verified account identifier.
            slug: The business's public identifier.
            lock: Lock the business row until the transaction ends.

        Returns:
            The business's payment fields.

        Raises:
            NotFoundError: If the business does not exist or the account does not manage it.
        """
        row = connection.execute(
            "SELECT b.id::text, b.name, b.currency, b.contact_email, b.payment_account_id, b.payments_enabled, "
            "b.plan, b.trial_ends_at, b.pay_online, b.pay_at_venue "
            "FROM businesses b JOIN business_members m ON m.business_id = b.id "
            "WHERE b.slug = %s AND m.user_id = %s" + (" FOR UPDATE OF b" if lock else ""),
            (slug, account),
        ).fetchone()
        if row is None:
            raise NotFoundError("Business not found.")
        return _PaymentRow(*row)

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
            business = self._member_business(connection, account, slug)
        ready = business.payments_enabled
        if business.payment_account_id is not None:
            try:
                ready = self._provider.can_take_payments(business.payment_account_id)
            except ProviderUnavailableError:
                ready = business.payments_enabled
            if ready != business.payments_enabled:
                with self._database.connection() as connection:
                    connection.execute(
                        "UPDATE businesses SET payments_enabled = %s WHERE id = %s", (ready, business.id)
                    )
        return self._settings_of(business, ready)

    def _settings_of(
        self, business: _PaymentRow, ready: bool, pay_online: bool | None = None, pay_at_venue: bool | None = None
    ) -> PaymentSettings:
        """Describe a business's payment set-up.

        Args:
            business: The business.
            ready: Whether its payment account can take payments.
            pay_online: The online choice, when it has just changed.
            pay_at_venue: The venue choice, when it has just changed.

        Returns:
            The set-up.
        """
        return PaymentSettings(
            self._provider.name,
            business.payment_account_id is not None,
            ready,
            business.online_allowed,
            business.pay_online if pay_online is None else pay_online,
            business.pay_at_venue if pay_at_venue is None else pay_at_venue,
        )

    def set_options(
        self, account: str, slug: str, pay_online: bool | None, pay_at_venue: bool | None
    ) -> PaymentSettings:
        """Choose how the business's customers pay for paid services.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            pay_online: Offer online payment, or ``None`` to leave it.
            pay_at_venue: Offer payment at the venue, or ``None`` to leave it.

        Returns:
            The payment set-up.

        Raises:
            NotFoundError: If the account does not manage the business.
            PlanRequiredError: If online payment is switched on without a plan that includes it.
            InvalidOptionsError: If online payment is switched on before the payment account is ready, or no
                way to pay would be left.
        """
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug, lock=True)
            online = business.pay_online if pay_online is None else pay_online
            venue = business.pay_at_venue if pay_at_venue is None else pay_at_venue
            if pay_online and not business.online_allowed:
                raise PlanRequiredError("Online payments come with the Starter and Pro plans.")
            if pay_online and not business.payments_enabled:
                raise InvalidOptionsError("Finish connecting Stripe before switching online payments on.")
            if not online and not venue:
                raise InvalidOptionsError("Keep at least one way for customers to pay.")
            connection.execute(
                "UPDATE businesses SET pay_online = %s, pay_at_venue = %s WHERE id = %s", (online, venue, business.id)
            )
        return self._settings_of(business, business.payments_enabled, online, venue)

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
            PlanRequiredError: If the business's plan does not include online payments.
            payments.base.ProviderUnavailableError: If the provider could not be reached.
            postgres.DatabaseUnavailableError: If the database cannot be reached.
        """
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug, lock=True)
            if not business.online_allowed:
                raise PlanRequiredError("Online payments come with the Starter and Pro plans.")
            payment_account = business.payment_account_id
            if payment_account is None:
                payment_account = self._provider.create_account(
                    business.name, business.contact_email, business.currency
                )
                connection.execute(
                    "UPDATE businesses SET payment_account_id = %s WHERE id = %s", (payment_account, business.id)
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
