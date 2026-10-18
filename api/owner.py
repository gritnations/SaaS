"""Owner routes: what a signed-in business owner can see and change.

Every route needs ``Authorization: Bearer <token>`` from the identity provider. The token is verified
here (``api.auth``); which business the account may manage is checked by ``booking.owner``. A business
the account does not manage answers 404, the same as one that does not exist.

Routes:
    * ``GET /owner/me``: the businesses the account manages.
    * ``GET /owner/businesses/{business}/bookings``: bookings with customer details for a run of days.
    * ``GET /owner/businesses/{business}/bookings/{reference}``: one booking by reference.
    * ``GET`` and ``POST /owner/businesses/{business}/services``; ``PATCH .../services/{service}``.
    * ``GET`` and ``PUT /owner/businesses/{business}/hours``.
    * ``GET`` and ``PATCH /owner/businesses/{business}/settings``.

Responses carry customer details, so the ingress boundary's ``Cache-Control: no-store`` applies.
"""

from collections.abc import Callable
from datetime import date, datetime, time
from typing import Annotated, Protocol, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from api.auth import TokenError, TokenVerifier
from api.booking import SLUG_PATTERN, Slug
from booking.availability import session_label, time_to_minutes
from booking.owner import (
    MAX_DAYS_PER_REQUEST,
    ConflictError,
    HoursBlock,
    InvalidChangeError,
    Membership,
    OwnerBooking,
    OwnerServiceRecord,
    Settings,
)
from booking.service import Business, NotFoundError
from postgres import DatabaseUnavailableError


class OwnerBackend(Protocol):
    """What the owner routes need from the booking domain. Tests substitute a fake."""

    def memberships(self, account: str) -> list[Membership]:
        """See :meth:`booking.owner.OwnerService.memberships`.

        Args:
            account: The verified account identifier.

        Returns:
            The memberships.
        """
        ...

    def bookings(self, account: str, slug: str, start: date, days: int) -> tuple[Business, list[OwnerBooking]]:
        """See :meth:`booking.owner.OwnerService.bookings`.

        Args:
            account: The verified account identifier.
            slug: Business slug.
            start: First local date.
            days: Number of days.

        Returns:
            The business and its bookings.
        """
        ...

    def booking_by_reference(self, account: str, slug: str, reference: str) -> tuple[Business, OwnerBooking]:
        """See :meth:`booking.owner.OwnerService.booking_by_reference`.

        Args:
            account: The verified account identifier.
            slug: Business slug.
            reference: Booking reference.

        Returns:
            The business and the booking.
        """
        ...

    def services(self, account: str, slug: str) -> list[OwnerServiceRecord]:
        """See :meth:`booking.owner.OwnerService.services`.

        Args:
            account: The verified account identifier.
            slug: Business slug.

        Returns:
            The services.
        """
        ...

    def create_service(self, account: str, slug: str, service: OwnerServiceRecord) -> OwnerServiceRecord:
        """See :meth:`booking.owner.OwnerService.create_service`.

        Args:
            account: The verified account identifier.
            slug: Business slug.
            service: The new service.

        Returns:
            The stored service.
        """
        ...

    def update_service(
        self, account: str, slug: str, service_slug: str, changes: dict[str, object]
    ) -> OwnerServiceRecord:
        """See :meth:`booking.owner.OwnerService.update_service`.

        Args:
            account: The verified account identifier.
            slug: Business slug.
            service_slug: Service slug.
            changes: Fields to change.

        Returns:
            The updated service.
        """
        ...

    def hours(self, account: str, slug: str) -> list[HoursBlock]:
        """See :meth:`booking.owner.OwnerService.hours`.

        Args:
            account: The verified account identifier.
            slug: Business slug.

        Returns:
            The working blocks.
        """
        ...

    def replace_hours(self, account: str, slug: str, blocks: list[HoursBlock]) -> list[HoursBlock]:
        """See :meth:`booking.owner.OwnerService.replace_hours`.

        Args:
            account: The verified account identifier.
            slug: Business slug.
            blocks: The new set.

        Returns:
            The stored blocks.
        """
        ...

    def settings(self, account: str, slug: str) -> Settings:
        """See :meth:`booking.owner.OwnerService.settings`.

        Args:
            account: The verified account identifier.
            slug: Business slug.

        Returns:
            The settings.
        """
        ...

    def update_settings(self, account: str, slug: str, changes: dict[str, object]) -> Settings:
        """See :meth:`booking.owner.OwnerService.update_settings`.

        Args:
            account: The verified account identifier.
            slug: Business slug.
            changes: Fields to change.

        Returns:
            The updated settings.
        """
        ...


T = TypeVar("T")

ServiceSlug = Annotated[str, Path(pattern=SLUG_PATTERN)]
"""A service slug as it appears in a request path."""

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Duration = Annotated[int, Field(ge=5, le=480)]
Price = Annotated[int, Field(ge=0, le=10_000_000)]
SortOrder = Annotated[int, Field(ge=0, le=10_000)]
ClockTime = Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]


class MembershipOut(BaseModel):
    """A business the account manages.

    Attributes:
        slug: Public identifier.
        name: Display name.
        role: The account's role.
    """

    slug: str
    name: str
    role: str


class MeOut(BaseModel):
    """The answer to ``/owner/me``.

    Attributes:
        businesses: The businesses the account manages. Empty when it manages none.
    """

    businesses: list[MembershipOut]


class OwnerBookingOut(BaseModel):
    """A booking with the customer's details.

    Attributes:
        reference: Booking reference.
        status: ``confirmed`` or ``cancelled``.
        service: Service slug.
        service_name: Service name.
        date: Local date.
        time: Local start as ``HH:MM``.
        label: The session as people read it.
        starts_at: Start, ISO 8601 with offset.
        ends_at: End, ISO 8601 with offset.
        price_minor: Price charged, in minor units.
        customer_name: Customer name.
        customer_email: Customer email.
        customer_phone: Customer phone, or ``None``.
        customer_note: Customer's note, or ``None``.
        created_at: When it was booked, ISO 8601.
    """

    reference: str
    status: str
    service: str
    service_name: str
    date: date
    time: str
    label: str
    starts_at: str
    ends_at: str
    price_minor: int
    customer_name: str
    customer_email: str
    customer_phone: str | None
    customer_note: str | None
    created_at: str


class BookingsOut(BaseModel):
    """The answer to a bookings request.

    Attributes:
        time_zone: IANA time zone that dates and times are in.
        bookings: Bookings in start order.
    """

    time_zone: str
    bookings: list[OwnerBookingOut]


class ServiceRecordOut(BaseModel):
    """A service as the owner manages it.

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


class ServiceCreateBody(BaseModel):
    """A new service.

    Attributes:
        slug: Public identifier, lower-case letters, digits and hyphens.
        name: Display name.
        duration_minutes: Session length, 5 to 480.
        price_minor: Price in minor units. Paid services cannot be booked until payments exist.
        active: Whether customers can see and book it.
        sort_order: Display position.
    """

    model_config = ConfigDict(extra="forbid")

    slug: Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]
    name: Name
    duration_minutes: Duration
    price_minor: Price = 0
    active: bool = True
    sort_order: SortOrder = 0


class ServiceUpdateBody(BaseModel):
    """Changes to a service. Only the fields sent are changed.

    Attributes:
        name: Display name.
        duration_minutes: Session length, 5 to 480.
        price_minor: Price in minor units.
        active: Whether customers can see and book it.
        sort_order: Display position.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    duration_minutes: Duration | None = None
    price_minor: Price | None = None
    active: bool | None = None
    sort_order: SortOrder | None = None


class HoursBlockBody(BaseModel):
    """One working block.

    Attributes:
        weekday: 0 = Sunday ... 6 = Saturday.
        start: Local start, ``HH:MM``.
        end: Local end, ``HH:MM``, after ``start``. ``24:00`` is not accepted; use ``23:59``.
    """

    model_config = ConfigDict(extra="forbid")

    weekday: Annotated[int, Field(ge=0, le=6)]
    start: ClockTime
    end: ClockTime

    @field_validator("start", "end")
    @classmethod
    def check_time(cls, value: str) -> str:
        """Accept only a real 24-hour time.

        Args:
            value: The submitted time.

        Returns:
            The time.

        Raises:
            ValueError: If it is not a valid time.
        """
        if time_to_minutes(value) is None:
            raise ValueError("not a valid time")
        return value


class HoursBody(BaseModel):
    """The complete set of working blocks.

    Attributes:
        blocks: Every working block of the week. An empty list closes every day.
    """

    model_config = ConfigDict(extra="forbid")

    blocks: Annotated[list[HoursBlockBody], Field(max_length=50)]


class HoursOut(BaseModel):
    """The working blocks.

    Attributes:
        blocks: Blocks by weekday and start.
    """

    blocks: list[HoursBlockBody]


class SettingsOut(BaseModel):
    """The booking settings.

    Attributes:
        name: Display name.
        time_zone: IANA time zone.
        currency: ISO 4217 code.
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


class SettingsBody(BaseModel):
    """Changes to the booking settings. Only the fields sent are changed.

    Attributes:
        name: Display name.
        break_minutes: Break between sessions, 0 to 240.
        notice_minutes: Notice period, 0 to 43200 (30 days).
        window_days: Booking window, 1 to 365.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    break_minutes: Annotated[int, Field(ge=0, le=240)] | None = None
    notice_minutes: Annotated[int, Field(ge=0, le=43200)] | None = None
    window_days: Annotated[int, Field(ge=1, le=365)] | None = None


def _clock(value: time) -> str:
    """Format a time of day as ``HH:MM``.

    Args:
        value: The time.

    Returns:
        The text.
    """
    return f"{value.hour:02d}:{value.minute:02d}"


def _booking_out(business: Business, booking: OwnerBooking) -> OwnerBookingOut:
    """Convert a booking for the response, in the business's time zone.

    Args:
        business: The business.
        booking: The booking.

    Returns:
        Its public form for the owner.
    """
    zone = business.zone
    local: datetime = booking.starts_at.astimezone(zone)
    minutes = local.hour * 60 + local.minute
    duration = int((booking.ends_at - booking.starts_at).total_seconds() // 60)
    return OwnerBookingOut(
        reference=booking.reference,
        status=booking.status,
        service=booking.service_slug,
        service_name=booking.service_name,
        date=local.date(),
        time=_clock(local.time()),
        label=session_label(minutes, duration),
        starts_at=local.isoformat(),
        ends_at=booking.ends_at.astimezone(zone).isoformat(),
        price_minor=booking.price_minor,
        customer_name=booking.customer_name,
        customer_email=booking.customer_email,
        customer_phone=booking.customer_phone,
        customer_note=booking.customer_note,
        created_at=booking.created_at.astimezone(zone).isoformat(),
    )


def _service_out(service: OwnerServiceRecord) -> ServiceRecordOut:
    """Convert a service for the response.

    Args:
        service: The service.

    Returns:
        Its public form for the owner.
    """
    return ServiceRecordOut(
        slug=service.slug,
        name=service.name,
        duration_minutes=service.duration_minutes,
        price_minor=service.price_minor,
        active=service.active,
        sort_order=service.sort_order,
    )


def _hours_out(blocks: list[HoursBlock]) -> HoursOut:
    """Convert working blocks for the response.

    Args:
        blocks: The blocks.

    Returns:
        Their public form.
    """
    return HoursOut(
        blocks=[HoursBlockBody(weekday=b.weekday, start=_clock(b.start), end=_clock(b.end)) for b in blocks]
    )


def _settings_out(settings: Settings) -> SettingsOut:
    """Convert settings for the response.

    Args:
        settings: The settings.

    Returns:
        Their public form.
    """
    return SettingsOut(
        name=settings.name,
        time_zone=settings.time_zone,
        currency=settings.currency,
        break_minutes=settings.break_minutes,
        notice_minutes=settings.notice_minutes,
        window_days=settings.window_days,
    )


def _parse_time(value: str) -> time:
    """Parse a validated ``HH:MM`` time.

    Args:
        value: The time text, already validated.

    Returns:
        The time of day.
    """
    return time(int(value[:2]), int(value[3:]))


def build_router(verifier: Callable[[], TokenVerifier | None], backend: Callable[[], OwnerBackend | None]) -> APIRouter:
    """Build the owner routes.

    Args:
        verifier: Returns the token verifier, or ``None`` when sign-in is not configured.
        backend: Returns the owner service, or ``None`` when the database is not configured.

    Returns:
        The router, to be included in the API.
    """
    router = APIRouter(prefix="/owner")

    def account(authorization: Annotated[str | None, Header()] = None) -> str:
        """Verify the bearer token and give the account identifier.

        Args:
            authorization: The ``Authorization`` header.

        Returns:
            The account identifier.

        Raises:
            HTTPException: 503 when sign-in is not configured, 401 when the token is missing or invalid.
        """
        checker = verifier()
        if checker is None:
            raise HTTPException(status_code=503, detail="Sign-in is unavailable.")
        token = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
        if not token:
            raise HTTPException(status_code=401, detail="Sign-in required.", headers={"WWW-Authenticate": "Bearer"})
        try:
            return checker.verify(token)
        except TokenError:
            raise HTTPException(
                status_code=401, detail="Sign-in required.", headers={"WWW-Authenticate": "Bearer"}
            ) from None

    Account = Annotated[str, Depends(account)]

    def service() -> OwnerBackend:
        """Get the owner service, or answer 503 when there is none.

        Returns:
            The owner service.

        Raises:
            HTTPException: 503 when the database is not configured.
        """
        found = backend()
        if found is None:
            raise HTTPException(status_code=503, detail="The dashboard is unavailable.")
        return found

    def answer(call: Callable[[], T]) -> T:
        """Run an owner operation and turn its outcomes into HTTP answers.

        Args:
            call: The operation.

        Returns:
            Its result.

        Raises:
            HTTPException: 404 not found, 409 conflict, 422 invalid change, 503 database unreachable.
        """
        try:
            return call()
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except InvalidChangeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except DatabaseUnavailableError:
            raise HTTPException(status_code=503, detail="The dashboard is unavailable.") from None

    @router.get("/me", response_model=MeOut)
    def me(who: Account) -> MeOut:
        """List the businesses the signed-in account manages.

        Args:
            who: The verified account.

        Returns:
            The memberships.
        """
        found = answer(lambda: service().memberships(who))
        return MeOut(businesses=[MembershipOut(slug=m.slug, name=m.name, role=m.role) for m in found])

    @router.get("/businesses/{business}/bookings", response_model=BookingsOut)
    def bookings(
        who: Account,
        business: Slug,
        start: date,
        days: Annotated[int, Query(ge=1, le=MAX_DAYS_PER_REQUEST)] = 7,
    ) -> BookingsOut:
        """List bookings with customer details.

        Args:
            who: The verified account.
            business: Business slug.
            start: First local date.
            days: Number of days.

        Returns:
            The bookings.
        """
        found, items = answer(lambda: service().bookings(who, business, start, days))
        return BookingsOut(time_zone=found.time_zone, bookings=[_booking_out(found, item) for item in items])

    @router.get("/businesses/{business}/bookings/{reference}", response_model=OwnerBookingOut)
    def booking(
        who: Account, business: Slug, reference: Annotated[str, Path(pattern=r"^[A-Za-z0-9]{10}$")]
    ) -> OwnerBookingOut:
        """Find one booking by reference.

        Args:
            who: The verified account.
            business: Business slug.
            reference: Booking reference, any letter case.

        Returns:
            The booking with the customer's details.
        """
        found, item = answer(lambda: service().booking_by_reference(who, business, reference))
        return _booking_out(found, item)

    @router.get("/businesses/{business}/services", response_model=list[ServiceRecordOut])
    def list_services(who: Account, business: Slug) -> list[ServiceRecordOut]:
        """List all services, active or not.

        Args:
            who: The verified account.
            business: Business slug.

        Returns:
            The services.
        """
        return [_service_out(s) for s in answer(lambda: service().services(who, business))]

    @router.post("/businesses/{business}/services", response_model=ServiceRecordOut, status_code=201)
    def create_service(who: Account, business: Slug, body: ServiceCreateBody) -> ServiceRecordOut:
        """Add a service.

        Args:
            who: The verified account.
            business: Business slug.
            body: The new service.

        Returns:
            The stored service.
        """
        record = OwnerServiceRecord(
            body.slug, body.name, body.duration_minutes, body.price_minor, body.active, body.sort_order
        )
        return _service_out(answer(lambda: service().create_service(who, business, record)))

    @router.patch("/businesses/{business}/services/{service_slug}", response_model=ServiceRecordOut)
    def update_service(
        who: Account, business: Slug, service_slug: ServiceSlug, body: ServiceUpdateBody
    ) -> ServiceRecordOut:
        """Change some fields of a service.

        Args:
            who: The verified account.
            business: Business slug.
            service_slug: Service slug.
            body: The changes.

        Returns:
            The updated service.
        """
        changes = body.model_dump(exclude_none=True)
        return _service_out(answer(lambda: service().update_service(who, business, service_slug, changes)))

    @router.get("/businesses/{business}/hours", response_model=HoursOut)
    def get_hours(who: Account, business: Slug) -> HoursOut:
        """List the working blocks.

        Args:
            who: The verified account.
            business: Business slug.

        Returns:
            The blocks.
        """
        return _hours_out(answer(lambda: service().hours(who, business)))

    @router.put("/businesses/{business}/hours", response_model=HoursOut)
    def put_hours(who: Account, business: Slug, body: HoursBody) -> HoursOut:
        """Replace all working blocks.

        Args:
            who: The verified account.
            business: Business slug.
            body: The complete new set.

        Returns:
            The stored blocks.
        """
        blocks = [HoursBlock(b.weekday, _parse_time(b.start), _parse_time(b.end)) for b in body.blocks]
        return _hours_out(answer(lambda: service().replace_hours(who, business, blocks)))

    @router.get("/businesses/{business}/settings", response_model=SettingsOut)
    def get_settings(who: Account, business: Slug) -> SettingsOut:
        """Read the booking settings.

        Args:
            who: The verified account.
            business: Business slug.

        Returns:
            The settings.
        """
        return _settings_out(answer(lambda: service().settings(who, business)))

    @router.patch("/businesses/{business}/settings", response_model=SettingsOut)
    def patch_settings(who: Account, business: Slug, body: SettingsBody) -> SettingsOut:
        """Change some booking settings.

        Args:
            who: The verified account.
            business: Business slug.
            body: The changes.

        Returns:
            The updated settings.
        """
        changes = body.model_dump(exclude_none=True)
        return _settings_out(answer(lambda: service().update_settings(who, business, changes)))

    return router
