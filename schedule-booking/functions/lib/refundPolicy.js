// Refund policy for cancelled bookings, isolated in one place on purpose.
//
// TODAY the policy is simply "full refund on any cancellation" — no
// cancellation rules have been agreed yet. When they are (e.g. free
// cancellation up to 24 hours before, partial or no refund after), this
// function's body is the ONLY thing that needs to change: callers just ask
// "how much do I refund?" and never encode policy themselves.
//
// booking: the booking document (needs .amount, .isPaid, .slotDate, .startMinutes)
// cancelledAt: Date the cancellation is happening
// Returns the refund amount in the smallest currency unit (0 for free bookings).
function calculateRefundAmount(booking, cancelledAt) { // eslint-disable-line no-unused-vars
    if (!booking || !booking.isPaid || !(booking.amount > 0)) return 0;

    // Future rules go here, using booking.slotDate/startMinutes vs cancelledAt.
    return booking.amount;
}

module.exports = { calculateRefundAmount };
