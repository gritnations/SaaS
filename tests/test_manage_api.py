"""Tests for the manage-link routes and the owner cancel and settings additions. Fakes stand in for the services.

What these tests establish: the manage code is required in the body and validated, outcomes map to the
right answers with one generic not-found, the booking answer returns the manage code once, and the
owner can cancel and change the new settings. Database behaviour is covered by ``test_manage_postgres.py``.
"""

import dataclasses
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN, TRUSTED_HOST, FakeDatabase
from test_booking_api import FakeBookings
from test_owner_api import GOOD, FakeOwners, FakeVerifier

from api.main import create_api
from booking.manage import ManagedBooking, TooLateError
from booking.service import Business, InvalidSlotError, NotFoundError, Service, SlotTakenError
from main import create_app
from postgres import DatabaseUnavailableError

LONDON = ZoneInfo("Europe/London")
TOKEN = "0123456789abcdef" * 3
BUSINESS = Business("internal-id", "example", "Example Advisory", "Europe/London", "GBP", 0, 240, 30)
START = datetime(2026, 11, 3, 9, 0, tzinfo=LONDON)
MANAGED = ManagedBooking(
    reference="ZA2JBN2C5B",
    status="confirmed",
    business=BUSINESS,
    service=Service("internal-service", "intro", "Introductory call", 30, 0),
    starts_at=START,
    ends_at=START + timedelta(minutes=30),
    can_cancel=True,
    can_move=True,
    cancel_deadline=START - timedelta(hours=24),
    move_deadline=START - timedelta(hours=24),
)
PATH = "/businesses/example/bookings/ZA2JBN2C5B"


class FakeManage:
    """A manage service answering from fixed data, recording calls, or raising a chosen outcome.

    Attributes:
        fail_with: If set, every call raises it.
        calls: ``(method, args)`` for each call.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        """Create the fake.

        Args:
            fail_with: An exception for every call to raise.
        """
        self.fail_with = fail_with
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _answer(self, name: str, *args: Any) -> ManagedBooking:
        """Record a call and answer.

        Args:
            name: The method.
            *args: Its arguments.

        Returns:
            The fixed booking, cancelled for a cancel.

        Raises:
            Exception: Whatever was passed as ``fail_with``.
        """
        self.calls.append((name, args))
        if self.fail_with is not None:
            raise self.fail_with
        return dataclasses.replace(MANAGED, status="cancelled", can_cancel=False) if name == "cancel" else MANAGED

    def view(self, slug: str, reference: str, token: str) -> ManagedBooking:
        """Answer a view.

        Args:
            slug: Business slug.
            reference: Reference.
            token: Code.

        Returns:
            The booking.
        """
        return self._answer("view", slug, reference, token)

    def cancel(self, slug: str, reference: str, token: str) -> ManagedBooking:
        """Answer a cancel.

        Args:
            slug: Business slug.
            reference: Reference.
            token: Code.

        Returns:
            The cancelled booking.
        """
        return self._answer("cancel", slug, reference, token)

    def move(self, slug: str, reference: str, token: str, day: date, start: str) -> ManagedBooking:
        """Answer a move.

        Args:
            slug: Business slug.
            reference: Reference.
            token: Code.
            day: New date.
            start: New time.

        Returns:
            The booking.
        """
        return self._answer("move", slug, reference, token, day, start)


def client_for(manage: FakeManage | None = None, owners: FakeOwners | None = None) -> TestClient:
    """Build a test client around the full application with fakes.

    Args:
        manage: The fake manage service, or ``None``.
        owners: The fake owner service, or ``None``.

    Returns:
        A client that reports server errors as responses.
    """
    environ = {"APP_ENV": "test", "ALLOWED_HOSTS": TRUSTED_HOST, "CORS_ALLOWED_ORIGINS": ORIGIN}
    api = create_api(
        environ,
        database=FakeDatabase(),
        bookings=FakeBookings(),
        owners=owners,
        verifier=FakeVerifier(),
        manage=manage,
    )
    return TestClient(create_app(environ, api=api), raise_server_exceptions=False)


def test_view_returns_the_booking_in_local_time_with_its_rules() -> None:
    """The view shows the session in UK time and what may be done."""
    manage = FakeManage()
    response = client_for(manage).post(f"{PATH}/manage", json={"token": TOKEN})
    assert response.status_code == 200
    body = response.json()
    assert (body["date"], body["time"], body["label"]) == ("2026-11-03", "09:00", "9:00 – 9:30 AM")
    assert body["can_cancel"] is True and body["business_name"] == "Example Advisory"
    assert manage.calls == [("view", ("example", "ZA2JBN2C5B", TOKEN))]
    assert "internal" not in response.text


@pytest.mark.parametrize(
    "body",
    [{}, {"token": "short"}, {"token": TOKEN.upper()}, {"token": TOKEN + "0"}, {"token": TOKEN, "extra": 1}],
)
def test_a_missing_or_malformed_code_never_reaches_the_service(body: dict[str, Any]) -> None:
    """Only a 48-character lower-case hex code in the body is accepted."""
    manage = FakeManage()
    assert client_for(manage).post(f"{PATH}/manage", json=body).status_code == 422
    assert manage.calls == []


def test_the_code_is_not_accepted_in_the_url() -> None:
    """A code in the query string is ignored: the body is still required."""
    assert client_for(FakeManage()).post(f"{PATH}/manage?token={TOKEN}", json={}).status_code == 422


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (NotFoundError("Booking not found."), 404),
        (TooLateError("It is too late to cancel online. Please contact the business."), 409),
        (InvalidSlotError("That time is not available."), 409),
        (SlotTakenError("That time has just been booked."), 409),
        (DatabaseUnavailableError("The database is unavailable."), 503),
    ],
)
def test_manage_outcomes_map_to_http_answers(outcome: Exception, status: int) -> None:
    """Each outcome has its status; not-found is always the same generic answer."""
    response = client_for(FakeManage(fail_with=outcome)).post(f"{PATH}/cancel", json={"token": TOKEN})
    assert response.status_code == status
    if status == 404:
        assert response.json() == {"detail": "Booking not found."}


def test_move_passes_the_new_date_and_time() -> None:
    """The move reaches the service with the parsed date and time."""
    manage = FakeManage()
    response = client_for(manage).post(f"{PATH}/move", json={"token": TOKEN, "date": "2026-11-04", "time": "10:30"})
    assert response.status_code == 200
    assert manage.calls[0] == ("move", ("example", "ZA2JBN2C5B", TOKEN, date(2026, 11, 4), "10:30"))


@pytest.mark.parametrize(
    "body", [{"token": TOKEN, "date": "2026-11-04"}, {"token": TOKEN, "date": "x", "time": "10:30"}]
)
def test_malformed_moves_are_rejected(body: dict[str, Any]) -> None:
    """A move needs a real date and a time."""
    assert client_for(FakeManage()).post(f"{PATH}/move", json=body).status_code == 422


def test_manage_routes_answer_503_without_a_service() -> None:
    """With no database, managing is unavailable rather than broken."""
    assert client_for(None).post(f"{PATH}/manage", json={"token": TOKEN}).status_code == 503


def test_a_new_booking_returns_its_manage_code_once() -> None:
    """The booking answer carries the code so the confirmation page can link to it."""
    bookings = FakeBookings()
    environ = {"APP_ENV": "test", "ALLOWED_HOSTS": TRUSTED_HOST}
    client = TestClient(create_app(environ, api=create_api(environ, database=FakeDatabase(), bookings=bookings)))
    response = client.post(
        "/businesses/example/bookings",
        json={"service": "intro", "date": "2026-11-03", "time": "09:00", "name": "A", "email": "a@example.com"},
    )
    assert response.status_code == 201
    assert "manage_token" in response.json()


def test_the_owner_can_cancel_a_booking() -> None:
    """The owner route needs sign-in and answers with the cancelled booking."""
    owners = FakeOwners()
    client = client_for(owners=owners)
    assert client.post(f"/owner{PATH}/cancel").status_code == 401
    response = client.post(f"/owner{PATH}/cancel", headers=GOOD)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert owners.calls[-1] == ("cancel_booking", ("11111111-1111-4111-8111-111111111111", "example", "ZA2JBN2C5B"))


def test_new_settings_are_validated_and_empty_values_clear() -> None:
    """Deadlines are bounded, the contact email and meeting link are checked, and empty strings clear them."""
    owners = FakeOwners()
    client = client_for(owners=owners)
    ok = client.patch(
        "/owner/businesses/example/settings",
        headers=GOOD,
        json={"cancel_cutoff_minutes": 120, "contact_email": "", "meeting_link": "https://meet.example.com/x"},
    )
    assert ok.status_code == 200
    assert owners.calls[-1][1][2] == {
        "cancel_cutoff_minutes": 120,
        "contact_email": None,
        "meeting_link": "https://meet.example.com/x",
    }
    for bad in (
        {"move_cutoff_minutes": 50000},
        {"contact_email": "not-an-email"},
        {"meeting_link": "http://meet.example.com"},
        {"meeting_link": "https://a b.com/xyz"},
    ):
        assert client.patch("/owner/businesses/example/settings", headers=GOOD, json=bad).status_code == 422


def test_settings_answer_includes_the_new_fields() -> None:
    """The settings response lists the deadlines, contact email and meeting link."""
    body = client_for(owners=FakeOwners()).get("/owner/businesses/example/settings", headers=GOOD).json()
    assert {"cancel_cutoff_minutes", "move_cutoff_minutes", "contact_email", "meeting_link"} <= set(body)


def test_public_contact_details_are_validated_and_empty_values_clear() -> None:
    """The public address, phone and email are checked, and empty strings clear them."""
    owners = FakeOwners()
    client = client_for(owners=owners)
    ok = client.patch(
        "/owner/businesses/example/settings",
        headers=GOOD,
        json={"public_address": "1 High Street, London", "public_phone": "020 7946 0000", "public_email": ""},
    )
    assert ok.status_code == 200
    assert owners.calls[-1][1][2] == {
        "public_address": "1 High Street, London",
        "public_phone": "020 7946 0000",
        "public_email": None,
    }
    for bad in (
        {"public_address": "ab"},
        {"public_address": "x" * 201},
        {"public_phone": "call me"},
        {"public_email": "not-an-email"},
    ):
        assert client.patch("/owner/businesses/example/settings", headers=GOOD, json=bad).status_code == 422
    body = client.get("/owner/businesses/example/settings", headers=GOOD).json()
    assert {"public_address", "public_phone", "public_email"} <= set(body)
