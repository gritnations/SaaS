"""Tests for a payment that already belongs to another booking, against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The tests connect as the database owner and share one
transaction with the payment service, so nothing is committed and everything is rolled back afterwards. If the
one-payment-one-booking index (migration 0016) is not there yet, the migration file is run inside that
transaction, so the tests can run before the migration is applied to the database.

What these tests establish: when a verified paid event carries a payment that another booking already holds,
the service reports that nothing changed instead of raising, leaves the booking unpaid, logs the conflict, and
the connection stays usable; and an ordinary paid event in the same setup still confirms its booking. They do
not establish how the webhook route answers: that follows from the service returning ``False``.
"""

import logging
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import psycopg
import pytest
from test_app_api_role_postgres import owner_connection  # noqa: F401
from test_manage_postgres import URL_ENV
from test_migrations import FILES, number

from booking.payments import PaymentService
from payments.base import AccountPayments, PaymentEvent
from postgres import Database

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

PAIR_FILE = next(path for path in FILES if number(path) == 16)
Connection = psycopg.Connection[tuple[object, ...]]


class OneTransaction:
    """Hands the payment service one open connection, and never commits or closes it."""

    def __init__(self, connection: Connection) -> None:
        """Keep the connection.

        Args:
            connection: The test's open connection.
        """
        self._connection = connection

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        """Lend the connection to the service.

        Yields:
            The test's connection.
        """
        yield self._connection


class Setup:
    """A business with one booking that holds a payment and one booking waiting for its own payment.

    Attributes:
        service: The payment service, working inside the test's transaction.
        connection: The test's connection.
        account: The business's payment account.
        waiting: Reference of the booking that is waiting for payment.
    """

    def __init__(self, service: PaymentService, connection: Connection, account: str, waiting: str) -> None:
        """Keep the parts.

        Args:
            service: The payment service.
            connection: The test's connection.
            account: The business's payment account.
            waiting: Reference of the booking that is waiting for payment.
        """
        self.service = service
        self.connection = connection
        self.account = account
        self.waiting = waiting

    def event(self, payment_id: str) -> PaymentEvent:
        """Build the verified paid event for the waiting booking.

        Args:
            payment_id: The payment the event carries.

        Returns:
            The event.
        """
        return PaymentEvent(
            provider="stripe",
            order_reference=self.waiting,
            status="paid",
            live=False,
            checkout_id=f"cs_test_{self.waiting}",
            account_id=self.account,
            amount_minor=2500,
            currency="GBP",
            payment_id=payment_id,
        )

    def state(self) -> tuple[object, ...]:
        """Read the waiting booking's state.

        Returns:
            ``(status, payment_status, payment_id)``.
        """
        found = self.connection.execute(
            "SELECT status, payment_status, payment_id FROM bookings WHERE reference = %s", (self.waiting,)
        ).fetchone()
        assert found is not None
        return found


@pytest.fixture
def setup(owner_connection: Connection) -> Setup:  # noqa: F811
    """Create the business and its two bookings in the test's transaction, with the index in place.

    Args:
        owner_connection: The owner's connection, rolled back after the test.

    Returns:
        The prepared setup.
    """
    exists = owner_connection.execute("SELECT to_regclass('public.bookings_payment_pair_key') IS NOT NULL").fetchone()
    if exists is not None and not exists[0]:
        owner_connection.execute(PAIR_FILE.read_text(encoding="utf-8"))
    account = "acct_conflict" + secrets.token_hex(4)
    business = owner_connection.execute(
        "INSERT INTO businesses (slug, name, payment_account_id) VALUES (%s, 'Conflict Test', %s) RETURNING id",
        ("conflict-" + secrets.token_hex(5), account),
    ).fetchone()
    assert business is not None
    service = owner_connection.execute(
        "INSERT INTO services (business_id, slug, name, duration_minutes, price_minor) "
        "VALUES (%s, 'hour', 'Hour', 60, 2500) RETURNING id",
        (business[0],),
    ).fetchone()
    assert service is not None
    day = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=60)
    holder, waiting = secrets.token_hex(5).upper(), secrets.token_hex(5).upper()
    for reference, hour, status, payment_status, provider, payment, checkout in (
        (holder, 9, "confirmed", "paid", "stripe", "pi_conflict", f"cs_test_{holder}"),
        (waiting, 11, "pending_payment", "awaiting", None, None, f"cs_test_{waiting}"),
    ):
        start = day.replace(hour=hour)
        owner_connection.execute(
            "INSERT INTO bookings (business_id, service_id, reference, status, starts_at, ends_at, blocked_until, "
            "price_minor, customer_name, customer_email, payment_status, payment_provider, payment_id, checkout_id, "
            "hold_expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 2500, 'Pat', 'pat@example.com', %s, %s, %s, %s, %s)",
            (
                business[0],
                service[0],
                reference,
                status,
                start,
                start + timedelta(hours=1),
                start + timedelta(hours=1),
                payment_status,
                provider,
                payment,
                checkout,
                start if status == "pending_payment" else None,
            ),
        )
    payments = PaymentService(
        cast(Database, OneTransaction(owner_connection)), cast(AccountPayments, None), "https://book.example.com"
    )
    return Setup(payments, owner_connection, account, waiting)


def test_a_payment_held_by_another_booking_is_reported_not_raised(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    """The service answers ``False``, logs the conflict, leaves the booking unpaid and keeps the connection usable."""
    with caplog.at_level(logging.ERROR, logger="booking.payments"):
        assert setup.service.record(setup.event("pi_conflict")) is False
    assert setup.state() == ("pending_payment", "awaiting", None)
    assert "conflicts with an existing booking" in caplog.text
    assert "constraint=bookings_payment_pair_key" in caplog.text
    assert setup.waiting in caplog.text


def test_a_late_payment_held_by_another_booking_is_reported_not_raised(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    """The recovery path for a payment that arrives after its time was taken also answers ``False``."""
    setup.connection.execute(
        "UPDATE bookings SET status = 'expired', payment_status = 'awaiting' WHERE reference = %s", (setup.waiting,)
    )
    setup.connection.execute(
        "INSERT INTO bookings (business_id, service_id, reference, status, starts_at, ends_at, blocked_until, "
        "price_minor, customer_name, customer_email, payment_status) "
        "SELECT business_id, service_id, %s, 'confirmed', starts_at, ends_at, blocked_until, 0, 'Sam', "
        "'sam@example.com', 'not_required' FROM bookings WHERE reference = %s",
        (secrets.token_hex(5).upper(), setup.waiting),
    )
    with caplog.at_level(logging.ERROR, logger="booking.payments"):
        assert setup.service.record(setup.event("pi_conflict")) is False
    assert setup.state() == ("expired", "awaiting", None)
    assert "conflicts with an existing booking" in caplog.text
    assert "constraint=bookings_payment_pair_key" in caplog.text


def test_an_ordinary_paid_event_still_confirms_in_the_same_setup(setup: Setup) -> None:
    """With a payment no other booking holds, the booking is confirmed, so the test setup itself is sound."""
    assert setup.service.record(setup.event("pi_fresh")) is True
    assert setup.state() == ("confirmed", "paid", "pi_fresh")
