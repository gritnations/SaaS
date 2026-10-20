"""Booking routes: the public booking surface of the API.

Routes, all scoped to one business by its public slug:
    * ``GET /businesses/{business}/services``: the services offered.
    * ``GET /businesses/{business}/availability``: a service's sessions for a run of days.
    * ``POST /businesses/{business}/bookings``: book a session. A paid service answers with a checkout
      link and the booking waits for its payment.

This module validates the shape of requests and turns booking outcomes into HTTP answers. Every rule
about prices, sessions and overlaps lives in ``booking.service``. No internal identifier is returned.
"""

import re
from collections.abc import Callable
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from booking.availability import Slot
from booking.service import (
    MAX_DAYS_PER_REQUEST,
    BookingBackend,
    BookingRequest,
    InvalidSlotError,
    NotFoundError,
    PaymentUnavailableError,
    SlotTakenError,
)
from postgres import DatabaseUnavailableError

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$"
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
PHONE_PATTERN = re.compile(r"^[0-9+()\- ]{5,32}$")

Slug = Annotated[str, Path(pattern=SLUG_PATTERN)]
"""A business slug as it appears in a request path."""


class BusinessOut(BaseModel):
    """A business as shown to customers.

    Attributes:
        slug: Public identifier.
        name: Display name.
        currency: ISO 4217 code that prices are in.
        time_zone: IANA time zone that dates and times are in.
        payment_options: How paid services can be paid now: ``online``, ``venue``, both, or neither.
    """

    slug: str
    name: str
    currency: str
    time_zone: str
    payment_options: list[str] = []


class ServiceOut(BaseModel):
    """A service as shown to customers.

    Attributes:
        slug: Public identifier.
        name: Display name.
        duration_minutes: Session length.
        price_minor: Price in minor units, such as pence.
        free: ``True`` when the price is zero.
    """

    slug: str
    name: str
    duration_minutes: int
    price_minor: int
    free: bool


class ServicesOut(BaseModel):
    """The answer to a services request.

    Attributes:
        business: The business.
        services: Its offered services, in display order.
    """

    business: BusinessOut
    services: list[ServiceOut]


class SlotOut(BaseModel):
    """One session.

    Attributes:
        time: Local start as ``HH:MM``.
        label: The session as people read it.
        available: ``True`` if it can be booked now.
    """

    time: str
    label: str
    available: bool


class DayOut(BaseModel):
    """One day's sessions.

    Attributes:
        date: The local date.
        slots: The sessions.
    """

    date: date
    slots: list[SlotOut]


class AvailabilityOut(BaseModel):
    """The answer to an availability request.

    Attributes:
        service: The service.
        time_zone: IANA time zone that dates and times are in.
        days: Open days in the booking window, with their sessions.
    """

    service: ServiceOut
    time_zone: str
    days: list[DayOut]


class BookingBody(BaseModel):
    """The body of a booking request. It carries no price: the server decides it.

    Attributes:
        service: Service slug.
        date: Local date.
        time: Local start time as ``HH:MM``.
        name: Customer name, 1 to 100 characters.
        email: Customer email.
        phone: Customer phone, optional.
        note: Customer's note or question, optional, up to 1000 characters.
        pay: ``online`` or ``venue`` for a paid service, optional. Omitted means online when offered.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    service: Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]
    date: date
    time: Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=254)]
    phone: Annotated[str, StringConstraints(max_length=32)] | None = None
    note: Annotated[str, StringConstraints(max_length=1000)] | None = None
    pay: Annotated[str, StringConstraints(pattern="^(online|venue)$")] | None = None

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        """Accept only a plausible email address.

        Args:
            value: The submitted address.

        Returns:
            The address.

        Raises:
            ValueError: If it is not a plausible address.
        """
        if not EMAIL_PATTERN.match(value):
            raise ValueError("not an email address")
        return value

    @field_validator("phone", "note")
    @classmethod
    def empty_is_none(cls, value: str | None) -> str | None:
        """Treat a blank optional field as absent.

        Args:
            value: The submitted value.

        Returns:
            The value, or ``None`` if it is blank.
        """
        return value or None

    @field_validator("phone")
    @classmethod
    def check_phone(cls, value: str | None) -> str | None:
        """Accept only digits, spaces and common phone punctuation.

        Args:
            value: The submitted phone number, or ``None``.

        Returns:
            The phone number, or ``None``.

        Raises:
            ValueError: If it contains anything else or is too short.
        """
        if value is not None and not PHONE_PATTERN.match(value):
            raise ValueError("not a phone number")
        return value


class BookingOut(BaseModel):
    """A new booking.

    Attributes:
        reference: Public booking reference.
        status: ``confirmed``, or ``pending_payment`` for a paid service until its payment is confirmed.
        service: The booked service.
        date: Local date.
        time: Local start as ``HH:MM``.
        label: The session as people read it.
        starts_at: Start, ISO 8601 with offset.
        ends_at: End, ISO 8601 with offset.
        manage_token: The private code for managing this booking. Returned once, only to the person who booked.
            A paid booking has none here: its manage link is emailed once the payment is confirmed.
        checkout_url: Where to pay, for a service paid online.
        payment_status: ``not_required``, ``awaiting`` (paying online) or ``at_venue``.
    """

    reference: str
    status: str
    service: ServiceOut
    date: date
    time: str
    label: str
    starts_at: str
    ends_at: str
    manage_token: str | None = None
    checkout_url: str | None = None
    payment_status: str = "not_required"


def _slot_out(slot: Slot) -> SlotOut:
    """Convert a session for the response.

    Args:
        slot: The session.

    Returns:
        Its public form.
    """
    return SlotOut(time=slot.time, label=slot.label, available=slot.available)


def build_router(backend: Callable[[], BookingBackend | None]) -> APIRouter:
    """Build the booking routes.

    Args:
        backend: Returns the booking service, or ``None`` when the database is not configured.

    Returns:
        The router, to be included in the API.
    """
    router = APIRouter(prefix="/businesses/{business}")

    def require() -> BookingBackend:
        """Get the booking service, or answer 503 when there is none.

        Returns:
            The booking service.

        Raises:
            HTTPException: 503 when the database is not configured.
        """
        service = backend()
        if service is None:
            raise HTTPException(status_code=503, detail="Booking is unavailable.")
        return service

    def service_out(slug: str, name: str, duration: int, price: int) -> ServiceOut:
        """Build a service's public form.

        Args:
            slug: Public identifier.
            name: Display name.
            duration: Session length.
            price: Price in minor units.

        Returns:
            The public form.
        """
        return ServiceOut(slug=slug, name=name, duration_minutes=duration, price_minor=price, free=price == 0)

    @router.get("/services", response_model=ServicesOut)
    def list_services(business: Slug) -> ServicesOut:
        """List the services a business offers.

        Args:
            business: The business slug.

        Returns:
            The business and its services.

        Raises:
            HTTPException: 404 for an unknown business, 503 when the database is unreachable.
        """
        try:
            found, services = require().list_services(business)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except DatabaseUnavailableError:
            raise HTTPException(status_code=503, detail="Booking is unavailable.") from None
        return ServicesOut(
            business=BusinessOut(
                slug=found.slug,
                name=found.name,
                currency=found.currency,
                time_zone=found.time_zone,
                payment_options=list(found.payment_options),
            ),
            services=[service_out(s.slug, s.name, s.duration_minutes, s.price_minor) for s in services],
        )

    @router.get("/availability", response_model=AvailabilityOut)
    def availability(
        business: Slug,
        service: Annotated[str, Query(pattern=SLUG_PATTERN)],
        start: date,
        days: Annotated[int, Query(ge=1, le=MAX_DAYS_PER_REQUEST)] = 7,
    ) -> AvailabilityOut:
        """List a service's sessions for a run of days.

        Args:
            business: The business slug.
            service: The service slug.
            start: First local date wanted.
            days: Number of days wanted.

        Returns:
            The open days inside the booking window, with their sessions.

        Raises:
            HTTPException: 404 for an unknown business or service, 503 when the database is unreachable.
        """
        try:
            found, offered, open_days = require().availability(business, service, start, days)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except DatabaseUnavailableError:
            raise HTTPException(status_code=503, detail="Booking is unavailable.") from None
        return AvailabilityOut(
            service=service_out(offered.slug, offered.name, offered.duration_minutes, offered.price_minor),
            time_zone=found.time_zone,
            days=[DayOut(date=day.date, slots=[_slot_out(slot) for slot in day.slots]) for day in open_days],
        )

    @router.post("/bookings", response_model=BookingOut, status_code=201)
    def create_booking(business: Slug, body: BookingBody) -> BookingOut:
        """Book a session: confirmed for a free service, held with a checkout link for a paid one.

        Args:
            business: The business slug.
            body: The customer's request.

        Returns:
            The booking.

        Raises:
            HTTPException: 404 for an unknown business or service, 402 for a paid service the business
                cannot take payment for now, 409 when the time is not available or was just taken, 503
                when the database is unreachable.
        """
        request = BookingRequest(
            service=body.service,
            date=body.date,
            time=body.time,
            name=body.name,
            email=body.email,
            phone=body.phone,
            note=body.note,
            pay=body.pay,
        )
        try:
            confirmed = require().create_booking(business, request)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PaymentUnavailableError as exc:
            raise HTTPException(status_code=402, detail=str(exc)) from None
        except (InvalidSlotError, SlotTakenError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except DatabaseUnavailableError:
            raise HTTPException(status_code=503, detail="Booking is unavailable.") from None
        offered = confirmed.service
        return BookingOut(
            reference=confirmed.reference,
            status=confirmed.status,
            service=service_out(offered.slug, offered.name, offered.duration_minutes, offered.price_minor),
            date=request.date,
            time=confirmed.slot.time,
            label=confirmed.slot.label,
            starts_at=confirmed.slot.starts_at.isoformat(),
            ends_at=confirmed.slot.ends_at.isoformat(),
            manage_token=confirmed.manage_token,
            checkout_url=confirmed.checkout_url,
            payment_status=confirmed.payment_status,
        )

    return router
