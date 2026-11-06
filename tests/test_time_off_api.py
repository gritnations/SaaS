"""Tests for the days-off routes. A fake owner service stands in for the database.

What these tests establish: the routes need sign-in, take one date or a range and an optional private note,
refuse anything else before the service is called, answer with the entries and how many existing bookings fall
inside new days off, and map every outcome to the right HTTP answer without revealing another business. The
rules themselves, and the effect on availability, are covered by ``test_time_off_postgres.py``.
"""

from datetime import date
from typing import Any

import pytest
from test_owner_api import ACCOUNT, ENTRY_ID, GOOD, FakeOwners, signed_in

from booking.owner import InvalidChangeError
from booking.service import NotFoundError
from postgres import DatabaseUnavailableError

BASE = "/owner/businesses/example/time-off"


def test_the_entries_are_listed() -> None:
    """The service's entries come back as plain dates and the private note."""
    owners = FakeOwners()
    response = signed_in(owners).get(BASE, headers=GOOD)
    assert response.status_code == 200
    assert response.json() == {
        "entries": [{"id": ENTRY_ID, "first_day": "2026-11-16", "last_day": "2026-11-20", "note": "Leave"}]
    }
    assert owners.calls == [("time_off", (ACCOUNT, "example"))]


def test_a_range_is_added_with_its_note_and_reports_the_bookings_inside_it() -> None:
    """The dates and the note reach the service as sent; the answer names the affected bookings."""
    owners = FakeOwners()
    response = signed_in(owners).post(
        BASE, headers=GOOD, json={"first_day": "2026-11-16", "last_day": "2026-11-20", "note": "  Leave  "}
    )
    assert response.status_code == 201
    assert response.json()["bookings_affected"] == 2
    assert response.json()["entries"][0]["note"] == "Leave"
    assert owners.calls == [("add_time_off", (ACCOUNT, "example", date(2026, 11, 16), date(2026, 11, 20), "Leave"))]


def test_a_single_day_needs_only_its_date() -> None:
    """Leaving out the last day closes just the first day, and an empty note is stored as no note."""
    owners = FakeOwners()
    response = signed_in(owners).post(BASE, headers=GOOD, json={"first_day": "2026-11-16", "note": "  "})
    assert response.status_code == 201
    assert owners.calls == [("add_time_off", (ACCOUNT, "example", date(2026, 11, 16), date(2026, 11, 16), None))]


def test_removing_an_entry_answers_with_what_is_left() -> None:
    """Removal is a POST, because the API's cross-origin rules allow no DELETE."""
    owners = FakeOwners()
    response = signed_in(owners).post(f"{BASE}/{ENTRY_ID}/remove", headers=GOOD)
    assert response.status_code == 200 and response.json() == {"entries": []}
    assert owners.calls == [("remove_time_off", (ACCOUNT, "example", ENTRY_ID))]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"first_day": "not-a-date"},
        {"first_day": "2026-13-40"},
        {"first_day": "2026-11-20", "last_day": "2026-11-16"},
        {"first_day": "2026-11-16", "note": "x" * 101},
        {"first_day": "2026-11-16", "business_id": "other"},
        {"first_day": "2026-11-16", "last_day": "later"},
    ],
)
def test_bad_days_off_are_refused_before_the_service_is_called(body: dict[str, Any]) -> None:
    """A missing or invalid date, a range that ends first, a long note and any extra field are all 422."""
    owners = FakeOwners()
    response = signed_in(owners).post(BASE, headers=GOOD, json=body)
    assert response.status_code == 422
    assert owners.calls == []


def test_a_malformed_entry_identifier_is_refused() -> None:
    """Only an identifier shaped like one reaches the service."""
    owners = FakeOwners()
    response = signed_in(owners).post(f"{BASE}/not-an-id/remove", headers=GOOD)
    assert response.status_code == 422
    assert owners.calls == []


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (NotFoundError("Business not found."), 404),
        (InvalidChangeError("Days off cannot start in the past."), 422),
        (DatabaseUnavailableError("The database is unavailable."), 503),
    ],
)
@pytest.mark.parametrize("call", ["list", "add", "remove"])
def test_outcomes_map_to_answers(call: str, failure: Exception, status: int) -> None:
    """Another owner's business is 404, a refused change 422 with its reason, an unreachable database 503."""
    api = signed_in(FakeOwners(fail_with=failure))
    if call == "list":
        response = api.get(BASE, headers=GOOD)
    elif call == "add":
        response = api.post(BASE, headers=GOOD, json={"first_day": "2026-11-16"})
    else:
        response = api.post(f"{BASE}/{ENTRY_ID}/remove", headers=GOOD)
    assert response.status_code == status
    if status == 422:
        assert response.json()["detail"] == "Days off cannot start in the past."
