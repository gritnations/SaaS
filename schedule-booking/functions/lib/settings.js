// Firestore-backed scheduling configuration the client manages from the
// admin panel: weekly working hours, blocked dates, and general settings
// (the Google Meet link and the break between sessions). Replaces the hardcoded constants
// that used to live in config.js.

const { timeToMinutes, isValidDateKey, normalizeBreak } = require('./availability');

// availabilityRules/{id}: { dayOfWeek: 0-6 (0 = Sunday), startTime: 'HH:mm', endTime: 'HH:mm' }
// A day with no rules is a non-working day — there's no separate "weekend"
// setting. Returns { 0: [{startTime,endTime}], 1: [...], ... }.
async function getRulesByDay(db) {
    const snap = await db.collection('availabilityRules').get();
    const byDay = {};

    snap.docs.forEach(function (doc) {
        const d = doc.data();
        const day = Number(d.dayOfWeek);
        const start = timeToMinutes(d.startTime);
        const end = timeToMinutes(d.endTime);

        // Skip malformed rules rather than failing the whole schedule.
        if (!Number.isInteger(day) || day < 0 || day > 6) return;
        if (isNaN(start) || isNaN(end) || end <= start) return;

        if (!byDay[day]) byDay[day] = [];
        byDay[day].push({ startTime: d.startTime, endTime: d.endTime });
    });

    return byDay;
}

// blockedDates/{dateKey}: the doc ID is the date ('YYYY-MM-DD'); an optional
// `reason` field is for the admin's own reference. This collection stays tiny
// by nature (holidays, time off), so it's read whole and filtered here.
async function getBlockedDateKeys(db) {
    const snap = await db.collection('blockedDates').get();
    const keys = new Set();
    snap.docs.forEach(function (doc) {
        if (isValidDateKey(doc.id)) keys.add(doc.id);
    });
    return keys;
}

// settings/general: { meetLink: 'https://meet.google.com/...', breakMinutes: 15 }
async function getGeneralSettings(db) {
    const snap = await db.collection('settings').doc('general').get();
    const data = snap.exists ? snap.data() : {};
    return {
        meetLink: typeof data.meetLink === 'string' ? data.meetLink.trim() : '',
        breakMinutes: normalizeBreak(data.breakMinutes)
    };
}

module.exports = { getRulesByDay, getBlockedDateKeys, getGeneralSettings };
