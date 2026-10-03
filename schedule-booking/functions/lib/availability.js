// Pure scheduling logic — no Firestore, no I/O. Everything here takes plain
// data in and returns plain data out, so it can be tested directly and reused
// under any storage layer.
//
// Builds the slotsByDate structure the frontend (booking.js) expects:
// { 'YYYY-MM-DD': [ { id, time, label, status }, ... ] }
// Every slot in the business day is included — booked ones are marked
// status: 'booked' rather than omitted, so the frontend can grey them out.
//
// Times are handled in whole minutes-since-midnight internally so services
// with different durations (30, 45, 60, 90 min...) all work, and overlap
// between bookings of DIFFERENT durations is detected correctly — a 60-min
// session at 09:00 must block a 30-min slot at 09:30 even though their start
// times differ.

const MONTH_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function pad2(n) {
    return n < 10 ? '0' + n : String(n);
}

function dateKeyFromDate(d) {
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

// 'HH:mm' -> minutes since midnight. Returns NaN for malformed input.
function timeToMinutes(time) {
    const match = /^(\d{1,2}):(\d{2})$/.exec(String(time));
    if (!match) return NaN;
    const h = Number(match[1]);
    const m = Number(match[2]);
    if (h > 23 || m > 59) return NaN;
    return h * 60 + m;
}

function minutesToTime(totalMinutes) {
    return pad2(Math.floor(totalMinutes / 60)) + ':' + pad2(totalMinutes % 60);
}

function format12Hour(totalMinutes) {
    const hour24 = Math.floor(totalMinutes / 60) % 24;
    const minute = totalMinutes % 60;
    const suffix = hour24 < 12 ? 'AM' : 'PM';
    let hour12 = hour24 % 12;
    if (hour12 === 0) hour12 = 12;
    return { text: hour12 + ':' + pad2(minute), suffix };
}

function formatSessionLabel(startMinutes, durationMinutes) {
    const start = format12Hour(startMinutes);
    const end = format12Hour(startMinutes + durationMinutes);
    if (start.suffix === end.suffix) {
        return start.text + ' – ' + end.text + ' ' + end.suffix;
    }
    return start.text + ' ' + start.suffix + ' – ' + end.text + ' ' + end.suffix;
}

// A break is a whole, non-negative number of minutes; anything else counts as none.
function normalizeBreak(breakMinutes) {
    const n = Number(breakMinutes);
    return Number.isInteger(n) && n > 0 ? n : 0;
}

// rulesForDay: [{ startTime: 'HH:mm', endTime: 'HH:mm' }, ...] — one day's
// working blocks (multiple blocks let a day have e.g. a lunch gap). Returns
// every session start that fits entirely inside a block. Starts are spaced by
// the session length PLUS the break between sessions (breakMinutes, default
// 0), so 45-min sessions with a 15-min break start every hour.
function slotTimesForDay(rulesForDay, durationMinutes, breakMinutes) {
    const step = durationMinutes + normalizeBreak(breakMinutes);
    const seen = new Set();
    const slots = [];

    (rulesForDay || []).forEach(function (rule) {
        const blockStart = timeToMinutes(rule.startTime);
        const blockEnd = timeToMinutes(rule.endTime);
        if (isNaN(blockStart) || isNaN(blockEnd) || durationMinutes <= 0) return;

        for (let m = blockStart; m + durationMinutes <= blockEnd; m += step) {
            if (seen.has(m)) continue; // overlapping rules must not produce duplicate slots
            seen.add(m);
            slots.push({
                time: minutesToTime(m),
                label: formatSessionLabel(m, durationMinutes),
                startMinutes: m
            });
        }
    });

    slots.sort(function (a, b) { return a.startMinutes - b.startMinutes; });
    return slots;
}

// True if the half-open intervals [aStart, aEnd) and [bStart, bEnd) overlap.
// Back-to-back sessions (one ends exactly when the next starts) do NOT overlap.
function intervalsOverlap(aStart, aEnd, bStart, bEnd) {
    return aStart < bEnd && aEnd > bStart;
}

// existingIntervals: [{ startMinutes, endMinutes }] for one date.
// breakMinutes widens every existing booking on BOTH sides, so a new session
// can't start (or end) inside the break around another one.
function overlapsAny(startMinutes, endMinutes, existingIntervals, breakMinutes) {
    const gap = normalizeBreak(breakMinutes);
    return (existingIntervals || []).some(function (b) {
        return intervalsOverlap(startMinutes, endMinutes, b.startMinutes - gap, b.endMinutes + gap);
    });
}

// rulesByDay: { 0: [...rules], 1: [...], ... } keyed by getDay() (0 = Sun).
// A weekday with no rules is a non-working day (this replaces the old
// hardcoded weekend list — weekends are simply days with no rules).
// blockedDateKeys: Set<'YYYY-MM-DD'> — holidays/time off, skipped entirely.
// bookedByDate: Map<'YYYY-MM-DD', [{ startMinutes, endMinutes }]>.
function buildSlotsByDate(options) {
    const { startDate, numDays, durationMinutes, rulesByDay, blockedDateKeys, bookedByDate, breakMinutes } = options;
    const result = {};
    const blocked = blockedDateKeys || new Set();
    const booked = bookedByDate || new Map();

    for (let i = 0; i < numDays; i++) {
        const d = new Date(startDate.getTime());
        d.setDate(d.getDate() + i);

        const key = dateKeyFromDate(d);
        if (blocked.has(key)) continue;

        const daySlots = slotTimesForDay((rulesByDay || {})[d.getDay()], durationMinutes, breakMinutes);
        if (daySlots.length === 0) continue;

        const bookedIntervals = booked.get(key) || [];

        result[key] = daySlots.map(function (t) {
            const isBooked = overlapsAny(t.startMinutes, t.startMinutes + durationMinutes, bookedIntervals, breakMinutes);
            return {
                id: key + '-' + t.time,
                time: t.time,
                label: t.label,
                status: isBooked ? 'booked' : 'available'
            };
        });
    }

    return result;
}

function isValidDateKey(key) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return false;
    const parts = key.split('-').map(Number);
    const d = new Date(parts[0], parts[1] - 1, parts[2]);
    // Reject impossible dates like 2026-02-31 that Date() would silently roll over.
    return d.getFullYear() === parts[0] && d.getMonth() === parts[1] - 1 && d.getDate() === parts[2];
}

function dateFromKey(key) {
    const parts = key.split('-').map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]);
}

// Server-side check that a requested (date, time) is a real slot for this
// service on this date — never trust a client-supplied time string alone.
function isValidSlotForDate(dateKey, time, durationMinutes, rulesByDay, blockedDateKeys, breakMinutes) {
    if (!isValidDateKey(dateKey)) return false;
    if (blockedDateKeys && blockedDateKeys.has(dateKey)) return false;

    const rules = (rulesByDay || {})[dateFromKey(dateKey).getDay()];
    return slotTimesForDay(rules, durationMinutes, breakMinutes).some(function (s) { return s.time === time; });
}

// Saudi Arabia is UTC+3 all year (no daylight saving).
const SAUDI_OFFSET_HOURS = 3;

// The first bookable day: TOMORROW in Saudi time, as a local-midnight Date (the
// same kind of Date dateFromKey/dateKeyFromDate use). Cloud Functions run in
// UTC, so "today" must be worked out in Saudi time explicitly — otherwise
// between 00:00 and 03:00 Saudi time same-day slots would open up.
function bookingWindowStart(now) {
    const saudi = new Date((now || new Date()).getTime() + SAUDI_OFFSET_HOURS * 3600 * 1000);
    return new Date(saudi.getUTCFullYear(), saudi.getUTCMonth(), saudi.getUTCDate() + 1);
}

// True if dateKey is a bookable day: from tomorrow (Saudi time) through
// windowDays days ahead. Enforced server-side because a hand-crafted request
// could otherwise book the past or years ahead.
function isWithinBookingWindow(dateKey, windowDays, now) {
    const start = bookingWindowStart(now);
    const end = new Date(start.getTime());
    end.setDate(end.getDate() + windowDays - 1);
    const d = dateFromKey(dateKey);
    return d >= start && d <= end;
}

module.exports = {
    SAUDI_OFFSET_HOURS,
    isWithinBookingWindow,
    normalizeBreak,
    bookingWindowStart,
    pad2,
    dateKeyFromDate,
    dateFromKey,
    timeToMinutes,
    minutesToTime,
    formatSessionLabel,
    slotTimesForDay,
    intervalsOverlap,
    overlapsAny,
    buildSlotsByDate,
    isValidDateKey,
    isValidSlotForDate,
    MONTH_SHORT,
    WEEKDAY_SHORT
};
