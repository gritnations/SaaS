// Display helpers. Pure functions — no DOM, no Firebase.

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];
const MONTH_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
export const WEEKDAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

export function pad2(n) {
    return n < 10 ? '0' + n : String(n);
}

export function dateKeyFromDate(d) {
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

export function dateFromKey(key) {
    const [y, m, d] = key.split('-').map(Number);
    return new Date(y, m - 1, d);
}

export function formatDateLabel(key) {
    const d = dateFromKey(key);
    return WEEKDAY_SHORT[d.getDay()] + ', ' + d.getDate() + ' ' + MONTH_SHORT[d.getMonth()] + ' ' + d.getFullYear();
}

// month is 0-based, like Date.
export function monthLabel(year, month) {
    return MONTH_NAMES[month] + ' ' + year;
}

// Inclusive string bounds for "every slotDate in this month". The upper bound
// can be a day that doesn't exist (e.g. 2026-09-31) — it's only ever compared
// as a string, so that's harmless and avoids month-length arithmetic.
export function monthRange(year, month) {
    const prefix = year + '-' + pad2(month + 1) + '-';
    return { start: prefix + '01', end: prefix + '31' };
}

// ---- quarters (Q1 = Jan-Mar ... Q4 = Oct-Dec); quarter is 0-based like month ----

export function quarterOf(year, month) {
    return { year: year, quarter: Math.floor(month / 3) };
}

export function quarterMonths(quarter) {
    return [quarter * 3, quarter * 3 + 1, quarter * 3 + 2];
}

export function quarterLabel(year, quarter) {
    return 'Q' + (quarter + 1) + ' ' + year + ' (' + MONTH_SHORT[quarter * 3] + ' – ' + MONTH_SHORT[quarter * 3 + 2] + ')';
}

// Inclusive string bounds for every slotDate in the quarter (same string-compare idea as monthRange).
export function quarterRange(year, quarter) {
    return {
        start: year + '-' + pad2(quarter * 3 + 1) + '-01',
        end: year + '-' + pad2(quarter * 3 + 3) + '-31'
    };
}

export function shiftQuarter(year, quarter, delta) {
    const index = year * 4 + quarter + delta;
    return { year: Math.floor(index / 4), quarter: ((index % 4) + 4) % 4 };
}

export function monthName(month) {
    return MONTH_NAMES[month];
}

// How to lay a month out as a Sunday-first grid.
export function monthGrid(year, month) {
    return {
        leadingBlanks: new Date(year, month, 1).getDay(),
        daysInMonth: new Date(year, month + 1, 0).getDate()
    };
}

export function shiftMonth(year, month, delta) {
    const d = new Date(year, month + delta, 1);
    return { year: d.getFullYear(), month: d.getMonth() };
}

function format12Hour(totalMinutes) {
    const hour24 = Math.floor(totalMinutes / 60) % 24;
    const minute = totalMinutes % 60;
    const suffix = hour24 < 12 ? 'AM' : 'PM';
    let hour12 = hour24 % 12;
    if (hour12 === 0) hour12 = 12;
    return { text: hour12 + ':' + pad2(minute), suffix };
}

export function formatTimeRange(startMinutes, durationMinutes) {
    const start = format12Hour(startMinutes);
    const end = format12Hour(startMinutes + durationMinutes);
    if (start.suffix === end.suffix) return start.text + ' – ' + end.text + ' ' + end.suffix;
    return start.text + ' ' + start.suffix + ' – ' + end.text + ' ' + end.suffix;
}

export function formatDuration(minutes) {
    if (minutes < 60) return minutes + ' min';
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return h + ' hr' + (m ? ' ' + m + ' min' : '');
}

// amount is in the smallest currency unit (halalas).
export function formatMoney(amount, currency) {
    const major = amount / 100;
    return currency + ' ' + (amount % 100 === 0 ? String(major) : major.toFixed(2));
}

export function formatPrice(amount, currency) {
    return amount > 0 ? formatMoney(amount, currency) : 'Free';
}
