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
from api.booking import EMAIL_PATTERN, SLUG_PATTERN, Slug
from booking.availability import session_label, time_to_minutes
from booking.owner import (
    BUSINESS_TYPES,
    MAX_DAYS_PER_REQUEST,
    PLANS,
    ConflictError,
    HoursBlock,
    InvalidChangeError,
    Membership,
    NewBusiness,
    OwnerBooking,
    OwnerServiceRecord,
    Settings,
)
from booking.payments import PaymentsBackend
from booking.service import Business, NotFoundError
from payments.base import ProviderUnavailableError
from postgres import DatabaseUnavailableError


class OwnerBackend(Protocol):
    """What the owner routes need from the booking domain. Tests substitute a fake."""

    def create_business(self, account: str, new: NewBusiness) -> Membership:
        """See :meth:`booking.owner.OwnerService.create_business`.

        Args:
            account: The verified account identifier.
            new: What the owner gave at sign-up.

        Returns:
            The new membership.
        """
        ...

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

    def cancel_booking(self, account: str, slug: str, reference: str) -> tuple[Business, OwnerBooking]:
        """See :meth:`booking.owner.OwnerService.cancel_booking`.

        Args:
            account: The verified account identifier.
            slug: Business slug.
            reference: Booking reference.

        Returns:
            The business and the booking after the change.
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
        display_name: The owner's name as given at sign-up, or ``None``.
    """

    slug: str
    name: str
    role: str
    display_name: str | None = None


class NewBusinessBody(BaseModel):
    """The sign-up details that create the account's business.

    Attributes:
        name: Company name, shown to customers.
        business_type: The kind of business.
        business_type_other: A short description, required when the type is ``other``.
        owner_name: The owner's name. Private.
        plan: The chosen plan. Only ``free`` is available.
        contact_email: Where booking emails go. Optional.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    business_type: Annotated[str, StringConstraints(pattern="^(" + "|".join(BUSINESS_TYPES) + ")$")]
    business_type_other: Annotated[str, StringConstraints(max_length=60)] | None = None
    owner_name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    plan: Annotated[str, StringConstraints(pattern="^(" + "|".join(PLANS) + ")$")]
    contact_email: Annotated[str, StringConstraints(max_length=254)] | None = None

    @field_validator("business_type_other", "contact_email")
    @classmethod
    def blank_is_none(cls, value: str | None) -> str | None:
        """Treat a blank optional field as absent.

        Args:
            value: The submitted value.

        Returns:
            The value, or ``None`` if blank.
        """
        return value or None

    @field_validator("contact_email")
    @classmethod
    def check_email(cls, value: str | None) -> str | None:
        """Accept a plausible address or nothing.

        Args:
            value: The submitted address.

        Returns:
            The address, or ``None``.

        Raises:
            ValueError: If it is not a plausible address.
        """
        if value is not None and not EMAIL_PATTERN.match(value):
            raise ValueError("not an email address")
        return value


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
        payment_status: ``not_required``, ``awaiting`` or ``paid``.
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
    payment_status: str = "not_required"


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
        cancel_cutoff_minutes: How long before a session a customer may still cancel online.
        move_cutoff_minutes: How long before a session a customer may still move it online.
        contact_email: Where the business receives booking emails, or ``None``.
        meeting_link: A link included in customer emails, or ``None``.
    """

    name: str
    time_zone: str
    currency: str
    break_minutes: int
    notice_minutes: int
    window_days: int
    cancel_cutoff_minutes: int
    move_cutoff_minutes: int
    contact_email: str | None
    meeting_link: str | None


class SettingsBody(BaseModel):
    """Changes to the booking settings. Only the fields sent are changed.

    Attributes:
        name: Display name.
        break_minutes: Break between sessions, 0 to 240.
        notice_minutes: Notice period, 0 to 43200 (30 days).
        window_days: Booking window, 1 to 365.
        cancel_cutoff_minutes: Online cancellation closes this long before a session, 0 to 43200.
        move_cutoff_minutes: Online moving closes this long before a session, 0 to 43200.
        contact_email: Where booking emails go. An empty string clears it.
        meeting_link: An ``https://`` link for customers. An empty string clears it.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Name | None = None
    break_minutes: Annotated[int, Field(ge=0, le=240)] | None = None
    notice_minutes: Annotated[int, Field(ge=0, le=43200)] | None = None
    window_days: Annotated[int, Field(ge=1, le=365)] | None = None
    cancel_cutoff_minutes: Annotated[int, Field(ge=0, le=43200)] | None = None
    move_cutoff_minutes: Annotated[int, Field(ge=0, le=43200)] | None = None
    contact_email: Annotated[str, StringConstraints(max_length=254)] | None = None
    meeting_link: Annotated[str, StringConstraints(max_length=500)] | None = None

    @field_validator("contact_email")
    @classmethod
    def check_email(cls, value: str | None) -> str | None:
        """Accept a plausible address, or an empty string to clear.

        Args:
            value: The submitted address.

        Returns:
            The address, or an empty string.

        Raises:
            ValueError: If it is neither.
        """
        if value and not EMAIL_PATTERN.match(value):
            raise ValueError("not an email address")
        return value

    @field_validator("meeting_link")
    @classmethod
    def check_link(cls, value: str | None) -> str | None:
        """Accept an ``https://`` link without spaces, or an empty string to clear.

        Args:
            value: The submitted link.

        Returns:
            The link, or an empty string.

        Raises:
            ValueError: If it is neither.
        """
        if value and (not value.startswith("https://") or any(c.isspace() for c in value) or len(value) < 12):
            raise ValueError("not an https link")
        return value

    def changes(self) -> dict[str, object]:
        """List the fields to change. Sent-but-empty contact email or meeting link means clear.

        Returns:
            Field name to new value.
        """
        sent = self.model_dump(exclude_unset=True)
        out: dict[str, object] = {}
        for name, value in sent.items():
            if name in ("contact_email", "meeting_link"):
                out[name] = value or None
            elif value is not None:
                out[name] = value
        return out


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
        payment_status=booking.payment_status,
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
        cancel_cutoff_minutes=settings.cancel_cutoff_minutes,
        move_cutoff_minutes=settings.move_cutoff_minutes,
        contact_email=settings.contact_email,
        meeting_link=settings.meeting_link,
    )


def _parse_time(value: str) -> time:
    """Parse a validated ``HH:MM`` time.

    Args:
        value: The time text, already validated.

    Returns:
        The time of day.
    """
    return time(int(value[:2]), int(value[3:]))


class PaymentsOut(BaseModel):
    """A business's online payment set-up.

    Attributes:
        available: Online payments are offered on this platform at all.
        provider: The payment provider's name, or ``None`` when not available.
        connected: The business has an account with the provider.
        enabled: The account can take payments, so paid services can be booked online.
    """

    available: bool
    provider: str | None
    connected: bool
    enabled: bool


class ConnectOut(BaseModel):
    """Where the owner completes their sign-up with the payment provider.

    Attributes:
        url: The provider's single-use link.
    """

    url: str


def build_router(
    verifier: Callable[[], TokenVerifier | None],
    backend: Callable[[], OwnerBackend | None],
    payments: Callable[[], PaymentsBackend | None] = lambda: None,
) -> APIRouter:
    """Build the owner routes.

    Args:
        verifier: Returns the token verifier, or ``None`` when sign-in is not configured.
        backend: Returns the owner service, or ``None`` when the database is not configured.
        payments: Returns the payment service, or ``None`` when online payments are not configured.

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
        return MeOut(
            businesses=[
                MembershipOut(slug=m.slug, name=m.name, role=m.role, display_name=m.display_name) for m in found
            ]
        )

    @router.post("/businesses", response_model=MembershipOut, status_code=201)
    def create_business(who: Account, body: NewBusinessBody) -> MembershipOut:
        """Create the signed-in account's business on the chosen plan.

        Args:
            who: The verified account.
            body: The sign-up details.

        Returns:
            The new membership, with the business's slug.
        """
        new = NewBusiness(
            body.name, body.business_type, body.business_type_other, body.owner_name, body.plan, body.contact_email
        )
        made = answer(lambda: service().create_business(who, new))
        return MembershipOut(slug=made.slug, name=made.name, role=made.role, display_name=made.display_name)

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
        changes = body.changes()
        return _settings_out(answer(lambda: service().update_settings(who, business, changes)))

    @router.post("/businesses/{business}/bookings/{reference}/cancel", response_model=OwnerBookingOut)
    def cancel_booking(
        who: Account, business: Slug, reference: Annotated[str, Path(pattern=r"^[A-Za-z0-9]{10}$")]
    ) -> OwnerBookingOut:
        """Cancel a booking on the business's behalf. The customer is emailed.

        Args:
            who: The verified account.
            business: Business slug.
            reference: Booking reference.

        Returns:
            The booking after the change.
        """
        found, item = answer(lambda: service().cancel_booking(who, business, reference))
        return _booking_out(found, item)

    @router.get("/businesses/{business}/payments", response_model=PaymentsOut)
    def get_payments(who: Account, business: Slug) -> PaymentsOut:
        """Read the business's online payment set-up, refreshed from the provider.

        Args:
            who: The verified account.
            business: Business slug.

        Returns:
            The set-up. ``available`` is ``False`` when online payments are not configured here.
        """
        found = payments()
        if found is None:
            service()
            return PaymentsOut(available=False, provider=None, connected=False, enabled=False)
        current = answer(lambda: found.settings(who, business))
        return PaymentsOut(
            available=True, provider=current.provider, connected=current.connected, enabled=current.enabled
        )

    @router.post("/businesses/{business}/payments/connect", response_model=ConnectOut)
    def connect_payments(who: Account, business: Slug) -> ConnectOut:
        """Start or continue the business's sign-up with the payment provider.

        Args:
            who: The verified account.
            business: Business slug.

        Returns:
            The provider's link. The page sends the owner there.

        Raises:
            HTTPException: 503 when online payments are not configured or the provider cannot be reached.
        """
        found = payments()
        if found is None:
            raise HTTPException(status_code=503, detail="Online payments are not available yet.")
        try:
            return ConnectOut(url=answer(lambda: found.connect(who, business)))
        except ProviderUnavailableError:
            raise HTTPException(status_code=503, detail="Online payments are unavailable right now.") from None

    return router
