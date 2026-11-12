"""Tests for the one-payment-one-booking rule (migration 0016) against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The tests connect as the database owner. If the index is not
there yet, migration 0016 is run inside the test's own transaction, and everything is rolled back afterwards, so
the database is not changed. If the index is already there, the tests check the real one. The ledger (0015) must
exist, because 0016 records itself in it.

What these tests establish: two bookings cannot hold the same gateway and payment identifier; the same
identifier from a different gateway is allowed; and any number of bookings without a payment identifier are
allowed. They do not establish that the application never tries to write a repeated pair: that is a behaviour of
the payment code, not of this rule.
"""

import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors
from test_app_api_role_postgres import owner_connection  # noqa: F401
from test_manage_postgres import URL_ENV
from test_migrations import FILES, number

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

PAIR_FIRST = 16
PAIR_FILE = next(path for path in FILES if number(path) == PAIR_FIRST)
Connection = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def pair_world(owner_connection: Connection) -> Iterator[tuple[Connection, str, str]]:  # noqa: F811
    """Make the index exist in the open transaction, then create a business with one service.

    Args:
        owner_connection: The owner's connection, rolled back after the test.

    Yields:
        The connection, the business id and the service id.
    """
    exists = owner_connection.execute("SELECT to_regclass('public.bookings_payment_pair_key') IS NOT NULL").fetchone()
    if exists is not None and not exists[0]:
        owner_connection.execute(PAIR_FILE.read_text(encoding="utf-8"))
    business = owner_connection.execute(
        "INSERT INTO businesses (slug, name) VALUES (%s, 'Pair Test') RETURNING id", ("pair-" + secrets.token_hex(5),)
    ).fetchone()
    assert business is not None
    service = owner_connection.execute(
        "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor) "
        "VALUES (%s, 'hour', 'Hour', 60, 2500) RETURNING id",
        (business[0],),
    ).fetchone()
    assert service is not None
    yield owner_connection, str(business[0]), str(service[0])


def add_booking(
    connection: Connection, business: str, service: str, hour: int, provider: str | None, payment: str | None
) -> None:
    """Insert a confirmed booking an hour long, starting at the given hour on a day far ahead.

    Args:
        connection: The owner's connection.
        business: The business id.
        service: The service id.
        hour: Hour of day the booking starts, so bookings in one test never overlap.
        provider: The payment gateway recorded on the booking, or ``None``.
        payment: The gateway's payment identifier, or ``None``.
    """
    start = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=60)
    connection.execute(
        "INSERT INTO bookings (business_id, service_id, reference, status, starts_at, ends_at, blocked_until, "
        "price_minor, customer_name, customer_email, payment_status, payment_provider, payment_id) "
        "VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, 2500, 'Pat', 'pat@example.com', 'paid', %s, %s)",
        (
            business,
            service,
            secrets.token_hex(5).upper(),
            start,
            start + timedelta(hours=1),
            start + timedelta(hours=1),
            provider,
            payment,
        ),
    )


def test_two_bookings_cannot_hold_the_same_payment(pair_world: tuple[Connection, str, str]) -> None:
    """The second booking with the same gateway and payment identifier is refused."""
    connection, business, service = pair_world
    add_booking(connection, business, service, 9, "stripe", "pi_pair_1")
    with pytest.raises(errors.UniqueViolation):
        add_booking(connection, business, service, 11, "stripe", "pi_pair_1")


def test_a_failed_ledger_row_leaves_no_index(owner_connection: Connection) -> None:  # noqa: F811
    """If the ledger row cannot be written, the index is not left behind."""
    exists = owner_connection.execute("SELECT to_regclass('public.bookings_payment_pair_key') IS NOT NULL").fetchone()
    if exists is None or exists[0]:
        pytest.skip("0016 is already applied, so there is nothing to apply here")
    owner_connection.execute("INSERT INTO schema_migrations (version) VALUES ('0016_unique_payment_pair')")
    with pytest.raises(errors.UniqueViolation):
        with owner_connection.transaction():  # a savepoint inside the test's own transaction
            owner_connection.execute(PAIR_FILE.read_text(encoding="utf-8"))
    after = owner_connection.execute("SELECT to_regclass('public.bookings_payment_pair_key') IS NOT NULL").fetchone()
    assert after is not None and not after[0]


def test_the_same_identifier_from_another_gateway_is_allowed(pair_world: tuple[Connection, str, str]) -> None:
    """The pair is what is unique: gateways may reuse each other's identifiers."""
    connection, business, service = pair_world
    add_booking(connection, business, service, 9, "stripe", "pi_pair_2")
    add_booking(connection, business, service, 11, "paymob", "pi_pair_2")


def test_bookings_without_a_payment_are_not_limited(pair_world: tuple[Connection, str, str]) -> None:
    """Free and pay-at-venue bookings have no payment identifier, and any number of them are allowed."""
    connection, business, service = pair_world
    for hour in (9, 11, 13):
        add_booking(connection, business, service, hour, None, None)
