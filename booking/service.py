"""The bounded booking service: the only booking code that reaches the database.

Trust path: ``api -> booking.service -> postgres.Database -> PostgreSQL``.

The service decides everything that matters for a booking on the server: which services exist, their
price, which sessions are free and whether a requested time is a real, bookable session. The caller
supplies only identifiers, a date, a time and the customer's details.

Paid services:
    * Each business chooses how its customers pay: online through its own payment account, at the venue,
      or both (:func:`payment_options`). Online needs a ready payment account, the business's choice and
      a plan that includes it (every plan, for now: see :data:`ONLINE_PAYMENTS_NEED_PAID_PLAN`). A paid service with no way to pay raises :class:`PaymentUnavailableError`.
    * A booking paid at the venue is confirmed at once and marked ``at_venue``.
    * A booking paid online starts as a hold (``pending_payment``) that blocks its time for
      :data:`HOLD_MINUTES`, and the customer is sent to a provider-hosted checkout on the business's own
      account. Only a verified payment notification confirms it (see ``booking.payments``).
    * An unpaid hold is released lazily: :func:`release_expired_holds` marks it ``expired`` before any
      new booking or move of the same business, so no scheduler is needed.

Double-booking is prevented twice: the service checks availability, and the ``bookings_no_overlap``
exclusion constraint refuses an overlapping row even when two requests race.
"""

import hashlib
import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg import errors

from booking.availability import (
    Block,
    Slot,
    booking_window,
    day_slots,
    normalize_break,
    time_to_minutes,
    weekday_index,
)
from booking.links import CustomerLinks, as_links
from payments.base import AccountPayments, CheckoutOrder, ProviderUnavailableError
from postgres import Database

LOGGER = logging.getLogger(__name__)

MAX_DAYS_PER_REQUEST = 14
"""The most days one availability request may cover."""

REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""Booking-reference characters. Letters and digits that are easily confused are left out."""

REFERENCE_LENGTH = 10

MANAGE_TOKEN_BYTES = 24
"""Random bytes in a manage code: 48 hex characters, the only credential a manage link carries."""

PAID_PLANS = ("starter", "pro")
"""Plans billed monthly. Each includes online card payments."""

ONLINE_PAYMENTS_NEED_PAID_PLAN = False
"""Whether online card payments need a paid plan or the trial. Off: every plan includes them until plan
contents are decided. Switching it on restores the plan rule everywhere it is checked."""

TRIAL_DAYS = 14
"""Length of the trial every new business starts with: the paid features, without a card."""

CHECKOUT_MINUTES = 31
"""How long a checkout accepts payment. The provider's minimum is 30 minutes."""

HOLD_MINUTES = CHECKOUT_MINUTES + 5
"""How long a paid booking holds its time: the checkout's life plus a margin, so that a payment made in
the checkout's last minute still finds its time held."""


class BookingError(Exception):
    """Base class for booking outcomes the API reports to the caller. Messages are fixed and public."""


class NotFoundError(BookingError):
    """The business or service does not exist, or the service is not offered."""


class PaymentUnavailableError(BookingError):
    """The service has a price and the business cannot take an online payment now."""


class InvalidSlotError(BookingError):
    """The requested date and time are not a bookable session for this service."""


class SlotTakenError(BookingError):
    """The session was free when checked but another booking took it first."""


@dataclass(frozen=True)
class Business:
    """A business as the booking service needs it.

    Attributes:
        id: Internal identifier. Never sent to a browser.
        slug: Public identifier used in URLs.
        name: Display name.
        time_zone: IANA time zone name.
        currency: ISO 4217 code.
        break_minutes: Break between sessions.
        notice_minutes: How long before a session it may still be booked.
        window_days: How many days ahead, counting today, are open for booking.
        payment_account_id: The business's payment account, or ``None``.
        payments_enabled: Whether the business's payment account can take payments.
        plan: The business's plan.
        trial_ends_at: When the business's trial ends, or ``None``.
        pay_online: The business offers online payment.
        pay_at_venue: The business offers payment at the venue.
        payment_options: How paid services can be paid now, filled in by :meth:`BookingService.list_services`.
        public_address: Address shown to customers, or ``None``.
        public_phone: Phone shown to customers, or ``None``.
        public_email: Email shown to customers, or ``None``.
    """

    id: str
    slug: str
    name: str
    time_zone: str
    currency: str
    break_minutes: int
    notice_minutes: int
    window_days: int
    payment_account_id: str | None = None
    payments_enabled: bool = False
    plan: str = "free"
    trial_ends_at: datetime | None = None
    pay_online: bool = False
    pay_at_venue: bool = True
    payment_options: tuple[str, ...] = ()
    public_address: str | None = None
    public_phone: str | None = None
    public_email: str | None = None

    @property
    def zone(self) -> ZoneInfo:
        """The business's time zone.

        Returns:
            The zone object.

        Raises:
            NotFoundError: If the stored zone name is unknown, so the business cannot take bookings.
        """
        try:
            return ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise NotFoundError("Business not found.") from None


@dataclass(frozen=True)
class Service:
    """A bookable service.

    Attributes:
        id: Internal identifier. Never sent to a browser.
        slug: Public identifier.
        name: Display name.
        duration_minutes: Session length.
        price_minor: Price in minor units, such as pence. ``0`` means free.
    """

    id: str
    slug: str
    name: str
    duration_minutes: int
    price_minor: int


@dataclass(frozen=True)
class DayAvailability:
    """One day's sessions for one service.

    Attributes:
        date: The local date.
        slots: The sessions, available or not.
    """

    date: date
    slots: list[Slot]


@dataclass(frozen=True)
class BookingRequest:
    """What a customer asks for. Validated for shape by the API before it arrives here.

    Attributes:
        service: Service slug.
        date: Local date.
        time: Local start time as ``HH:MM``.
        name: Customer name.
        email: Customer email.
        phone: Customer phone, if given.
        note: Customer's note or question, if given.
        pay: ``online`` or ``venue`` for a paid service, or ``None`` to let the business's options decide,
            preferring online.
    """

    service: str
    date: date
    time: str
    name: str
    email: str
    phone: str | None
    note: str | None
    pay: str | None = None


@dataclass(frozen=True)
class Confirmation:
    """A new booking, as returned to the customer.

    Attributes:
        reference: The public booking reference.
        service: The booked service.
        slot: The booked session.
        manage_token: The manage code, given once to the person who booked. Only its hash is stored. A
            paid booking gets its code by email once the payment is confirmed.
        status: ``confirmed``, or ``pending_payment`` while a paid booking waits for its payment.
        checkout_url: Where to pay, for a booking paid online.
        payment_status: ``not_required``, ``awaiting`` (paying online) or ``at_venue``.
    """

    reference: str
    service: Service
    slot: Slot
    manage_token: str | None = None
    status: str = "confirmed"
    checkout_url: str | None = None
    payment_status: str = "not_required"


class BookingNotifier(Protocol):
    """What the booking services need to announce a change. It must never raise."""

    def booking_event(self, kind: str, booking_id: str, manage_token: str | None) -> None:
        """Send the emails for one booking event and record the attempts.

        Args:
            kind: ``confirmed``, ``cancelled`` or ``moved``.
            booking_id: The booking's internal identifier.
            manage_token: The manage code when the caller holds it, so a link can be included.
        """
        ...


class BookingBackend(Protocol):
    """What the API needs from the booking service. Tests substitute a fake."""

    def list_services(self, business_slug: str) -> tuple[Business, list[Service]]:
        """See :meth:`BookingService.list_services`.

        Args:
            business_slug: The business's public identifier.

        Returns:
            The business and its offered services.
        """
        ...

    def availability(
        self, business_slug: str, service_slug: str, start: date, days: int
    ) -> tuple[Business, Service, list[DayAvailability]]:
        """See :meth:`BookingService.availability`.

        Args:
            business_slug: The business's public identifier.
            service_slug: The service's public identifier.
            start: First local date wanted.
            days: Number of days wanted.

        Returns:
            The business, the service and each day's sessions.
        """
        ...

    def create_booking(self, business_slug: str, request: BookingRequest) -> Confirmation:
        """See :meth:`BookingService.create_booking`.

        Args:
            business_slug: The business's public identifier.
            request: What the customer asked for.

        Returns:
            The confirmed booking.
        """
        ...


def hash_token(token: str) -> bytes:
    """Hash a manage code for storage and comparison.

    Args:
        token: The manage code.

    Returns:
        Its SHA-256 digest.
    """
    return hashlib.sha256(token.encode("ascii", "replace")).digest()


def online_payments_allowed(plan: str, trial_ends_at: datetime | None, now: datetime) -> bool:
    """Decide whether a business's plan includes online card payments.

    Args:
        plan: The business's plan.
        trial_ends_at: When its trial ends, or ``None``.
        now: The current instant.

    Returns:
        ``True`` on every plan while :data:`ONLINE_PAYMENTS_NEED_PAID_PLAN` is off. Otherwise ``True`` on a
        paid plan (also while a renewal is overdue) or during the trial.
    """
    if not ONLINE_PAYMENTS_NEED_PAID_PLAN:
        return True
    return plan in PAID_PLANS or (trial_ends_at is not None and trial_ends_at > now)


def release_expired_holds(connection: psycopg.Connection[Any], business_id: str) -> None:
    """Mark a business's unpaid holds whose time has run out as ``expired``, releasing their time.

    Args:
        connection: An open connection, inside the transaction that is about to take a time.
        business_id: The business.
    """
    connection.execute(
        "UPDATE bookings SET status = 'expired' "
        "WHERE business_id = %s AND status = 'pending_payment' AND hold_expires_at <= now()",
        (business_id,),
    )


def new_reference() -> str:
    """Generate a booking reference from a cryptographically secure source.

    Returns:
        :data:`REFERENCE_LENGTH` characters from :data:`REFERENCE_ALPHABET`.
    """
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


class BookingService:
    """Booking operations backed by PostgreSQL."""

    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] | None = None,
        notifier: BookingNotifier | None = None,
        payments: AccountPayments | None = None,
        site_url: str | CustomerLinks | None = None,
    ) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
            clock: Returns the current aware instant. ``None`` means the system clock in UTC.
            notifier: Sends booking emails after a change is committed. ``None`` sends nothing.
            payments: The payment provider for paid services. ``None`` means paid services cannot be booked.
            site_url: Where checkouts return to: a site URL (staging page layout) or customer links. ``None``
                means paid services cannot be booked.
        """
        self._database = database
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._notifier = notifier
        self._payments = payments
        self._links = as_links(site_url)

    @staticmethod
    def _business(connection: psycopg.Connection[Any], slug: str) -> Business:
        """Load a business by slug.

        Args:
            connection: An open connection.
            slug: The business's public identifier.

        Returns:
            The business.

        Raises:
            NotFoundError: If no business has that slug.
        """
        row = connection.execute(
            "SELECT id::text, slug, name, time_zone, currency, break_minutes, notice_minutes, window_days, "
            "payment_account_id, payments_enabled, plan, trial_ends_at, pay_online, pay_at_venue, "
            "public_address, public_phone, public_email "
            "FROM businesses WHERE slug = %s",
            (slug,),
        ).fetchone()
        if row is None:
            raise NotFoundError("Business not found.")
        return Business(*row)

    @staticmethod
    def _service(connection: psycopg.Connection[Any], business: Business, slug: str) -> Service:
        """Load an offered service of a business by slug.

        Args:
            connection: An open connection.
            business: The business.
            slug: The service's public identifier.

        Returns:
            The service.

        Raises:
            NotFoundError: If the business offers no active service with that slug.
        """
        row = connection.execute(
            "SELECT id::text, slug, name, duration_minutes, price_minor FROM services "
            "WHERE business_id = %s AND slug = %s AND active",
            (business.id, slug),
        ).fetchone()
        if row is None:
            raise NotFoundError("Service not found.")
        return Service(*row)

    @staticmethod
    def _blocks(connection: psycopg.Connection[Any], business: Business) -> dict[int, list[Block]]:
        """Load the business's working blocks, grouped by weekday (0 = Sunday).

        Args:
            connection: An open connection.
            business: The business.

        Returns:
            Blocks per weekday. A weekday with no blocks is closed.
        """
        rows = connection.execute(
            "SELECT weekday, start_time, end_time FROM weekly_hours WHERE business_id = %s",
            (business.id,),
        ).fetchall()
        blocks: dict[int, list[Block]] = {}
        for weekday, start, end in rows:
            blocks.setdefault(weekday, []).append(Block(start.hour * 60 + start.minute, end.hour * 60 + end.minute))
        return blocks

    @staticmethod
    def _booked(
        connection: psycopg.Connection[Any],
        business: Business,
        start: datetime,
        end: datetime,
        exclude_reference: str | None = None,
    ) -> list[tuple[datetime, datetime]]:
        """Load the ranges already taken that touch a period.

        Args:
            connection: An open connection.
            business: The business.
            start: Start of the period.
            end: End of the period.
            exclude_reference: A booking to leave out, so that moving a booking does not collide with itself.

        Returns:
            ``(starts_at, blocked_until)`` for each confirmed booking and each unexpired hold overlapping
            the period, widened by the current break so that a new session cannot start inside the break
            before a booking.
        """
        gap = timedelta(minutes=normalize_break(business.break_minutes))
        rows = connection.execute(
            "SELECT starts_at, blocked_until FROM bookings WHERE business_id = %s "
            "AND (status = 'confirmed' OR (status = 'pending_payment' AND hold_expires_at > now())) "
            "AND starts_at < %s AND blocked_until > %s AND reference IS DISTINCT FROM %s",
            (business.id, end + gap, start - gap, exclude_reference),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def _days(
        self,
        connection: psycopg.Connection[Any],
        business: Business,
        service: Service,
        dates: Sequence[date],
        exclude_reference: str | None = None,
    ) -> list[DayAvailability]:
        """Build the sessions for a run of dates inside the booking window.

        Args:
            connection: An open connection.
            business: The business.
            service: The service.
            dates: Local dates, in order. Dates outside the booking window are skipped.
            exclude_reference: A booking whose own time does not count as taken.

        Returns:
            One entry per open date in the window that has at least one session.
        """
        zone = business.zone
        now = self._clock()
        first, last = booking_window(now, zone, business.window_days)
        wanted = [day for day in dates if first <= day <= last]
        if not wanted:
            return []
        period_start = datetime.combine(wanted[0], datetime.min.time(), zone)
        period_end = datetime.combine(wanted[-1] + timedelta(days=1), datetime.min.time(), zone)
        blocks = self._blocks(connection, business)
        booked = self._booked(connection, business, period_start, period_end, exclude_reference)
        earliest = now + timedelta(minutes=business.notice_minutes)
        result = []
        for day in wanted:
            slots = day_slots(
                day,
                blocks.get(weekday_index(day), []),
                service.duration_minutes,
                business.break_minutes,
                zone,
                booked,
                earliest,
            )
            if slots:
                result.append(DayAvailability(day, slots))
        return result

    def list_services(self, business_slug: str) -> tuple[Business, list[Service]]:
        """List the services a business offers.

        Args:
            business_slug: The business's public identifier.

        Returns:
            The business and its active services, in display order.

        Raises:
            NotFoundError: If the business does not exist.
            postgres.DatabaseUnavailableError: If the database cannot be reached.
        """
        with self._database.connection() as connection:
            business = self._business(connection, business_slug)
            rows = connection.execute(
                "SELECT id::text, slug, name, duration_minutes, price_minor FROM services "
                "WHERE business_id = %s AND active ORDER BY sort_order, name",
                (business.id,),
            ).fetchall()
        return replace(business, payment_options=tuple(self.payment_options(business))), [Service(*row) for row in rows]

    def availability(
        self, business_slug: str, service_slug: str, start: date, days: int
    ) -> tuple[Business, Service, list[DayAvailability]]:
        """List a service's sessions for a run of days.

        Args:
            business_slug: The business's public identifier.
            service_slug: The service's public identifier.
            start: First local date wanted.
            days: Number of days wanted, capped at :data:`MAX_DAYS_PER_REQUEST`.

        Returns:
            The business, the service and each open day's sessions inside the booking window.

        Raises:
            NotFoundError: If the business or service does not exist.
            postgres.DatabaseUnavailableError: If the database cannot be reached.
        """
        count = min(max(days, 1), MAX_DAYS_PER_REQUEST)
        dates = [start + timedelta(days=offset) for offset in range(count)]
        with self._database.connection() as connection:
            business = self._business(connection, business_slug)
            service = self._service(connection, business, service_slug)
            return business, service, self._days(connection, business, service, dates)

    def create_booking(self, business_slug: str, request: BookingRequest) -> Confirmation:
        """Book a session. A free service is confirmed at once; a paid one is held until it is paid.

        The requested time must be an available session as :meth:`availability` would list it at this
        moment. The insert runs in the same transaction as the check, and the database's exclusion
        constraint settles any race. For a paid service the checkout is created after the hold is
        committed; if that fails, the hold is released at once.

        Args:
            business_slug: The business's public identifier.
            request: What the customer asked for.

        Returns:
            The booking: confirmed, or waiting for payment with the checkout link.

        Raises:
            NotFoundError: If the business or service does not exist.
            PaymentUnavailableError: If the service has a price and the business cannot take an online
                payment now.
            InvalidSlotError: If the date and time are not an available session.
            SlotTakenError: If another booking took the session first.
            postgres.DatabaseUnavailableError: If the database cannot be reached.
        """
        if time_to_minutes(request.time) is None:
            raise InvalidSlotError("That time is not available.")
        try:
            with self._database.connection() as connection:
                business = self._business(connection, business_slug)
                service = self._service(connection, business, request.service)
                method = self._payment_method(business, service, request.pay)
                paid = method == "online"
                release_expired_holds(connection, business.id)
                days = self._days(connection, business, service, [request.date])
                slot = next(
                    (s for day in days for s in day.slots if s.time == request.time and s.available),
                    None,
                )
                if slot is None:
                    raise InvalidSlotError("That time is not available.")
                blocked_until = slot.ends_at + timedelta(minutes=normalize_break(business.break_minutes))
                reference = new_reference()
                token = None if paid else secrets.token_hex(MANAGE_TOKEN_BYTES)
                row = connection.execute(
                    "INSERT INTO bookings (business_id, service_id, reference, status, starts_at, ends_at, "
                    "blocked_until, price_minor, customer_name, customer_email, customer_phone, customer_note, "
                    "manage_token_hash, payment_status, hold_expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "CASE WHEN %s THEN now() + make_interval(mins => %s) END) "
                    "RETURNING id::text, hold_expires_at",
                    (
                        business.id,
                        service.id,
                        reference,
                        "pending_payment" if paid else "confirmed",
                        slot.starts_at,
                        slot.ends_at,
                        blocked_until,
                        service.price_minor,
                        request.name,
                        request.email,
                        request.phone,
                        request.note,
                        None if token is None else hash_token(token),
                        {"online": "awaiting", "venue": "at_venue"}.get(method, "not_required"),
                        paid,
                        HOLD_MINUTES,
                    ),
                ).fetchone()
        except errors.ExclusionViolation:
            raise SlotTakenError("That time has just been booked.") from None
        if row is None:
            raise SlotTakenError("That time has just been booked.")
        booking_id, hold_expires_at = str(row[0]), row[1]
        if not paid:
            if self._notifier is not None:
                self._notifier.booking_event("confirmed", booking_id, token)
            status = "at_venue" if method == "venue" else "not_required"
            return Confirmation(reference, service, slot, token, payment_status=status)
        checkout_url = self._start_checkout(business, service, reference, request.email, booking_id, hold_expires_at)
        return Confirmation(reference, service, slot, None, "pending_payment", checkout_url, "awaiting")

    def payment_options(self, business: Business) -> list[str]:
        """List how the business's customers can pay for a paid service now.

        Args:
            business: The business.

        Returns:
            ``online`` when it offers online payment, its plan includes it and its payment account is
            ready; ``venue`` when it offers payment at the venue. Empty when a paid service cannot be booked.
        """
        options = []
        online = (
            business.pay_online
            and online_payments_allowed(business.plan, business.trial_ends_at, self._clock())
            and self._payments is not None
            and self._links is not None
            and business.payments_enabled
            and business.payment_account_id is not None
        )
        if online:
            options.append("online")
        if business.pay_at_venue:
            options.append("venue")
        return options

    def _payment_method(self, business: Business, service: Service, wanted: str | None) -> str:
        """Choose how a booking is paid.

        Args:
            business: The business.
            service: The booked service.
            wanted: The customer's choice, or ``None``.

        Returns:
            ``free`` for a free service, else ``online`` or ``venue``.

        Raises:
            PaymentUnavailableError: If the service has a price and the wanted way, or every way, is not
                available.
        """
        if service.price_minor == 0:
            return "free"
        options = self.payment_options(business)
        if wanted is not None:
            if wanted not in options:
                raise PaymentUnavailableError("That way of paying is not available for this business.")
            return wanted
        if not options:
            raise PaymentUnavailableError("This service cannot be booked online yet.")
        return options[0]

    def _start_checkout(
        self,
        business: Business,
        service: Service,
        reference: str,
        email: str,
        booking_id: str,
        hold_expires_at: datetime,
    ) -> str:
        """Create the checkout for a committed hold and store its identifier; release the hold on failure.

        Args:
            business: The business. Its payment account is known to be set.
            service: The booked service.
            reference: The booking reference.
            email: The customer's email, prefilled at checkout.
            booking_id: The hold's internal identifier.
            hold_expires_at: When the hold runs out.

        Returns:
            The checkout link.

        Raises:
            PaymentUnavailableError: If the checkout could not be created. The hold is released.
        """
        if self._payments is None or self._links is None or business.payment_account_id is None:
            raise PaymentUnavailableError("Online payment is not available for this business yet.")
        back = self._links.booking(business.slug)
        order = CheckoutOrder(
            reference=reference,
            amount_minor=service.price_minor,
            currency=business.currency,
            description=f"{service.name}, {business.name}",
            account_id=business.payment_account_id,
            customer_email=email,
            success_url=f"{back}#paid={reference}",
            cancel_url=f"{back}#unpaid={reference}",
            expires_at=hold_expires_at - timedelta(minutes=HOLD_MINUTES - CHECKOUT_MINUTES),
        )
        try:
            checkout = self._payments.start_checkout(order)
        except ProviderUnavailableError:
            with self._database.connection() as connection:
                connection.execute(
                    "UPDATE bookings SET status = 'expired' WHERE id = %s AND status = 'pending_payment'",
                    (booking_id,),
                )
            raise PaymentUnavailableError("Online payment is unavailable right now. Please try again.") from None
        with self._database.connection() as connection:
            connection.execute("UPDATE bookings SET checkout_id = %s WHERE id = %s", (checkout.checkout_id, booking_id))
        LOGGER.info("checkout started reference=%s", reference)
        return checkout.checkout_url
