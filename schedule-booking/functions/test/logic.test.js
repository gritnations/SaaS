// Run with: node test/logic.test.js
// Pure-logic checks: slot generation, overlap, validation, service parsing,
// refund policy, and email rendering. No Firestore involved.

const a = require('../lib/availability');
const { normalizeService, isPaidService, toPublicService } = require('../lib/services');
const { calculateRefundAmount } = require('../lib/refundPolicy');
const email = require('../lib/email');

let pass = 0;
let fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.log('FAIL:', name); } }

// ---- slotTimesForDay ----
const rules = [{ startTime: '09:00', endTime: '17:00' }];
check('60min in 9-17 => 8 slots', a.slotTimesForDay(rules, 60).length === 8);
check('30min in 9-17 => 16 slots', a.slotTimesForDay(rules, 30).length === 16);
check('90min in 9-17 => 5 slots', a.slotTimesForDay(rules, 90).length === 5);
check('90min last slot starts 15:00', a.slotTimesForDay(rules, 90).slice(-1)[0].time === '15:00');
check('label 60min', a.slotTimesForDay(rules, 60)[0].label === '9:00 – 10:00 AM');
check('label crossing noon', a.slotTimesForDay(rules, 60)[2].label === '11:00 AM – 12:00 PM');
check('label 45min', a.slotTimesForDay([{ startTime: '09:30', endTime: '11:00' }], 45)[0].label === '9:30 – 10:15 AM');

const split = [{ startTime: '09:00', endTime: '12:00' }, { startTime: '13:00', endTime: '17:00' }];
const splitSlots = a.slotTimesForDay(split, 60);
check('split day => 3 + 4 = 7 slots', splitSlots.length === 7);
check('no slot starts at 12:00 (lunch)', !splitSlots.some(function (s) { return s.time === '12:00'; }));

const oSlots = a.slotTimesForDay([{ startTime: '09:00', endTime: '12:00' }, { startTime: '10:00', endTime: '13:00' }], 60);
const oTimes = oSlots.map(function (s) { return s.time; });
check('overlapping rules dedupe', new Set(oTimes).size === oTimes.length);

check('empty rules => no slots', a.slotTimesForDay([], 60).length === 0);
check('undefined rules => no slots', a.slotTimesForDay(undefined, 60).length === 0);
check('zero duration => no slots (no infinite loop)', a.slotTimesForDay(rules, 0).length === 0);
check('malformed time ignored', a.slotTimesForDay([{ startTime: 'nope', endTime: '17:00' }], 60).length === 0);

// ---- overlap ----
check('overlap: same interval', a.intervalsOverlap(540, 600, 540, 600));
check('overlap: partial', a.intervalsOverlap(540, 600, 570, 630));
check('NO overlap: back-to-back', !a.intervalsOverlap(540, 600, 600, 660));
check('overlap: contained', a.intervalsOverlap(540, 720, 570, 600));

// ---- cross-duration blocking in the public slot list ----
const start = new Date(2026, 8, 27); // Sun 27 Sep 2026
const rulesByDay = { 0: rules };
const booked = new Map([['2026-09-27', [{ startMinutes: 540, endMinutes: 600 }]]]);
const day30 = a.buildSlotsByDate({ startDate: start, numDays: 1, durationMinutes: 30, rulesByDay, bookedByDate: booked })['2026-09-27'];
check('30min: 09:00 booked', day30.find(function (s) { return s.time === '09:00'; }).status === 'booked');
check('30min: 09:30 booked (inside the 60min booking)', day30.find(function (s) { return s.time === '09:30'; }).status === 'booked');
check('30min: 10:00 available (back-to-back)', day30.find(function (s) { return s.time === '10:00'; }).status === 'available');

const booked2 = new Map([['2026-09-27', [{ startMinutes: 570, endMinutes: 600 }]]]);
const day60 = a.buildSlotsByDate({ startDate: start, numDays: 1, durationMinutes: 60, rulesByDay, bookedByDate: booked2 })['2026-09-27'];
check('60min: 09:00 booked (contains the 09:30 booking)', day60.find(function (s) { return s.time === '09:00'; }).status === 'booked');
check('60min: 10:00 available', day60.find(function (s) { return s.time === '10:00'; }).status === 'available');

// ---- working days / blocked dates ----
const fiveDays = { 0: rules, 1: rules, 2: rules, 3: rules, 4: rules };
const week = a.buildSlotsByDate({ startDate: start, numDays: 7, durationMinutes: 60, rulesByDay: fiveDays });
check('Sun-Thu only => 5 days in a week', Object.keys(week).length === 5);
check('Friday skipped', !week['2026-10-02']);
const blockedWeek = a.buildSlotsByDate({ startDate: start, numDays: 7, durationMinutes: 60, rulesByDay: fiveDays, blockedDateKeys: new Set(['2026-09-28']) });
check('blocked date skipped', !blockedWeek['2026-09-28'] && Object.keys(blockedWeek).length === 4);

// ---- validation ----
check('valid key', a.isValidDateKey('2026-09-27'));
check('rejects 2026-02-31', !a.isValidDateKey('2026-02-31'));
check('rejects garbage', !a.isValidDateKey('nope') && !a.isValidDateKey('2026-9-7'));
check('valid slot accepted', a.isValidSlotForDate('2026-09-27', '10:00', 60, rulesByDay));
check('off-grid time rejected (09:30 for 60min)', !a.isValidSlotForDate('2026-09-27', '09:30', 60, rulesByDay));
check('non-working day rejected', !a.isValidSlotForDate('2026-10-02', '10:00', 60, rulesByDay));
check('blocked date rejected', !a.isValidSlotForDate('2026-09-27', '10:00', 60, rulesByDay, new Set(['2026-09-27'])));
check('time outside hours rejected', !a.isValidSlotForDate('2026-09-27', '17:00', 60, rulesByDay));

// ---- services.normalizeService ----
const ok = normalizeService('s1', { name: ' Business Review ', durationMinutes: 60, amount: 50000, sortOrder: 2, description: ' d ' });
check('valid service normalizes', ok && ok.name === 'Business Review' && ok.description === 'd' && ok.amount === 50000 && ok.active === true);
check('missing amount => free', normalizeService('s', { name: 'x', durationMinutes: 30 }).amount === 0);
check('free detected', !isPaidService(normalizeService('s', { name: 'x', durationMinutes: 30, amount: 0 })));
check('paid detected', isPaidService(ok));
check('active:false respected', normalizeService('s', { name: 'x', durationMinutes: 30, active: false }).active === false);
check('rejects missing name', normalizeService('s', { durationMinutes: 30 }) === null);
check('rejects blank name', normalizeService('s', { name: '   ', durationMinutes: 30 }) === null);
check('rejects zero duration', normalizeService('s', { name: 'x', durationMinutes: 0 }) === null);
check('rejects fractional duration', normalizeService('s', { name: 'x', durationMinutes: 30.5 }) === null);
check('rejects negative amount', normalizeService('s', { name: 'x', durationMinutes: 30, amount: -5 }) === null);
check('rejects fractional amount', normalizeService('s', { name: 'x', durationMinutes: 30, amount: 10.5 }) === null);
check('rejects null doc', normalizeService('s', null) === null);
const pub = toPublicService(ok);
check('public service has isFree and no internal fields', pub.isFree === false && pub.active === undefined && pub.sortOrder === undefined);

// ---- refund policy ----
check('paid => full refund', calculateRefundAmount({ isPaid: true, amount: 50000 }, new Date()) === 50000);
check('free => 0', calculateRefundAmount({ isPaid: false, amount: 0 }, new Date()) === 0);
check('paid flag but zero amount => 0', calculateRefundAmount({ isPaid: true, amount: 0 }, new Date()) === 0);
check('null booking => 0', calculateRefundAmount(null, new Date()) === 0);

// ---- email templates ----
const evil = {
    id: 'b1', customerName: '<script>alert(1)</script>', customerEmail: 'a@b.co', customerMobile: '+966500000000',
    serviceName: 'Review & "Plan"', isPaid: true, amount: 50000, currency: 'SAR',
    slotDateLabel: 'Wed, 23 Sep', slotTimeLabel: '9:00 – 10:00 AM', meetLink: 'javascript:alert(1)',
    manageUrl: 'https://example.com/pages/manage-booking.html?bookingId=b1&token=abc'
};
const html = email.bookingConfirmedHtml(evil);
check('name is escaped', !html.includes('<script>') && html.includes('&lt;script&gt;'));
check('service name escaped', html.includes('Review &amp; &quot;Plan&quot;'));
check('javascript: meet link dropped', !html.includes('javascript:') && !html.includes('Join Google Meet'));
check('valid manage link kept', html.includes('Manage my booking') && html.includes('token=abc'));
check('paid amount shown', html.includes('SAR 500.00'));
const good = email.bookingConfirmedHtml(Object.assign({}, evil, { meetLink: 'https://meet.google.com/abc-defg-hij' }));
check('https meet link rendered', good.includes('Join Google Meet') && good.includes('https://meet.google.com/abc-defg-hij'));
const free = email.bookingConfirmedHtml(Object.assign({}, evil, { isPaid: false, amount: 0 }));
check('free booking shows no amount', !free.includes('Amount paid'));
check('owner mail says Free booking', email.ownerNotificationHtml(Object.assign({}, evil, { isPaid: false }), 'x').includes('Free booking'));
check('safeUrl rejects non-http', email.safeUrl('ftp://x') === '' && email.safeUrl('') === '' && email.safeUrl(undefined) === '');

console.log('passed:', pass, 'failed:', fail);
process.exit(fail ? 1 : 0);
