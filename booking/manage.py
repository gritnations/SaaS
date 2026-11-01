"""Customer self-service by private link: view, cancel and move a booking.

Trust path: ``api -> booking.manage -> postgres.Database -> PostgreSQL``.

The manage code in the link is the only credential. It is compared, in constant time, with the SHA-256
hash stored on the booking; the code itself is never stored. A wrong business, reference or code all
give the same :class:`booking.service.NotFoundError`, so the answer reveals nothing.

Deadlines are the business's own: a booking can be cancelled up to ``cancel_cutoff_minutes`` and moved
up to ``move_cutoff_minutes`` before it starts. A move is checked against availability with the booking's
own time left out, then applied in the same transaction; the database's overlap constraint settles any
race. Emails are sent after the change is committed and can never undo it.
"""

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg import errors

from booking.availability import Slot, normalize_break, time_to_minutes
from booking.service import (
    BookingError,
    BookingNotifier,
    BookingService,
    Business,
    InvalidSlotError,
    NotFoundError,
    Service,
    SlotTakenError,
    hash_token,
    release_expired_holds,
)
from postgres import Database


class TooLateError(BookingError):
    """The deadline for this change has passed, or the booking is no longer active."""


@dataclass(frozen=True)
class ManagedBooking:
    """A booking as its customer sees it through the manage link.

    Attributes:
        reference: Public booking reference.
        status: ``confirmed`` or ``cancelled``.
        business: The business.
        service: The booked service.
        starts_at: Start, aware.
        ends_at: End, aware.
        can_cancel: Whether the customer may cancel now.
        can_move: Whether the customer may move it now.
        cancel_deadline: The last moment to cancel online.
        move_deadline: The last moment to move online.
    """

    reference: str
    status: str
    business: Business
    service: Service
    starts_at: datetime
    ends_at: datetime
    can_cancel: bool
    can_move: bool
    cancel_deadline: datetime
    move_deadline: datetime


_LOOKUP = (
    "SELECT b.id::text, b.reference, b.status, b.starts_at, b.ends_at, b.manage_token_hash, "
    "s.id::text, s.slug, s.name, s.duration_minutes, s.price_minor, "
    "z.id::text, z.slug, z.name, z.time_zone, z.currency, z.break_minutes, z.notice_minutes, z.window_days, "
    "z.cancel_cutoff_minutes, z.move_cutoff_minutes "
    "FROM bookings b JOIN services s ON s.id = b.service_id JOIN businesses z ON z.id = b.business_id "
    "WHERE z.slug = %s AND b.reference = %s"
)


@dataclass(frozen=True)
class _Row:
    """A booking loaded for management, with what the rules need.

    Attributes:
        booking_id: Internal identifier.
        managed: The customer's view.
        cancel_cutoff: Minutes before the start after which cancelling online is closed.
        move_cutoff: Minutes before the start after which moving online is closed.
    """

    booking_id: str
    managed: ManagedBooking
    cancel_cutoff: int
    move_cutoff: int


class ManageService:
    """Customer self-service operations backed by PostgreSQL."""

    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] | None = None,
        notifier: BookingNotifier | None = None,
    ) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
            clock: Returns the current aware instant. ``None`` means the system clock in UTC.
            notifier: Sends emails after a change is committed. ``None`` sends nothing.
        """
        self._database = database
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._notifier = notifier
        self._bookings = BookingService(database, self._clock)

    def _load(self, connection: psycopg.Connection[Any], slug: str, reference: str, token: str, lock: bool) -> _Row:
        """Load a booking and check its manage code.

        Args:
            connection: An open connection.
            slug: The business's public identifier.
            reference: The booking reference, in any letter case.
            token: The manage code from the link.
            lock: Whether to lock the booking row for an update in this transaction.

        Returns:
            The booking and its rules.

        Raises:
            NotFoundError: If the business, reference or code does not match.
        """
        row = connection.execute(
            _LOOKUP + (" FOR UPDATE OF b" if lock else ""), (slug, reference.strip().upper())
        ).fetchone()
        stored = bytes(row[5]) if row is not None and row[5] is not None else b""
        if row is None or not stored or not hmac.compare_digest(stored, hash_token(token)):
            raise NotFoundError("Booking not found.")
        business = Business(*row[11:19])
        service = Service(*row[6:11])
        now = self._clock()
        starts_at: datetime = row[3]
        cancel_cutoff, move_cutoff = int(row[19]), int(row[20])
        cancel_deadline = starts_at - timedelta(minutes=cancel_cutoff)
        move_deadline = starts_at - timedelta(minutes=move_cutoff)
        active = row[2] == "confirmed"
        managed = ManagedBooking(
            reference=row[1],
            status=row[2],
            business=business,
            service=service,
            starts_at=starts_at,
            ends_at=row[4],
            can_cancel=active and now <= cancel_deadline,
            can_move=active and now <= move_deadline,
            cancel_deadline=cancel_deadline,
            move_deadline=move_deadline,
        )
        return _Row(row[0], managed, cancel_cutoff, move_cutoff)

    def view(self, slug: str, reference: str, token: str) -> ManagedBooking:
        """Show a booking to the holder of its manage code.

        Args:
            slug: The business's public identifier.
            reference: The booking reference.
            token: The manage code.

        Returns:
            The booking and what may be done with it now.

        Raises:
            NotFoundError: If the business, reference or code does not match.
        """
        with self._database.connection() as connection:
            return self._load(connection, slug, reference, token, lock=False).managed

    def cancel(self, slug: str, reference: str, token: str) -> ManagedBooking:
        """Cancel a booking for its customer. Cancelling an already cancelled booking changes nothing.

        Args:
            slug: The business's public identifier.
            reference: The booking reference.
            token: The manage code.

        Returns:
            The booking after the change.

        Raises:
            NotFoundError: If the business, reference or code does not match.
            TooLateError: If the cancellation deadline has passed.
        """
        with self._database.connection() as connection:
            loaded = self._load(connection, slug, reference, token, lock=True)
            if loaded.managed.status == "cancelled":
                return loaded.managed
            if not loaded.managed.can_cancel:
                raise TooLateError("It is too late to cancel online. Please contact the business.")
            connection.execute(
                "UPDATE bookings SET status = 'cancelled', cancelled_at = now(), cancelled_by = 'customer' "
                "WHERE id = %s",
                (loaded.booking_id,),
            )
            after = self._load(connection, slug, reference, token, lock=False).managed
        if self._notifier is not None:
            self._notifier.booking_event("cancelled", loaded.booking_id, None)
        return after

    def move(self, slug: str, reference: str, token: str, day: date, start: str) -> ManagedBooking:
        """Move a booking to another available session of the same service.

        Args:
            slug: The business's public identifier.
            reference: The booking reference.
            token: The manage code.
            day: The new local date.
            start: The new local start, ``HH:MM``.

        Returns:
            The booking after the move.

        Raises:
            NotFoundError: If the business, reference or code does not match.
            TooLateError: If the booking is cancelled or the move deadline has passed.
            InvalidSlotError: If the new time is not an available session, or is the current time.
            SlotTakenError: If another booking took the new time first.
        """
        if time_to_minutes(start) is None:
            raise InvalidSlotError("That time is not available.")
        try:
            with self._database.connection() as connection:
                loaded = self._load(connection, slug, reference, token, lock=True)
                current = loaded.managed
                if not current.can_move:
                    raise TooLateError("This booking can no longer be moved online. Please contact the business.")
                release_expired_holds(connection, current.business.id)
                days = self._bookings._days(
                    connection, current.business, current.service, [day], exclude_reference=current.reference
                )
                slot: Slot | None = next(
                    (s for d in days for s in d.slots if s.time == start and s.available),
                    None,
                )
                if slot is None or slot.starts_at == current.starts_at:
                    raise InvalidSlotError("That time is not available.")
                gap = timedelta(minutes=normalize_break(current.business.break_minutes))
                connection.execute(
                    "UPDATE bookings SET starts_at = %s, ends_at = %s, blocked_until = %s, "
                    "move_count = move_count + 1 WHERE id = %s",
                    (slot.starts_at, slot.ends_at, slot.ends_at + gap, loaded.booking_id),
                )
                after = self._load(connection, slug, reference, token, lock=False).managed
        except (errors.ExclusionViolation, errors.DeadlockDetected):
            # See BookingService.create_booking: a deadlock between overlapping requests means the time is taken.
            raise SlotTakenError("That time has just been booked.") from None
        if self._notifier is not None:
            self._notifier.booking_event("moved", loaded.booking_id, token)
        return after
