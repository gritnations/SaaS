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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg import errors

from booking.availability import Slot, normalize_break, time_to_minutes
from booking.refunds import refund_paid_booking
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
from payments.base import BookingRefunds
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
        payment_status: ``not_required``, ``at_venue``, ``paid`` (taken online) or ``refunded``.
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
    payment_status: str = "not_required"


_LOOKUP = (
    "SELECT b.id::text, b.reference, b.status, b.starts_at, b.ends_at, b.manage_token_hash, "
    "s.id::text, s.slug, s.name, s.duration_minutes, s.price_minor, "
    "z.id::text, z.slug, z.name, z.time_zone, z.currency, z.break_minutes, z.notice_minutes, z.window_days, "
    "z.cancel_cutoff_minutes, z.move_cutoff_minutes, b.payment_status, b.payment_id, z.payment_account_id, b.payment_provider "
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
        payment_id: The online payment, if one was taken.
        payment_account: The business's payment account, if it has one.
        payment_provider: The gateway that took the payment, if one did.
    """

    booking_id: str
    managed: ManagedBooking
    cancel_cutoff: int
    move_cutoff: int
    payment_id: str | None = None
    payment_account: str | None = None
    payment_provider: str | None = None


class ManageService:
    """Customer self-service operations backed by PostgreSQL."""

    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] | None = None,
        notifier: BookingNotifier | None = None,
        refunds: Mapping[str, BookingRefunds] | None = None,
    ) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
            clock: Returns the current aware instant. ``None`` means the system clock in UTC.
            notifier: Sends emails after a change is committed. ``None`` sends nothing.
            refunds: Refund adapters by gateway name, to give a paid booking's payment back when the customer
                cancels. ``None`` means a paid booking cannot be cancelled online.
        """
        self._refunds = refunds
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
            payment_status=row[21],
        )
        return _Row(row[0], managed, cancel_cutoff, move_cutoff, row[22], row[23], row[24])

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
            RefundUnavailableError: If a paid booking cannot be refunded automatically. It is left as it was.
            payments.base.ProviderUnavailableError: If the refund failed. The booking is left as it was, so the
                customer can try again; a refund already made is found, never repeated.
        """
        with self._database.connection() as connection:
            loaded = self._load(connection, slug, reference, token, lock=True)
            if loaded.managed.status == "cancelled":
                return loaded.managed
            if not loaded.managed.can_cancel:
                raise TooLateError("It is too late to cancel online. Please contact the business.")
            paid = loaded.managed.payment_status == "paid"
            if paid:
                # The refund comes first: a failed refund leaves the booking untouched, and a repeat is safe.
                refund_paid_booking(
                    self._refunds,
                    loaded.payment_provider,
                    loaded.payment_account,
                    loaded.payment_id,
                    loaded.managed.reference,
                )
            connection.execute(
                "UPDATE bookings SET status = 'cancelled', cancelled_at = now(), cancelled_by = 'customer', "
                "payment_status = CASE WHEN %s THEN 'refunded' ELSE payment_status END WHERE id = %s",
                (paid, loaded.booking_id),
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
