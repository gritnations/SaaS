// How bookings are presented in the admin panel: status labels, filtering,
// sorting and the overview numbers. Pure functions — no DOM, no Firebase.

// tone drives the badge colour: ok | wait | quiet | bad
export const STATUS_META = {
    confirmed: { label: 'Confirmed', tone: 'ok', group: 'confirmed' },
    pending_payment: { label: 'Awaiting payment', tone: 'wait', group: 'pending' },
    expired: { label: 'Expired', tone: 'quiet', group: 'closed' },
    cancelled: { label: 'Cancelled', tone: 'quiet', group: 'closed' },
    no_show: { label: 'No-show', tone: 'bad', group: 'no_show' },
    paid_slot_conflict: { label: 'Needs refund', tone: 'bad', group: 'refund' },
    refunded: { label: 'Refunded', tone: 'quiet', group: 'closed' }
};

export const FILTERS = [
    { id: 'all', label: 'All' },
    { id: 'confirmed', label: 'Confirmed' },
    { id: 'pending', label: 'Awaiting payment' },
    { id: 'closed', label: 'Cancelled / expired' },
    { id: 'no_show', label: 'No-shows' },
    { id: 'refund', label: 'Needs refund' }
];

export function statusMeta(status) {
    return STATUS_META[status] || { label: String(status || 'Unknown'), tone: 'quiet', group: 'closed' };
}

// How ONE booking is shown. A cancelled booking also carries its refund
// follow-up: 'needs_manual' (the automatic refund failed) and 'pending' (the
// refund was started but never recorded) both need the owner to act in Paymob.
export function bookingMeta(b) {
    if (b.status === 'cancelled') {
        if (b.refundStatus === 'needs_manual') return { label: 'Cancelled — needs refund', tone: 'bad', group: 'refund' };
        if (b.refundStatus === 'pending') return { label: 'Cancelled — check refund', tone: 'bad', group: 'refund' };
        if (b.refundStatus === 'refunded' || b.refundStatus === 'refunded_manually') return { label: 'Cancelled — refunded', tone: 'quiet', group: 'closed' };
    }
    return statusMeta(b.status);
}

export function refundOutstanding(b) {
    return b.status === 'paid_slot_conflict'
        || (b.status === 'cancelled' && (b.refundStatus === 'needs_manual' || b.refundStatus === 'pending'));
}

// Bookings made before variable-length services only stored `slotTime` and
// were always 60 minutes — derive the interval the same way the backend does.
export function bookingInterval(data) {
    if (Number.isInteger(data.startMinutes) && Number.isInteger(data.endMinutes)) {
        return { startMinutes: data.startMinutes, endMinutes: data.endMinutes };
    }
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(data.slotTime || ''));
    if (!m) return null;
    const start = Number(m[1]) * 60 + Number(m[2]);
    return { startMinutes: start, endMinutes: start + 60 };
}

export function normalizeBooking(id, data) {
    const interval = bookingInterval(data);
    return {
        id: id,
        status: data.status,
        slotDate: data.slotDate,
        startMinutes: interval ? interval.startMinutes : null,
        endMinutes: interval ? interval.endMinutes : null,
        serviceName: data.serviceName || 'Advisory session',
        customerName: data.customerName || '',
        customerEmail: data.customerEmail || '',
        customerMobile: data.customerMobile || '',
        isPaid: !!data.isPaid,
        amount: Number.isInteger(data.amount) ? data.amount : 0,
        currency: data.currency || '',
        paymobTransactionId: data.paymobTransactionId || null,
        refundAmount: Number.isInteger(data.refundAmount) ? data.refundAmount : 0,
        refundStatus: data.refundStatus || null,
        cancelledBy: data.cancelledBy || null
    };
}

export function sortBookings(bookings) {
    return bookings.slice().sort(function (a, b) {
        if (a.slotDate !== b.slotDate) return a.slotDate < b.slotDate ? -1 : 1;
        return (a.startMinutes || 0) - (b.startMinutes || 0);
    });
}

export function filterBookings(bookings, filterId) {
    if (filterId === 'all') return bookings;
    return bookings.filter(function (b) { return bookingMeta(b).group === filterId; });
}

export function countByFilter(bookings) {
    const counts = {};
    FILTERS.forEach(function (f) { counts[f.id] = 0; });
    bookings.forEach(function (b) {
        counts.all++;
        counts[bookingMeta(b).group]++;
    });
    return counts;
}

// ---- the quarter calendar ------------------------------------------------

// Which bookings put a number on a calendar day: sessions that are (or were) actually
// held for that day, meaning confirmed ones and no-shows. Cancelled, refunded, expired
// and still-awaiting-payment bookings don't.
export const COUNTED_STATUSES = ['confirmed', 'no_show'];

// { 'YYYY-MM-DD': number of counted bookings that day }, only for days with at least one.
export function countsByDay(bookings) {
    const counts = {};
    bookings.forEach(function (b) {
        if (COUNTED_STATUSES.indexOf(b.status) === -1 || !b.slotDate) return;
        counts[b.slotDate] = (counts[b.slotDate] || 0) + 1;
    });
    return counts;
}

// scope: { kind: 'quarter' } | { kind: 'month', year, month } | { kind: 'day', date: 'YYYY-MM-DD' }
// (month is 0-based). Every loaded booking is already inside the quarter.
export function inScope(booking, scope) {
    if (!scope || scope.kind === 'quarter') return true;
    if (scope.kind === 'day') return booking.slotDate === scope.date;
    if (scope.kind === 'month') {
        const prefix = scope.year + '-' + String(scope.month + 1).padStart(2, '0') + '-';
        return typeof booking.slotDate === 'string' && booking.slotDate.indexOf(prefix) === 0;
    }
    return true;
}

export function scopeBookings(bookings, scope) {
    return bookings.filter(function (b) { return inScope(b, scope); });
}

// The moment a booking's session begins, as a Date (local time).
export function startsAt(booking) {
    if (!booking.slotDate || booking.startMinutes === null) return null;
    const [y, m, d] = booking.slotDate.split('-').map(Number);
    return new Date(y, m - 1, d, Math.floor(booking.startMinutes / 60), booking.startMinutes % 60);
}

export function hasStarted(booking, now) {
    const t = startsAt(booking);
    return !!t && t <= now;
}

// Cancel is offered only for a confirmed session that hasn't begun (the server
// enforces the same rule).
export function canCancel(booking, now) {
    return booking.status === 'confirmed' && !hasStarted(booking, now);
}

// "Mark refunded" is offered once the money has been (or must be) returned by hand.
export function canMarkRefunded(booking) {
    return refundOutstanding(booking);
}

// Overview numbers for a set of bookings (normally one month's).
//   attended  = confirmed sessions whose start time has passed, plus no-shows
//               (a no-show was a real session that was held open for someone)
//   revenue   = money actually kept: paid bookings that are confirmed or no-show.
//               Cancelled bookings are refunded in full today, so they're excluded;
//               "needs refund" payments are counted separately, not as revenue.
export function computeStats(bookings, now) {
    let upcoming = 0;
    let completed = 0;
    let noShows = 0;
    let cancelled = 0;
    let needsRefund = 0;
    let awaitingPayment = 0;
    let revenue = 0;
    let currency = '';

    bookings.forEach(function (b) {
        if (b.currency) currency = b.currency;

        if (b.status === 'confirmed') {
            if (hasStarted(b, now)) completed++; else upcoming++;
            if (b.isPaid) revenue += b.amount;
        } else if (b.status === 'no_show') {
            noShows++;
            if (b.isPaid) revenue += b.amount;
        } else if (b.status === 'cancelled') {
            cancelled++;
            if (refundOutstanding(b)) needsRefund++;
        } else if (b.status === 'paid_slot_conflict') {
            needsRefund++;
        } else if (b.status === 'pending_payment') {
            awaitingPayment++;
        }
    });

    const attended = completed + noShows;
    return {
        upcoming,
        completed,
        noShows,
        cancelled,
        needsRefund,
        awaitingPayment,
        revenue,
        currency,
        noShowRate: attended > 0 ? noShows / attended : null
    };
}
