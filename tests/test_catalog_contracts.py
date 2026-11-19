"""Tests for the catalog records: businesses, services, working hours and days off.

What these tests establish: the catalog records refuse values of the wrong type, unknown fields and values outside
the database's limits; the plan list equals the latest database check in ``migrations/``; records loaded from a
row keep stored text as it is, even text the input rules would now refuse. They do not establish anything about
the owner text rules, which come with the administration records.
"""

import re
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from booking.owner import HoursBlock, OwnerServiceRecord, ServiceChanges, TimeOff
from booking.service import Business, Service
from contracts import Plan, from_row

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

RIGHT_TO_LEFT_OVERRIDE = chr(0x202E)
ZERO_WIDTH_SPACE = chr(0x200B)

BUSINESS: dict[str, Any] = {
    "id": "internal-business-id",
    "slug": "example",
    "name": "Example Advisory",
    "time_zone": "Europe/London",
    "currency": "GBP",
    "break_minutes": 0,
    "notice_minutes": 240,
    "window_days": 30,
}

SERVICE: dict[str, Any] = {
    "id": "internal-service-id",
    "slug": "intro",
    "name": "Introductory call",
    "duration_minutes": 30,
    "price_minor": 0,
}

OWNER_SERVICE: dict[str, Any] = {
    "slug": "intro",
    "name": "Introductory call",
    "duration_minutes": 30,
    "price_minor": 0,
    "active": True,
    "sort_order": 0,
}


def latest_check(constraint: str, column: str) -> set[str]:
    """Read the values in the last ``ADD CONSTRAINT`` with this name across the migrations, in file order.

    Args:
        constraint: The constraint name, such as ``businesses_plan_check``.
        column: The column the check lists values for.

    Returns:
        The allowed values.
    """
    pattern = re.compile(rf"ADD CONSTRAINT {constraint}\s+CHECK \({column} IN \(([^)]*)\)\)")
    found: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        found += pattern.findall(path.read_text(encoding="utf-8"))
    assert found, f"no {constraint} found in migrations"
    return set(re.findall(r"'([^']*)'", found[-1]))


def test_the_plan_list_equals_the_database_check() -> None:
    """A plan added to the database without the code (or the reverse) fails here, in CI, without a database."""
    assert set(get_args(Plan)) == latest_check("businesses_plan_check", "plan")


def test_valid_catalog_records_are_accepted() -> None:
    """The values the platform stores today pass, including every optional business field."""
    Business(**BUSINESS)
    Business(
        **BUSINESS,
        payment_account_id="acct_test_1",
        payments_enabled=True,
        plan="pro",
        trial_ends_at=datetime(2026, 12, 1, tzinfo=UTC),
        pay_online=True,
        pay_at_venue=False,
        public_address="1 High Street, London",
        public_phone="020 7946 0000",
        public_email="hello@example.com",
        payment_options=("online", "venue"),
    )
    Service(**SERVICE)
    OwnerServiceRecord(**OWNER_SERVICE)
    HoursBlock(weekday=0, start=time(9, 0), end=time(17, 0))
    TimeOff(id="entry-1", first_day=date(2026, 12, 24), last_day=date(2026, 12, 26), note=None)
    ServiceChanges()


@pytest.mark.parametrize(
    "change",
    [
        {"slug": "ab"},
        {"slug": "Example"},
        {"name": ""},
        {"name": "x" * 121},
        {"currency": "gbp"},
        {"break_minutes": 241},
        {"notice_minutes": -1},
        {"window_days": 0},
        {"window_days": "30"},
        {"plan": "enterprise"},
        {"trial_ends_at": datetime(2026, 12, 1)},
        {"public_phone": "123"},
        {"payment_options": ("card",)},
        {"payment_options": ["online"]},
        {"owner": "someone"},
    ],
)
def test_a_business_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Strict, and within the database's limits: web name, currency, booking rules, plan and aware times."""
    with pytest.raises(ValidationError):
        Business(**{**BUSINESS, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"slug": "x"},
        {"name": ""},
        {"duration_minutes": 4},
        {"duration_minutes": 481},
        {"price_minor": -1},
        {"price_minor": 10_000_001},
        {"price_minor": 25.0},
    ],
)
def test_a_service_refuses_wrong_values(change: dict[str, Any]) -> None:
    """A service has a valid web name and a duration and price within the database's limits."""
    with pytest.raises(ValidationError):
        Service(**{**SERVICE, **change})
    with pytest.raises(ValidationError):
        OwnerServiceRecord(**{**OWNER_SERVICE, **{k: v for k, v in change.items() if k != "id"}})


@pytest.mark.parametrize(
    "change",
    [{"sort_order": -1}, {"sort_order": 10_001}, {"active": 1}],
)
def test_an_owner_service_record_refuses_wrong_values(change: dict[str, Any]) -> None:
    """Display position within the owner API's limits, and a real true or false for active."""
    with pytest.raises(ValidationError):
        OwnerServiceRecord(**{**OWNER_SERVICE, **change})


@pytest.mark.parametrize(
    "change",
    [{"duration_minutes": 4}, {"price_minor": -1}, {"name": ""}, {"active": "false"}, {"slug": "new-slug"}],
)
def test_service_changes_refuse_wrong_values(change: dict[str, Any]) -> None:
    """Only the fields that may change, each within its limits. A service's web name cannot be changed."""
    with pytest.raises(ValidationError):
        ServiceChanges(**change)


@pytest.mark.parametrize(
    "fields",
    [
        {"weekday": 7, "start": time(9, 0), "end": time(17, 0)},
        {"weekday": -1, "start": time(9, 0), "end": time(17, 0)},
        {"weekday": 1, "start": "09:00", "end": time(17, 0)},
    ],
)
def test_a_working_block_refuses_wrong_values(fields: dict[str, Any]) -> None:
    """Weekday 0 (Sunday) to 6, as the database stores it, and real times."""
    with pytest.raises(ValidationError):
        HoursBlock(**fields)


def test_a_working_block_that_ends_before_it_starts_is_left_to_the_service() -> None:
    """The record accepts it, so the owner gets the service's clear 422 rather than a server error."""
    HoursBlock(weekday=1, start=time(17, 0), end=time(9, 0))


@pytest.mark.parametrize(
    "change",
    [
        {"note": "x" * 101},
        {"first_day": "2026-12-24"},
        {"id": ""},
        {"last_day": date(2026, 12, 23)},
        {"last_day": date(2027, 12, 25)},
    ],
)
def test_days_off_refuse_wrong_values(change: dict[str, Any]) -> None:
    """A note up to 100 characters, real dates, an identifier, and a run of 0 to 365 days as in the database."""
    with pytest.raises(ValidationError):
        TimeOff(**{"id": "entry-1", "first_day": date(2026, 12, 24), "last_day": date(2026, 12, 26), **change})


def test_a_record_is_built_from_a_row_in_field_order() -> None:
    """Trailing fields a query leaves out take their defaults; a row with too many columns is an error."""
    row = tuple(BUSINESS.values())
    business = from_row(Business, row)
    assert business.slug == "example" and business.plan == "free" and business.payment_options == ()
    with pytest.raises(ValueError, match="fields"):
        from_row(Service, (*SERVICE.values(), "extra"))


def test_a_stored_name_is_loaded_as_it_is() -> None:
    """Rows saved before the text rules load unchanged: loading checks shape and limits only, never the input
    rules, so an old name with invisible or direction characters cannot turn a page into a server error."""
    name = f"Old{ZERO_WIDTH_SPACE} Name{RIGHT_TO_LEFT_OVERRIDE}"
    business = from_row(Business, (*{**BUSINESS, "name": name}.values(),))
    service = from_row(Service, (*{**SERVICE, "name": name}.values(),))
    assert business.name == name and service.name == name


def test_catalog_records_cannot_be_changed_after_they_are_built() -> None:
    """Frozen, as the dataclasses they replace were."""
    business = Business(**BUSINESS)
    with pytest.raises(ValidationError):
        business.name = "Someone else"
