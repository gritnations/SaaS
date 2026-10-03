// Creates a Google Calendar event (with its own unique Google Meet link) on the
// owner's calendar for one booking.
//
// Auth: a Google service account with domain-wide delegation, acting AS the
// owner (config.CALENDAR_OWNER_EMAIL). The service-account key JSON lives in
// the GOOGLE_CALENDAR_KEY secret. The Admin console grants it only the
// calendar.events scope.
//
// Never throws: any problem returns null so the caller can fall back to the
// static Meet link. A booking must never fail because Calendar is unreachable.

const logger = require('firebase-functions/logger');
const { getInterval } = require('./bookings');
const { TIMEZONE, TIMEZONE_OFFSET, CALENDAR_OWNER_EMAIL } = require('./config');

const SCOPE = 'https://www.googleapis.com/auth/calendar.events';
const EVENTS_URL = 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
    + '?conferenceDataVersion=1&sendUpdates=none';

function pad(n) { return (n < 10 ? '0' : '') + n; }

// slotDate 'YYYY-MM-DD' + minutes since midnight -> '2026-10-04T09:00:00+03:00'
// (Saudi time has no daylight saving, so a fixed offset is exact.)
function toLocalDateTime(dateKey, minutes) {
    return dateKey + 'T' + pad(Math.floor(minutes / 60)) + ':' + pad(minutes % 60) + ':00' + TIMEZONE_OFFSET;
}

function buildEventBody(booking) {
    const interval = getInterval(booking);
    if (!interval) return null;

    const description = [
        'Booked online.',
        'Customer: ' + booking.customerName,
        'Email: ' + booking.customerEmail,
        booking.customerMobile ? 'Mobile: ' + booking.customerMobile : null,
        'Booking ref: ' + booking.id
    ].filter(Boolean).join('\n');

    return {
        summary: (booking.serviceName || 'Advisory session') + ' — ' + booking.customerName,
        description: description,
        start: { dateTime: toLocalDateTime(booking.slotDate, interval.startMinutes), timeZone: TIMEZONE },
        end: { dateTime: toLocalDateTime(booking.slotDate, interval.endMinutes), timeZone: TIMEZONE },
        // Inviting the customer lets them join without waiting to be admitted;
        // anyone else who somehow gets the link still has to knock.
        attendees: [{ email: booking.customerEmail }],
        conferenceData: {
            createRequest: { requestId: booking.id, conferenceSolutionKey: { type: 'hangoutsMeet' } }
        },
        reminders: { useDefault: true }
    };
}

// Returns (url, body, method = 'POST') -> parsed JSON (or '' for an empty 204).
function defaultRequester(keyJson) {
    const { JWT } = require('google-auth-library');
    const key = JSON.parse(keyJson);
    const client = new JWT({ email: key.client_email, key: key.private_key, scopes: [SCOPE], subject: CALENDAR_OWNER_EMAIL });
    return async function (url, body, method) {
        const res = await client.request({ url: url, method: method || 'POST', data: body });
        return res.data;
    };
}

function eventUrl(eventId) {
    return 'https://www.googleapis.com/calendar/v3/calendars/primary/events/' + encodeURIComponent(eventId) + '?sendUpdates=none';
}

function hasKey(keyJson) {
    return !!keyJson && String(keyJson).trim().startsWith('{');
}

// Removes the booking's calendar event (e.g. on cancellation). Returns true if
// it is gone afterwards — including "it was already gone" (404/410) — and false
// on any other problem. Never throws; a cancellation must not fail over Calendar.
async function deleteMeeting(keyJson, eventId, request) {
    if (!eventId) return true; // nothing was ever created (Calendar wasn't set up) — nothing to remove
    try {
        if (!request && !hasKey(keyJson)) return false;
        const del = request || defaultRequester(keyJson);
        await del(eventUrl(eventId), undefined, 'DELETE');
        return true;
    } catch (err) {
        const status = err && (err.code || (err.response && err.response.status));
        if (status === 404 || status === 410) return true;
        logger.error('deleteMeeting failed', { eventId: eventId, err: String(err && err.message || err) });
        return false;
    }
}

// Moves the booking's calendar event to its (new) time, keeping the same Meet
// link. Returns true on success; never throws.
async function moveMeeting(keyJson, booking, eventId, request) {
    if (!eventId) return false;
    try {
        const body = buildEventBody(booking);
        if (!body) return false;
        if (!request && !hasKey(keyJson)) return false;
        const patch = request || defaultRequester(keyJson);
        await patch(eventUrl(eventId), { start: body.start, end: body.end }, 'PATCH');
        return true;
    } catch (err) {
        logger.error('moveMeeting failed', { bookingId: booking.id, err: String(err && err.message || err) });
        return false;
    }
}

// `request` is injectable for tests: (url, body) -> parsed JSON response.
async function createMeeting(keyJson, booking, request) {
    try {
        const body = buildEventBody(booking);
        if (!body) return null;
        if (!request && (!keyJson || !String(keyJson).trim().startsWith('{'))) return null; // secret not set up yet

        const post = request || defaultRequester(keyJson);
        const event = await post(EVENTS_URL, body);

        const meetLink = event && (event.hangoutLink
            || (event.conferenceData && (event.conferenceData.entryPoints || []).filter(function (e) { return e.entryPointType === 'video'; }).map(function (e) { return e.uri; })[0]));
        if (!event || !event.id || !meetLink) {
            logger.error('createMeeting: Calendar response had no Meet link', { bookingId: booking.id });
            return null;
        }
        return { meetLink: meetLink, eventId: event.id };
    } catch (err) {
        logger.error('createMeeting failed', { bookingId: booking.id, err: String(err && err.message || err) });
        return null;
    }
}

module.exports = { createMeeting, deleteMeeting, moveMeeting, buildEventBody, toLocalDateTime };
