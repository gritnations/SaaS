"""Tests for the record of processed payment events (migration 0017) against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The tests connect as the database owner and share one
transaction with the payment service, so nothing is committed and everything is rolled back afterwards. If the
migrations 0016 or 0017 are not applied yet, their files are run inside that transaction first. The ledger
(0015) must exist, because each migration records itself in it.

What these tests establish: a booking-changing event is recorded once, by provider and event identifier; a second
delivery of the same identifier changes nothing, even when the booking has since been put back, while a new
identifier is applied; an event that changed nothing (a conflict, a mismatched amount, an unknown checkout)
leaves no record and can be delivered again; an event without an identifier is still applied; and the table is
owner-only except for the API role's insert. They do not establish how the provider numbers its events.
"""

import logging
import os
from dataclasses import replace
from typing import cast

import psycopg
import pytest
from psycopg import errors
from test_app_api_role_postgres import owner_connection  # noqa: F401
from test_manage_postgres import URL_ENV
from test_migrations import FILES, number
from test_payment_conflict_postgres import Setup, setup  # noqa: F401

from payments.base import PaymentEvent

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

EVENTS_FILE = next(path for path in FILES if number(path) == 17)
Connection = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def events(setup: Setup) -> Setup:  # noqa: F811
    """Make the event table exist in the open transaction, on top of the payment-conflict setup.

    Args:
        setup: A business with one booking that holds a payment and one waiting for its own.

    Returns:
        The same setup.
    """
    exists = setup.connection.execute("SELECT to_regclass('public.payment_events') IS NOT NULL").fetchone()
    if exists is not None and not exists[0]:
        setup.connection.execute(EVENTS_FILE.read_text(encoding="utf-8"))
    return setup


def stored(setup: Setup, event_id: str) -> int:
    """Count the stored records for an event identifier.

    Args:
        setup: The setup, for its connection.
        event_id: The provider's event identifier.

    Returns:
        How many rows hold it.
    """
    found = setup.connection.execute(
        "SELECT count(*) FROM payment_events WHERE provider = 'stripe' AND event_id = %s", (event_id,)
    ).fetchone()
    assert found is not None
    return cast(int, found[0])


def put_back(setup: Setup) -> None:
    """Put the waiting booking back to waiting for payment, as if the first delivery had never been applied.

    Args:
        setup: The setup, for its connection.
    """
    setup.connection.execute(
        "UPDATE bookings SET status = 'pending_payment', payment_status = 'awaiting', payment_id = NULL, "
        "payment_provider = NULL, manage_token_hash = NULL, hold_expires_at = starts_at WHERE reference = %s",
        (setup.waiting,),
    )


def test_a_repeat_of_the_same_event_is_ignored_by_its_identifier(events: Setup) -> None:
    """The first delivery confirms and is recorded; the same identifier again changes nothing, a new one does."""
    paid = replace(events.event("pi_events_1"), event_id="evt_1")
    assert events.service.record(paid) is True
    assert events.state() == ("confirmed", "paid", "pi_events_1")
    assert stored(events, "evt_1") == 1
    put_back(events)
    assert events.service.record(paid) is False
    assert events.state() == ("pending_payment", "awaiting", None)
    assert stored(events, "evt_1") == 1
    assert events.service.record(replace(paid, event_id="evt_2")) is True
    assert events.state() == ("confirmed", "paid", "pi_events_1")


def test_an_event_that_changed_nothing_leaves_no_record(events: Setup, caplog: pytest.LogCaptureFixture) -> None:
    """A payment held by another booking, a wrong amount and an unknown checkout are not recorded."""
    with caplog.at_level(logging.ERROR, logger="booking.payments"):
        conflict = replace(events.event("pi_conflict"), event_id="evt_conflict")
        assert events.service.record(conflict) is False
    assert stored(events, "evt_conflict") == 0
    wrong = replace(events.event("pi_events_2"), event_id="evt_wrong", amount_minor=100)
    assert events.service.record(wrong) is False
    assert stored(events, "evt_wrong") == 0
    unknown = replace(events.event("pi_events_3"), event_id="evt_unknown", checkout_id="cs_test_nobody")
    assert events.service.record(replace(unknown, order_reference="NOBODY")) is False
    assert stored(events, "evt_unknown") == 0
    assert events.state() == ("pending_payment", "awaiting", None)


def test_an_event_without_an_identifier_is_still_applied(events: Setup) -> None:
    """Providers that send no identifier keep working on the booking's own state; nothing is recorded."""
    assert events.service.record(events.event("pi_events_4")) is True
    assert events.state() == ("confirmed", "paid", "pi_events_4")
    found = events.connection.execute("SELECT count(*) FROM payment_events").fetchone()
    assert found is not None and found[0] == 0


def test_an_expired_checkout_and_a_refund_are_recorded_once(events: Setup) -> None:
    """The expiry and the refund paths also record the event and ignore a repeat of the same identifier."""
    expired = replace(events.event("pi_events_5"), status="expired", event_id="evt_expired")
    assert events.service.record(expired) is True
    assert events.state()[0] == "expired"
    assert stored(events, "evt_expired") == 1
    put_back(events)
    assert events.service.record(expired) is False
    assert events.state()[0] == "pending_payment"
    refund = PaymentEvent(
        provider="stripe", order_reference="", status="refunded", live=False, account_id=events.account,
        amount_minor=2500, currency="GBP", payment_id="pi_conflict", event_id="evt_refund",
    )  # fmt: skip
    assert events.service.record(refund) is True
    assert stored(events, "evt_refund") == 1
    events.connection.execute("UPDATE bookings SET payment_status = 'paid' WHERE payment_id = 'pi_conflict'")
    assert events.service.record(refund) is False
    paid_again = events.connection.execute(
        "SELECT payment_status FROM bookings WHERE payment_id = 'pi_conflict'"
    ).fetchone()
    assert paid_again is not None and paid_again[0] == "paid"


def test_the_event_table_is_owner_only_except_for_the_api_roles_insert(events: Setup) -> None:
    """Row security is on, the public roles hold nothing, and the API role may only insert."""
    connection = events.connection
    found = connection.execute("SELECT relrowsecurity FROM pg_class WHERE oid = 'public.payment_events'::regclass")
    assert found.fetchone() == (True,)
    for role in ("anon", "authenticated", "app_api"):
        exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if exists is None:
            continue
        held = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
            if connection.execute(
                "SELECT has_table_privilege(%s, 'public.payment_events', %s)", (role, privilege)
            ).fetchone()
            == (True,)
        }
        assert held == ({"INSERT"} if role == "app_api" else set()), role


def test_one_event_is_one_row_per_provider(events: Setup) -> None:
    """The same identifier from another provider is allowed; the same pair twice is refused."""
    connection = events.connection
    connection.execute("INSERT INTO payment_events (provider, event_id) VALUES ('stripe', 'evt_pk')")
    connection.execute("INSERT INTO payment_events (provider, event_id) VALUES ('paymob', 'evt_pk')")
    with pytest.raises(errors.UniqueViolation):
        with connection.transaction():
            connection.execute("INSERT INTO payment_events (provider, event_id) VALUES ('stripe', 'evt_pk')")
