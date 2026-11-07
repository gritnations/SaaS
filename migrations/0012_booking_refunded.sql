-- A paid booking that its customer cancels in time is refunded in full; the booking then records it.

ALTER TABLE bookings DROP CONSTRAINT bookings_payment_status_check;
ALTER TABLE bookings ADD CONSTRAINT bookings_payment_status_check
    CHECK (payment_status IN ('not_required', 'awaiting', 'paid', 'at_venue', 'refunded'));
