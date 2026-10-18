"""The owner side of the booking service: what a signed-in owner may see and change.

Trust path: ``api -> booking.owner -> postgres.Database -> PostgreSQL``.

Every operation starts from an account identifier that the API has already verified, and a business
slug. :meth:`OwnerService._member_business` joins the two through ``business_members``; when the account
does not manage the business, the answer is :class:`booking.service.NotFoundError`, exactly as for a
business that does not exist, so the routes reveal nothing about other businesses.

Customer details leave the database only through this module, and only to a member of the business.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import psycopg
from psycopg import errors

from booking.service import BookingError, Business, NotFoundError
from postgres import Database

MAX_DAYS_PER_REQUEST = 62
"""The most days one bookings request may cover."""

MAX_BLOCKS = 50
"""The most working blocks a business may have across the week."""

REFERENCE_PATTERN = re.compile(r"^[A-Z0-9]{10}$")


class ConflictError(BookingError):
    """The change would clash with existing data, such as a service identifier already in use."""


class InvalidChangeError(BookingError):
    """The change is well formed but not allowed, such as a working block that ends before it starts."""


@dataclass(frozen=True)
class Membership:
    """A business an account may manage.

    Attributes:
        slug: The business's public identifier.
        name: Display name.
        role: The account's role, currently always ``owner``.
    """

    slug: str
    name: str
    role: str


@dataclass(frozen=True)
class OwnerBooking:
    """A booking with the customer's details, for the business's own staff.

    Attributes:
        reference: Public booking reference.
        status: ``confirmed`` or ``cancelled``.
        service_slug: The booked service's identifier.
        service_name: The booked service's name.
        starts_at: Start, aware.
        ends_at: End, aware.
        price_minor: Price charged, in minor units.
        customer_name: Customer name.
        customer_email: Customer email.
        customer_phone: Customer phone, if given.
        customer_note: Customer's note, if given.
        created_at: When the booking was made.
    """

    reference: str
    status: str
    service_slug: str
    service_name: str
    starts_at: datetime
    ends_at: datetime
    price_minor: int
    customer_name: str
    customer_email: str
    customer_phone: str | None
    customer_note: str | None
    created_at: datetime


@dataclass(frozen=True)
class OwnerServiceRecord:
    """A service as the owner manages it, including inactive ones.

    Attributes:
        slug: Public identifier.
        name: Display name.
        duration_minutes: Session length.
        price_minor: Price in minor units.
        active: Whether customers can see and book it.
        sort_order: Display position.
    """

    slug: str
    name: str
    duration_minutes: int
    price_minor: int
    active: bool
    sort_order: int


@dataclass(frozen=True)
class HoursBlock:
    """One working block.

    Attributes:
        weekday: 0 = Sunday ... 6 = Saturday.
        start: Local start time.
        end: Local end time, after ``start``.
    """

    weekday: int
    start: time
    end: time


@dataclass(frozen=True)
class Settings:
    """The business's booking settings.

    Attributes:
        name: Display name.
        time_zone: IANA time zone. Read-only here.
        currency: ISO 4217 code. Read-only here.
        break_minutes: Break between sessions.
        notice_minutes: How long before a session it may still be booked.
        window_days: How many days ahead, counting today, are open for booking.
    """

    name: str
    time_zone: str
    currency: str
    break_minutes: int
    notice_minutes: int
    window_days: int


_BOOKING_COLUMNS = (
    "b.reference, b.status, s.slug, s.name, b.starts_at, b.ends_at, b.price_minor, b.customer_name, "
    "b.customer_email, b.customer_phone, b.customer_note, b.created_at"
)
_SERVICE_COLUMNS = "slug, name, duration_minutes, price_minor, active, sort_order"


class OwnerService:
    """Owner operations backed by PostgreSQL."""

    def __init__(self, database: Database) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
        """
        self._database = database

    @staticmethod
    def _member_business(connection: psycopg.Connection[Any], account: str, slug: str) -> Business:
        """Load a business the account manages.

        Args:
            connection: An open connection.
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The business.

        Raises:
            NotFoundError: If the business does not exist or the account does not manage it.
        """
        row = connection.execute(
            "SELECT b.id::text, b.slug, b.name, b.time_zone, b.currency, b.break_minutes, b.notice_minutes, "
            "b.window_days FROM businesses b JOIN business_members m ON m.business_id = b.id "
            "WHERE b.slug = %s AND m.user_id = %s",
            (slug, account),
        ).fetchone()
        if row is None:
            raise NotFoundError("Business not found.")
        return Business(*row)

    def memberships(self, account: str) -> list[Membership]:
        """List the businesses an account manages.

        Args:
            account: The verified account identifier.

        Returns:
            The memberships, by business name.
        """
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT b.slug, b.name, m.role FROM business_members m JOIN businesses b ON b.id = m.business_id "
                "WHERE m.user_id = %s ORDER BY b.name",
                (account,),
            ).fetchall()
        return [Membership(*row) for row in rows]

    def bookings(self, account: str, slug: str, start: date, days: int) -> tuple[Business, list[OwnerBooking]]:
        """List bookings that start within a run of local days, with customer details.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            start: First local date.
            days: Number of days, capped at :data:`MAX_DAYS_PER_REQUEST`.

        Returns:
            The business and its bookings in start order, cancelled ones included.

        Raises:
            NotFoundError: If the account does not manage the business.
        """
        count = min(max(days, 1), MAX_DAYS_PER_REQUEST)
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug)
            zone = business.zone
            period_start = datetime.combine(start, time.min, zone)
            period_end = datetime.combine(start + timedelta(days=count), time.min, zone)
            rows = connection.execute(
                f"SELECT {_BOOKING_COLUMNS} FROM bookings b JOIN services s ON s.id = b.service_id "
                "WHERE b.business_id = %s AND b.starts_at >= %s AND b.starts_at < %s ORDER BY b.starts_at",
                (business.id, period_start, period_end),
            ).fetchall()
        return business, [OwnerBooking(*row) for row in rows]

    def booking_by_reference(self, account: str, slug: str, reference: str) -> tuple[Business, OwnerBooking]:
        """Find one booking by its reference.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            reference: The booking reference, in any letter case.

        Returns:
            The business and the booking.

        Raises:
            NotFoundError: If the account does not manage the business, or the business has no booking
                with that reference.
        """
        wanted = reference.strip().upper()
        if not REFERENCE_PATTERN.match(wanted):
            raise NotFoundError("Booking not found.")
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug)
            row = connection.execute(
                f"SELECT {_BOOKING_COLUMNS} FROM bookings b JOIN services s ON s.id = b.service_id "
                "WHERE b.business_id = %s AND b.reference = %s",
                (business.id, wanted),
            ).fetchone()
        if row is None:
            raise NotFoundError("Booking not found.")
        return business, OwnerBooking(*row)

    def services(self, account: str, slug: str) -> list[OwnerServiceRecord]:
        """List all services of the business, active or not.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The services in display order.

        Raises:
            NotFoundError: If the account does not manage the business.
        """
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug)
            rows = connection.execute(
                f"SELECT {_SERVICE_COLUMNS} FROM services WHERE business_id = %s ORDER BY sort_order, name",
                (business.id,),
            ).fetchall()
        return [OwnerServiceRecord(*row) for row in rows]

    def create_service(self, account: str, slug: str, service: OwnerServiceRecord) -> OwnerServiceRecord:
        """Add a service.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            service: The new service. Its shape is validated by the API.

        Returns:
            The stored service.

        Raises:
            NotFoundError: If the account does not manage the business.
            ConflictError: If the business already has a service with that identifier.
        """
        try:
            with self._database.connection() as connection:
                business = self._member_business(connection, account, slug)
                row = connection.execute(
                    "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor, active, sort_order) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_SERVICE_COLUMNS}",
                    (
                        business.id,
                        service.slug,
                        service.name,
                        service.duration_minutes,
                        service.price_minor,
                        service.active,
                        service.sort_order,
                    ),
                ).fetchone()
        except errors.UniqueViolation:
            raise ConflictError("A service with that identifier already exists.") from None
        return OwnerServiceRecord(*row)  # type: ignore[misc]

    def update_service(self, account: str, slug: str, service_slug: str, changes: dict[str, Any]) -> OwnerServiceRecord:
        """Change some fields of a service. Existing bookings keep the times and price they were made with.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            service_slug: The service's identifier.
            changes: Fields to change, from ``name``, ``duration_minutes``, ``price_minor``, ``active`` and
                ``sort_order``. Values are validated by the API.

        Returns:
            The updated service.

        Raises:
            NotFoundError: If the account does not manage the business or the service does not exist.
        """
        allowed = {"name", "duration_minutes", "price_minor", "active", "sort_order"}
        fields = [field for field in changes if field in allowed]
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug)
            if fields:
                assignments = ", ".join(f"{field} = %s" for field in fields)
                row = connection.execute(
                    f"UPDATE services SET {assignments} WHERE business_id = %s AND slug = %s "
                    f"RETURNING {_SERVICE_COLUMNS}",
                    (*[changes[field] for field in fields], business.id, service_slug),
                ).fetchone()
            else:
                row = connection.execute(
                    f"SELECT {_SERVICE_COLUMNS} FROM services WHERE business_id = %s AND slug = %s",
                    (business.id, service_slug),
                ).fetchone()
        if row is None:
            raise NotFoundError("Service not found.")
        return OwnerServiceRecord(*row)

    def hours(self, account: str, slug: str) -> list[HoursBlock]:
        """List the working blocks.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The blocks by weekday and start.

        Raises:
            NotFoundError: If the account does not manage the business.
        """
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug)
            rows = connection.execute(
                "SELECT weekday, start_time, end_time FROM weekly_hours WHERE business_id = %s "
                "ORDER BY weekday, start_time",
                (business.id,),
            ).fetchall()
        return [HoursBlock(*row) for row in rows]

    def replace_hours(self, account: str, slug: str, blocks: list[HoursBlock]) -> list[HoursBlock]:
        """Replace all working blocks in one transaction. Existing bookings are not affected.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            blocks: The complete new set. An empty list closes every day.

        Returns:
            The stored blocks.

        Raises:
            NotFoundError: If the account does not manage the business.
            InvalidChangeError: If a block ends before it starts, a weekday is out of range, or there are
                more than :data:`MAX_BLOCKS` blocks.
        """
        if len(blocks) > MAX_BLOCKS:
            raise InvalidChangeError(f"At most {MAX_BLOCKS} working blocks are allowed.")
        for block in blocks:
            if not 0 <= block.weekday <= 6 or block.end <= block.start:
                raise InvalidChangeError("Each working block must end after it starts, on a weekday 0 to 6.")
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug)
            connection.execute("DELETE FROM weekly_hours WHERE business_id = %s", (business.id,))
            for block in blocks:
                connection.execute(
                    "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) VALUES (%s, %s, %s, %s)",
                    (business.id, block.weekday, block.start, block.end),
                )
        return sorted(blocks, key=lambda block: (block.weekday, block.start))

    @staticmethod
    def _settings(business: Business) -> Settings:
        """Read the settings out of a loaded business.

        Args:
            business: The business.

        Returns:
            Its settings.
        """
        return Settings(
            business.name,
            business.time_zone,
            business.currency,
            business.break_minutes,
            business.notice_minutes,
            business.window_days,
        )

    def settings(self, account: str, slug: str) -> Settings:
        """Read the booking settings.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The settings.

        Raises:
            NotFoundError: If the account does not manage the business.
        """
        with self._database.connection() as connection:
            return self._settings(self._member_business(connection, account, slug))

    def update_settings(self, account: str, slug: str, changes: dict[str, Any]) -> Settings:
        """Change some booking settings. Existing bookings are not affected.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            changes: Fields to change, from ``name``, ``break_minutes``, ``notice_minutes`` and
                ``window_days``. Values are validated by the API.

        Returns:
            The updated settings.

        Raises:
            NotFoundError: If the account does not manage the business.
        """
        allowed = {"name", "break_minutes", "notice_minutes", "window_days"}
        fields = [field for field in changes if field in allowed]
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug)
            if fields:
                assignments = ", ".join(f"{field} = %s" for field in fields)
                connection.execute(
                    f"UPDATE businesses SET {assignments} WHERE id = %s",
                    (*[changes[field] for field in fields], business.id),
                )
                business = self._member_business(connection, account, slug)
            return self._settings(business)
