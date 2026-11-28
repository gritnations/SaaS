"""Reminder emails, sent to customers before their booking.

A business's customers get a reminder about 24 hours before, and on a paid plan or in the trial an earlier one,
48 hours, 72 hours or a week before. The business switches each on or off. A clock outside the API calls the run
endpoint every few minutes. Each run:

1. claims the confirmed bookings that are due, in one statement that skips rows another run holds, so two runs
   never claim the same booking;
2. sends each claimed booking's reminder through :class:`booking.notify.Notifier`, and records when the email
   provider accepted it;
3. clears the claim of each one whose send failed, so a later run tries again, up to :data:`MAX_ATTEMPTS`.

A booking is due for a reminder when it is confirmed, starts within the reminder's lead time from now, was made or
last moved at least that long before its start, has not had that reminder for its current start, and has had fewer
than :data:`MAX_ATTEMPTS` attempts. The earlier reminder is also sent only while it is still clearly earlier than
the 24-hour one: while the start is further off than halfway between the two (48 hours away for a 72-hour
reminder). So bookings already close when the setting is turned on, or after an outage, do not get two emails close
together. The run reads bookings and settings as they are now, so a cancelled booking is never reminded and a moved
one is reminded for its new time.

Delivery is at most once: a run that stops after claiming and before sending leaves that booking unreminded. A
booking cancelled in the moment between its claim and its send can still receive the reminder.
"""

import logging
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from booking.notify import Notifier, ReminderKind
from booking.service import PAID_PLANS
from postgres import Database

LOGGER = logging.getLogger(__name__)

REMINDER_LEAD = timedelta(hours=24)
"""How long before the start the later reminder is sent."""

MAX_ATTEMPTS = 3
"""Sends tried for one reminder of one booking time before it is left alone."""

BATCH_SIZE = 50
"""The most reminders one run claims. Any left over are claimed by the next run."""

_CLAIM = (
    "UPDATE bookings SET reminder_starts_at = starts_at, reminder_attempts = reminder_attempts + 1, "
    "reminder_sent_at = NULL "
    "WHERE id IN ("
    "SELECT b.id FROM bookings b JOIN businesses z ON z.id = b.business_id "
    "WHERE z.reminder_day_enabled AND b.status = 'confirmed' AND b.starts_at > now() "
    "AND b.starts_at <= now() + %(lead)s AND b.starts_at - %(lead)s >= COALESCE(b.moved_at, b.created_at) "
    "AND b.reminder_starts_at IS DISTINCT FROM b.starts_at AND b.reminder_attempts < %(attempts)s "
    "ORDER BY b.starts_at LIMIT %(limit)s FOR UPDATE OF b SKIP LOCKED"
    ") RETURNING id::text, starts_at"
)

_CLAIM_EARLY = (
    "UPDATE bookings SET reminder_early_starts_at = starts_at, reminder_early_attempts = reminder_early_attempts + 1, "
    "reminder_early_sent_at = NULL "
    "WHERE id IN ("
    "SELECT b.id FROM bookings b JOIN businesses z ON z.id = b.business_id "
    "WHERE z.reminder_early_enabled AND (z.plan = ANY(%(plans)s::text[]) OR z.trial_ends_at > now()) "
    "AND b.status = 'confirmed' "
    "AND b.starts_at > now() + make_interval(hours => (z.reminder_early_hours + %(later)s) / 2) "
    "AND b.starts_at <= now() + make_interval(hours => z.reminder_early_hours::int) "
    "AND b.starts_at - make_interval(hours => z.reminder_early_hours::int) >= COALESCE(b.moved_at, b.created_at) "
    "AND b.reminder_early_starts_at IS DISTINCT FROM b.starts_at AND b.reminder_early_attempts < %(attempts)s "
    "ORDER BY b.starts_at LIMIT %(limit)s FOR UPDATE OF b SKIP LOCKED"
    ") RETURNING id::text, starts_at"
)

_SENT: dict[ReminderKind, str] = {
    "reminder": "UPDATE bookings SET reminder_sent_at = now() WHERE id = %s AND reminder_starts_at = %s",
    "reminder_early": (
        "UPDATE bookings SET reminder_early_sent_at = now() WHERE id = %s AND reminder_early_starts_at = %s"
    ),
}

_RELEASE: dict[ReminderKind, str] = {
    "reminder": "UPDATE bookings SET reminder_starts_at = NULL WHERE id = %s AND reminder_starts_at = %s",
    "reminder_early": (
        "UPDATE bookings SET reminder_early_starts_at = NULL WHERE id = %s AND reminder_early_starts_at = %s"
    ),
}


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
        """Send the reminders that are due now, the 24-hour ones first.

        Returns:
            Counts of what this run did.

        Raises:
            postgres.DatabaseUnavailableError: If the database cannot be reached to claim bookings. Nothing is
                claimed and nothing is sent.
        """
        with self._database.connection() as connection:
            later = connection.execute(
                _CLAIM, {"lead": REMINDER_LEAD, "attempts": MAX_ATTEMPTS, "limit": BATCH_SIZE}
            ).fetchall()
            early = connection.execute(
                _CLAIM_EARLY,
                {
                    "plans": list(PAID_PLANS),
                    "later": int(REMINDER_LEAD.total_seconds() // 3600),
                    "attempts": MAX_ATTEMPTS,
                    "limit": BATCH_SIZE - len(later),
                },
            ).fetchall()
        claimed: list[tuple[ReminderKind, str, datetime]] = [("reminder", row[0], row[1]) for row in later]
        claimed += [("reminder_early", row[0], row[1]) for row in early]
        result = RunResult(claimed=len(claimed))
        for kind, booking_id, starts_at in claimed:
            outcome = self._notifier.reminder(booking_id, kind)
            setattr(result, outcome, getattr(result, outcome) + 1)
            if outcome == "sent":
                self._update(_SENT[kind], "record", booking_id, starts_at)
            elif outcome == "failed":
                self._update(_RELEASE[kind], "release", booking_id, starts_at)
        return result

    def _update(self, statement: str, purpose: str, booking_id: str, starts_at: datetime) -> None:
        """Record a send, or clear a claim after a failed one. A failure here is not raised.

        Args:
            statement: One of :data:`_SENT` or :data:`_RELEASE`.
            purpose: ``record`` or ``release``, for the log.
            booking_id: The booking.
            starts_at: The start the claim was made for; a claim for another start is left as it is.
        """
        try:
            with self._database.connection() as connection:
                connection.execute(statement, (booking_id, starts_at))
        except Exception as exc:  # noqa: BLE001 (the booking stays claimed and is not tried again)
            LOGGER.warning("reminder claim %s failed (%s)", purpose, type(exc).__name__)
