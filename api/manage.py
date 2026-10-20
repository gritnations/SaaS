"""Manage-link routes: a customer views, cancels or moves a booking with its private code.

Routes, scoped to one business by slug and one booking by reference:
    * ``POST /businesses/{business}/bookings/{reference}/manage``: view.
    * ``POST /businesses/{business}/bookings/{reference}/cancel``: cancel.
    * ``POST /businesses/{business}/bookings/{reference}/move``: move to another session.

The code travels in the JSON body, never in the URL, so it stays out of access logs. Every mismatch is the
same 404. Rules on deadlines and availability live in ``booking.manage``.
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, ConfigDict, StringConstraints

from api.booking import ServiceOut, Slug
from booking.availability import session_label
from booking.manage import ManagedBooking, TooLateError
from booking.service import InvalidSlotError, NotFoundError, SlotTakenError
from postgres import DatabaseUnavailableError

Token = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{48}$")]
Reference = Annotated[str, Path(pattern=r"^[A-Za-z0-9]{10}$")]


class ManageBackend(Protocol):
    """What the manage routes need from the booking domain. Tests substitute a fake."""

    def view(self, slug: str, reference: str, token: str) -> ManagedBooking:
        """See :meth:`booking.manage.ManageService.view`.

        Args:
            slug: Business slug.
            reference: Booking reference.
            token: Manage code.

        Returns:
            The booking.
        """
        ...

    def cancel(self, slug: str, reference: str, token: str) -> ManagedBooking:
        """See :meth:`booking.manage.ManageService.cancel`.

        Args:
            slug: Business slug.
            reference: Booking reference.
            token: Manage code.

        Returns:
            The booking after the change.
        """
        ...

    def move(self, slug: str, reference: str, token: str, day: date, start: str) -> ManagedBooking:
        """See :meth:`booking.manage.ManageService.move`.

        Args:
            slug: Business slug.
            reference: Booking reference.
            token: Manage code.
            day: New local date.
            start: New local start.

        Returns:
            The booking after the move.
        """
        ...


class TokenBody(BaseModel):
    """A request carrying only the manage code.

    Attributes:
        token: The 48-character manage code from the link.
    """

    model_config = ConfigDict(extra="forbid")

    token: Token


class MoveBody(BaseModel):
    """A move request.

    Attributes:
        token: The manage code.
        date: New local date.
        time: New local start, ``HH:MM``.
    """

    model_config = ConfigDict(extra="forbid")

    token: Token
    date: date
    time: Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]


class ManagedOut(BaseModel):
    """A booking as its customer sees it.

    Attributes:
        reference: Booking reference.
        status: ``confirmed`` or ``cancelled``.
        business_name: The business.
        service: The booked service.
        date: Local date.
        time: Local start, ``HH:MM``.
        label: The session as people read it.
        time_zone: IANA time zone.
        starts_at: Start, ISO 8601 with offset.
        can_cancel: Whether it can be cancelled online now.
        can_move: Whether it can be moved online now.
        cancel_deadline: Last moment to cancel online, ISO 8601.
        move_deadline: Last moment to move online, ISO 8601.
    """

    reference: str
    status: str
    business_name: str
    service: ServiceOut
    date: date
    time: str
    label: str
    time_zone: str
    starts_at: str
    can_cancel: bool
    can_move: bool
    cancel_deadline: str
    move_deadline: str


def managed_out(booking: ManagedBooking) -> ManagedOut:
    """Convert a managed booking for the response, in the business's time zone.

    Args:
        booking: The booking.

    Returns:
        Its public form.
    """
    zone = booking.business.zone
    local: datetime = booking.starts_at.astimezone(zone)
    minutes = local.hour * 60 + local.minute
    service = booking.service
    return ManagedOut(
        reference=booking.reference,
        status=booking.status,
        business_name=booking.business.name,
        service=ServiceOut(
            slug=service.slug,
            name=service.name,
            duration_minutes=service.duration_minutes,
            price_minor=service.price_minor,
            free=service.price_minor == 0,
        ),
        date=local.date(),
        time=f"{local.hour:02d}:{local.minute:02d}",
        label=session_label(minutes, service.duration_minutes),
        time_zone=booking.business.time_zone,
        starts_at=local.isoformat(),
        can_cancel=booking.can_cancel,
        can_move=booking.can_move,
        cancel_deadline=booking.cancel_deadline.astimezone(zone).isoformat(),
        move_deadline=booking.move_deadline.astimezone(zone).isoformat(),
    )


def build_router(backend: Callable[[], ManageBackend | None]) -> APIRouter:
    """Build the manage-link routes.

    Args:
        backend: Returns the manage service, or ``None`` when the database is not configured.

    Returns:
        The router, to be included in the API.
    """
    router = APIRouter(prefix="/businesses/{business}/bookings/{reference}")

    def run(call: Callable[[ManageBackend], ManagedBooking]) -> ManagedOut:
        """Run a manage operation and turn its outcomes into HTTP answers.

        Args:
            call: The operation, given the service.

        Returns:
            The booking's public form.

        Raises:
            HTTPException: 404 not found, 409 too late or unavailable, 503 unavailable.
        """
        service = backend()
        if service is None:
            raise HTTPException(status_code=503, detail="Booking is unavailable.")
        try:
            return managed_out(call(service))
        except NotFoundError:
            raise HTTPException(status_code=404, detail="Booking not found.") from None
        except (TooLateError, InvalidSlotError, SlotTakenError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except DatabaseUnavailableError:
            raise HTTPException(status_code=503, detail="Booking is unavailable.") from None

    @router.post("/manage", response_model=ManagedOut)
    def view(business: Slug, reference: Reference, body: TokenBody) -> ManagedOut:
        """Show a booking to the holder of its manage code.

        Args:
            business: Business slug.
            reference: Booking reference.
            body: The manage code.

        Returns:
            The booking.
        """
        return run(lambda s: s.view(business, reference, body.token))

    @router.post("/cancel", response_model=ManagedOut)
    def cancel(business: Slug, reference: Reference, body: TokenBody) -> ManagedOut:
        """Cancel a booking.

        Args:
            business: Business slug.
            reference: Booking reference.
            body: The manage code.

        Returns:
            The booking after the change.
        """
        return run(lambda s: s.cancel(business, reference, body.token))

    @router.post("/move", response_model=ManagedOut)
    def move(business: Slug, reference: Reference, body: MoveBody) -> ManagedOut:
        """Move a booking to another available session.

        Args:
            business: Business slug.
            reference: Booking reference.
            body: The manage code and the new date and time.

        Returns:
            The booking after the move.
        """
        return run(lambda s: s.move(business, reference, body.token, body.date, body.time))

    return router
