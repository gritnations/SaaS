// What the customer's browser may learn about a booking from just its id (the
// page they're sent back to after paying). Deliberately minimal: no name,
// email, phone, links or tokens — only what's needed to say "you're booked" /
// "still confirming" / "that didn't go through".

const { formatSessionLabel } = require('./availability');
const { getInterval } = require('./bookings');
const { formatDateLabel } = require('./confirmation');

const STATUS_MAP = {
    pending_payment: 'pending',
    confirmed: 'confirmed',
    no_show: 'confirmed',
    paid_slot_conflict: 'conflict',
    expired: 'expired',
    cancelled: 'cancelled',
    refunded: 'cancelled'
};

function toPublicStatus(booking) {
    const interval = getInterval(booking);
    return {
        status: STATUS_MAP[booking.status] || 'unknown',
        serviceName: booking.serviceName || 'Advisory session',
        slotDateLabel: booking.slotDate ? formatDateLabel(booking.slotDate) : '',
        slotTimeLabel: interval ? formatSessionLabel(interval.startMinutes, interval.endMinutes - interval.startMinutes) : (booking.slotTime || ''),
        isPaid: !!booking.isPaid,
        amount: booking.amount,
        currency: booking.currency
    };
}

module.exports = { toPublicStatus };
