// Run with: node test/meeting.test.js
// Per-booking Google Meet links: the Calendar event that is built, and the
// fallback to the static link whenever Calendar can't be used. No network:
// the Calendar call is injected and fetch (used for emails) is stubbed.

const { createFakeDb } = require('./fakeFirestore');
const { createMeeting, buildEventBody, toLocalDateTime } = require('../lib/meeting');
const { finalizeConfirmedBooking } = require('../lib/confirmation');

let pass = 0;
let fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.log('FAIL:', name); } }

const booking = {
    id: 'bk1', status: 'confirmed', slotDate: '2026-10-04', slotTime: '09:00', startMinutes: 540, endMinutes: 630,
    serviceName: 'Business Review', customerName: 'Alice Alpha', customerEmail: 'alice@example.com',
    customerMobile: '+966500000000', isPaid: false, amount: 0, currency: 'SAR', manageToken: 'tok'
};

const sentEmails = [];
global.fetch = async function (url, init) {
    sentEmails.push(JSON.parse(init.body));
    return { ok: true, status: 200, text: async function () { return ''; }, json: async function () { return {}; } };
};

(async function run() {
    // ---- the event that gets built
    check('local date-time uses Saudi offset', toLocalDateTime('2026-10-04', 540) === '2026-10-04T09:00:00+03:00');
    check('local date-time handles minutes', toLocalDateTime('2026-10-04', 630) === '2026-10-04T10:30:00+03:00');

    const body = buildEventBody(booking);
    check('event starts and ends at the booked times', body.start.dateTime === '2026-10-04T09:00:00+03:00' && body.end.dateTime === '2026-10-04T10:30:00+03:00');
    check('event is in the Riyadh time zone', body.start.timeZone === 'Asia/Riyadh' && body.end.timeZone === 'Asia/Riyadh');
    check('customer is invited', body.attendees.length === 1 && body.attendees[0].email === 'alice@example.com');
    check('asks Google for a new Meet room, keyed by booking id', body.conferenceData.createRequest.requestId === 'bk1' && body.conferenceData.createRequest.conferenceSolutionKey.type === 'hangoutsMeet');
    check('title names the service and customer', body.summary.indexOf('Business Review') === 0 && body.summary.indexOf('Alice Alpha') > 0);
    check('legacy booking (no interval) is 60 min', buildEventBody({ id: 'x', slotDate: '2026-10-04', slotTime: '10:00', customerName: 'L', customerEmail: 'l@example.com' }).end.dateTime === '2026-10-04T11:00:00+03:00');

    // ---- createMeeting
    let seen = null;
    const okRequest = async function (url, b) { seen = { url: url, body: b }; return { id: 'evt123', hangoutLink: 'https://meet.google.com/aaa-bbbb-ccc' }; };
    const made = await createMeeting('{"k":1}', booking, okRequest);
    check('returns the link and event id', made && made.meetLink === 'https://meet.google.com/aaa-bbbb-ccc' && made.eventId === 'evt123');
    check('calls the Calendar events endpoint with Meet enabled and no Google emails', seen.url.indexOf('/calendars/primary/events') > 0 && seen.url.indexOf('conferenceDataVersion=1') > 0 && seen.url.indexOf('sendUpdates=none') > 0);

    const viaEntryPoints = await createMeeting('{"k":1}', booking, async function () {
        return { id: 'e2', conferenceData: { entryPoints: [{ entryPointType: 'phone', uri: 'tel:+1' }, { entryPointType: 'video', uri: 'https://meet.google.com/ddd-eeee-fff' }] } };
    });
    check('falls back to the video entry point', viaEntryPoints && viaEntryPoints.meetLink === 'https://meet.google.com/ddd-eeee-fff');

    check('null when Calendar throws', (await createMeeting('{"k":1}', booking, async function () { throw new Error('boom'); })) === null);
    check('null when response has no link', (await createMeeting('{"k":1}', booking, async function () { return { id: 'e3' }; })) === null);
    check('null when secret is not set up (placeholder text)', (await createMeeting('placeholder', booking)) === null);
    check('null when secret is empty', (await createMeeting('', booking)) === null);
    check('null when key JSON is malformed', (await createMeeting('{not json', booking)) === null);

    // ---- finalizeConfirmedBooking end to end
    async function finalize(requestCalendar, staticLink) {
        sentEmails.length = 0;
        const db = createFakeDb();
        db.seed('bookings', 'bk1', Object.assign({}, booking));
        db.seed('settings', 'general', { meetLink: staticLink });
        const result = await finalizeConfirmedBooking(db, booking, 'fake-resend-key', '{"k":1}', { requestCalendar: requestCalendar });
        return { result: result, stored: db.raw('bookings', 'bk1'), emails: sentEmails.slice() };
    }

    const good = await finalize(okRequest, 'https://meet.google.com/static-room-xyz');
    check('unique link is returned', good.result.meetLink === 'https://meet.google.com/aaa-bbbb-ccc');
    check('unique link and event id are stored on the booking', good.stored.meetLink === 'https://meet.google.com/aaa-bbbb-ccc' && good.stored.calendarEventId === 'evt123');
    check('customer email carries the unique link, not the static one', good.emails.some(function (e) { return e.html.indexOf('aaa-bbbb-ccc') >= 0; }) && !good.emails.some(function (e) { return e.html.indexOf('static-room-xyz') >= 0; }));

    const down = await finalize(async function () { throw new Error('Calendar down'); }, 'https://meet.google.com/static-room-xyz');
    check('Calendar failure falls back to the static link', down.result.meetLink === 'https://meet.google.com/static-room-xyz' && down.stored.meetLink === 'https://meet.google.com/static-room-xyz');
    check('fallback stores no event id', down.stored.calendarEventId === undefined);
    check('fallback still sends the emails', down.emails.length > 0 && down.emails.some(function (e) { return e.html.indexOf('static-room-xyz') >= 0; }));

    const none = await finalize(async function () { throw new Error('down'); }, '');
    check('no Calendar and no static link: booking still confirms, email still sent, no link', none.result.meetLink === '' && none.stored.meetLink === null && none.emails.length > 0);

    console.log('passed: ' + pass + ' failed: ' + fail);
    process.exit(fail === 0 ? 0 : 1);
})().catch(function (e) { console.error(e); process.exit(1); });
