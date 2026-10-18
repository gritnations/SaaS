"""Tests for the booking routes. A fake booking service stands in for the database.

What these tests establish: request shapes are validated, booking outcomes map to the right HTTP
answers, no price or internal identifier crosses the boundary, and the routes fail safely without a
database. Database behaviour, including the overlap constraint, is covered by
``test_booking_postgres.py``.
"""

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN, TRUSTED_HOST, FakeDatabase

from api.main import create_api
from booking.availability import Slot
from booking.service import (
    BookingRequest,
    Business,
    Confirmation,
    DayAvailability,
    InvalidSlotError,
    NotFoundError,
    PaymentUnavailableError,
    Service,
    SlotTakenError,
)
from main import create_app
from postgres import DatabaseUnavailableError

LONDON = ZoneInfo("Europe/London")
BUSINESS = Business("internal-business-id", "example", "Example Advisory", "Europe/London", "GBP", 0, 240, 30)
INTRO = Service("internal-service-id", "intro", "Introductory call", 30, 0)
SLOT = Slot(
    "09:00",
    "9:00 – 9:30 AM",
    datetime(2026, 11, 3, 9, 0, tzinfo=LONDON),
    datetime(2026, 11, 3, 9, 30, tzinfo=LONDON),
    True,
)
VALID = {
    "service": "intro",
    "date": "2026-11-03",
    "time": "09:00",
    "name": "Sample Customer",
    "email": "customer@example.com",
}


class FakeBookings:
    """A booking service that answers from fixed data, or raises a chosen outcome.

    Attributes:
        fail_with: If set, every call raises it.
        received: The booking requests passed to :meth:`create_booking`.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        """Create the fake.

        Args:
            fail_with: An exception for every call to raise.
        """
        self.fail_with = fail_with
        self.received: list[BookingRequest] = []

    def _check(self) -> None:
        """Raise the configured outcome, if any.

        Raises:
            Exception: Whatever was passed as ``fail_with``.
        """
        if self.fail_with is not None:
            raise self.fail_with

    def list_services(self, business_slug: str) -> tuple[Business, list[Service]]:
        """Return the fixed business and service.

        Args:
            business_slug: Ignored.

        Returns:
            The fixed business and its one service.
        """
        self._check()
        return BUSINESS, [INTRO]

    def availability(
        self, business_slug: str, service_slug: str, start: date, days: int
    ) -> tuple[Business, Service, list[DayAvailability]]:
        """Return one day with one session.

        Args:
            business_slug: Ignored.
            service_slug: Ignored.
            start: Ignored.
            days: Ignored.

        Returns:
            The fixed business, service and day.
        """
        self._check()
        return BUSINESS, INTRO, [DayAvailability(date(2026, 11, 3), [SLOT])]

    def create_booking(self, business_slug: str, request: BookingRequest) -> Confirmation:
        """Record the request and confirm it.

        Args:
            business_slug: Ignored.
            request: The request, recorded in :attr:`received`.

        Returns:
            A fixed confirmation.
        """
        self.received.append(request)
        self._check()
        return Confirmation("ABCDEFGH23", INTRO, SLOT)


def client_for(bookings: FakeBookings | None) -> TestClient:
    """Build a test client around the full application with a fake booking service.

    Args:
        bookings: The fake, or ``None`` for no booking service.

    Returns:
        A client that reports server errors as responses.
    """
    environ = {"APP_ENV": "test", "ALLOWED_HOSTS": TRUSTED_HOST, "CORS_ALLOWED_ORIGINS": ORIGIN}
    api = create_api(environ, database=FakeDatabase(), bookings=bookings)
    return TestClient(create_app(environ, api=api), raise_server_exceptions=False)


def test_services_lists_public_fields_only() -> None:
    """Services carry slugs and prices in pence, never internal identifiers."""
    response = client_for(FakeBookings()).get("/businesses/example/services")
    assert response.status_code == 200
    body = response.json()
    assert body["business"] == {
        "slug": "example",
        "name": "Example Advisory",
        "currency": "GBP",
        "time_zone": "Europe/London",
    }
    assert body["services"] == [
        {"slug": "intro", "name": "Introductory call", "duration_minutes": 30, "price_minor": 0, "free": True}
    ]
    assert "internal" not in response.text


def test_availability_returns_days_and_slots() -> None:
    """Availability lists each day's sessions with their state."""
    response = client_for(FakeBookings()).get(
        "/businesses/example/availability", params={"service": "intro", "start": "2026-11-03", "days": 7}
    )
    assert response.status_code == 200
    assert response.json()["days"] == [
        {"date": "2026-11-03", "slots": [{"time": "09:00", "label": "9:00 – 9:30 AM", "available": True}]}
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"service": "intro", "start": "2026-11-03", "days": 15},
        {"service": "intro", "start": "2026-11-03", "days": 0},
        {"service": "INTRO", "start": "2026-11-03"},
        {"service": "intro", "start": "not-a-date"},
        {"start": "2026-11-03"},
    ],
)
def test_availability_rejects_malformed_queries(params: dict[str, Any]) -> None:
    """Too many days, a bad slug, a bad date or a missing service is a 422."""
    response = client_for(FakeBookings()).get("/businesses/example/availability", params=params)
    assert response.status_code == 422


def test_a_malformed_business_slug_is_rejected() -> None:
    """Path slugs follow the same pattern as the database."""
    assert client_for(FakeBookings()).get("/businesses/Bad_Slug/services").status_code == 422


def test_booking_confirms_and_passes_only_the_customer_details() -> None:
    """A valid request returns 201 with the reference, and the service receives the cleaned request."""
    bookings = FakeBookings()
    response = client_for(bookings).post(
        "/businesses/example/bookings", json={**VALID, "name": "  Sample Customer  ", "phone": "", "note": ""}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["reference"] == "ABCDEFGH23"
    assert body["status"] == "confirmed"
    assert body["starts_at"] == "2026-11-03T09:00:00+00:00"
    assert bookings.received[0].name == "Sample Customer"
    assert bookings.received[0].phone is None
    assert bookings.received[0].note is None


@pytest.mark.parametrize(
    "change",
    [
        {"price_minor": 0},
        {"amount": 0},
        {"email": "not-an-email"},
        {"name": ""},
        {"name": "x" * 101},
        {"time": "9am"},
        {"date": "2026-02-31"},
        {"phone": "call me <script>"},
        {"note": "x" * 1001},
        {"service": "../intro"},
    ],
)
def test_booking_rejects_malformed_or_extra_fields(change: dict[str, Any]) -> None:
    """A client-sent price, bad contact details or a malformed date, time or slug is refused before the service."""
    bookings = FakeBookings()
    response = client_for(bookings).post("/businesses/example/bookings", json={**VALID, **change})
    assert response.status_code == 422
    assert bookings.received == []


def test_validation_errors_do_not_echo_submitted_values() -> None:
    """A rejected email is named as a field, but its value is not repeated back."""
    response = client_for(FakeBookings()).post(
        "/businesses/example/bookings", json={**VALID, "email": "secret-value-xyz"}
    )
    assert response.status_code == 422
    assert "secret-value-xyz" not in response.text


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (NotFoundError("Service not found."), 404),
        (PaymentUnavailableError("Online payment is not available yet."), 402),
        (InvalidSlotError("That time is not available."), 409),
        (SlotTakenError("That time has just been booked."), 409),
        (DatabaseUnavailableError("The database is unavailable."), 503),
    ],
)
def test_booking_outcomes_map_to_http_answers(outcome: Exception, status: int) -> None:
    """Each booking outcome has its own status and a fixed, public message."""
    response = client_for(FakeBookings(fail_with=outcome)).post("/businesses/example/bookings", json=VALID)
    assert response.status_code == status
    if status != 503:
        assert response.json() == {"detail": str(outcome)}
    else:
        assert response.json() == {"detail": "Booking is unavailable."}


def test_an_unexpected_error_is_a_generic_500() -> None:
    """A fault inside the service never leaks its text."""
    response = client_for(FakeBookings(fail_with=RuntimeError("internal detail"))).get("/businesses/example/services")
    assert response.status_code == 500
    assert "internal detail" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/businesses/example/services"),
        ("get", "/businesses/example/availability?service=intro&start=2026-11-03"),
    ],
)
def test_routes_answer_503_without_a_booking_service(method: str, path: str) -> None:
    """With no database, booking is unavailable rather than broken."""
    response = getattr(client_for(None), method)(path)
    assert response.status_code == 503
    assert response.json() == {"detail": "Booking is unavailable."}


def test_the_allowed_origin_can_call_booking_from_a_browser() -> None:
    """The CORS policy lets the configured site read booking answers, and no other."""
    client = client_for(FakeBookings())
    allowed = client.get("/businesses/example/services", headers={"Origin": ORIGIN})
    other = client.get("/businesses/example/services", headers={"Origin": "https://other.example"})
    assert allowed.headers.get("access-control-allow-origin") == ORIGIN
    assert "access-control-allow-origin" not in other.headers
