// Firestore access for the bookings collection. All writes go through here
// so the "no two people hold overlapping time" guarantee lives in one place,
// inside transactions, rather than being re-implemented per caller.
//
// Status lifecycle:
//   paid service:  pending_payment -> confirmed   (webhook)   or -> expired (hold ran out)
//   free service:  confirmed immediately (no hold, no payment)
//   any confirmed: -> cancelled
//   edge case:     paid after the slot was lost to someone else -> paid_slot_conflict
//                  (needs a refund / manual attention — never silently double-booked)

const crypto = require('crypto');
const { HOLD_WINDOW_MINUTES } = require('./config');
const { timeToMinutes, overlapsAny, normalizeBreak } = require('./availability');

const ACTIVE_STATUSES = ['pending_payment', 'confirmed'];
const LEGACY_DEFAULT_DURATION_MINUTES = 60;

// Random secret embedded in the customer's cancel/reschedule link. Kept
// separate from the Firestore document ID so the internal ID isn't the same
// value exposed in a public URL.
function generateManageToken() {
    return crypto.randomBytes(24).toString('hex');
}

// The interval a booking occupies, in minutes since midnight. Bookings made
// before variable-duration support only stored `slotTime` (and were always
// 60 minutes), so derive the interval for those rather than misreading them.
function getInterval(data) {
    if (Number.isInteger(data.startMinutes) && Number.isInteger(data.endMinutes)) {
        return { startMinutes: data.startMinutes, endMinutes: data.endMinutes };
    }
    const start = timeToMinutes(data.slotTime);
    if (isNaN(start)) return null;
    return { startMinutes: start, endMinutes: start + LEGACY_DEFAULT_DURATION_MINUTES };
}

// A booking blocks time if it's confirmed, or pending AND still within its
// hold window.
function isActiveNow(data, now) {
    if (data.status === 'confirmed') return true;
    if (data.status === 'pending_payment') {
        return !!data.expiresAt && data.expiresAt.toDate() > now;
    }
    return false;
}

// Reserves a slot. Runs inside a Firestore transaction (server-side
// transactions lock the queried range, so two near-simultaneous requests for
// overlapping times can't both succeed) and fails with SLOT_TAKEN if any
// active booking on that date overlaps the requested interval.
//
// Free services are created already `confirmed` — no hold, no expiry.
async function reserveSlot(db, params) {
    const {
        dateKey, time, startMinutes, durationMinutes,
        service, customerName, customerEmail, customerMobile,
        isPaid, amount, currency, breakMinutes
    } = params;

    const bookingRef = db.collection('bookings').doc();
    const now = new Date();
    const endMinutes = startMinutes + durationMinutes;
    const manageToken = generateManageToken();

    await db.runTransaction(async function (tx) {
        const existing = await tx.get(
            db.collection('bookings')
                .where('slotDate', '==', dateKey)
                .where('status', 'in', ACTIVE_STATUSES)
        );

        const intervals = [];
        existing.docs.forEach(function (doc) {
            const data = doc.data();
            if (!isActiveNow(data, now)) return;
            const interval = getInterval(data);
            if (interval) intervals.push(interval);
        });

        if (overlapsAny(startMinutes, endMinutes, intervals, breakMinutes)) {
            throw new Error('SLOT_TAKEN');
        }

        tx.set(bookingRef, {
            serviceId: service.id,
            // Snapshots: editing or deleting the service later must not
            // rewrite what this customer actually booked and paid for.
            serviceName: service.name,
            durationMinutes: durationMinutes,
            // The break in force when this was booked; used again if a late
            // payment has to re-check for a clash (see confirmPaidBooking).
            breakMinutes: normalizeBreak(breakMinutes),
            slotDate: dateKey,
            slotTime: time,
            startMinutes: startMinutes,
            endMinutes: endMinutes,
            customerName,
            customerEmail,
            customerMobile,
            isPaid: !!isPaid,
            amount,
            currency,
            status: isPaid ? 'pending_payment' : 'confirmed',
            manageToken: manageToken,
            meetLink: null,
            paymobSpecialReference: isPaid ? bookingRef.id : null,
            paymobIntentionId: null,
            paymobTransactionId: null,
            createdAt: now,
            expiresAt: isPaid ? new Date(now.getTime() + HOLD_WINDOW_MINUTES * 60 * 1000) : null,
            confirmedAt: isPaid ? null : now,
            cancelledAt: null
        });
    });

    return { id: bookingRef.id, manageToken: manageToken };
}

async function attachPaymobIntention(db, bookingId, intentionId) {
    await db.collection('bookings').doc(bookingId).update({ paymobIntentionId: intentionId });
}

// Applies a verified payment to a booking. Returns one of:
//   { outcome: 'confirmed', booking }         — booking is now confirmed
//   { outcome: 'conflict', booking }          — customer paid but the time was
//                                               taken by someone else meanwhile
//   { outcome: 'already_handled' }            — already confirmed/cancelled/etc,
//                                               so a duplicate or retried webhook
//                                               is harmless
//   { outcome: 'not_found' }
//
// A payment can legitimately arrive AFTER the 20-minute hold lapsed (the
// customer left the checkout page open). If the time is still free we honor
// it — they paid, they get their slot. If someone else has since booked an
// overlapping time, confirming would double-book, so we flag a conflict for
// refund instead.
async function confirmPaidBooking(db, bookingId, providerTransactionId) {
    const bookingRef = db.collection('bookings').doc(bookingId);
    const now = new Date();

    return db.runTransaction(async function (tx) {
        const snap = await tx.get(bookingRef);
        if (!snap.exists) return { outcome: 'not_found' };

        const data = snap.data();
        if (data.status !== 'pending_payment' && data.status !== 'expired') {
            return { outcome: 'already_handled' };
        }

        const mine = getInterval(data);
        const others = await tx.get(
            db.collection('bookings')
                .where('slotDate', '==', data.slotDate)
                .where('status', 'in', ACTIVE_STATUSES)
        );

        const otherIntervals = [];
        others.docs.forEach(function (doc) {
            if (doc.id === bookingId) return;
            const other = doc.data();
            if (!isActiveNow(other, now)) return;
            const interval = getInterval(other);
            if (interval) otherIntervals.push(interval);
        });

        if (mine && overlapsAny(mine.startMinutes, mine.endMinutes, otherIntervals, data.breakMinutes)) {
            tx.update(bookingRef, {
                status: 'paid_slot_conflict',
                paymobTransactionId: providerTransactionId
            });
            return { outcome: 'conflict', booking: Object.assign({ id: bookingId }, data) };
        }

        tx.update(bookingRef, {
            status: 'confirmed',
            paymobTransactionId: providerTransactionId,
            confirmedAt: now
        });

        return {
            outcome: 'confirmed',
            booking: Object.assign({ id: bookingId }, data, { status: 'confirmed', paymobTransactionId: providerTransactionId })
        };
    });
}

// Immediately releases a pending booking whose customer never reached the
// payment page (e.g. the gateway call failed), rather than leaving the time
// blocked until the hold window runs out. Guarded so it can't clobber a
// booking that was confirmed in the meantime.
async function abandonPendingBooking(db, bookingId) {
    const ref = db.collection('bookings').doc(bookingId);
    await db.runTransaction(async function (tx) {
        const snap = await tx.get(ref);
        if (snap.exists && snap.data().status === 'pending_payment') {
            tx.update(ref, { status: 'expired' });
        }
    });
}

async function getBookingById(db, bookingId) {
    if (typeof bookingId !== 'string' || !bookingId) return null;
    const snap = await db.collection('bookings').doc(bookingId).get();
    if (!snap.exists) return null;
    return Object.assign({ id: snap.id }, snap.data());
}

// Frees any pending_payment booking whose hold window has passed. Meant to
// run on a schedule (see expirePendingBookings in index.js). Free bookings
// have no expiresAt, so they never match.
async function expireStalePending(db) {
    const now = new Date();
    const stale = await db.collection('bookings')
        .where('status', '==', 'pending_payment')
        .where('expiresAt', '<=', now)
        .get();

    const batch = db.batch();
    stale.docs.forEach(function (doc) {
        batch.update(doc.ref, { status: 'expired' });
    });
    if (!stale.empty) await batch.commit();

    return stale.size;
}

// Every active reservation in the date range as occupied intervals, keyed by
// date: Map<'YYYY-MM-DD', [{ startMinutes, endMinutes }]>.
// excludeBookingId leaves one booking out — used when that booking is being
// rescheduled, so its own current time doesn't block the times around it.
async function loadBookedIntervalsByDate(db, startDateKey, endDateKey, excludeBookingId) {
    const now = new Date();
    const snap = await db.collection('bookings')
        .where('slotDate', '>=', startDateKey)
        .where('slotDate', '<=', endDateKey)
        .where('status', 'in', ACTIVE_STATUSES)
        .get();

    const byDate = new Map();
    snap.docs.forEach(function (doc) {
        if (excludeBookingId && doc.id === excludeBookingId) return;
        const data = doc.data();
        if (!isActiveNow(data, now)) return;
        const interval = getInterval(data);
        if (!interval) return;

        if (!byDate.has(data.slotDate)) byDate.set(data.slotDate, []);
        byDate.get(data.slotDate).push(interval);
    });

    return byDate;
}

module.exports = {
    ACTIVE_STATUSES,
    generateManageToken,
    getInterval,
    isActiveNow,
    reserveSlot,
    attachPaymobIntention,
    confirmPaidBooking,
    abandonPendingBooking,
    getBookingById,
    expireStalePending,
    loadBookedIntervalsByDate
};
