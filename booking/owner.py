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
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import psycopg
from psycopg import errors

from booking.service import (
    TRIAL_DAYS,
    BookingError,
    BookingNotifier,
    Business,
    NotFoundError,
    online_payments_allowed,
)
from postgres import Database

MAX_DAYS_PER_REQUEST = 62
"""The most days one bookings request may cover."""

MAX_BLOCKS = 50
"""The most working blocks a business may have across the week."""

REFERENCE_PATTERN = re.compile(r"^[A-Z0-9]{10}$")

BUSINESS_TYPES = (
    "barbershop",
    "car_wash",
    "clinic",
    "consulting",
    "education",
    "events",
    "spa",
    "photographer",
    "salon",
    "fitness",
    "other",
)
"""The kinds of business an owner can choose at sign-up. Matches the database check."""

PLANS = ("free", "starter", "pro")
"""Every plan. Matches the database check."""

SIGNUP_PLANS = ("free",)
"""Plans a business can start on. Every business starts free with a trial and upgrades later."""

PLAN_PRICES = {"starter": ("Starter", 900), "pro": ("Pro", 2900)}
"""``plan -> (display name, monthly price in pence)`` for the paid plans. Test placeholders until the real
plans and prices are decided. Every paid plan currently includes the same features as the free plan."""

PLAN_CURRENCY = "GBP"
"""The currency plans are billed in, whatever the business's own currency: prices are set in pounds only.
Provisional: may change once plan pricing for other regions is decided."""

REGIONS = {
    "uk": ("Europe/London", "GBP"),
    "ae": ("Asia/Dubai", "AED"),
    "sa": ("Asia/Riyadh", "SAR"),
    "qa": ("Asia/Qatar", "QAR"),
}
"""``region -> (IANA time zone, ISO 4217 currency)`` a business can be set up in. Both are fixed at sign-up:
bookings store amounts without a currency, and payments are checked against the business's currency, so
changing either later needs a planned move-over. Provisional: the list of regions may change; keep it in
step with ``NewBusinessBody.region`` in ``api/owner.py`` and the region choices on the sign-up and set-up pages."""

DEFAULT_HOURS = [(weekday, time(9, 0), time(17, 0)) for weekday in range(1, 6)]
"""Monday to Friday, 09:00 to 17:00, for a new business."""

DEFAULT_SERVICE = ("intro", "Introductory call", 30, 0)
"""``(slug, name, minutes, price)`` of the one free service a new business starts with."""


def slugify(name: str) -> str:
    """Turn a company name into a business slug.

    Args:
        name: The company name.

    Returns:
        Lower-case letters, digits and single hyphens, 3 to 40 characters. Falls back to ``business``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug if len(slug) >= 3 else "business"


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
        display_name: The owner's name as given at sign-up, or ``None``. Private to the dashboard.
        plan: ``free``, ``starter`` or ``pro``.
        plan_status: ``active``, or ``past_due`` when a renewal failed.
        trial_ends_at: When the business's trial ends, or ``None``.
        online_payments: The plan, or the trial, includes online payments now.
        plan_ends_at: When a cancelled paid plan ends, or ``None``.
        time_zone: The business's IANA time zone, which the owner screens show its dates in.
    """

    slug: str
    name: str
    role: str
    display_name: str | None = None
    plan: str = "free"
    plan_status: str = "active"
    trial_ends_at: datetime | None = None
    online_payments: bool = False
    plan_ends_at: datetime | None = None
    time_zone: str = "Europe/London"


@dataclass(frozen=True)
class NewBusiness:
    """What an owner gives at sign-up to create their business. Shape is validated by the API.

    Attributes:
        name: Company name, shown to customers.
        business_type: One of :data:`BUSINESS_TYPES`.
        business_type_other: A short description when the type is ``other``, else ``None``.
        owner_name: The owner's name. Private.
        plan: One of :data:`SIGNUP_PLANS`.
        contact_email: Where booking emails go, or ``None``.
        region: One of :data:`REGIONS`; sets the business's time zone and currency.
    """

    name: str
    business_type: str
    business_type_other: str | None
    owner_name: str
    plan: str
    contact_email: str | None
    region: str = "uk"


@dataclass(frozen=True)
class OwnerBooking:
    """A booking with the customer's details, for the business's own staff.

    Attributes:
        reference: Public booking reference.
        status: ``confirmed``, ``cancelled``, ``pending_payment`` (held while the customer pays) or
            ``expired`` (only listed when a payment arrived after its hold ran out, so it needs a refund).
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
        payment_status: ``not_required``, ``awaiting`` or ``paid``.
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
    payment_status: str = "not_required"


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
        cancel_cutoff_minutes: How long before a session a customer may still cancel online.
        move_cutoff_minutes: How long before a session a customer may still move it online.
        contact_email: Where the business receives booking emails, or ``None``.
        meeting_link: A link included in customer emails, or ``None``.
        public_address: Address shown to customers, or ``None``.
        public_phone: Phone shown to customers, or ``None``.
        public_email: Email shown to customers, or ``None``.
    """

    name: str
    time_zone: str
    currency: str
    break_minutes: int
    notice_minutes: int
    window_days: int
    cancel_cutoff_minutes: int = 1440
    move_cutoff_minutes: int = 1440
    contact_email: str | None = None
    meeting_link: str | None = None
    public_address: str | None = None
    public_phone: str | None = None
    public_email: str | None = None


_BOOKING_COLUMNS = (
    "b.reference, b.status, s.slug, s.name, b.starts_at, b.ends_at, b.price_minor, b.customer_name, "
    "b.customer_email, b.customer_phone, b.customer_note, b.created_at, b.payment_status"
)
_SERVICE_COLUMNS = "slug, name, duration_minutes, price_minor, active, sort_order"


class OwnerService:
    """Owner operations backed by PostgreSQL."""

    def __init__(self, database: Database, notifier: BookingNotifier | None = None) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
            notifier: Sends booking emails after an owner change is committed. ``None`` sends nothing.
        """
        self._database = database
        self._notifier = notifier

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
                "SELECT b.slug, b.name, m.role, m.display_name, b.plan, b.plan_status, b.trial_ends_at, b.plan_ends_at, "
                "b.time_zone "
                "FROM business_members m JOIN businesses b ON b.id = m.business_id "
                "WHERE m.user_id = %s ORDER BY b.name",
                (account,),
            ).fetchall()
        now = datetime.now(UTC)
        return [
            Membership(r[0], r[1], r[2], r[3], r[4], r[5], r[6], online_payments_allowed(r[4], r[6], now), r[7], r[8])
            for r in rows
        ]

    def create_business(self, account: str, new: NewBusiness) -> Membership:
        """Create the account's business with sensible defaults, in one transaction.

        The business gets a unique slug from its name, Monday to Friday 09:00 to 17:00, one free 30-minute
        service, the time zone and currency of the chosen region, payment at the venue, the account as its owner,
        the free plan and a :data:`booking.service.TRIAL_DAYS`-day trial of the paid features.

        Args:
            account: The verified account identifier.
            new: What the owner gave at sign-up.

        Returns:
            The new membership.

        Raises:
            ConflictError: If the account already has a business.
            InvalidChangeError: If the type or plan is not offered, or ``other`` has no description.
        """
        if new.business_type not in BUSINESS_TYPES or new.plan not in SIGNUP_PLANS:
            raise InvalidChangeError("That business type or plan is not available.")
        if new.region not in REGIONS:
            raise InvalidChangeError("That region is not available.")
        time_zone, currency = REGIONS[new.region]
        if (new.business_type == "other") != bool(new.business_type_other):
            raise InvalidChangeError("Describe the business when its type is other.")
        base = slugify(new.name)
        try:
            with self._database.connection() as connection:
                if connection.execute("SELECT 1 FROM business_members WHERE user_id = %s", (account,)).fetchone():
                    raise ConflictError("This account already has a business.")
                taken = {
                    row[0]
                    for row in connection.execute(
                        "SELECT slug FROM businesses WHERE slug = %s OR slug LIKE %s", (base, base + "-%")
                    ).fetchall()
                }
                slug = base
                counter = 2
                while slug in taken:
                    suffix = f"-{counter}"
                    slug = base[: 40 - len(suffix)].rstrip("-") + suffix
                    counter += 1
                row: Any = connection.execute(
                    "INSERT INTO businesses (slug, name, business_type, business_type_other, plan, contact_email, "
                    "time_zone, currency, trial_ends_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(days => %s)) "
                    "RETURNING id::text, trial_ends_at",
                    (
                        slug,
                        new.name,
                        new.business_type,
                        new.business_type_other,
                        new.plan,
                        new.contact_email,
                        time_zone,
                        currency,
                        TRIAL_DAYS,
                    ),
                ).fetchone()
                business_id, trial_ends_at = str(row[0]), row[1]
                connection.execute(
                    "INSERT INTO business_members (business_id, user_id, role, display_name) "
                    "VALUES (%s, %s, 'owner', %s)",
                    (business_id, account, new.owner_name),
                )
                service_slug, service_name, minutes, price = DEFAULT_SERVICE
                connection.execute(
                    "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor, sort_order) "
                    "VALUES (%s, %s, %s, %s, %s, 1)",
                    (business_id, service_slug, service_name, minutes, price),
                )
                for weekday, start, end in DEFAULT_HOURS:
                    connection.execute(
                        "INSERT INTO weekly_hours (business_id, weekday, start_time, end_time) VALUES (%s, %s, %s, %s)",
                        (business_id, weekday, start, end),
                    )
        except errors.UniqueViolation:
            raise ConflictError("This account already has a business.") from None
        return Membership(
            slug, new.name, "owner", new.owner_name, new.plan, "active", trial_ends_at, True, time_zone=time_zone
        )

    def bookings(self, account: str, slug: str, start: date, days: int) -> tuple[Business, list[OwnerBooking]]:
        """List bookings that start within a run of local days, with customer details.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            start: First local date.
            days: Number of days, capped at :data:`MAX_DAYS_PER_REQUEST`.

        Returns:
            The business and its bookings in start order, cancelled ones included. Holds that ran out
            unpaid are left out.

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
                "WHERE b.business_id = %s AND b.starts_at >= %s AND b.starts_at < %s "
                "AND NOT (b.status = 'expired' AND b.payment_status <> 'paid') "
                "AND NOT (b.status = 'pending_payment' AND b.hold_expires_at <= now()) ORDER BY b.starts_at",
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
    def _settings(connection: psycopg.Connection[Any], business: Business) -> Settings:
        """Read the settings of a loaded business, including the notification and manage-link rules.

        Args:
            connection: An open connection.
            business: The business.

        Returns:
            Its settings.
        """
        extra: Any = connection.execute(
            "SELECT cancel_cutoff_minutes, move_cutoff_minutes, contact_email, meeting_link, public_address, "
            "public_phone, public_email FROM businesses WHERE id = %s",
            (business.id,),
        ).fetchone()
        return Settings(
            business.name,
            business.time_zone,
            business.currency,
            business.break_minutes,
            business.notice_minutes,
            business.window_days,
            *extra,
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
            return self._settings(connection, self._member_business(connection, account, slug))

    def update_settings(self, account: str, slug: str, changes: dict[str, Any]) -> Settings:
        """Change some booking settings. Existing bookings are not affected.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            changes: Fields to change, from ``name``, ``break_minutes``, ``notice_minutes``, ``window_days``,
                ``cancel_cutoff_minutes``, ``move_cutoff_minutes``, ``contact_email``, ``meeting_link``,
                ``public_address``, ``public_phone`` and ``public_email`` (``None`` clears the last five). Values are validated by the API.

        Returns:
            The updated settings.

        Raises:
            NotFoundError: If the account does not manage the business.
        """
        allowed = {
            "name",
            "break_minutes",
            "notice_minutes",
            "window_days",
            "cancel_cutoff_minutes",
            "move_cutoff_minutes",
            "contact_email",
            "meeting_link",
            "public_address",
            "public_phone",
            "public_email",
        }
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
            return self._settings(connection, business)

    def cancel_booking(self, account: str, slug: str, reference: str) -> tuple[Business, OwnerBooking]:
        """Cancel a booking on the business's behalf. No deadline applies; the customer is emailed.

        Cancelling an already cancelled booking changes nothing and sends nothing.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            reference: The booking reference, in any letter case.

        Returns:
            The business and the booking after the change.

        Raises:
            NotFoundError: If the account does not manage the business, or it has no such booking.
        """
        wanted = reference.strip().upper()
        if not REFERENCE_PATTERN.match(wanted):
            raise NotFoundError("Booking not found.")
        with self._database.connection() as connection:
            business = self._member_business(connection, account, slug)
            changed: Any = connection.execute(
                "UPDATE bookings SET status = 'cancelled', cancelled_at = now(), cancelled_by = 'owner' "
                "WHERE business_id = %s AND reference = %s AND status = 'confirmed' RETURNING id::text",
                (business.id, wanted),
            ).fetchone()
            row = connection.execute(
                f"SELECT {_BOOKING_COLUMNS} FROM bookings b JOIN services s ON s.id = b.service_id "
                "WHERE b.business_id = %s AND b.reference = %s",
                (business.id, wanted),
            ).fetchone()
        if row is None:
            raise NotFoundError("Booking not found.")
        if changed is not None and self._notifier is not None:
            self._notifier.booking_event("cancelled", str(changed[0]), None)
        return business, OwnerBooking(*row)
