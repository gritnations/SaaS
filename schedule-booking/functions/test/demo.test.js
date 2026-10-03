// Run with: node test/demo.test.js
// The shared payment-event handler, the public booking status, and the
// local-demo-only pieces (mock gateway, email capture) — including proof that
// the demo pieces cannot activate outside the emulator.

const { createFakeDb } = require('./fakeFirestore');
const email = require('../lib/email');
const { handlePaymentEvent } = require('../lib/payments');
const { toPublicStatus } = require('../lib/bookingStatus');
const MockGateway = require('../lib/gateways/mock');

let pass = 0;
let fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.log('FAIL:', name); } }

const MIN = 60 * 1000;
const secrets = { resendApiKey: 'placeholder', calendarKey: 'placeholder' };

function pendingBooking(over) {
    return Object.assign({
        status: 'pending_payment', slotDate: '2026-09-28', slotTime: '10:00', startMinutes: 600, endMinutes: 645,
        serviceName: 'Executive Advisory Session', customerName: 'Pat Payer', customerEmail: 'pat@example.com',
        customerMobile: '+966500000000', isPaid: true, amount: 50000, currency: 'SAR', manageToken: 'secret-token',
        expiresAt: new Date(Date.now() + 10 * MIN)
    }, over);
}

(async function run() {
    // ---- MockGateway can only exist in the emulator
    delete process.env.FUNCTIONS_EMULATOR;
    let threw = false;
    try { new MockGateway({ checkoutBase: 'http://x/demo/checkout.html' }); } catch (e) { threw = /emulator/.test(e.message); }
    check('MockGateway refuses to construct outside the emulator', threw);
    process.env.FUNCTIONS_EMULATOR = 'true';
    const mock = new MockGateway({ checkoutBase: 'http://127.0.0.1:8899/demo/checkout.html' });
    const co = await mock.createCheckout({ orderId: 'abc123DEF456', amount: 50000 });
    check('mock checkout URL points at the demo page with the booking id', co.checkoutUrl === 'http://127.0.0.1:8899/demo/checkout.html?bookingId=abc123DEF456');
    check('mock returns a provider reference', co.providerReference === 'demo-intention-abc123DEF456');
    check('booking id is URL-encoded', (await mock.createCheckout({ orderId: 'a b&c' })).checkoutUrl.endsWith('bookingId=a%20b%26c'));
    delete process.env.FUNCTIONS_EMULATOR;

    // ---- email capture
    const captured = [];
    email.setEmailCapture(async function (m) { captured.push(m); });
    const eb = { id: 'b1', customerName: 'Pat', customerEmail: 'pat@example.com', customerMobile: '+966500000000', serviceName: 'S', isPaid: true, amount: 50000, currency: 'SAR', slotDateLabel: 'Mon, 28 Sep', slotTimeLabel: '10:00 – 10:45 AM', meetLink: 'https://meet.google.com/abc-defg-hij', manageUrl: 'https://example.com/x' };
    await email.sendBookingConfirmationEmails('placeholder', eb);
    check('placeholder key: both emails captured, none sent', captured.length === 2);
    check('customer email is addressed to the customer', captured[0].to === 'pat@example.com' && /confirmed/i.test(captured[0].subject));
    check('captured html is the real template (has the Meet link)', captured[0].html.indexOf('meet.google.com/abc-defg-hij') > 0);

    let fetched = 0;
    global.fetch = async function () { fetched++; return { ok: true, status: 200, text: async function () { return ''; } }; };
    captured.length = 0;
    await email.sendBookingConfirmationEmails('re_realkey123', eb);
    check('a REAL key still sends through Resend, not the capture', fetched === 2 && captured.length === 0);
    fetched = 0;
    email.setEmailCapture(null);
    await email.sendBookingConfirmationEmails('placeholder', eb);
    check('with no capture registered (production), even a placeholder key goes to Resend', fetched === 2);
    email.setEmailCapture(async function (m) { captured.push(m); });
    captured.length = 0;

    // ---- toPublicStatus: statuses and privacy
    const st = function (status) { return toPublicStatus(pendingBooking({ status: status })).status; };
    check('status mapping', st('pending_payment') === 'pending' && st('confirmed') === 'confirmed' && st('no_show') === 'confirmed'
        && st('paid_slot_conflict') === 'conflict' && st('expired') === 'expired' && st('cancelled') === 'cancelled' && st('weird') === 'unknown');
    const pub = toPublicStatus(pendingBooking({}));
    check('public status has the display fields', pub.serviceName === 'Executive Advisory Session' && pub.slotDateLabel === 'Mon, 28 Sep' && pub.slotTimeLabel === '10:00 – 10:45 AM' && pub.amount === 50000 && pub.currency === 'SAR');
    const text = JSON.stringify(pub);
    check('public status leaks no personal data or tokens', ['Pat', 'pat@example.com', '+966', 'secret-token', 'manageToken', 'meetLink'].every(function (x) { return text.indexOf(x) < 0; }));

    // ---- handlePaymentEvent
    {
        const db = createFakeDb();
        db.seed('bookings', 'B1', pendingBooking({}));
        const out = await handlePaymentEvent(db, { orderId: 'B1', providerTransactionId: 'txn-1', status: 'paid' }, secrets);
        check('paid event confirms the booking', out === 'confirmed' && db.raw('bookings', 'B1').status === 'confirmed');
        check('transaction id recorded', db.raw('bookings', 'B1').paymobTransactionId === 'txn-1');
        check('customer AND owner emails were sent', captured.length === 2 && captured[0].to === 'pat@example.com');
        const before = captured.length;
        check('a repeated event is harmless', (await handlePaymentEvent(db, { orderId: 'B1', providerTransactionId: 'txn-1', status: 'paid' }, secrets)) === 'already_handled' && captured.length === before);
    }
    {
        const db = createFakeDb();
        db.seed('bookings', 'B2', pendingBooking({}));
        captured.length = 0;
        check('non-paid event is ignored', (await handlePaymentEvent(db, { orderId: 'B2', providerTransactionId: 't', status: 'failed' }, secrets)) === 'ignored');
        check('...and changes nothing, sends nothing', db.raw('bookings', 'B2').status === 'pending_payment' && captured.length === 0);
        check('empty payload is invalid', (await handlePaymentEvent(db, null, secrets)) === 'invalid');
        check('missing orderId is invalid', (await handlePaymentEvent(db, { status: 'paid', providerTransactionId: 't' }, secrets)) === 'invalid');
        check('unknown booking => not_found', (await handlePaymentEvent(db, { orderId: 'nope', providerTransactionId: 't', status: 'paid' }, secrets)) === 'not_found');
    }
    {
        // paid after the hold lapsed AND someone else took the time
        const db = createFakeDb();
        db.seed('bookings', 'A', pendingBooking({ expiresAt: new Date(Date.now() - 5 * MIN) }));
        db.seed('bookings', 'Z', pendingBooking({ status: 'confirmed', customerName: 'Other', customerEmail: 'other@example.com', expiresAt: null }));
        captured.length = 0;
        const out = await handlePaymentEvent(db, { orderId: 'A', providerTransactionId: 'txn-A', status: 'paid' }, secrets);
        check('clash => conflict outcome, booking flagged', out === 'conflict' && db.raw('bookings', 'A').status === 'paid_slot_conflict');
        check('only the OWNER is alerted; the customer gets no false "confirmed"', captured.length === 1 && captured[0].to !== 'pat@example.com' && /refund/i.test(captured[0].subject));
        check('alert names the transaction to refund', captured[0].html.indexOf('txn-A') > 0);
    }

    console.log('passed: ' + pass + ' failed: ' + fail);
    process.exit(fail === 0 ? 0 : 1);
})().catch(function (e) { console.error(e); process.exit(1); });
