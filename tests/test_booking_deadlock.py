"""Tests that a database deadlock between overlapping requests is answered as "that time is taken". A fake database
stands in, so these run without PostgreSQL.

What these tests establish: when PostgreSQL stops a booking or a move because two overlapping requests deadlocked,
the customer gets the same refusal as for any lost race, not an internal error; and an unrelated database error is
still not hidden. That real simultaneous requests do deadlock, and are then refused cleanly with exactly one winner,
is shown against a real database in ``test_booking_races_postgres.py``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any, cast

import pytest
from psycopg import errors

from booking.manage import ManageService
from booking.service import BookingRequest, BookingService, SlotTakenError
from postgres import Database


class FailingDatabase:
    """A database whose every connection fails with a chosen PostgreSQL error as soon as it is used.

    Attributes:
        failure: The error to raise.
    """

    def __init__(self, failure: Exception) -> None:
        """Create the fake.

        Args:
            failure: The error every connection raises.
        """
        self.failure = failure

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Raise the configured error instead of yielding a connection.

        Yields:
            Never.

        Raises:
            Exception: The configured error.
        """
        raise self.failure
        yield  # pragma: no cover


def database(failure: Exception) -> Database:
    """Build a fake database that fails with the given error.

    Args:
        failure: The error every connection raises.

    Returns:
        The fake, typed as the real boundary.
    """
    return cast(Database, FailingDatabase(failure))


REQUEST = BookingRequest("hour", date(2026, 11, 20), "10:00", "Test Customer", "customer@example.com", None, None)


def test_a_deadlock_while_booking_is_answered_as_the_time_being_taken() -> None:
    """Two overlapping requests deadlock: the stopped one is refused as a lost race."""
    service = BookingService(database(errors.DeadlockDetected("deadlock detected")))
    with pytest.raises(SlotTakenError):
        service.create_booking("any", REQUEST)


def test_a_deadlock_while_moving_is_answered_as_the_time_being_taken() -> None:
    """A move that deadlocks with an overlapping request is refused as a lost race."""
    manage = ManageService(database(errors.DeadlockDetected("deadlock detected")))
    with pytest.raises(SlotTakenError):
        manage.move("any", "ABCDEFGHJK", "0" * 48, date(2026, 11, 20), "15:00")


def test_an_unrelated_database_error_is_not_hidden() -> None:
    """Only the overlap conflict and its deadlock are turned into a refusal; anything else still surfaces."""
    service = BookingService(database(errors.UndefinedTable("relation missing")))
    with pytest.raises(errors.UndefinedTable):
        service.create_booking("any", REQUEST)
