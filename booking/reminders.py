"""Reminder emails, sent to customers about 24 hours before their booking.

A clock outside the API calls the run endpoint every few minutes. Each run:

1. claims the confirmed bookings that are due, in one statement that skips rows another run holds, so two runs
   never claim the same booking;
2. sends each claimed booking's reminder through :class:`booking.notify.Notifier`;
3. clears the claim of each one whose send failed, so a later run tries again, up to :data:`MAX_ATTEMPTS`.

A booking is due when it is confirmed, starts within :data:`REMINDER_LEAD` from now and has not started, was made
or last moved at least :data:`REMINDER_LEAD` before its start, has not had a reminder for its current start, and
has had fewer than :data:`MAX_ATTEMPTS` attempts. The run reads bookings as they are now, so a cancelled booking is
never reminded and a moved one is reminded for its new time.

Delivery is at most once: a run that stops after claiming and before sending leaves that booking unreminded. A
booking cancelled in the moment between its claim and its send can still receive the reminder.
"""

import logging
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from booking.notify import Notifier
from postgres import Database

LOGGER = logging.getLogger(__name__)

REMINDER_LEAD = timedelta(hours=24)
"""How long before the start a reminder is sent."""

MAX_ATTEMPTS = 3
"""Sends tried for one booking time before it is left alone."""

BATCH_SIZE = 50
"""The most bookings one run claims. Any left over are claimed by the next run."""

_CLAIM = (
    "UPDATE bookings SET reminder_starts_at = starts_at, reminder_attempts = reminder_attempts + 1 "
    "WHERE id IN ("
    "SELECT id FROM bookings "
    "WHERE status = 'confirmed' AND starts_at > now() AND starts_at <= now() + %(lead)s "
    "AND starts_at - %(lead)s >= COALESCE(moved_at, created_at) "
    "AND reminder_starts_at IS DISTINCT FROM starts_at AND reminder_attempts < %(attempts)s "
    "ORDER BY starts_at LIMIT %(limit)s FOR UPDATE SKIP LOCKED"
    ") RETURNING id::text, starts_at"
)

_RELEASE = "UPDATE bookings SET reminder_starts_at = NULL WHERE id = %s AND reminder_starts_at = %s"


class RunResult(BaseModel):
    """What one reminder run did.

    Attributes:
        claimed: Bookings claimed by this run.
        sent: Reminders the email provider accepted.
        skipped: Reminders recorded as skipped because email is not configured.
        failed: Reminders that failed and will be tried again, up to the attempt limit.
        dropped: Bookings gone by the time of sending, which are not tried again.
    """

    model_config = ConfigDict(extra="forbid")

    claimed: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    dropped: int = 0


class ReminderService:
    """Claims due bookings and sends their reminders."""

    def __init__(self, database: Database, notifier: Notifier) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
            notifier: Sends and records the emails.
        """
        self._database = database
        self._notifier = notifier

    def run(self) -> RunResult:
        """Send the reminders that are due now.

        Returns:
            Counts of what this run did.

        Raises:
            postgres.DatabaseUnavailableError: If the database cannot be reached to claim bookings. Nothing is
                claimed and nothing is sent.
        """
        with self._database.connection() as connection:
            claimed = connection.execute(
                _CLAIM, {"lead": REMINDER_LEAD, "attempts": MAX_ATTEMPTS, "limit": BATCH_SIZE}
            ).fetchall()
        result = RunResult(claimed=len(claimed))
        for booking_id, starts_at in claimed:
            outcome = self._notifier.reminder(booking_id)
            setattr(result, outcome, getattr(result, outcome) + 1)
            if outcome == "failed":
                self._release(booking_id, starts_at)
        return result

    def _release(self, booking_id: str, starts_at: datetime) -> None:
        """Clear a claim after a failed send, so a later run tries again. A failure here is not raised.

        Args:
            booking_id: The booking.
            starts_at: The start the claim was made for; a claim for another start is left as it is.
        """
        try:
            with self._database.connection() as connection:
                connection.execute(_RELEASE, (booking_id, starts_at))
        except Exception as exc:  # noqa: BLE001 (the booking stays claimed and is not tried again)
            LOGGER.warning("reminder claim release failed (%s)", type(exc).__name__)
