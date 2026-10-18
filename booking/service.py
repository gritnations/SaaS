"""The bounded booking service: the only booking code that reaches the database.

Trust path: ``api -> booking.service -> postgres.Database -> PostgreSQL``.

The service decides everything that matters for a booking on the server: which services exist, their
price, which sessions are free and whether a requested time is a real, bookable session. The caller
supplies only identifiers, a date, a time and the customer's details.

Slice 1 limits:
    * Only free services can be booked. A paid service raises :class:`PaymentUnavailableError`.
    * There is no cancellation, rescheduling or hold.

Double-booking is prevented twice: the service checks availability, and the ``bookings_no_overlap``
exclusion constraint refuses an overlapping row even when two requests race.
"""

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
from postgres import Database

MAX_DAYS_PER_REQUEST = 14
"""The most days one availability request may cover."""

REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""Booking-reference characters. Letters and digits that are easily confused are left out."""

REFERENCE_LENGTH = 10


class BookingError(Exception):
    """Base class for booking outcomes the API reports to the caller. Messages are fixed and public."""


class NotFoundError(BookingError):
    """The business or service does not exist, or the service is not offered."""


class PaymentUnavailableError(BookingError):
    """The service has a price and payments are not available yet."""


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
    """

    id: str
    slug: str
    name: str
    time_zone: str
    currency: str
    break_minutes: int
    notice_minutes: int
    window_days: int

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
    """

    service: str
    date: date
    time: str
    name: str
    email: str
    phone: str | None
    note: str | None


@dataclass(frozen=True)
class Confirmation:
    """A confirmed booking, as returned to the customer.

    Attributes:
        reference: The public booking reference.
        service: The booked service.
        slot: The booked session.
    """

    reference: str
    service: Service
    slot: Slot


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


def new_reference() -> str:
    """Generate a booking reference from a cryptographically secure source.

    Returns:
        :data:`REFERENCE_LENGTH` characters from :data:`REFERENCE_ALPHABET`.
    """
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


class BookingService:
    """Booking operations backed by PostgreSQL."""

    def __init__(self, database: Database, clock: Callable[[], datetime] | None = None) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
            clock: Returns the current aware instant. ``None`` means the system clock in UTC.
        """
        self._database = database
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

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
            "SELECT id::text, slug, name, time_zone, currency, break_minutes, notice_minutes, window_days "
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
        connection: psycopg.Connection[Any], business: Business, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Load the ranges already taken that touch a period.

        Args:
            connection: An open connection.
            business: The business.
            start: Start of the period.
            end: End of the period.

        Returns:
            ``(starts_at, blocked_until)`` for each confirmed booking overlapping the period, widened by
            the current break so that a new session cannot start inside the break before a booking.
        """
        gap = timedelta(minutes=normalize_break(business.break_minutes))
        rows = connection.execute(
            "SELECT starts_at, blocked_until FROM bookings "
            "WHERE business_id = %s AND status = 'confirmed' AND starts_at < %s AND blocked_until > %s",
            (business.id, end + gap, start - gap),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def _days(
        self,
        connection: psycopg.Connection[Any],
        business: Business,
        service: Service,
        dates: Sequence[date],
    ) -> list[DayAvailability]:
        """Build the sessions for a run of dates inside the booking window.

        Args:
            connection: An open connection.
            business: The business.
            service: The service.
            dates: Local dates, in order. Dates outside the booking window are skipped.

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
        booked = self._booked(connection, business, period_start, period_end)
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
        return business, [Service(*row) for row in rows]

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
        """Book a session of a free service.

        The requested time must be an available session as :meth:`availability` would list it at this
        moment. The insert runs in the same transaction as the check, and the database's exclusion
        constraint settles any race.

        Args:
            business_slug: The business's public identifier.
            request: What the customer asked for.

        Returns:
            The confirmed booking.

        Raises:
            NotFoundError: If the business or service does not exist.
            PaymentUnavailableError: If the service has a price.
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
                if service.price_minor > 0:
                    raise PaymentUnavailableError("Online payment is not available yet.")
                days = self._days(connection, business, service, [request.date])
                slot = next(
                    (s for day in days for s in day.slots if s.time == request.time and s.available),
                    None,
                )
                if slot is None:
                    raise InvalidSlotError("That time is not available.")
                blocked_until = slot.ends_at + timedelta(minutes=normalize_break(business.break_minutes))
                reference = new_reference()
                connection.execute(
                    "INSERT INTO bookings (business_id, service_id, reference, status, starts_at, ends_at, "
                    "blocked_until, price_minor, customer_name, customer_email, customer_phone, customer_note) "
                    "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        business.id,
                        service.id,
                        reference,
                        slot.starts_at,
                        slot.ends_at,
                        blocked_until,
                        service.price_minor,
                        request.name,
                        request.email,
                        request.phone,
                        request.note,
                    ),
                )
        except errors.ExclusionViolation:
            raise SlotTakenError("That time has just been booked.") from None
        return Confirmation(reference, service, slot)
