// Run with: node test/lib.test.js   (from the admin folder)
// Tests the admin panel's pure logic: formatting, validation, weekly-hours
// conversion and the overview numbers.

import * as fmt from '../js/lib/format.js';
import * as v from '../js/lib/validate.js';
import * as sched from '../js/lib/schedule.js';
import * as bm from '../js/lib/bookingsModel.js';

let pass = 0;
let fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.log('FAIL:', name); } }

// ---------------- format ----------------
check('dateKeyFromDate pads', fmt.dateKeyFromDate(new Date(2026, 0, 5)) === '2026-01-05');
check('dateFromKey round-trips', fmt.dateKeyFromDate(fmt.dateFromKey('2026-12-31')) === '2026-12-31');
check('date label', fmt.formatDateLabel('2026-09-27') === 'Sun, 27 Sep 2026');
check('month label', fmt.monthLabel(2026, 8) === 'September 2026');
check('month range', JSON.stringify(fmt.monthRange(2026, 8)) === JSON.stringify({ start: '2026-09-01', end: '2026-09-31' }));
check('shiftMonth forward across year', JSON.stringify(fmt.shiftMonth(2026, 11, 1)) === JSON.stringify({ year: 2027, month: 0 }));
check('shiftMonth back across year', JSON.stringify(fmt.shiftMonth(2026, 0, -1)) === JSON.stringify({ year: 2025, month: 11 }));
check('time range same suffix', fmt.formatTimeRange(540, 60) === '9:00 – 10:00 AM');
check('time range across noon', fmt.formatTimeRange(690, 60) === '11:30 AM – 12:30 PM');
check('time range 45 min', fmt.formatTimeRange(570, 45) === '9:30 – 10:15 AM');
check('duration <60', fmt.formatDuration(45) === '45 min');
check('duration exactly 1h', fmt.formatDuration(60) === '1 hr');
check('duration 1h30', fmt.formatDuration(90) === '1 hr 30 min');
check('money whole', fmt.formatMoney(50000, 'SAR') === 'SAR 500');
check('money with halalas', fmt.formatMoney(50050, 'SAR') === 'SAR 500.50');
check('price free', fmt.formatPrice(0, 'SAR') === 'Free');
check('price paid', fmt.formatPrice(12000, 'SAR') === 'SAR 120');

// ---------------- price parsing ----------------
check('price 500', v.parsePriceToMinorUnits('500') === 50000);
check('price 500.5', v.parsePriceToMinorUnits('500.5') === 50050);
check('price 149.50', v.parsePriceToMinorUnits('149.50') === 14950);
check('price 19.99 has no float error', v.parsePriceToMinorUnits('19.99') === 1999);
check('price 0', v.parsePriceToMinorUnits('0') === 0);
check('price with spaces', v.parsePriceToMinorUnits('  12  ') === 1200);
check('price rejects negative', v.parsePriceToMinorUnits('-5') === null);
check('price rejects 3 decimals', v.parsePriceToMinorUnits('1.234') === null);
check('price rejects text', v.parsePriceToMinorUnits('abc') === null);
check('price rejects empty', v.parsePriceToMinorUnits('') === null);
check('price rejects comma decimal', v.parsePriceToMinorUnits('1,5') === null);
check('price rejects exponent', v.parsePriceToMinorUnits('1e3') === null);

// ---------------- service validation ----------------
const good = { name: ' Business Review ', description: ' d ', durationMinutes: '60', price: '500', sortOrder: '2', active: true };
const ok = v.validateService(good);
check('valid service ok', ok.ok && ok.value.name === 'Business Review' && ok.value.description === 'd'
    && ok.value.durationMinutes === 60 && ok.value.amount === 50000 && ok.value.sortOrder === 2 && ok.value.active === true);
check('stored value has exactly the fields the rules allow',
    JSON.stringify(Object.keys(ok.value).sort()) === JSON.stringify(['active', 'amount', 'description', 'durationMinutes', 'name', 'sortOrder']));
check('integers are integers (rules require int)', Number.isInteger(ok.value.amount) && Number.isInteger(ok.value.durationMinutes) && Number.isInteger(ok.value.sortOrder));
check('free service', v.validateService(Object.assign({}, good, { price: '0' })).value.amount === 0);
check('blank sort order defaults to 0', v.validateService(Object.assign({}, good, { sortOrder: '' })).value.sortOrder === 0);
check('inactive service', v.validateService(Object.assign({}, good, { active: false })).value.active === false);
check('rejects empty name', !v.validateService(Object.assign({}, good, { name: '  ' })).ok);
check('rejects long name', !v.validateService(Object.assign({}, good, { name: 'x'.repeat(121) })).ok);
check('rejects long description', !v.validateService(Object.assign({}, good, { description: 'x'.repeat(501) })).ok);
check('rejects fractional duration', !v.validateService(Object.assign({}, good, { durationMinutes: '30.5' })).ok);
check('rejects too-short duration', !v.validateService(Object.assign({}, good, { durationMinutes: '4' })).ok);
check('rejects too-long duration', !v.validateService(Object.assign({}, good, { durationMinutes: '481' })).ok);
check('rejects bad price', !v.validateService(Object.assign({}, good, { price: 'free' })).ok);
check('error messages are keyed by field', v.validateService({ name: '', durationMinutes: 'x', price: 'y' }).errors.name !== undefined
    && v.validateService({ name: '', durationMinutes: 'x', price: 'y' }).errors.durationMinutes !== undefined
    && v.validateService({ name: '', durationMinutes: 'x', price: 'y' }).errors.price !== undefined);
check('invalid service has no value', v.validateService({}).value === null);

// ---------------- meet link ----------------
check('meet: empty allowed', v.validateMeetLink('').ok && v.validateMeetLink('').value === '');
check('meet: https ok + trimmed', v.validateMeetLink('  https://meet.google.com/abc-defg-hij ').value === 'https://meet.google.com/abc-defg-hij');
check('meet: http rejected', !v.validateMeetLink('http://meet.google.com/x').ok);
check('meet: javascript rejected', !v.validateMeetLink('javascript:alert(1)').ok);
check('meet: bare domain rejected', !v.validateMeetLink('meet.google.com/x').ok);
check('meet: over-long rejected', !v.validateMeetLink('https://x.com/' + 'a'.repeat(400)).ok);

// ---------------- date keys ----------------
check('dateKey valid', v.validateDateKey('2026-10-03'));
check('dateKey rejects impossible date', !v.validateDateKey('2026-02-31'));
check('dateKey rejects unpadded', !v.validateDateKey('2026-1-3'));
check('dateKey rejects empty', !v.validateDateKey(''));

// ---------------- schedule ----------------
const docs = [
    { id: 'a', dayOfWeek: 0, startTime: '13:00', endTime: '17:00' },
    { id: 'b', dayOfWeek: 0, startTime: '09:00', endTime: '12:00' },
    { id: 'c', dayOfWeek: 3, startTime: '10:00', endTime: '14:00' },
    { id: 'bad1', dayOfWeek: 9, startTime: '10:00', endTime: '14:00' },
    { id: 'bad2', dayOfWeek: 1, startTime: '14:00', endTime: '10:00' },
    { id: 'bad3', dayOfWeek: 1, startTime: 'nope', endTime: '10:00' }
];
const week = sched.rulesToWeek(docs);
check('rulesToWeek groups by day and sorts blocks', week[0].length === 2 && week[0][0].start === '09:00' && week[0][1].start === '13:00');
check('rulesToWeek keeps a single block day', week[3].length === 1 && week[3][0].end === '14:00');
check('rulesToWeek skips malformed docs', week[1].length === 0 && week.every(function (d) { return d.length <= 2; }));
check('rulesToWeek has 7 days', week.length === 7);

const written = sched.weekToRules(week);
check('weekToRules writes one doc per block', written.length === 3);
check('weekToRules ids are deterministic', written.map(function (w) { return w.id; }).join() === 'day-0-0,day-0-1,day-3-0');
check('weekToRules data shape matches the rules', JSON.stringify(Object.keys(written[0].data)) === JSON.stringify(['dayOfWeek', 'startTime', 'endTime']) && written[0].data.dayOfWeek === 0);
check('round trip preserves the schedule', JSON.stringify(sched.rulesToWeek(written.map(function (w) { return w.data; }))) === JSON.stringify(week));
check('emptyWeek writes nothing', sched.weekToRules(sched.emptyWeek()).length === 0);

check('minutesToTime', sched.minutesToTime(0) === '00:00' && sched.minutesToTime(545) === '09:05' && sched.minutesToTime(23 * 60 + 59) === '23:59');
check('minutesToTime round-trips timeToMinutes', sched.minutesToTime(sched.timeToMinutes('17:45')) === '17:45');
check('nextBlock on an empty day', JSON.stringify(sched.nextBlock([])) === JSON.stringify({ start: '09:00', end: '17:00' }));
check('nextBlock follows the last block by up to 4h', JSON.stringify(sched.nextBlock([{ start: '09:00', end: '12:00' }])) === JSON.stringify({ start: '12:00', end: '16:00' }));
check('nextBlock is capped at end of day', JSON.stringify(sched.nextBlock([{ start: '09:00', end: '22:00' }])) === JSON.stringify({ start: '22:00', end: '23:59' }));
check('nextBlock with no room left returns blanks', JSON.stringify(sched.nextBlock([{ start: '09:00', end: '23:59' }])) === JSON.stringify({ start: '', end: '' }));
check('nextBlock tolerates a half-filled last block', JSON.stringify(sched.nextBlock([{ start: '09:00', end: '' }])) === JSON.stringify({ start: '09:00', end: '17:00' }));

const names = fmt.WEEKDAY_NAMES;
check('valid week has no problems', sched.validateWeek(week, names).length === 0);
{
    const w = sched.emptyWeek();
    w[1] = [{ start: '09:00', end: '12:00' }, { start: '11:00', end: '15:00' }];
    check('overlapping blocks flagged', sched.validateWeek(w, names).some(function (p) { return /overlap/.test(p) && /Monday/.test(p); }));
}
{
    const w = sched.emptyWeek();
    w[2] = [{ start: '09:00', end: '12:00' }, { start: '12:00', end: '15:00' }];
    check('back-to-back blocks are fine', sched.validateWeek(w, names).length === 0);
}
{
    const w = sched.emptyWeek();
    w[4] = [{ start: '15:00', end: '09:00' }];
    check('end before start flagged', sched.validateWeek(w, names).some(function (p) { return /Thursday/.test(p) && /ends before/.test(p); }));
}
{
    const w = sched.emptyWeek();
    w[5] = [{ start: '', end: '09:00' }];
    check('empty time flagged', sched.validateWeek(w, names).some(function (p) { return /Friday/.test(p) && /needs both/.test(p); }));
}

// ---------------- bookings model ----------------
const mk = function (over) {
    return bm.normalizeBooking('id', Object.assign({
        status: 'confirmed', slotDate: '2026-09-20', startMinutes: 600, endMinutes: 660, serviceName: 'Review',
        customerName: 'A', customerEmail: 'a@b.co', customerMobile: '+966500000000', isPaid: true, amount: 50000, currency: 'SAR'
    }, over));
};

check('normalize new-style booking', mk({}).startMinutes === 600 && mk({}).endMinutes === 660);
check('normalize legacy booking (slotTime only) as 60 min',
    bm.normalizeBooking('x', { status: 'confirmed', slotDate: '2026-09-20', slotTime: '10:30' }).endMinutes === 690);
check('normalize tolerates missing fields', bm.normalizeBooking('x', {}).serviceName === 'Advisory session' && bm.normalizeBooking('x', {}).amount === 0);
check('status meta known', bm.statusMeta('confirmed').label === 'Confirmed' && bm.statusMeta('paid_slot_conflict').label === 'Needs refund');
check('status meta unknown falls back safely', bm.statusMeta('weird').label === 'weird' && bm.statusMeta(undefined).label === 'Unknown');

const list = [
    mk({ slotDate: '2026-09-22', startMinutes: 540, endMinutes: 600, status: 'confirmed' }),
    mk({ slotDate: '2026-09-20', startMinutes: 780, endMinutes: 840, status: 'cancelled' }),
    mk({ slotDate: '2026-09-20', startMinutes: 540, endMinutes: 600, status: 'no_show' }),
    mk({ slotDate: '2026-09-21', startMinutes: 540, endMinutes: 600, status: 'pending_payment' }),
    mk({ slotDate: '2026-09-21', startMinutes: 660, endMinutes: 720, status: 'paid_slot_conflict' }),
    mk({ slotDate: '2026-09-23', startMinutes: 540, endMinutes: 600, status: 'expired' })
];
const sorted = bm.sortBookings(list);
check('sorted by date then time', sorted[0].startMinutes === 540 && sorted[0].slotDate === '2026-09-20' && sorted[1].slotDate === '2026-09-20' && sorted[1].startMinutes === 780);
check('sort does not mutate the input', list[0].slotDate === '2026-09-22');
check('filter: all', bm.filterBookings(list, 'all').length === 6);
check('filter: confirmed', bm.filterBookings(list, 'confirmed').length === 1);
check('filter: closed = cancelled + expired', bm.filterBookings(list, 'closed').length === 2);
check('filter: refund', bm.filterBookings(list, 'refund').length === 1);
check('filter: no_show', bm.filterBookings(list, 'no_show').length === 1);
check('filter: pending', bm.filterBookings(list, 'pending').length === 1);
const counts = bm.countByFilter(list);
check('counts match filters', counts.all === 6 && counts.closed === 2 && counts.refund === 1 && counts.confirmed === 1);

const now = new Date(2026, 8, 21, 12, 0); // 21 Sep 2026, 12:00
check('hasStarted: earlier today', bm.hasStarted(mk({ slotDate: '2026-09-21', startMinutes: 540, endMinutes: 600 }), now));
check('hasStarted: later today is not', !bm.hasStarted(mk({ slotDate: '2026-09-21', startMinutes: 900, endMinutes: 960 }), now));
check('hasStarted: future date is not', !bm.hasStarted(mk({ slotDate: '2026-09-22' }), now));
check('hasStarted: exactly at start counts', bm.hasStarted(mk({ slotDate: '2026-09-21', startMinutes: 720, endMinutes: 780 }), now));
check('hasStarted: no interval => false', !bm.hasStarted(bm.normalizeBooking('x', { slotDate: '2026-09-21' }), now));

const stats = bm.computeStats([
    mk({ slotDate: '2026-09-18', status: 'confirmed', amount: 50000 }),                 // completed, paid
    mk({ slotDate: '2026-09-19', status: 'confirmed', isPaid: false, amount: 0 }),      // completed, free
    mk({ slotDate: '2026-09-25', status: 'confirmed', amount: 50000 }),                 // upcoming, paid
    mk({ slotDate: '2026-09-17', status: 'no_show', amount: 20000 }),                   // no-show, paid
    mk({ slotDate: '2026-09-16', status: 'cancelled', amount: 50000 }),                 // refunded -> no revenue
    mk({ slotDate: '2026-09-15', status: 'paid_slot_conflict', amount: 50000 }),        // needs refund -> no revenue
    mk({ slotDate: '2026-09-26', status: 'pending_payment', amount: 50000 }),           // not paid yet
    mk({ slotDate: '2026-09-14', status: 'expired', amount: 50000 })
], now);
check('stats: completed', stats.completed === 2);
check('stats: upcoming', stats.upcoming === 1);
check('stats: no-shows', stats.noShows === 1);
check('stats: cancelled', stats.cancelled === 1);
check('stats: needs refund', stats.needsRefund === 1);
check('stats: awaiting payment', stats.awaitingPayment === 1);
check('stats: revenue = paid confirmed + paid no-show only', stats.revenue === 50000 + 50000 + 20000);
check('stats: currency picked up', stats.currency === 'SAR');
check('stats: no-show rate = no-shows / attended', Math.abs(stats.noShowRate - 1 / 3) < 1e-9);
check('stats: no-show rate is null with no sessions held', bm.computeStats([], now).noShowRate === null);
check('stats: empty month is all zeros', (function (s) { return s.revenue === 0 && s.upcoming === 0 && s.completed === 0; })(bm.computeStats([], now)));

// ---- break between sessions
check('break: 0 ok', v.validateBreakMinutes('0').ok && v.validateBreakMinutes('0').value === 0);
check('break: 15 ok, returned as a number', v.validateBreakMinutes('15').value === 15);
check('break: number input ok', v.validateBreakMinutes(15).value === 15);
check('break: trimmed', v.validateBreakMinutes('  30 ').value === 30);
check('break: 120 is the max', v.validateBreakMinutes('120').ok && !v.validateBreakMinutes('121').ok);
check('break: empty rejected', !v.validateBreakMinutes('').ok && !v.validateBreakMinutes(undefined).ok && !v.validateBreakMinutes(null).ok);
check('break: negative rejected', !v.validateBreakMinutes('-5').ok);
check('break: decimal rejected', !v.validateBreakMinutes('7.5').ok);
check('break: text rejected', !v.validateBreakMinutes('abc').ok && !v.validateBreakMinutes('15 min').ok);
check('break: absurdly long number rejected', !v.validateBreakMinutes('99999').ok);
check('break: error message is human', /0 to 120/.test(v.validateBreakMinutes('x').error));

// ---- cancelling and refund follow-up
{
    const B = function (over) {
        return bm.normalizeBooking('b', Object.assign({ status: 'confirmed', slotDate: '2026-10-11', startMinutes: 600, endMinutes: 660, serviceName: 'S', customerName: 'C', isPaid: true, amount: 50000, currency: 'SAR' }, over));
    };
    const before = new Date(2026, 9, 11, 9, 0);   // an hour before the 10:00 session
    const after = new Date(2026, 9, 11, 10, 30);

    check('cancel offered: confirmed and not yet started', bm.canCancel(B({}), before));
    check('cancel not offered once the session has started', !bm.canCancel(B({}), after));
    check('cancel not offered for other statuses', ['cancelled', 'pending_payment', 'expired', 'no_show', 'paid_slot_conflict', 'refunded'].every(function (st) { return !bm.canCancel(B({ status: st }), before); }));

    check('normalize keeps refund fields', B({ status: 'cancelled', refundAmount: 50000, refundStatus: 'needs_manual', cancelledBy: 'admin' }).refundAmount === 50000
        && B({ status: 'cancelled', refundStatus: 'needs_manual' }).refundStatus === 'needs_manual' && B({}).refundStatus === null && B({}).refundAmount === 0);

    const meta = function (over) { return bm.bookingMeta(B(Object.assign({ status: 'cancelled' }, over))); };
    check('cancelled + refund failed => needs refund (bad, refund group)', meta({ refundStatus: 'needs_manual' }).group === 'refund' && meta({ refundStatus: 'needs_manual' }).tone === 'bad' && /needs refund/.test(meta({ refundStatus: 'needs_manual' }).label));
    check('cancelled + refund started but never recorded => also flagged', meta({ refundStatus: 'pending' }).group === 'refund');
    check('cancelled + refunded (auto or by hand) => closed, quiet', meta({ refundStatus: 'refunded' }).group === 'closed' && meta({ refundStatus: 'refunded_manually' }).group === 'closed' && /refunded/.test(meta({ refundStatus: 'refunded' }).label));
    check('cancelled free booking => plain Cancelled', meta({ refundStatus: 'none' }).label === 'Cancelled' && meta({}).label === 'Cancelled');
    check('old style statuses unchanged', bm.bookingMeta(B({ status: 'paid_slot_conflict' })).group === 'refund' && bm.bookingMeta(B({ status: 'refunded' })).label === 'Refunded' && bm.bookingMeta(B({})).label === 'Confirmed');

    check('refundOutstanding', bm.refundOutstanding(B({ status: 'paid_slot_conflict' })) && bm.refundOutstanding(B({ status: 'cancelled', refundStatus: 'needs_manual' }))
        && bm.refundOutstanding(B({ status: 'cancelled', refundStatus: 'pending' })) && !bm.refundOutstanding(B({ status: 'cancelled', refundStatus: 'refunded' }))
        && !bm.refundOutstanding(B({ status: 'cancelled', refundStatus: 'refunded_manually' })) && !bm.refundOutstanding(B({ status: 'confirmed' })) && !bm.refundOutstanding(B({ status: 'refunded' })));
    check('mark refunded offered exactly when a refund is outstanding', bm.canMarkRefunded(B({ status: 'cancelled', refundStatus: 'needs_manual' })) && bm.canMarkRefunded(B({ status: 'paid_slot_conflict' })) && !bm.canMarkRefunded(B({})));

    const list = [B({}), B({ status: 'cancelled', refundStatus: 'needs_manual' }), B({ status: 'cancelled', refundStatus: 'refunded' }), B({ status: 'paid_slot_conflict' }), B({ status: 'refunded' })];
    const counts = bm.countByFilter(list);
    check('filters: the Needs refund chip counts failed-refund cancellations and clashes', counts.refund === 2 && counts.closed === 2 && counts.confirmed === 1 && counts.all === 5);
    check('filtering by Needs refund returns those two', bm.filterBookings(list, 'refund').length === 2);
    const stats = bm.computeStats(list, before);
    check('overview: cancelled counted, outstanding refunds counted', stats.cancelled === 2 && stats.needsRefund === 2 && stats.upcoming === 1);
    check('overview: cancelled money is not revenue', stats.revenue === 50000);
}

// ---- quarter calendar
check('quarterOf: month -> quarter', fmt.quarterOf(2026, 0).quarter === 0 && fmt.quarterOf(2026, 2).quarter === 0 && fmt.quarterOf(2026, 3).quarter === 1 && fmt.quarterOf(2026, 8).quarter === 2 && fmt.quarterOf(2026, 11).quarter === 3);
check('quarterMonths', JSON.stringify(fmt.quarterMonths(2)) === '[6,7,8]' && JSON.stringify(fmt.quarterMonths(0)) === '[0,1,2]' && JSON.stringify(fmt.quarterMonths(3)) === '[9,10,11]');
check('quarterLabel', fmt.quarterLabel(2026, 2) === 'Q3 2026 (Jul – Sep)' && fmt.quarterLabel(2027, 0) === 'Q1 2027 (Jan – Mar)');
check('quarterRange bounds cover the three months', fmt.quarterRange(2026, 2).start === '2026-07-01' && fmt.quarterRange(2026, 2).end === '2026-09-31' && fmt.quarterRange(2026, 3).start === '2026-10-01' && fmt.quarterRange(2026, 3).end === '2026-12-31');
check('quarterRange: a Sept 30 booking is inside, Oct 1 is not', '2026-09-30' <= fmt.quarterRange(2026, 2).end && '2026-10-01' > fmt.quarterRange(2026, 2).end && '2026-07-01' >= fmt.quarterRange(2026, 2).start && '2026-06-30' < fmt.quarterRange(2026, 2).start);
check('shiftQuarter forwards, across the year end', JSON.stringify(fmt.shiftQuarter(2026, 3, 1)) === '{"year":2027,"quarter":0}' && JSON.stringify(fmt.shiftQuarter(2026, 1, 1)) === '{"year":2026,"quarter":2}');
check('shiftQuarter backwards, across the year start', JSON.stringify(fmt.shiftQuarter(2026, 0, -1)) === '{"year":2025,"quarter":3}' && JSON.stringify(fmt.shiftQuarter(2026, 2, -2)) === '{"year":2026,"quarter":0}');
check('shiftQuarter by several', JSON.stringify(fmt.shiftQuarter(2026, 2, 5)) === '{"year":2027,"quarter":3}' && JSON.stringify(fmt.shiftQuarter(2026, 2, -9)) === '{"year":2024,"quarter":1}');
check('monthGrid: Sep 2026 starts on a Tuesday and has 30 days', fmt.monthGrid(2026, 8).leadingBlanks === 2 && fmt.monthGrid(2026, 8).daysInMonth === 30);
check('monthGrid: leap February, and a Sunday-start month', fmt.monthGrid(2028, 1).daysInMonth === 29 && fmt.monthGrid(2026, 1).daysInMonth === 28 && fmt.monthGrid(2026, 1).leadingBlanks === 0);
check('monthName', fmt.monthName(0) === 'January' && fmt.monthName(11) === 'December');
{
    const B = function (date, status) { return bm.normalizeBooking('x' + Math.random(), { status: status, slotDate: date, startMinutes: 600, endMinutes: 660, serviceName: 'S', customerName: 'C', isPaid: false, amount: 0 }); };
    const list = [B('2026-09-27', 'confirmed'), B('2026-09-27', 'confirmed'), B('2026-09-27', 'no_show'), B('2026-09-27', 'cancelled'), B('2026-09-27', 'pending_payment'),
        B('2026-09-28', 'cancelled'), B('2026-09-29', 'paid_slot_conflict'), B('2026-09-30', 'expired'), B('2026-09-30', 'refunded'), B('2026-08-04', 'confirmed'), B('2026-07-21', 'no_show')];
    const counts = bm.countsByDay(list);
    check('day counts: confirmed and no-shows only', counts['2026-09-27'] === 3 && counts['2026-08-04'] === 1 && counts['2026-07-21'] === 1);
    check('day counts: days with only cancelled / expired / refunded / conflict / unpaid are absent', counts['2026-09-28'] === undefined && counts['2026-09-29'] === undefined && counts['2026-09-30'] === undefined);
    check('day counts: nothing else', Object.keys(counts).length === 3);
    check('day counts: empty list and missing dates', Object.keys(bm.countsByDay([])).length === 0 && Object.keys(bm.countsByDay([{ status: 'confirmed' }])).length === 0);
    check('scope quarter: everything', bm.scopeBookings(list, { kind: 'quarter' }).length === list.length && bm.scopeBookings(list, undefined).length === list.length);
    check('scope month: September only', bm.scopeBookings(list, { kind: 'month', year: 2026, month: 8 }).length === 9);
    check('scope month: August has one, October none', bm.scopeBookings(list, { kind: 'month', year: 2026, month: 7 }).length === 1 && bm.scopeBookings(list, { kind: 'month', year: 2026, month: 9 }).length === 0);
    check('scope month: a different year does not match', bm.scopeBookings(list, { kind: 'month', year: 2025, month: 8 }).length === 0);
    check('scope day: ALL statuses on that day, so cancelled ones stay reachable', bm.scopeBookings(list, { kind: 'day', date: '2026-09-27' }).length === 5);
    check('scope day: a day with none', bm.scopeBookings(list, { kind: 'day', date: '2026-09-01' }).length === 0);
    check('scope + filter chips compose', bm.filterBookings(bm.scopeBookings(list, { kind: 'day', date: '2026-09-27' }), 'confirmed').length === 2 && bm.countByFilter(bm.scopeBookings(list, { kind: 'month', year: 2026, month: 8 })).all === 9);
}

console.log('passed:', pass, 'failed:', fail);
process.exit(fail ? 1 : 0);
