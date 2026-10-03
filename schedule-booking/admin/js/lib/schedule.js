// Weekly working hours: converting between the Firestore documents
// (availabilityRules — one document per block of hours) and the shape the
// editor works with, plus validation. Pure functions.
//
// Week model: an array of 7 days indexed by getDay() (0 = Sunday), each an
// array of blocks: [{ start: 'HH:mm', end: 'HH:mm' }, ...]. A day with no
// blocks is a day off.

const TIME_PATTERN = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;

export function isTime(text) {
    return typeof text === 'string' && TIME_PATTERN.test(text);
}

export function timeToMinutes(text) {
    if (!isTime(text)) return NaN;
    const [h, m] = text.split(':').map(Number);
    return h * 60 + m;
}

export function minutesToTime(totalMinutes) {
    const h = Math.floor(totalMinutes / 60);
    const m = totalMinutes % 60;
    return (h < 10 ? '0' + h : String(h)) + ':' + (m < 10 ? '0' + m : String(m));
}

// Sensible starting values for the "+ Add hours" button: an empty day gets
// 09:00-17:00; otherwise a block of up to four hours right after the last one.
// Returns blank times (which validation will flag) if there's no room left.
export function nextBlock(blocks) {
    const last = blocks.length ? blocks[blocks.length - 1].end : '';
    if (!isTime(last)) return { start: '09:00', end: '17:00' };

    const s = timeToMinutes(last);
    const e = Math.min(s + 240, 23 * 60 + 59);
    if (e <= s) return { start: '', end: '' };
    return { start: last, end: minutesToTime(e) };
}

export function emptyWeek() {
    return [[], [], [], [], [], [], []];
}

// docs: [{ id, dayOfWeek, startTime, endTime }] — malformed ones are skipped,
// mirroring how the booking backend reads them.
export function rulesToWeek(docs) {
    const week = emptyWeek();
    (docs || []).forEach(function (d) {
        const day = Number(d.dayOfWeek);
        if (!Number.isInteger(day) || day < 0 || day > 6) return;
        if (!isTime(d.startTime) || !isTime(d.endTime)) return;
        if (timeToMinutes(d.endTime) <= timeToMinutes(d.startTime)) return;
        week[day].push({ start: d.startTime, end: d.endTime });
    });
    week.forEach(function (blocks) {
        blocks.sort(function (a, b) { return timeToMinutes(a.start) - timeToMinutes(b.start); });
    });
    return week;
}

// -> [{ id, data }] ready to write. IDs are deterministic (day-<d>-<i>) so the
// whole week can be replaced idempotently.
export function weekToRules(week) {
    const out = [];
    week.forEach(function (blocks, day) {
        blocks.forEach(function (b, i) {
            out.push({ id: 'day-' + day + '-' + i, data: { dayOfWeek: day, startTime: b.start, endTime: b.end } });
        });
    });
    return out;
}

// Returns [] when fine, else human-readable problems. `dayNames` labels the
// messages (kept as a parameter so this file has no display dependencies).
export function validateWeek(week, dayNames) {
    const problems = [];

    week.forEach(function (blocks, day) {
        const label = dayNames[day];
        const parsed = [];

        blocks.forEach(function (b, i) {
            const n = i + 1;
            if (!isTime(b.start) || !isTime(b.end)) {
                problems.push(label + ': hours block ' + n + ' needs both a start and an end time.');
                return;
            }
            const s = timeToMinutes(b.start);
            const e = timeToMinutes(b.end);
            if (e <= s) {
                problems.push(label + ': hours block ' + n + ' ends before it starts.');
                return;
            }
            parsed.push({ s, e, n });
        });

        parsed.sort(function (a, b) { return a.s - b.s; });
        for (let i = 1; i < parsed.length; i++) {
            if (parsed[i].s < parsed[i - 1].e) {
                problems.push(label + ': hours blocks ' + parsed[i - 1].n + ' and ' + parsed[i].n + ' overlap.');
            }
        }
    });

    return problems;
}
