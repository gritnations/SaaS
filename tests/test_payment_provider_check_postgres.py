"""Tests for the rule that a payment identifier always comes with its gateway (migration 0019), against a real
PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The tests connect as the database owner. If migration 0019
(or the one-payment-one-booking index it builds on, 0016) is not there yet, the file is run inside the test's own
transaction, and everything is rolled back afterwards, so the database is not changed.

What these tests establish: a booking cannot hold a payment identifier without a gateway, whichever write path
tries it; a gateway with no payment identifier (a paid event that carried none) and a booking with neither are
still allowed; and the rule is recorded as a validated constraint. They do not establish that the application never
tries to write such a row: the payment code always writes the gateway and the payment identifier together.
"""

import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors
from test_app_api_role_postgres import owner_connection  # noqa: F401
from test_manage_postgres import URL_ENV
from test_migrations import FILES, number
from test_payment_pair_postgres import add_booking, pair_world  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

CHECK_FILE = next(path for path in FILES if number(path) == 19)
CONSTRAINT = "bookings_payment_needs_provider"
Connection = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def check_world(pair_world: tuple[Connection, str, str]) -> Iterator[tuple[Connection, str, str]]:  # noqa: F811
    """Make the constraint exist in the open transaction, on top of the payment-pair test business.

    Args:
        pair_world: The owner's connection, a business id and a service id.

    Yields:
        The same three values.
    """
    connection = pair_world[0]
    exists = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = %s)", (CONSTRAINT,)
    ).fetchone()
    if exists is not None and not exists[0]:
        connection.execute(CHECK_FILE.read_text(encoding="utf-8"))
    yield pair_world


def test_a_payment_identifier_without_a_gateway_is_refused(check_world: tuple[Connection, str, str]) -> None:
    """A booking row cannot carry a payment identifier and no gateway."""
    connection, business, service = check_world
    with pytest.raises(errors.CheckViolation):
        with connection.transaction():
            add_booking(connection, business, service, 9, None, "pi_orphan")


def test_a_gateway_and_payment_together_are_allowed(check_world: tuple[Connection, str, str]) -> None:
    """The normal paid booking is unaffected."""
    connection, business, service = check_world
    add_booking(connection, business, service, 9, "stripe", "pi_with_gateway")


def test_a_gateway_without_a_payment_identifier_and_neither_are_allowed(
    check_world: tuple[Connection, str, str],
) -> None:
    """A paid event that carried no payment identifier, and a booking with no payment at all, are still stored."""
    connection, business, service = check_world
    add_booking(connection, business, service, 9, "stripe", None)
    add_booking(connection, business, service, 11, None, None)


def test_an_update_cannot_strip_the_gateway_from_a_payment(check_world: tuple[Connection, str, str]) -> None:
    """The rule holds for updates as well as inserts."""
    connection, business, service = check_world
    add_booking(connection, business, service, 9, "stripe", "pi_strip")
    with pytest.raises(errors.CheckViolation):
        with connection.transaction():
            connection.execute("UPDATE bookings SET payment_provider = NULL WHERE payment_id = 'pi_strip'")


def test_the_rule_is_a_validated_constraint_on_bookings(check_world: tuple[Connection, str, str]) -> None:
    """The constraint exists on ``bookings`` and has been checked against every existing row."""
    connection = check_world[0]
    found = connection.execute(
        "SELECT convalidated FROM pg_constraint WHERE conname = %s AND conrelid = 'public.bookings'::regclass",
        (CONSTRAINT,),
    ).fetchone()
    assert found == (True,)
