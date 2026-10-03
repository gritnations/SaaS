// Run with: node test/manage.test.js
// Cancel + reschedule (lib/manage.js), the calendar delete/move calls, the
// Paymob refund call, the new emails and the admin check. No network: the
// gateway, calendar and email are all stubs.

const { createFakeDb } = require('./fakeFirestore');
const email = require('../lib/email');
const meeting = require('../lib/meeting');
const manage = require('../lib/manage');
const { isAdminToken, bearerToken } = require('../lib/adminAuth');
const PaymobGateway = require('../lib/gateways/paymob');

let pass = 0;
let fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.log('FAIL:', name); } }

const NOW = new Date('2026-09-23T12:00:00Z');      // Wed 23 Sep 2026, 15:00 Saudi
const SESSION = '2026-10-11';                      // a Sunday: 10:00 Saudi = 07:00Z

const sent = [];
email.setEmailCapture(async function (m) { sent.push(m); });

function seedRules(db, breakMinutes, blocked) {
    for (let d = 0; d <= 4; d++) db.seed('availabilityRules', 'r' + d, { dayOfWeek: d, startTime: '09:00', endTime: '17:00' });
    db.seed('settings', 'general', { meetLink: '', breakMinutes: breakMinutes || 0 });
    (blocked || []).forEach(function (k) { db.seed('blockedDates', k, { reason: 'x' }); });
}

function booking(over) {
    return Object.assign({
        status: 'confirmed', slotDate: SESSION, slotTime: '10:00', startMinutes: 600, endMinutes: 660,
        serviceName: 'Business Review', customerName: 'Pat Payer', customerEmail: 'pat@example.com', customerMobile: '+966500000000',
        isPaid: true, amount: 50000, currency: 'SAR', manageToken: 'a'.repeat(48), meetLink: 'https://meet.google.com/abc-defg-hij',
        calendarEventId: 'evt1', paymobTransactionId: '987654'
    }, over);
}

function stubs() {
    const s = { refunds: [], calls: [], refundShouldFail: false, calendarShouldFail: null };
    s.gateway = { refund: async function (r) { s.refunds.push(r); if (s.refundShouldFail) throw new Error('Paymob refund failed (500): boom'); return { refundId: 'rf-1' }; } };
    s.calendar = async function (url, body, method) {
        s.calls.push({ url: url, body: body, method: method });
        if (s.calendarShouldFail) { const e = new Error('cal'); e.code = s.calendarShouldFail; throw e; }
        return '';
    };
    return s;
}

function opts(s, over) {
    return Object.assign({ by: 'customer', now: NOW, refundGateway: s.gateway, calendarKey: 'placeholder', resendApiKey: 'placeholder', calendarRequest: s.calendar }, over);
}

const to = function (who) { return sent.filter(function (m) { return m.to === who; }); };

(async function run() {
    // ---------------------------------------------------------- helpers
    check('sessionStart: 10:00 Saudi = 07:00 UTC', manage.sessionStart(booking()).toISOString() === '2026-10-11T07:00:00.000Z');
    check('not started an hour before', !manage.hasSessionStarted(booking(), new Date('2026-10-11T06:00:00Z')));
    check('started at the start time', manage.hasSessionStarted(booking(), new Date('2026-10-11T07:00:00Z')));
    check('unreadable time counts as started (safe)', manage.hasSessionStarted({ slotDate: 'nope', slotTime: '??' }, NOW));
    check('changeable: confirmed + future', manage.isChangeable(booking(), NOW));
    check('not changeable: cancelled / pending / expired', ['cancelled', 'pending_payment', 'expired', 'no_show', 'paid_slot_conflict'].every(function (st) { return !manage.isChangeable(booking({ status: st }), NOW); }));
    const tok = 'a'.repeat(48);
    check('token: correct', manage.tokenMatches(booking(), tok));
    check('token: wrong / short / empty / non-string / missing', !manage.tokenMatches(booking(), 'b'.repeat(48)) && !manage.tokenMatches(booking(), 'a') && !manage.tokenMatches(booking(), '')
        && !manage.tokenMatches(booking(), undefined) && !manage.tokenMatches(booking(), 12345) && !manage.tokenMatches({ status: 'confirmed' }, tok) && !manage.tokenMatches(booking({ manageToken: '' }), ''));
    const view = manage.toManageView(booking(), NOW);
    check('view: flags and refund preview', view.canCancel && view.canReschedule && view.refundAmount === 50000 && view.durationMinutes === 60 && view.meetLink.indexOf('meet.google.com') > 0);
    check('view leaks no personal data or secrets', ['Pat', 'pat@example.com', '+966', 'aaaaaaaa', 'manageToken', '987654', 'evt1'].every(function (x) { return JSON.stringify(view).indexOf(x) < 0; }));
    const pastView = manage.toManageView(booking(), new Date('2026-10-12T00:00:00Z'));
    check('view: session already happened => cannot change, no Meet link', !pastView.canCancel && !pastView.canReschedule && pastView.sessionHasPassed && pastView.meetLink === '');
    check('view of a cancelled booking shows what was refunded', manage.toManageView(booking({ status: 'cancelled', refundAmount: 50000, refundStatus: 'refunded' }), NOW).refundStatus === 'refunded');

    // ---------------------------------------------------------- cancel: paid, refund works
    {
        const db = createFakeDb(); db.seed('bookings', 'B1', booking()); sent.length = 0; const s = stubs();
        const r = await manage.cancelBooking(db, 'B1', opts(s));
        const doc = db.raw('bookings', 'B1');
        check('paid cancel: outcome + refund reported', r.outcome === 'cancelled' && r.refundAmount === 50000 && r.refundStatus === 'refunded');
        check('paid cancel: booking marked cancelled with who/when', doc.status === 'cancelled' && doc.cancelledBy === 'customer' && doc.cancelledAt instanceof Date);
        check('paid cancel: refund asked for the right transaction and amount', s.refunds.length === 1 && s.refunds[0].providerTransactionId === '987654' && s.refunds[0].amount === 50000 && s.refunds[0].currency === 'SAR');
        check('paid cancel: refund result stored', doc.refundStatus === 'refunded' && doc.refundId === 'rf-1' && doc.refundAmount === 50000);
        check('paid cancel: calendar event deleted', s.calls.length === 1 && s.calls[0].method === 'DELETE' && s.calls[0].url.indexOf('/events/evt1') > 0 && s.calls[0].url.indexOf('sendUpdates=none') > 0);
        check('paid cancel: customer + owner emailed', to('pat@example.com').length === 1 && to('contact@example.com').length === 1);
        check('customer email says the refund was issued', /refund of SAR 500\.00 has been issued/.test(to('pat@example.com')[0].html));
        check('owner email says refund went automatically', /sent automatically/.test(to('contact@example.com')[0].html) && !/ACTION NEEDED/.test(to('contact@example.com')[0].subject));
    }

    // ---------------------------------------------------------- cancel: refund fails => still cancelled, flagged
    {
        const db = createFakeDb(); db.seed('bookings', 'B2', booking()); sent.length = 0; const s = stubs(); s.refundShouldFail = true;
        const r = await manage.cancelBooking(db, 'B2', opts(s));
        const doc = db.raw('bookings', 'B2');
        check('refund failure: booking is STILL cancelled', r.outcome === 'cancelled' && doc.status === 'cancelled');
        check('refund failure: flagged for a manual refund with the reason', r.refundStatus === 'needs_manual' && doc.refundStatus === 'needs_manual' && /boom/.test(doc.refundError));
        check('refund failure: customer told it is being processed (not "issued")', /processing your refund/.test(to('pat@example.com')[0].html) && !/has been issued/.test(to('pat@example.com')[0].html));
        check('refund failure: owner email is an ACTION NEEDED with the amount', /ACTION NEEDED/.test(to('contact@example.com')[0].subject) && /refund SAR 500\.00/.test(to('contact@example.com')[0].html));
    }
    {
        const db = createFakeDb(); db.seed('bookings', 'B3', booking({ paymobTransactionId: null })); sent.length = 0; const s = stubs();
        const r = await manage.cancelBooking(db, 'B3', opts(s));
        check('no transaction id on file => manual refund, gateway not called', r.refundStatus === 'needs_manual' && s.refunds.length === 0 && db.raw('bookings', 'B3').status === 'cancelled');
    }
    {
        const db = createFakeDb(); db.seed('bookings', 'B4', booking()); sent.length = 0; const s = stubs();
        const r = await manage.cancelBooking(db, 'B4', opts(s, { refundGateway: null }));
        check('no gateway available => manual refund', r.refundStatus === 'needs_manual');
    }

    // ---------------------------------------------------------- cancel: free
    {
        const db = createFakeDb(); db.seed('bookings', 'F1', booking({ isPaid: false, amount: 0, paymobTransactionId: null })); sent.length = 0; const s = stubs();
        const r = await manage.cancelBooking(db, 'F1', opts(s));
        check('free cancel: nothing to refund, gateway untouched', r.outcome === 'cancelled' && r.refundAmount === 0 && r.refundStatus === 'none' && s.refunds.length === 0 && db.raw('bookings', 'F1').refundStatus === 'none');
        check('free cancel: no refund wording in the customer email', !/refund/i.test(to('pat@example.com')[0].html));
        check('free cancel: owner told there is nothing to refund', /No payment to refund/.test(to('contact@example.com')[0].html));
    }

    // ---------------------------------------------------------- cancel: calendar
    {
        const db = createFakeDb(); db.seed('bookings', 'C1', booking({ calendarEventId: undefined })); sent.length = 0; const s = stubs();
        await manage.cancelBooking(db, 'C1', opts(s));
        check('no calendar event on the booking => no calendar call, no alert', s.calls.length === 0 && !sent.some(function (m) { return /Calendar/.test(m.subject); }));
    }
    {
        const db = createFakeDb(); db.seed('bookings', 'C2', booking()); sent.length = 0; const s = stubs(); s.calendarShouldFail = 500;
        const r = await manage.cancelBooking(db, 'C2', opts(s));
        check('calendar delete failing does not undo the cancel or the refund', r.outcome === 'cancelled' && r.refundStatus === 'refunded' && db.raw('bookings', 'C2').status === 'cancelled');
        check('...but the owner is told to remove the event', sent.some(function (m) { return m.subject === 'Calendar event could not be removed'; }));
    }
    {
        const db = createFakeDb(); db.seed('bookings', 'C3', booking()); sent.length = 0; const s = stubs(); s.calendarShouldFail = 404;
        await manage.cancelBooking(db, 'C3', opts(s));
        check('event already gone (404) counts as removed, no alert', !sent.some(function (m) { return /Calendar/.test(m.subject); }));
    }

    // ---------------------------------------------------------- cancel: refusals
    {
        const db = createFakeDb(); db.seed('bookings', 'X1', booking()); const s = stubs();
        await manage.cancelBooking(db, 'X1', opts(s)); sent.length = 0; s.refunds.length = 0; s.calls.length = 0;
        const again = await manage.cancelBooking(db, 'X1', opts(s));
        check('cancelling twice: second is already_cancelled and reports the first refund', again.outcome === 'already_cancelled' && again.refundAmount === 50000 && again.refundStatus === 'refunded');
        check('cancelling twice: no second refund, calendar call or email', s.refunds.length === 0 && s.calls.length === 0 && sent.length === 0);
    }
    {
        const db = createFakeDb(); const s = stubs();
        db.seed('bookings', 'P1', booking({ status: 'pending_payment' })); db.seed('bookings', 'P2', booking({ status: 'expired' })); db.seed('bookings', 'P3', booking({ status: 'paid_slot_conflict' })); db.seed('bookings', 'P4', booking({ status: 'no_show' }));
        const outs = [];
        for (const id of ['P1', 'P2', 'P3', 'P4']) outs.push((await manage.cancelBooking(db, id, opts(s))).outcome);
        check('only a confirmed booking can be cancelled', outs.every(function (o) { return o === 'not_cancellable'; }) && s.refunds.length === 0);
        check('...and their status is untouched', db.raw('bookings', 'P1').status === 'pending_payment' && db.raw('bookings', 'P3').status === 'paid_slot_conflict');
    }
    {
        const db = createFakeDb(); db.seed('bookings', 'T1', booking()); const s = stubs();
        const late = await manage.cancelBooking(db, 'T1', opts(s, { now: new Date('2026-10-11T07:30:00Z') }));
        check('a session that has begun cannot be cancelled', late.outcome === 'too_late' && db.raw('bookings', 'T1').status === 'confirmed' && s.refunds.length === 0);
        check('unknown booking => not_found', (await manage.cancelBooking(db, 'nope', opts(s))).outcome === 'not_found');
    }
    {
        const db = createFakeDb(); db.seed('bookings', 'A1', booking()); sent.length = 0; const s = stubs();
        await manage.cancelBooking(db, 'A1', opts(s, { by: 'admin' }));
        check('admin cancel is recorded as admin and the owner email says so', db.raw('bookings', 'A1').cancelledBy === 'admin' && /by you \(admin panel\)/.test(to('contact@example.com')[0].html));
    }

    // ---------------------------------------------------------- reschedule
    const NEW_DAY = '2026-10-12';   // Monday, 60-min slots on the hour
    {
        const db = createFakeDb(); seedRules(db); db.seed('bookings', 'R1', booking()); sent.length = 0; const s = stubs();
        const r = await manage.rescheduleBooking(db, 'R1', opts(s, { dateKey: NEW_DAY, time: '14:00' }));
        const doc = db.raw('bookings', 'R1');
        check('reschedule: succeeds', r.outcome === 'rescheduled');
        check('reschedule: booking now at the new time, same length', doc.slotDate === NEW_DAY && doc.slotTime === '14:00' && doc.startMinutes === 840 && doc.endMinutes === 900);
        check('reschedule: still confirmed, still paid, nothing else touched', doc.status === 'confirmed' && doc.amount === 50000 && doc.manageToken === tok && doc.meetLink === 'https://meet.google.com/abc-defg-hij');
        check('reschedule: history kept', doc.rescheduleCount === 1 && doc.previousSlots.length === 1 && doc.previousSlots[0].slotDate === SESSION && doc.previousSlots[0].startMinutes === 600);
        check('reschedule: calendar event moved (PATCH, new times, same event)', s.calls.length === 1 && s.calls[0].method === 'PATCH' && s.calls[0].url.indexOf('/events/evt1') > 0
            && s.calls[0].body.start.dateTime === '2026-10-12T14:00:00+03:00' && s.calls[0].body.end.dateTime === '2026-10-12T15:00:00+03:00' && Object.keys(s.calls[0].body).sort().join() === 'end,start');
        check('reschedule: customer email shows new AND old time, keeps the Meet link', to('pat@example.com').length === 1
            && /Mon, 12 Oct/.test(to('pat@example.com')[0].html) && /Previously: Sun, 11 Oct/.test(to('pat@example.com')[0].html) && /meet\.google\.com\/abc-defg-hij/.test(to('pat@example.com')[0].html));
        check('reschedule: owner emailed too', to('contact@example.com').length === 1 && /was Sun, 11 Oct/.test(to('contact@example.com')[0].html));

        const again = await manage.rescheduleBooking(db, 'R1', opts(s, { dateKey: '2026-10-13', time: '09:00' }));
        check('can be rescheduled again; count and history grow', again.outcome === 'rescheduled' && db.raw('bookings', 'R1').rescheduleCount === 2 && db.raw('bookings', 'R1').previousSlots.length === 2);
    }
    {
        const db = createFakeDb(); seedRules(db, 0, ['2026-10-14']); db.seed('bookings', 'R2', booking({ startMinutes: 600, endMinutes: 690, slotTime: '10:00' })); const s = stubs();   // a 90-minute booking
        const r = await manage.rescheduleBooking(db, 'R2', opts(s, { dateKey: NEW_DAY, time: '13:30' }));
        check('the booked length (90 min) is preserved on the new time', r.outcome === 'rescheduled' && db.raw('bookings', 'R2').endMinutes - db.raw('bookings', 'R2').startMinutes === 90);
    }
    {
        const db = createFakeDb(); seedRules(db, 0, ['2026-10-14']); db.seed('bookings', 'R3', booking()); sent.length = 0; const s = stubs();
        const out = async function (dateKey, time) { return (await manage.rescheduleBooking(db, 'R3', opts(s, { dateKey: dateKey, time: time }))).outcome; };
        check('same time => same_slot', await out(SESSION, '10:00') === 'same_slot');
        check('off-grid time => invalid_slot', await out(NEW_DAY, '10:30') === 'invalid_slot');
        check('outside working hours => invalid_slot', await out(NEW_DAY, '17:00') === 'invalid_slot' && await out(NEW_DAY, '08:00') === 'invalid_slot');
        check('non-working day (Friday) => invalid_slot', await out('2026-10-16', '10:00') === 'invalid_slot');
        check('blocked date => invalid_slot', await out('2026-10-14', '10:00') === 'invalid_slot');
        check('today / past => invalid_slot', await out('2026-09-23', '10:00') === 'invalid_slot' && await out('2026-09-01', '10:00') === 'invalid_slot');
        check('beyond the booking window => invalid_slot', await out('2026-12-25', '10:00') === 'invalid_slot');
        check('garbage input => invalid_slot', await out('nope', '10:00') === 'invalid_slot' && await out(NEW_DAY, 'zz') === 'invalid_slot' && await out(undefined, undefined) === 'invalid_slot');
        check('all refusals left the booking exactly as it was, with no calendar call or email', db.raw('bookings', 'R3').slotDate === SESSION && db.raw('bookings', 'R3').startMinutes === 600 && s.calls.length === 0 && sent.length === 0);
    }
    {
        const db = createFakeDb(); seedRules(db); db.seed('bookings', 'R4', booking()); db.seed('bookings', 'OTHER', booking({ slotDate: NEW_DAY, slotTime: '14:00', startMinutes: 840, endMinutes: 900, customerName: 'Other', manageToken: 'c'.repeat(48) })); const s = stubs();
        const r = await manage.rescheduleBooking(db, 'R4', opts(s, { dateKey: NEW_DAY, time: '14:00' }));
        check('moving onto someone else\'s time => slot_taken, nothing changed', r.outcome === 'slot_taken' && db.raw('bookings', 'R4').slotDate === SESSION && s.calls.length === 0);
        db.seed('bookings', 'PEND', booking({ status: 'pending_payment', expiresAt: new Date(NOW.getTime() + 600000), slotDate: NEW_DAY, slotTime: '15:00', startMinutes: 900, endMinutes: 960 }));
        check('a live payment hold also blocks it', (await manage.rescheduleBooking(db, 'R4', opts(s, { dateKey: NEW_DAY, time: '15:00' }))).outcome === 'slot_taken');
        db.seed('bookings', 'LAPSED', booking({ status: 'pending_payment', expiresAt: new Date(NOW.getTime() - 600000), slotDate: NEW_DAY, slotTime: '16:00', startMinutes: 960, endMinutes: 1020 }));
        check('a lapsed hold does not', (await manage.rescheduleBooking(db, 'R4', opts(s, { dateKey: NEW_DAY, time: '16:00' }))).outcome === 'rescheduled');
    }
    {
        // the break between sessions applies to a reschedule
        const db = createFakeDb(); seedRules(db, 15);
        db.seed('bookings', 'M1', booking({ slotDate: SESSION, slotTime: '10:00', startMinutes: 600, endMinutes: 645 }));   // 45-min session; break 15 => hourly grid
        // someone else's session that ends at 10:50, so 11:00 is only 10 minutes later: inside the 15-minute break
        db.seed('bookings', 'M2', booking({ slotDate: NEW_DAY, slotTime: '10:05', startMinutes: 605, endMinutes: 650, customerName: 'Other', manageToken: 'd'.repeat(48) }));
        const s = stubs();
        check('a start inside the break after another booking => slot_taken', (await manage.rescheduleBooking(db, 'M1', opts(s, { dateKey: NEW_DAY, time: '11:00' }))).outcome === 'slot_taken');
        // someone else's session starting at 14:50, so a 14:00-14:45 session would end only 5 minutes before it
        db.seed('bookings', 'M3', booking({ slotDate: NEW_DAY, slotTime: '14:50', startMinutes: 890, endMinutes: 935, customerName: 'Third', manageToken: 'e'.repeat(48) }));
        check('a start whose session would end inside the break BEFORE another booking => slot_taken', (await manage.rescheduleBooking(db, 'M1', opts(s, { dateKey: NEW_DAY, time: '14:00' }))).outcome === 'slot_taken');
        check('...but 09:00 (ends 09:45, well before the 10:05 booking + break) is allowed', (await manage.rescheduleBooking(db, 'M1', opts(s, { dateKey: NEW_DAY, time: '09:00' }))).outcome === 'rescheduled');
        check('a start clear of the break is allowed (12:00)', (await manage.rescheduleBooking(db, 'M1', opts(s, { dateKey: NEW_DAY, time: '12:00' }))).outcome === 'rescheduled');
    }
    {
        // A booking's own current time never blocks moving it. This only shows when the old
        // booking sits OFF today's grid (made under an earlier break setting): 10:30-11:15 with
        // a 15-minute break would otherwise block 11:00, the very next slot.
        const db = createFakeDb(); seedRules(db, 15);
        db.seed('bookings', 'S1', booking({ slotDate: NEW_DAY, slotTime: '10:30', startMinutes: 630, endMinutes: 675 }));
        const s = stubs();
        const r = await manage.rescheduleBooking(db, 'S1', opts(s, { dateKey: NEW_DAY, time: '11:00' }));
        check('moving to the next slot works: the booking\'s own old time is ignored', r.outcome === 'rescheduled' && db.raw('bookings', 'S1').startMinutes === 660);
    }
    {
        const db = createFakeDb(); seedRules(db); const s = stubs();
        db.seed('bookings', 'N1', booking({ status: 'cancelled' })); db.seed('bookings', 'N2', booking({ status: 'pending_payment' })); db.seed('bookings', 'N3', booking());
        check('cancelled / pending bookings cannot be rescheduled', (await manage.rescheduleBooking(db, 'N1', opts(s, { dateKey: NEW_DAY, time: '14:00' }))).outcome === 'not_changeable' && (await manage.rescheduleBooking(db, 'N2', opts(s, { dateKey: NEW_DAY, time: '14:00' }))).outcome === 'not_changeable');
        check('a session that has begun cannot be rescheduled', (await manage.rescheduleBooking(db, 'N3', opts(s, { dateKey: NEW_DAY, time: '14:00', now: new Date('2026-10-11T07:05:00Z') }))).outcome === 'not_changeable');
        check('unknown booking => not_found', (await manage.rescheduleBooking(db, 'nope', opts(s, { dateKey: NEW_DAY, time: '14:00' }))).outcome === 'not_found');
    }
    {
        const db = createFakeDb(); seedRules(db); db.seed('bookings', 'K1', booking()); sent.length = 0; const s = stubs(); s.calendarShouldFail = 500;
        const r = await manage.rescheduleBooking(db, 'K1', opts(s, { dateKey: NEW_DAY, time: '14:00' }));
        check('calendar failing does not undo the reschedule; owner is told to fix the event', r.outcome === 'rescheduled' && db.raw('bookings', 'K1').slotDate === NEW_DAY && sent.some(function (m) { return m.subject === 'Calendar event could not be moved'; }));
    }

    // ---------------------------------------------------------- calendar helpers
    {
        const seen = [];
        const req = async function (url, body, method) { seen.push({ url: url, body: body, method: method }); return ''; };
        check('deleteMeeting: sends DELETE', (await meeting.deleteMeeting('{"k":1}', 'ev/1', req)) === true && seen[0].method === 'DELETE' && seen[0].url.endsWith('/events/ev%2F1?sendUpdates=none'));
        check('deleteMeeting: nothing to delete counts as done', (await meeting.deleteMeeting('{"k":1}', undefined, req)) === true && seen.length === 1);
        check('deleteMeeting: 404 and 410 count as gone', (await meeting.deleteMeeting('k', 'e', async function () { const e = new Error('x'); e.code = 404; throw e; })) === true && (await meeting.deleteMeeting('k', 'e', async function () { const e = new Error('x'); e.response = { status: 410 }; throw e; })) === true);
        check('deleteMeeting: other errors => false, never throws', (await meeting.deleteMeeting('k', 'e', async function () { throw new Error('boom'); })) === false);
        check('deleteMeeting: no key and no request => false', (await meeting.deleteMeeting('placeholder', 'e')) === false);
        check('moveMeeting: PATCH with only start and end', (await meeting.moveMeeting('{"k":1}', booking({ id: 'b' }), 'evt9', req)) === true && seen[1].method === 'PATCH' && Object.keys(seen[1].body).sort().join() === 'end,start');
        check('moveMeeting: no event id / failure / no key => false', (await meeting.moveMeeting('{"k":1}', booking(), undefined, req)) === false
            && (await meeting.moveMeeting('k', booking(), 'e', async function () { throw new Error('x'); })) === false && (await meeting.moveMeeting('placeholder', booking(), 'e')) === false);
    }

    // ---------------------------------------------------------- Paymob refund call
    {
        let call = null;
        global.fetch = async function (url, init) { call = { url: url, init: init, body: JSON.parse(init.body) }; return { ok: true, status: 200, json: async function () { return { id: 4242 }; }, text: async function () { return ''; } }; };
        const g = new PaymobGateway({ secretKey: 'sk_test', baseUrl: 'https://ksa.paymob.com' });
        const out = await g.refund({ providerTransactionId: '987654', amount: 50000 });
        check('paymob refund: right endpoint, auth, and body', call.url === 'https://ksa.paymob.com/api/acceptance/void_refund/refund' && call.init.headers.Authorization === 'Token sk_test'
            && call.body.transaction_id === '987654' && call.body.amount_cents === 50000);
        check('paymob refund: returns the refund id', out.refundId === '4242');
        global.fetch = async function () { return { ok: false, status: 400, text: async function () { return 'already refunded'; } }; };
        let msg = ''; try { await g.refund({ providerTransactionId: '1', amount: 100 }); } catch (e) { msg = e.message; }
        check('paymob refund: a rejected refund throws with the reason', /400/.test(msg) && /already refunded/.test(msg));
        let m2 = ''; try { await new PaymobGateway({}).refund({ providerTransactionId: '1', amount: 100 }); } catch (e) { m2 = e.message; }
        check('paymob refund: needs the secret key', /secretKey/.test(m2));
        let m3 = ''; try { await g.refund({ providerTransactionId: '', amount: 100 }); } catch (e) { m3 = e.message; }
        let m4 = ''; try { await g.refund({ providerTransactionId: '1', amount: 0 }); } catch (e) { m4 = e.message; }
        check('paymob refund: refuses a missing transaction id or zero amount', /transaction id/.test(m3) && /positive amount/.test(m4));
    }

    // ---------------------------------------------------------- emails escape input
    {
        const evil = { id: 'x', customerName: '<script>alert(1)</script>', serviceName: '<b>S</b>', slotDateLabel: 'D', slotTimeLabel: 'T', currency: 'SAR', bookUrl: 'javascript:alert(1)', meetLink: 'javascript:alert(2)', manageUrl: 'javascript:alert(3)' };
        const html = email.bookingCancelledHtml(evil, { refundAmount: 100, refundStatus: 'refunded' }) + email.bookingRescheduledHtml(evil, { slotDateLabel: '<i>o</i>', slotTimeLabel: 'T' });
        check('cancel/reschedule emails escape names and reject javascript: links', html.indexOf('<script>') < 0 && html.indexOf('<b>S') < 0 && html.indexOf('<i>o') < 0 && html.indexOf('javascript:') < 0);
    }

    // ---------------------------------------------------------- admin check
    check('admin: verified + listed', isAdminToken({ email: 'owner@example.com', email_verified: true }, ['owner@example.com']));
    check('admin: case-insensitive', isAdminToken({ email: 'Owner@Example.com', email_verified: true }, ['owner@example.com']));
    check('admin: unverified email refused', !isAdminToken({ email: 'owner@example.com', email_verified: false }, ['owner@example.com']) && !isAdminToken({ email: 'owner@example.com' }, ['owner@example.com']));
    check('admin: not on the list refused', !isAdminToken({ email: 'stranger@example.com', email_verified: true }, ['owner@example.com']));
    check('admin: missing / odd tokens refused', !isAdminToken(null, ['a']) && !isAdminToken({}, ['a']) && !isAdminToken({ email: 5, email_verified: true }, ['5']) && !isAdminToken({ email: 'a@b.co', email_verified: 'true' }, ['a@b.co']) && !isAdminToken({ email: 'a@b.co', email_verified: true }, []));
    check('bearerToken parses the header', bearerToken('Bearer abc.def') === 'abc.def' && bearerToken('bearer abc') === 'abc' && bearerToken('Basic abc') === '' && bearerToken('') === '' && bearerToken(undefined) === '' && bearerToken('Bearer a b') === '');

    console.log('passed: ' + pass + ' failed: ' + fail);
    process.exit(fail === 0 ? 0 : 1);
})().catch(function (e) { console.error(e); process.exit(1); });
