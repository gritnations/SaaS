"""Tests for the owner routes. A fake verifier and a fake owner service stand in for sign-in and the database.

What these tests establish: every owner route needs a valid token, request shapes are validated before
the service is called, outcomes map to the right HTTP answers, booking times are shown in the
business's time zone, and the browser may send the owner's edits from the allowed origin. Membership
checks against a real database are covered by ``test_owner_postgres.py``.
"""

import dataclasses
from datetime import UTC, date, datetime, time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN, TRUSTED_HOST, FakeDatabase

from api.auth import TokenError
from api.main import create_api
from booking.owner import (
    ConflictError,
    HoursBlock,
    InvalidChangeError,
    Membership,
    NewBusiness,
    OwnerBooking,
    OwnerServiceRecord,
    Settings,
)
from booking.service import Business, NotFoundError
from main import create_app
from postgres import DatabaseUnavailableError

ACCOUNT = "11111111-1111-4111-8111-111111111111"
GOOD = {"Authorization": "Bearer good-token"}
BUSINESS = Business("internal-id", "example", "Example Advisory", "Europe/London", "GBP", 0, 240, 30)
BOOKING = OwnerBooking(
    "ZA2JBN2C5B",
    "confirmed",
    "intro",
    "Introductory call",
    datetime(2026, 10, 20, 8, 30, tzinfo=UTC),
    datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
    0,
    "Sample Customer",
    "customer@example.com",
    "+44 7700 900000",
    None,
    datetime(2026, 10, 18, 16, 48, tzinfo=UTC),
)
SERVICE = OwnerServiceRecord("intro", "Introductory call", 30, 0, True, 1)
SETTINGS = Settings("Example Advisory", "Europe/London", "GBP", 0, 240, 30)


class FakeVerifier:
    """Accepts exactly one token."""

    def verify(self, token: str) -> str:
        """Accept ``good-token`` only.

        Args:
            token: The bearer token.

        Returns:
            The test account.

        Raises:
            TokenError: For any other token.
        """
        if token != "good-token":
            raise TokenError("The token is not valid.")
        return ACCOUNT


class FakeOwners:
    """An owner service answering from fixed data, recording calls, or raising a chosen outcome.

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

    def _record(self, name: str, *args: Any) -> None:
        """Record a call, then raise the configured outcome, if any.

        Args:
            name: The method called.
            *args: Its arguments.

        Raises:
            Exception: Whatever was passed as ``fail_with``.
        """
        self.calls.append((name, args))
        if self.fail_with is not None:
            raise self.fail_with

    def create_business(self, account: str, new: NewBusiness) -> Membership:
        """Echo the new business as a membership.

        Args:
            account: The account.
            new: The sign-up details.

        Returns:
            The membership.
        """
        self._record("create_business", account, new)
        return Membership("new-company", new.name, "owner", new.owner_name)

    def memberships(self, account: str) -> list[Membership]:
        """Return one membership.

        Args:
            account: The account.

        Returns:
            The membership.
        """
        self._record("memberships", account)
        return [Membership("example", "Example Advisory", "owner", time_zone="Asia/Riyadh")]

    def bookings(self, account: str, slug: str, start: date, days: int) -> tuple[Business, list[OwnerBooking]]:
        """Return one booking.

        Args:
            account: The account.
            slug: Business slug.
            start: First date.
            days: Day count.

        Returns:
            The business and booking.
        """
        self._record("bookings", account, slug, start, days)
        return BUSINESS, [BOOKING]

    def booking_by_reference(self, account: str, slug: str, reference: str) -> tuple[Business, OwnerBooking]:
        """Return the booking.

        Args:
            account: The account.
            slug: Business slug.
            reference: Reference.

        Returns:
            The business and booking.
        """
        self._record("booking_by_reference", account, slug, reference)
        return BUSINESS, BOOKING

    def services(self, account: str, slug: str) -> list[OwnerServiceRecord]:
        """Return one service.

        Args:
            account: The account.
            slug: Business slug.

        Returns:
            The services.
        """
        self._record("services", account, slug)
        return [SERVICE]

    def create_service(self, account: str, slug: str, service: OwnerServiceRecord) -> OwnerServiceRecord:
        """Echo the new service.

        Args:
            account: The account.
            slug: Business slug.
            service: The service.

        Returns:
            The service.
        """
        self._record("create_service", account, slug, service)
        return service

    def update_service(
        self, account: str, slug: str, service_slug: str, changes: dict[str, object]
    ) -> OwnerServiceRecord:
        """Return the fixed service.

        Args:
            account: The account.
            slug: Business slug.
            service_slug: Service slug.
            changes: The changes.

        Returns:
            The service.
        """
        self._record("update_service", account, slug, service_slug, changes)
        return SERVICE

    def hours(self, account: str, slug: str) -> list[HoursBlock]:
        """Return one block.

        Args:
            account: The account.
            slug: Business slug.

        Returns:
            The blocks.
        """
        self._record("hours", account, slug)
        return [HoursBlock(1, time(9, 0), time(17, 0))]

    def replace_hours(self, account: str, slug: str, blocks: list[HoursBlock]) -> list[HoursBlock]:
        """Echo the blocks.

        Args:
            account: The account.
            slug: Business slug.
            blocks: The blocks.

        Returns:
            The blocks.
        """
        self._record("replace_hours", account, slug, blocks)
        return blocks

    def settings(self, account: str, slug: str) -> Settings:
        """Return the settings.

        Args:
            account: The account.
            slug: Business slug.

        Returns:
            The settings.
        """
        self._record("settings", account, slug)
        return SETTINGS

    def cancel_booking(self, account: str, slug: str, reference: str) -> tuple[Business, OwnerBooking]:
        """Return the booking as cancelled.

        Args:
            account: The account.
            slug: Business slug.
            reference: Reference.

        Returns:
            The business and the cancelled booking.
        """
        self._record("cancel_booking", account, slug, reference)
        return BUSINESS, dataclasses.replace(BOOKING, status="cancelled")

    def update_settings(self, account: str, slug: str, changes: dict[str, object]) -> Settings:
        """Return the settings.

        Args:
            account: The account.
            slug: Business slug.
            changes: The changes.

        Returns:
            The settings.
        """
        self._record("update_settings", account, slug, changes)
        return SETTINGS


def client_for(owners: FakeOwners | None, verifier: FakeVerifier | None = None) -> TestClient:
    """Build a test client around the full application.

    Args:
        owners: The fake owner service, or ``None``.
        verifier: The fake verifier, or ``None`` for sign-in not configured.

    Returns:
        A client that reports server errors as responses.
    """
    environ = {"APP_ENV": "test", "ALLOWED_HOSTS": TRUSTED_HOST, "CORS_ALLOWED_ORIGINS": ORIGIN}
    api = create_api(environ, database=FakeDatabase(), owners=owners, verifier=verifier)
    return TestClient(create_app(environ, api=api), raise_server_exceptions=False)


def signed_in(owners: FakeOwners | None = None) -> TestClient:
    """Build a client with sign-in configured.

    Args:
        owners: The fake owner service. ``None`` means a fresh one.

    Returns:
        The client.
    """
    return client_for(owners if owners is not None else FakeOwners(), FakeVerifier())


ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get", "/owner/me", None),
    ("get", "/owner/businesses/example/bookings?start=2026-10-19", None),
    ("get", "/owner/businesses/example/bookings/ZA2JBN2C5B", None),
    ("get", "/owner/businesses/example/services", None),
    ("post", "/owner/businesses/example/services", {"slug": "x1", "name": "X", "duration_minutes": 30}),
    ("patch", "/owner/businesses/example/services/intro", {"name": "Y"}),
    ("get", "/owner/businesses/example/hours", None),
    ("put", "/owner/businesses/example/hours", {"blocks": []}),
    ("get", "/owner/businesses/example/settings", None),
    ("patch", "/owner/businesses/example/settings", {"window_days": 14}),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer bad"}, {"Authorization": "Basic abc"}])
def test_every_owner_route_needs_a_valid_token(
    method: str, path: str, body: dict[str, Any] | None, headers: dict[str, str]
) -> None:
    """Without a valid bearer token each route answers 401 and the service is never called."""
    owners = FakeOwners()
    kwargs: dict[str, Any] = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    response = getattr(signed_in(owners), method)(path, **kwargs)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert owners.calls == []


def test_owner_routes_answer_503_when_sign_in_is_not_configured() -> None:
    """With no verifier, the dashboard is unavailable rather than open."""
    response = client_for(FakeOwners(), None).get("/owner/me", headers=GOOD)
    assert response.status_code == 503


def test_me_lists_the_accounts_businesses() -> None:
    """The verified account's memberships come back with each business's time zone; the service saw that account."""
    owners = FakeOwners()
    response = signed_in(owners).get("/owner/me", headers=GOOD)
    assert response.status_code == 200
    assert response.json() == {
        "businesses": [
            {
                "slug": "example",
                "name": "Example Advisory",
                "role": "owner",
                "display_name": None,
                "plan": "free",
                "plan_status": "active",
                "trial_ends_at": None,
                "online_payments": False,
                "plan_ends_at": None,
                "time_zone": "Asia/Riyadh",
            }
        ]
    }
    assert owners.calls == [("memberships", (ACCOUNT,))]


def test_bookings_show_customer_details_in_local_time() -> None:
    """08:30 UTC on 20 October is 09:30 in London, and the customer's details are included."""
    response = signed_in().get("/owner/businesses/example/bookings?start=2026-10-19&days=7", headers=GOOD)
    assert response.status_code == 200
    item = response.json()["bookings"][0]
    assert item["reference"] == "ZA2JBN2C5B"
    assert (item["date"], item["time"], item["label"]) == ("2026-10-20", "09:30", "9:30 – 10:00 AM")
    assert item["customer_email"] == "customer@example.com"
    assert item["starts_at"] == "2026-10-20T09:30:00+01:00"


def test_reference_lookup_accepts_any_letter_case() -> None:
    """The path allows lower case; the service normalises it."""
    owners = FakeOwners()
    assert signed_in(owners).get("/owner/businesses/example/bookings/za2jbn2c5b", headers=GOOD).status_code == 200
    assert owners.calls[0][1][2] == "za2jbn2c5b"


@pytest.mark.parametrize("reference", ["SHORT", "ZA2JBN2C5B1", "ZA2JBN-C5B"])
def test_malformed_references_are_rejected(reference: str) -> None:
    """Only 10 letters or digits reach the service."""
    response = signed_in().get(f"/owner/businesses/example/bookings/{reference}", headers=GOOD)
    assert response.status_code in (404, 422)


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (NotFoundError("Business not found."), 404),
        (ConflictError("A service with that identifier already exists."), 409),
        (InvalidChangeError("Each working block must end after it starts, on a weekday 0 to 6."), 422),
        (DatabaseUnavailableError("The database is unavailable."), 503),
    ],
)
def test_owner_outcomes_map_to_http_answers(outcome: Exception, status: int) -> None:
    """Each owner outcome has its own status."""
    response = signed_in(FakeOwners(fail_with=outcome)).get("/owner/businesses/example/services", headers=GOOD)
    assert response.status_code == status


def test_creating_a_service_passes_validated_fields() -> None:
    """A new service reaches the service layer with defaults filled in."""
    owners = FakeOwners()
    response = signed_in(owners).post(
        "/owner/businesses/example/services",
        headers=GOOD,
        json={"slug": "deep-dive", "name": " Deep dive ", "duration_minutes": 120, "price_minor": 0},
    )
    assert response.status_code == 201
    created = owners.calls[0][1][2]
    assert (created.slug, created.name, created.duration_minutes, created.active) == (
        "deep-dive",
        "Deep dive",
        120,
        True,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "Bad Slug", "name": "X", "duration_minutes": 30},
        {"slug": "ok", "name": "", "duration_minutes": 30},
        {"slug": "ok", "name": "X", "duration_minutes": 4},
        {"slug": "ok", "name": "X", "duration_minutes": 30, "price_minor": -1},
        {"slug": "ok", "name": "X", "duration_minutes": 30, "business_id": "other"},
    ],
)
def test_invalid_new_services_are_rejected(body: dict[str, Any]) -> None:
    """Bad slugs, names, durations, prices or extra fields never reach the service."""
    owners = FakeOwners()
    response = signed_in(owners).post("/owner/businesses/example/services", headers=GOOD, json=body)
    assert response.status_code == 422
    assert owners.calls == []


def test_updating_a_service_sends_only_the_fields_given() -> None:
    """A partial update changes only what was sent."""
    owners = FakeOwners()
    response = signed_in(owners).patch("/owner/businesses/example/services/intro", headers=GOOD, json={"active": False})
    assert response.status_code == 200
    assert owners.calls[0][1][3] == {"active": False}


@pytest.mark.parametrize(
    "block",
    [
        {"weekday": 7, "start": "09:00", "end": "17:00"},
        {"weekday": 1, "start": "9:00", "end": "17:00"},
        {"weekday": 1, "start": "09:00", "end": "24:00"},
        {"weekday": 1, "start": "09:00", "end": "17:00", "extra": 1},
    ],
)
def test_invalid_hours_are_rejected(block: dict[str, Any]) -> None:
    """Weekdays outside 0-6, malformed times and extra fields are refused."""
    owners = FakeOwners()
    response = signed_in(owners).put("/owner/businesses/example/hours", headers=GOOD, json={"blocks": [block]})
    assert response.status_code == 422
    assert owners.calls == []


def test_too_many_hour_blocks_are_rejected() -> None:
    """More than 50 blocks is refused."""
    blocks = [{"weekday": 1, "start": "09:00", "end": "10:00"}] * 51
    assert signed_in().put("/owner/businesses/example/hours", headers=GOOD, json={"blocks": blocks}).status_code == 422


def test_hours_round_trip() -> None:
    """Blocks sent are parsed to times and returned in the same text form."""
    blocks = [{"weekday": 2, "start": "13:00", "end": "17:30"}, {"weekday": 2, "start": "09:00", "end": "12:00"}]
    response = signed_in().put("/owner/businesses/example/hours", headers=GOOD, json={"blocks": blocks})
    assert response.status_code == 200
    assert response.json()["blocks"] == blocks


@pytest.mark.parametrize(
    "body", [{"break_minutes": 241}, {"notice_minutes": -1}, {"window_days": 0}, {"currency": "USD"}]
)
def test_invalid_settings_are_rejected(body: dict[str, Any]) -> None:
    """Out-of-range settings and read-only fields are refused."""
    owners = FakeOwners()
    response = signed_in(owners).patch("/owner/businesses/example/settings", headers=GOOD, json=body)
    assert response.status_code == 422
    assert owners.calls == []


def test_the_allowed_origin_may_send_owner_edits() -> None:
    """A browser preflight for PATCH with an Authorization header is allowed from the configured origin only."""
    client = signed_in()
    headers = {"Access-Control-Request-Method": "PATCH", "Access-Control-Request-Headers": "authorization,content-type"}
    allowed = client.options("/owner/businesses/example/settings", headers={"Origin": ORIGIN, **headers})
    other = client.options("/owner/businesses/example/settings", headers={"Origin": "https://other.example", **headers})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert other.status_code == 400


def test_owner_answers_are_never_cached() -> None:
    """Responses carrying customer details are marked no-store."""
    response = signed_in().get("/owner/businesses/example/bookings?start=2026-10-19", headers=GOOD)
    assert response.headers["cache-control"] == "no-store"
