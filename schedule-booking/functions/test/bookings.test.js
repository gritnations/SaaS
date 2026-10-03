// Run with: node test/bookings.test.js
// Exercises the reservation / payment-confirmation / expiry logic in
// lib/bookings.js against an in-memory Firestore stand-in.

const { createFakeDb } = require('./fakeFirestore');
const b = require('../lib/bookings');

let pass = 0;
let fail = 0;
function check(name, cond) {
    if (cond) { pass++; } else { fail++; console.log('FAIL:', name); }
}

const MIN = 60 * 1000;
const DAY = '2026-09-27';
const svc60 = { id: 'svc60', name: 'Business Review' };
const svc30 = { id: 'svc30', name: 'Quick Call' };

function reserveParams(over) {
    return Object.assign({
        dateKey: DAY, time: '09:00', startMinutes: 540, durationMinutes: 60, service: svc60,
        customerName: 'A', customerEmail: 'a@b.co', customerMobile: '+966500000000',
        isPaid: true, amount: 50000, currency: 'SAR'
    }, over);
}

async function rejectsWith(promise, message) {
    try { await promise; return false; } catch (e) { return e.message === message; }
}

(async function run() {
    // ---- reserveSlot: paid vs free ----
    {
        const db = createFakeDb();
        const paid = await b.reserveSlot(db, reserveParams({}));
        const doc = db.raw('bookings', paid.id);
        check('paid: starts pending_payment', doc.status === 'pending_payment');
        check('paid: has a hold expiry in the future', doc.expiresAt instanceof Date && doc.expiresAt > new Date());
        check('paid: not yet confirmed', doc.confirmedAt === null);
        check('paid: stores interval + snapshots', doc.startMinutes === 540 && doc.endMinutes === 600 && doc.serviceName === 'Business Review' && doc.durationMinutes === 60);
        check('paid: manage token stored and returned', doc.manageToken === paid.manageToken && paid.manageToken.length === 48);
        check('paid: paymob reference is the booking id', doc.paymobSpecialReference === paid.id);

        const free = await b.reserveSlot(db, reserveParams({ time: '11:00', startMinutes: 660, isPaid: false, amount: 0 }));
        const fdoc = db.raw('bookings', free.id);
        check('free: confirmed immediately', fdoc.status === 'confirmed');
        check('free: no hold expiry', fdoc.expiresAt === null);
        check('free: confirmedAt set', fdoc.confirmedAt instanceof Date);
        check('free: no paymob reference', fdoc.paymobSpecialReference === null);
    }

    // ---- reserveSlot: overlap detection across DIFFERENT durations ----
    {
        const db = createFakeDb();
        await b.reserveSlot(db, reserveParams({})); // 09:00-10:00, pending (active)

        check('same slot rejected', await rejectsWith(b.reserveSlot(db, reserveParams({})), 'SLOT_TAKEN'));
        check('30min starting inside the 60min booking rejected',
            await rejectsWith(b.reserveSlot(db, reserveParams({ time: '09:30', startMinutes: 570, durationMinutes: 30, service: svc30 })), 'SLOT_TAKEN'));
        check('back-to-back 30min at 10:00 allowed',
            !(await rejectsWith(b.reserveSlot(db, reserveParams({ time: '10:00', startMinutes: 600, durationMinutes: 30, service: svc30 })), 'SLOT_TAKEN')));
        check('a 90min starting before and running into it rejected',
            await rejectsWith(b.reserveSlot(db, reserveParams({ time: '08:30', startMinutes: 510, durationMinutes: 90 })), 'SLOT_TAKEN'));
        check('same time on a DIFFERENT date allowed',
            !(await rejectsWith(b.reserveSlot(db, reserveParams({ dateKey: '2026-09-28' })), 'SLOT_TAKEN')));
    }

    // ---- reserveSlot: only ACTIVE bookings block ----
    {
        const db = createFakeDb();
        db.seed('bookings', 'old-pending', { slotDate: DAY, slotTime: '09:00', startMinutes: 540, endMinutes: 600, status: 'pending_payment', expiresAt: new Date(Date.now() - 5 * MIN) });
        db.seed('bookings', 'old-cancelled', { slotDate: DAY, slotTime: '10:00', startMinutes: 600, endMinutes: 660, status: 'cancelled' });
        db.seed('bookings', 'old-expired', { slotDate: DAY, slotTime: '11:00', startMinutes: 660, endMinutes: 720, status: 'expired' });

        check('lapsed pending hold does not block', !(await rejectsWith(b.reserveSlot(db, reserveParams({})), 'SLOT_TAKEN')));
        check('cancelled booking does not block', !(await rejectsWith(b.reserveSlot(db, reserveParams({ time: '10:00', startMinutes: 600 })), 'SLOT_TAKEN')));
        check('expired booking does not block', !(await rejectsWith(b.reserveSlot(db, reserveParams({ time: '11:00', startMinutes: 660 })), 'SLOT_TAKEN')));
    }

    // ---- legacy bookings (pre-variable-duration: slotTime only) still block ----
    {
        const db = createFakeDb();
        db.seed('bookings', 'legacy', { slotDate: DAY, slotTime: '10:00', status: 'confirmed' });
        check('legacy 60min booking blocks an overlapping 30min slot',
            await rejectsWith(b.reserveSlot(db, reserveParams({ time: '10:30', startMinutes: 630, durationMinutes: 30 })), 'SLOT_TAKEN'));
        check('...but not the next hour', !(await rejectsWith(b.reserveSlot(db, reserveParams({ time: '11:00', startMinutes: 660 })), 'SLOT_TAKEN')));
    }

    // ---- confirmPaidBooking ----
    {
        const db = createFakeDb();
        const r = await b.reserveSlot(db, reserveParams({}));

        const first = await b.confirmPaidBooking(db, r.id, 'txn-1');
        check('pending -> confirmed', first.outcome === 'confirmed' && db.raw('bookings', r.id).status === 'confirmed');
        check('records the transaction id + confirmedAt', db.raw('bookings', r.id).paymobTransactionId === 'txn-1' && db.raw('bookings', r.id).confirmedAt instanceof Date);
        check('returned booking is usable for emails', first.booking.id === r.id && first.booking.manageToken === r.manageToken);

        const dup = await b.confirmPaidBooking(db, r.id, 'txn-1');
        check('duplicate/retried webhook is harmless', dup.outcome === 'already_handled');
        check('unknown booking => not_found', (await b.confirmPaidBooking(db, 'nope', 't')).outcome === 'not_found');
    }

    // late payment where the time is STILL FREE: customer paid, they keep their slot
    {
        const db = createFakeDb();
        const r = await b.reserveSlot(db, reserveParams({}));
        db.seed('bookings', r.id, Object.assign({}, db.raw('bookings', r.id), { status: 'expired' })); // sweep already ran
        const late = await b.confirmPaidBooking(db, r.id, 'txn-late');
        check('late payment, time still free => confirmed', late.outcome === 'confirmed' && db.raw('bookings', r.id).status === 'confirmed');
    }

    // late payment where someone ELSE took the time: must NOT double-book
    {
        const db = createFakeDb();
        db.seed('bookings', 'A', { slotDate: DAY, slotTime: '09:00', startMinutes: 540, endMinutes: 600, status: 'pending_payment', expiresAt: new Date(Date.now() - 5 * MIN), customerName: 'A', manageToken: 'tok' });
        db.seed('bookings', 'B', { slotDate: DAY, slotTime: '09:30', startMinutes: 570, endMinutes: 630, status: 'confirmed', confirmedAt: new Date() }); // overlaps, confirmed meanwhile
        const res = await b.confirmPaidBooking(db, 'A', 'txn-A');
        check('late payment, time taken => conflict, not confirmed', res.outcome === 'conflict');
        check('conflicting booking flagged for refund', db.raw('bookings', 'A').status === 'paid_slot_conflict' && db.raw('bookings', 'A').paymobTransactionId === 'txn-A');
        check('the other booking is untouched', db.raw('bookings', 'B').status === 'confirmed');
        check('conflict result carries the booking (for the owner alert)', res.booking.id === 'A');
    }

    // other statuses can't be resurrected by a payment event
    {
        const db = createFakeDb();
        db.seed('bookings', 'C', { slotDate: DAY, slotTime: '09:00', startMinutes: 540, endMinutes: 600, status: 'cancelled' });
        check('cancelled booking not resurrected', (await b.confirmPaidBooking(db, 'C', 't')).outcome === 'already_handled' && db.raw('bookings', 'C').status === 'cancelled');
    }

    // ---- expireStalePending ----
    {
        const db = createFakeDb();
        db.seed('bookings', 'stale', { status: 'pending_payment', expiresAt: new Date(Date.now() - MIN) });
        db.seed('bookings', 'fresh', { status: 'pending_payment', expiresAt: new Date(Date.now() + 10 * MIN) });
        db.seed('bookings', 'free', { status: 'confirmed', expiresAt: null });
        db.seed('bookings', 'paid', { status: 'confirmed', expiresAt: new Date(Date.now() - MIN) }); // confirmed keeps an old expiresAt
        const n = await b.expireStalePending(db);
        check('expires exactly the one stale pending booking', n === 1 && db.raw('bookings', 'stale').status === 'expired');
        check('fresh hold untouched', db.raw('bookings', 'fresh').status === 'pending_payment');
        check('free/confirmed bookings never expired', db.raw('bookings', 'free').status === 'confirmed' && db.raw('bookings', 'paid').status === 'confirmed');
    }

    // ---- abandonPendingBooking ----
    {
        const db = createFakeDb();
        db.seed('bookings', 'p', { status: 'pending_payment' });
        db.seed('bookings', 'c', { status: 'confirmed' });
        await b.abandonPendingBooking(db, 'p');
        await b.abandonPendingBooking(db, 'c');
        await b.abandonPendingBooking(db, 'missing'); // must not throw
        check('abandon releases a pending booking', db.raw('bookings', 'p').status === 'expired');
        check('abandon never touches a confirmed booking', db.raw('bookings', 'c').status === 'confirmed');
    }

    // ---- loadBookedIntervalsByDate ----
    {
        const db = createFakeDb();
        db.seed('bookings', '1', { slotDate: '2026-09-27', slotTime: '09:00', startMinutes: 540, endMinutes: 600, status: 'confirmed' });
        db.seed('bookings', '2', { slotDate: '2026-09-27', slotTime: '10:00', status: 'confirmed' }); // legacy
        db.seed('bookings', '3', { slotDate: '2026-09-27', slotTime: '12:00', startMinutes: 720, endMinutes: 780, status: 'pending_payment', expiresAt: new Date(Date.now() + 5 * MIN) });
        db.seed('bookings', '4', { slotDate: '2026-09-27', slotTime: '13:00', startMinutes: 780, endMinutes: 840, status: 'pending_payment', expiresAt: new Date(Date.now() - 5 * MIN) }); // lapsed
        db.seed('bookings', '5', { slotDate: '2026-09-27', slotTime: '14:00', startMinutes: 840, endMinutes: 900, status: 'cancelled' });
        db.seed('bookings', '6', { slotDate: '2026-10-30', slotTime: '09:00', startMinutes: 540, endMinutes: 600, status: 'confirmed' }); // outside range
        const map = await b.loadBookedIntervalsByDate(db, '2026-09-20', '2026-10-05');
        const day = map.get('2026-09-27') || [];
        check('active intervals loaded (confirmed, legacy, live pending) and nothing else', day.length === 3);
        check('legacy booking derived to a 60min interval', day.some(i => i.startMinutes === 600 && i.endMinutes === 660));
        check('out-of-range date excluded', !map.has('2026-10-30'));
    }

    console.log('passed:', pass, 'failed:', fail);
    process.exit(fail ? 1 : 0);
})().catch(function (e) { console.error('TEST CRASHED:', e); process.exit(2); });
