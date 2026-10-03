// Run with: node test/breaks.test.js
// The break between sessions: how slots are spaced, how bookings block their
// neighbours, and that behaviour is unchanged when no break is set.

const { createFakeDb } = require('./fakeFirestore');
const a = require('../lib/availability');
const b = require('../lib/bookings');
const { getGeneralSettings } = require('../lib/settings');

let pass = 0;
let fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.log('FAIL:', name); } }

const times = function (slots) { return slots.map(function (s) { return s.time; }).join(','); };
const nine2five = [{ startTime: '09:00', endTime: '17:00' }];

// ---- slot spacing
check('no break: 45-min sessions back to back (unchanged)', times(a.slotTimesForDay(nine2five, 45)).startsWith('09:00,09:45,10:30'));
check('undefined/0 break identical', times(a.slotTimesForDay(nine2five, 45, 0)) === times(a.slotTimesForDay(nine2five, 45, undefined)));
check('45 min + 15 break => every hour', times(a.slotTimesForDay(nine2five, 45, 15)) === '09:00,10:00,11:00,12:00,13:00,14:00,15:00,16:00');
check('label shows the session only, not the break', a.slotTimesForDay(nine2five, 45, 15)[0].label === '9:00 – 9:45 AM');
check('60 min + 15 break => 09:00,10:15,11:30,12:45,14:00,15:15', times(a.slotTimesForDay(nine2five, 60, 15)) === '09:00,10:15,11:30,12:45,14:00,15:15');
check('every session still ends by closing time', a.slotTimesForDay(nine2five, 60, 15).every(function (s) { return s.startMinutes + 60 <= 17 * 60; }));
check('no break needed after the LAST session (16:00-16:45 fits a 16:45 close)', times(a.slotTimesForDay([{ startTime: '09:00', endTime: '16:45' }], 45, 15)).endsWith('16:00'));
check('grid restarts after a lunch gap', times(a.slotTimesForDay([{ startTime: '09:00', endTime: '12:00' }, { startTime: '13:00', endTime: '15:00' }], 45, 15)) === '09:00,10:00,11:00,13:00,14:00');
check('bad break values ignored (negative, decimal, text, null, NaN)', [-5, 7.5, 'abc', null, NaN].every(function (v) { return times(a.slotTimesForDay(nine2five, 60, v)) === times(a.slotTimesForDay(nine2five, 60)); }));
check('normalizeBreak', a.normalizeBreak(15) === 15 && a.normalizeBreak('15') === 15 && a.normalizeBreak(-1) === 0 && a.normalizeBreak(2.5) === 0 && a.normalizeBreak(undefined) === 0);

// ---- overlap with a break: both sides
const booked = [{ startMinutes: 600, endMinutes: 645 }]; // 10:00-10:45
check('no break: 10:45 start is fine (back to back)', !a.overlapsAny(645, 690, booked, 0));
check('15 break: 10:45 start blocked (inside the break after)', a.overlapsAny(645, 690, booked, 15));
check('15 break: 10:59 start blocked', a.overlapsAny(659, 704, booked, 15));
check('15 break: 11:00 start allowed (break fully over)', !a.overlapsAny(660, 705, booked, 15));
check('15 break: session ending 09:50 blocked (inside the break before)', a.overlapsAny(545, 590, booked, 15));
check('15 break: session ending 09:45 allowed', !a.overlapsAny(540, 585, booked, 15));
check('break applies across DIFFERENT durations (30-min call at 10:45)', a.overlapsAny(645, 675, booked, 15));

// ---- buildSlotsByDate end to end (Sundays 09:00-17:00, one 45-min booking at 10:00)
const rulesByDay = { 0: nine2five };
const start = new Date(2026, 8, 27); // Sun 27 Sep
const bookedMap = new Map([['2026-09-27', booked]]);
const build = function (duration, breakMinutes) {
    return a.buildSlotsByDate({ startDate: start, numDays: 1, durationMinutes: duration, rulesByDay, bookedByDate: bookedMap, breakMinutes })['2026-09-27'];
};
const bookedTimes = function (slots) { return slots.filter(function (s) { return s.status === 'booked'; }).map(function (s) { return s.time; }).join(','); };

check('45+15: grid is hourly', times(build(45, 15)) === '09:00,10:00,11:00,12:00,13:00,14:00,15:00,16:00');
check('45+15: only the 10:00 slot is taken; 09:00 and 11:00 leave exactly the break', bookedTimes(build(45, 15)) === '10:00');
check('45, no break: neighbours 09:45 and 10:30 are taken (unchanged behaviour)', bookedTimes(build(45)) === '09:45,10:30');
check('30-min service + 15 break keeps clear of the break on both sides', bookedTimes(build(30, 15)) === '09:45,10:30');
check('...and offers 09:00 and 11:15', build(30, 15).filter(function (s) { return s.status === 'available' && (s.time === '09:00' || s.time === '11:15'); }).length === 2);

// ---- server-side slot validation uses the break
check('valid: 11:00 with 45+15', a.isValidSlotForDate('2026-09-27', '11:00', 45, rulesByDay, undefined, 15));
check('invalid: 10:45 is off the 45+15 grid', !a.isValidSlotForDate('2026-09-27', '10:45', 45, rulesByDay, undefined, 15));
check('10:30 is valid with no break (unchanged 45-min grid) but off-grid with 45+15', a.isValidSlotForDate('2026-09-27', '10:30', 45, rulesByDay) && !a.isValidSlotForDate('2026-09-27', '10:30', 45, rulesByDay, undefined, 15));

(async function run() {
    const MIN = 60 * 1000;
    const svc = { id: 's', name: 'Session' };
    const params = function (over) {
        return Object.assign({ dateKey: '2026-09-27', time: '10:00', startMinutes: 600, durationMinutes: 45, service: svc,
            customerName: 'A', customerEmail: 'a@b.co', customerMobile: '+966500000000', isPaid: false, amount: 0, currency: 'SAR', breakMinutes: 15 }, over);
    };
    const rejects = async function (p) { try { await p; return false; } catch (e) { return e.message === 'SLOT_TAKEN'; } };

    // ---- reserveSlot enforces it inside the transaction
    {
        const db = createFakeDb();
        const first = await b.reserveSlot(db, params({}));
        check('booking stores the break it was made under', db.raw('bookings', first.id).breakMinutes === 15);
        check('10:45 start rejected (inside break)', await rejects(b.reserveSlot(db, params({ time: '10:45', startMinutes: 645 }))));
        check('09:15 start rejected (would end inside the break before)', await rejects(b.reserveSlot(db, params({ time: '09:15', startMinutes: 555 }))));
        check('11:00 start accepted', !(await rejects(b.reserveSlot(db, params({ time: '11:00', startMinutes: 660 })))));
    }
    {
        const db = createFakeDb();
        await b.reserveSlot(db, params({ breakMinutes: 0 }));
        check('break 0: back-to-back 10:45 still accepted (unchanged)', !(await rejects(b.reserveSlot(db, params({ time: '10:45', startMinutes: 645, breakMinutes: 0 })))));
        const legacy = await b.reserveSlot(db, params({ time: '15:00', startMinutes: 900, breakMinutes: undefined }));
        check('missing break is stored as 0', db.raw('bookings', legacy.id).breakMinutes === 0);
    }

    // ---- a late payment re-checks with the booking's own break
    {
        const db = createFakeDb();
        db.seed('bookings', 'A', { slotDate: '2026-09-27', slotTime: '10:00', startMinutes: 600, endMinutes: 645, breakMinutes: 15, status: 'pending_payment', expiresAt: new Date(Date.now() - 5 * MIN), customerName: 'A', manageToken: 't' });
        db.seed('bookings', 'B', { slotDate: '2026-09-27', slotTime: '10:45', startMinutes: 645, endMinutes: 690, status: 'confirmed', confirmedAt: new Date() });
        const res = await b.confirmPaidBooking(db, 'A', 'txn');
        check('late payment next door to another booking, inside the break => conflict', res.outcome === 'conflict');
    }
    {
        const db = createFakeDb();
        db.seed('bookings', 'A', { slotDate: '2026-09-27', slotTime: '10:00', startMinutes: 600, endMinutes: 645, status: 'pending_payment', expiresAt: new Date(Date.now() + 5 * MIN), customerName: 'A', manageToken: 't' });
        db.seed('bookings', 'B', { slotDate: '2026-09-27', slotTime: '10:45', startMinutes: 645, endMinutes: 690, status: 'confirmed', confirmedAt: new Date() });
        check('older booking with no stored break still confirms back-to-back (unchanged)', (await b.confirmPaidBooking(db, 'A', 'txn')).outcome === 'confirmed');
    }

    // ---- reading the setting
    {
        const db = createFakeDb();
        check('missing settings => break 0', (await getGeneralSettings(db)).breakMinutes === 0);
        db.seed('settings', 'general', { meetLink: 'https://meet.google.com/x', breakMinutes: 15 });
        const s = await getGeneralSettings(db);
        check('reads break and keeps the Meet link', s.breakMinutes === 15 && s.meetLink === 'https://meet.google.com/x');
        db.seed('settings', 'general', { meetLink: '', breakMinutes: -30 });
        check('garbage break => 0', (await getGeneralSettings(db)).breakMinutes === 0);
        db.seed('settings', 'general', { meetLink: '' });
        check('settings without the field => 0 (existing installs)', (await getGeneralSettings(db)).breakMinutes === 0);
    }

    console.log('passed: ' + pass + ' failed: ' + fail);
    process.exit(fail === 0 ? 0 : 1);
})().catch(function (e) { console.error(e); process.exit(1); });
