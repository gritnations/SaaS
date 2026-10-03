// Cancel and reschedule a booking — used by the customer's manage page (via the
// private link token) and by the admin panel's Cancel button.
//
// The state changes happen inside Firestore transactions. Everything that talks
// to the outside world afterwards (refund, calendar, emails) is best-effort and
// NEVER undoes or blocks the cancellation / reschedule itself: the customer's
// booking has changed, so a Paymob or Google hiccup becomes a flagged follow-up
// ("Needs refund") instead of a failed request.

const logger = require('firebase-functions/logger');
const { safeEqual } = require('./hmacUtil');
const { BOOKING_WINDOW_DAYS } = require('./config');
const {
    ACTIVE_STATUSES, getInterval, isActiveNow
} = require('./bookings');
const {
    SAUDI_OFFSET_HOURS, overlapsAny, isValidSlotForDate, isWithinBookingWindow, normalizeBreak, timeToMinutes
} = require('./availability');
const { calculateRefundAmount } = require('./refundPolicy');
const { getRulesByDay, getBlockedDateKeys, getGeneralSettings } = require('./settings');
const { deleteMeeting, moveMeeting } = require('./meeting');
const { sendCancellationEmails, sendRescheduleEmails, sendOwnerAlert, ownerNotificationHtml } = require('./email');
const { toEmailBooking } = require('./confirmation');
const { toPublicStatus } = require('./bookingStatus');

// ---------------------------------------------------------------- helpers

// When the session starts, as a real instant. Slot times are Saudi local time.
function sessionStart(booking) {
    const interval = getInterval(booking);
    if (!interval || !/^\d{4}-\d{2}-\d{2}$/.test(String(booking.slotDate))) return null;
    const p = booking.slotDate.split('-').map(Number);
    return new Date(Date.UTC(p[0], p[1] - 1, p[2], 0, interval.startMinutes) - SAUDI_OFFSET_HOURS * 3600 * 1000);
}

function hasSessionStarted(booking, now) {
    const start = sessionStart(booking);
    return !start || start <= now; // an unreadable time is treated as "too late to change"
}

// A booking can be cancelled or moved only while it is confirmed and hasn't begun.
function isChangeable(booking, now) {
    return booking.status === 'confirmed' && !hasSessionStarted(booking, now);
}

// The private link's token, compared in constant time.
function tokenMatches(booking, token) {
    return typeof token === 'string' && typeof booking.manageToken === 'string'
        && booking.manageToken.length > 0 && safeEqual(booking.manageToken, token);
}

// What the manage page is allowed to know.
function toManageView(booking, now) {
    const pub = toPublicStatus(booking);
    const changeable = isChangeable(booking, now);
    const interval = getInterval(booking);
    return Object.assign(pub, {
        canCancel: changeable,
        canReschedule: changeable,
        durationMinutes: interval ? interval.endMinutes - interval.startMinutes : null,
        // What a cancellation would refund right now (policy lives in refundPolicy.js).
        refundAmount: changeable ? calculateRefundAmount(booking, now) : (booking.refundAmount || 0),
        refundStatus: booking.refundStatus || 'none',
        sessionHasPassed: booking.status === 'confirmed' && !changeable,
        meetLink: changeable && typeof booking.meetLink === 'string' ? booking.meetLink : ''
    });
}

// ---------------------------------------------------------------- cancel

// options: { by: 'customer' | 'admin', now, refundGateway, calendarKey,
//            resendApiKey, calendarRequest }
// Returns { outcome: 'cancelled' | 'already_cancelled' | 'too_late' |
//           'not_cancellable' | 'not_found', ... }.
async function cancelBooking(db, bookingId, options) {
    const now = options.now || new Date();
    const ref = db.collection('bookings').doc(bookingId);

    const tx = await db.runTransaction(async function (t) {
        const snap = await t.get(ref);
        if (!snap.exists) return { outcome: 'not_found' };

        const data = snap.data();
        if (data.status === 'cancelled') return { outcome: 'already_cancelled', booking: Object.assign({ id: bookingId }, data) };
        if (data.status !== 'confirmed') return { outcome: 'not_cancellable' };
        if (hasSessionStarted(data, now)) return { outcome: 'too_late' };

        const refundAmount = calculateRefundAmount(data, now);
        t.update(ref, {
            status: 'cancelled',
            cancelledAt: now,
            cancelledBy: options.by,
            refundAmount: refundAmount,
            refundStatus: refundAmount > 0 ? 'pending' : 'none'
        });
        return { outcome: 'cancelled', booking: Object.assign({ id: bookingId }, data), refundAmount: refundAmount };
    });

    if (tx.outcome === 'already_cancelled') {
        return {
            outcome: 'already_cancelled',
            refundAmount: tx.booking.refundAmount || 0,
            refundStatus: tx.booking.refundStatus || 'none'
        };
    }
    if (tx.outcome !== 'cancelled') return { outcome: tx.outcome };

    const booking = tx.booking;
    const refundAmount = tx.refundAmount;

    // ---- refund (never throws) -----------------------------------------
    let refundStatus = 'none';
    if (refundAmount > 0) {
        refundStatus = 'needs_manual';
        const update = {};
        try {
            if (!options.refundGateway || !booking.paymobTransactionId) {
                throw new Error(!booking.paymobTransactionId ? 'No payment transaction id on the booking.' : 'No refund gateway available.');
            }
            const result = await options.refundGateway.refund({
                providerTransactionId: booking.paymobTransactionId,
                amount: refundAmount,
                currency: booking.currency
            });
            refundStatus = 'refunded';
            update.refundId = (result && result.refundId) || '';
        } catch (err) {
            logger.error('cancelBooking: automatic refund failed — needs a manual refund', { bookingId: bookingId, err: String(err && err.message || err) });
            update.refundError = String(err && err.message || err).slice(0, 300);
        }
        update.refundStatus = refundStatus;
        try { await ref.update(update); } catch (err) { logger.error('cancelBooking: could not record refund status', err); }
    }

    // ---- calendar (never throws) ---------------------------------------
    const calendarOk = await deleteMeeting(options.calendarKey, booking.calendarEventId, options.calendarRequest);

    // ---- emails (never throw) ------------------------------------------
    const emailBooking = toEmailBooking(booking, '');
    try {
        await sendCancellationEmails(options.resendApiKey, emailBooking, { refundAmount: refundAmount, refundStatus: refundStatus, by: options.by });
    } catch (err) {
        logger.error('cancelBooking: cancelled but email failed', { bookingId: bookingId, err: String(err) });
    }
    if (!calendarOk) {
        try {
            await sendOwnerAlert(options.resendApiKey, 'Calendar event could not be removed',
                ownerNotificationHtml(emailBooking, 'This booking was cancelled, but its Google Calendar event could not be deleted automatically — please remove it from your calendar:'));
        } catch (err) {
            logger.error('cancelBooking: calendar alert email failed', err);
        }
    }

    return { outcome: 'cancelled', refundAmount: refundAmount, refundStatus: refundStatus };
}

// ---------------------------------------------------------------- reschedule

// options: { dateKey, time, now, calendarKey, resendApiKey, calendarRequest }
// Moves a confirmed booking to another free time with the SAME length. No
// payment is involved: the service and price are unchanged.
// Returns { outcome: 'rescheduled' | 'invalid_slot' | 'same_slot' | 'slot_taken' |
//           'not_changeable' | 'not_found', booking?, view? }.
async function rescheduleBooking(db, bookingId, options) {
    const now = options.now || new Date();
    const ref = db.collection('bookings').doc(bookingId);

    const current = await ref.get();
    if (!current.exists) return { outcome: 'not_found' };
    const before = Object.assign({ id: bookingId }, current.data());
    if (!isChangeable(before, now)) return { outcome: 'not_changeable' };

    const oldInterval = getInterval(before);
    const durationMinutes = oldInterval.endMinutes - oldInterval.startMinutes;
    const startMinutes = timeToMinutes(options.time);

    const [rulesByDay, blockedDateKeys, settings] = await Promise.all([getRulesByDay(db), getBlockedDateKeys(db), getGeneralSettings(db)]);
    const breakMinutes = normalizeBreak(settings.breakMinutes);

    if (isNaN(startMinutes)
        || !isWithinBookingWindow(options.dateKey, BOOKING_WINDOW_DAYS, now)
        || !isValidSlotForDate(options.dateKey, options.time, durationMinutes, rulesByDay, blockedDateKeys, breakMinutes)) {
        return { outcome: 'invalid_slot' };
    }
    if (options.dateKey === before.slotDate && startMinutes === oldInterval.startMinutes) return { outcome: 'same_slot' };

    let result;
    try {
        result = await db.runTransaction(async function (t) {
            const snap = await t.get(ref);
            const data = snap.data();
            if (!snap.exists || !isChangeable(data, now)) return { outcome: 'not_changeable' };

            const others = await t.get(
                db.collection('bookings').where('slotDate', '==', options.dateKey).where('status', 'in', ACTIVE_STATUSES)
            );
            const intervals = [];
            others.docs.forEach(function (doc) {
                if (doc.id === bookingId) return;
                const other = doc.data();
                if (!isActiveNow(other, now)) return;
                const interval = getInterval(other);
                if (interval) intervals.push(interval);
            });
            if (overlapsAny(startMinutes, startMinutes + durationMinutes, intervals, breakMinutes)) throw new Error('SLOT_TAKEN');

            const update = {
                slotDate: options.dateKey,
                slotTime: options.time,
                startMinutes: startMinutes,
                endMinutes: startMinutes + durationMinutes,
                breakMinutes: breakMinutes,
                rescheduledAt: now,
                rescheduleCount: (data.rescheduleCount || 0) + 1,
                previousSlots: (data.previousSlots || []).concat([{
                    slotDate: data.slotDate, slotTime: data.slotTime,
                    startMinutes: oldInterval.startMinutes, endMinutes: oldInterval.endMinutes, movedAt: now
                }])
            };
            t.update(ref, update);
            return { outcome: 'rescheduled', booking: Object.assign({ id: bookingId }, data, update) };
        });
    } catch (err) {
        if (err.message === 'SLOT_TAKEN') return { outcome: 'slot_taken' };
        throw err;
    }
    if (result.outcome !== 'rescheduled') return { outcome: result.outcome };

    const after = result.booking;

    // ---- calendar + emails (never throw) -------------------------------
    const moved = await moveMeeting(options.calendarKey, after, after.calendarEventId, options.calendarRequest);
    const emailBooking = toEmailBooking(after, typeof after.meetLink === 'string' ? after.meetLink : '');
    const oldLabels = toEmailBooking(before, '');
    try {
        await sendRescheduleEmails(options.resendApiKey, emailBooking, { slotDateLabel: oldLabels.slotDateLabel, slotTimeLabel: oldLabels.slotTimeLabel });
    } catch (err) {
        logger.error('rescheduleBooking: moved but email failed', { bookingId: bookingId, err: String(err) });
    }
    if (after.calendarEventId && !moved) {
        try {
            await sendOwnerAlert(options.resendApiKey, 'Calendar event could not be moved',
                ownerNotificationHtml(emailBooking, 'This booking was rescheduled, but its Google Calendar event could not be moved automatically — please update it in your calendar:'));
        } catch (err) {
            logger.error('rescheduleBooking: calendar alert email failed', err);
        }
    }

    return { outcome: 'rescheduled', booking: after, view: toManageView(after, now) };
}

module.exports = {
    sessionStart, hasSessionStarted, isChangeable, tokenMatches, toManageView,
    cancelBooking, rescheduleBooking
};
