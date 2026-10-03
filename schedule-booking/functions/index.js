// Booking backend — Firebase Cloud Functions (2nd gen).
// See ../README.md and the architecture plan this was built from for the
// full design rationale. Functions:
//
//   listServices          (HTTP GET,  public)  — the bookable services + prices
//   availableSlots        (HTTP GET,  public)  — slot listing for one service
//   bookingRequest        (HTTP POST, public)  — reserve a slot; free services
//                                                confirm instantly, paid ones
//                                                go on to payment
//   paymobWebhookIngest   (HTTP POST, public)  — verify + relay Paymob callbacks
//   processPaymobEvent    (Pub/Sub trigger)    — confirm paid booking, send emails
//   expirePendingBookings (Scheduled, hourly)  — free unpaid expired holds

const { onRequest } = require('firebase-functions/v2/https');
const { onMessagePublished } = require('firebase-functions/v2/pubsub');
const { onSchedule } = require('firebase-functions/v2/scheduler');
const { PubSub } = require('@google-cloud/pubsub');
const admin = require('firebase-admin');
const logger = require('firebase-functions/logger');

admin.initializeApp();
const db = admin.firestore();
const pubsub = new PubSub();

const {
    ALLOWED_ORIGINS,
    ADMIN_ORIGINS,
    ADMIN_EMAILS,
    CURRENCY,
    BOOKING_WINDOW_DAYS,
    PUBLIC_SITE_ORIGIN,
    paymobSecretKey,
    paymobPublicKey,
    paymobHmacSecret,
    paymobWebhookSharedSecret,
    resendApiKey,
    googleCalendarKey
} = require('./lib/config');
const {
    isValidDateKey, isValidSlotForDate, timeToMinutes, isWithinBookingWindow
} = require('./lib/availability');
const { safeEqual } = require('./lib/hmacUtil');
const PaymobGateway = require('./lib/gateways/paymob');
const {
    reserveSlot, attachPaymobIntention, abandonPendingBooking,
    expireStalePending
} = require('./lib/bookings');
const { getActiveServices, getServiceById, isPaidService, toPublicService } = require('./lib/services');
const { getRulesByDay, getBlockedDateKeys, getGeneralSettings } = require('./lib/settings');
const { finalizeConfirmedBooking } = require('./lib/confirmation');
const { setEmailCapture } = require('./lib/email');
const { handlePaymentEvent } = require('./lib/payments');
const { toPublicStatus } = require('./lib/bookingStatus');
const MockGateway = require('./lib/gateways/mock');
const { checkAndRecord, clientIpFromForwardedFor } = require('./lib/rateLimit');
const { computeSlotsByDate } = require('./lib/slots');
const manage = require('./lib/manage');
const { isAdminToken, bearerToken } = require('./lib/adminAuth');

const PAYMOB_EVENTS_TOPIC = 'paymob-payment-events';

// Pinned explicitly (rather than relying on the implicit default) because
// staying in us-central1/us-east1/us-west1 is what keeps this inside GCP's
// Always Free tier — a deliberate choice made earlier in this project over
// deploying to the Dammam (me-central2) region. Don't change this without
// re-checking that trade-off.
const REGION = 'us-central1';

// True ONLY inside the local Firebase emulator (the emulator sets it; a
// deployed function never has it). Everything demo-related is gated on this.
const IS_EMULATOR = process.env.FUNCTIONS_EMULATOR === 'true';

if (IS_EMULATOR) {
    // Local demo: emails that would go to Resend (while its key is still the
    // placeholder) are kept in Firestore instead, so the demo mailbox page can
    // show exactly what customers and the owner would receive.
    setEmailCapture(async function (message) {
        await db.collection('demoOutbox').add(Object.assign({ sentAt: new Date() }, message));
    });
}

// options: { origins, headers } — the admin endpoint allows the admin panel's
// origin and the Authorization header; everything else allows the public site.
function applyCors(req, res, options) {
    const origins = (options && options.origins) || ALLOWED_ORIGINS;
    const origin = req.headers.origin;
    if (origin && origins.indexOf(origin) !== -1) {
        res.set('Access-Control-Allow-Origin', origin);
    }
    res.set('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.set('Access-Control-Allow-Headers', (options && options.headers) || 'Content-Type');
}

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MOBILE_PATTERN = /^[+\d][\d\s-]{6,20}$/;
const MAX_NAME_LENGTH = 100;
const MAX_EMAIL_LENGTH = 254;

// ---------------------------------------------------------------------------
// listServices — GET /listServices
// ---------------------------------------------------------------------------
exports.listServices = onRequest({ region: REGION }, async function (req, res) {
    applyCors(req, res);
    if (req.method === 'OPTIONS') { res.status(204).send(''); return; }
    if (req.method !== 'GET') { res.status(405).json({ error: 'Method not allowed' }); return; }

    try {
        const services = await getActiveServices(db);
        res.set('Cache-Control', 'no-store'); // admin edits must show up immediately
        res.status(200).json({ services: services.map(toPublicService), currency: CURRENCY });
    } catch (err) {
        logger.error('listServices failed', err);
        res.status(500).json({ error: 'Internal error' });
    }
});

// ---------------------------------------------------------------------------
// availableSlots — GET /availableSlots?serviceId=...
// ---------------------------------------------------------------------------
exports.availableSlots = onRequest({ region: REGION }, async function (req, res) {
    applyCors(req, res);
    if (req.method === 'OPTIONS') { res.status(204).send(''); return; }
    if (req.method !== 'GET') { res.status(405).json({ error: 'Method not allowed' }); return; }

    try {
        const service = await getServiceById(db, String(req.query.serviceId || ''));
        if (!service || !service.active) { res.status(404).json({ error: 'Unknown service.' }); return; }

        const slotsByDate = await computeSlotsByDate(db, { durationMinutes: service.durationMinutes });

        res.set('Cache-Control', 'no-store');
        res.status(200).json({ slotsByDate });
    } catch (err) {
        logger.error('availableSlots failed', err);
        res.status(500).json({ error: 'Internal error' });
    }
});

// ---------------------------------------------------------------------------
// bookingRequest — POST /bookingRequest
//   { serviceId, slotId, date, name, email, mobile }
// Responds { confirmed: true, ... } for a free service, or { checkoutUrl }
// for a paid one.
// ---------------------------------------------------------------------------
exports.bookingRequest = onRequest(
    { region: REGION, secrets: [paymobSecretKey, paymobPublicKey, resendApiKey, googleCalendarKey] },
    async function (req, res) {
        applyCors(req, res);
        if (req.method === 'OPTIONS') { res.status(204).send(''); return; }
        if (req.method !== 'POST') { res.status(405).json({ error: 'Method not allowed' }); return; }

        try {
            const ip = clientIpFromForwardedFor(req.headers['x-forwarded-for'], req.ip);
            const allowed = await checkAndRecord(db, ip);
            if (!allowed) { res.status(429).json({ error: 'Too many requests, please try again later.' }); return; }

            const body = req.body || {};
            const dateKey = String(body.date || '');
            const slotId = String(body.slotId || '');
            const name = String(body.name || '').trim();
            const email = String(body.email || '').trim();
            const mobile = String(body.mobile || '').trim();

            if (!name || name.length > MAX_NAME_LENGTH) { res.status(400).json({ error: 'Please enter your name (up to ' + MAX_NAME_LENGTH + ' characters).' }); return; }
            if (email.length > MAX_EMAIL_LENGTH || !EMAIL_PATTERN.test(email)) { res.status(400).json({ error: 'Valid email is required.' }); return; }
            if (!MOBILE_PATTERN.test(mobile)) { res.status(400).json({ error: 'Valid mobile number is required.' }); return; }

            // The service, not the client, decides duration and price.
            const service = await getServiceById(db, String(body.serviceId || ''));
            if (!service || !service.active) { res.status(400).json({ error: 'Unknown service.' }); return; }

            // slotId is `${date}-${HH:mm}`; require it to agree with `date`.
            const prefix = dateKey + '-';
            const time = slotId.startsWith(prefix) ? slotId.slice(prefix.length) : '';
            const startMinutes = timeToMinutes(time);

            if (!isValidDateKey(dateKey) || isNaN(startMinutes) || !isWithinBookingWindow(dateKey, BOOKING_WINDOW_DAYS)) {
                res.status(400).json({ error: 'Invalid or unavailable date.' }); return;
            }

            const [rulesByDay, blockedDateKeys, settings] = await Promise.all([getRulesByDay(db), getBlockedDateKeys(db), getGeneralSettings(db)]);
            const breakMinutes = settings.breakMinutes;
            if (!isValidSlotForDate(dateKey, time, service.durationMinutes, rulesByDay, blockedDateKeys, breakMinutes)) {
                res.status(400).json({ error: 'Invalid or unknown slot.' }); return;
            }

            const isPaid = isPaidService(service);

            let booking;
            try {
                booking = await reserveSlot(db, {
                    dateKey,
                    time,
                    startMinutes,
                    durationMinutes: service.durationMinutes,
                    service,
                    customerName: name,
                    customerEmail: email,
                    customerMobile: mobile,
                    isPaid,
                    breakMinutes,
                    amount: service.amount,
                    currency: CURRENCY
                });
            } catch (err) {
                if (err.message === 'SLOT_TAKEN') { res.status(409).json({ error: 'That time was just taken. Please pick another.' }); return; }
                throw err;
            }

            // ---- Free service: confirmed already, no payment step at all ----
            if (!isPaid) {
                const stored = await db.collection('bookings').doc(booking.id).get();
                const confirmedBooking = Object.assign({ id: booking.id }, stored.data());
                const { meetLink, manageUrl } = await finalizeConfirmedBooking(db, confirmedBooking, resendApiKey.value(), googleCalendarKey.value());
                res.status(200).json({ confirmed: true, bookingId: booking.id, meetLink, manageUrl });
                return;
            }

            // ---- Paid service: hand off to the payment gateway ----
            // Each v2 function has its own host, so req.hostname is NOT where the
            // webhook lives; use the project's fixed functions URL instead.
            const functionsBaseUrl = 'https://' + REGION + '-' + process.env.GCLOUD_PROJECT + '.cloudfunctions.net';
            try {
                // Local demo: with placeholder Paymob keys (emulator only) the
                // customer is sent to the fake checkout page instead.
                const useDemoCheckout = IS_EMULATOR && paymobSecretKey.value() === 'placeholder';
                const demoOrigin = ALLOWED_ORIGINS.indexOf(req.headers.origin) !== -1 ? req.headers.origin : ALLOWED_ORIGINS[ALLOWED_ORIGINS.length - 1];
                const gateway = useDemoCheckout
                    ? new MockGateway({ checkoutBase: demoOrigin + '/demo/checkout.html' })
                    : new PaymobGateway({
                        secretKey: paymobSecretKey.value(),
                        publicKey: paymobPublicKey.value()
                    });
                const { checkoutUrl, providerReference } = await gateway.createCheckout({
                    amount: service.amount,
                    currency: CURRENCY,
                    orderId: booking.id,
                    customer: { name, email, mobile },
                    notificationUrl: functionsBaseUrl + '/paymobWebhookIngest',
                    redirectUrl: PUBLIC_SITE_ORIGIN + '/pages/book-a-session.html?bookingId=' + booking.id
                });

                await attachPaymobIntention(db, booking.id, providerReference);
                res.status(200).json({ checkoutUrl });
            } catch (err) {
                // The customer never reached checkout — don't leave their
                // slot locked for the rest of the hold window.
                await abandonPendingBooking(db, booking.id).catch(function () {});
                throw err;
            }
        } catch (err) {
            logger.error('bookingRequest failed', err);
            res.status(500).json({ error: 'Something went wrong. Please try again.' });
        }
    }
);

// ---------------------------------------------------------------------------
// paymobWebhookIngest — POST, called by Paymob. Verifies HMAC, then relays
// the verified event to Pub/Sub. Never touches Firestore directly (see the
// least-privilege rationale in the architecture plan).
// ---------------------------------------------------------------------------
exports.paymobWebhookIngest = onRequest(
    { region: REGION, secrets: [paymobHmacSecret, paymobWebhookSharedSecret] },
    async function (req, res) {
        if (req.method !== 'POST') { res.status(405).send('Method not allowed'); return; }

        // Our own shared secret, configured as a custom header in Paymob's
        // portal — the one check that proves this call came from the webhook
        // we configured, not an arbitrary caller of this public URL.
        const providedSharedSecret = req.headers['x-webhook-secret'];
        if (!providedSharedSecret || !safeEqual(providedSharedSecret, paymobWebhookSharedSecret.value())) {
            logger.warn('paymobWebhookIngest: missing/incorrect shared secret header');
            res.status(401).send('Unauthorized');
            return;
        }

        // Deliberately constructed with ONLY the HMAC secret — this function
        // never holds the payment-creation secretKey/publicKey, so even if
        // this internet-facing endpoint were compromised, it can't forge a
        // real Paymob payment session, only verify or reject callbacks.
        const gateway = new PaymobGateway({ hmacSecret: paymobHmacSecret.value() });
        const { verified, event } = gateway.verifyWebhook({ body: req.body, query: req.query });
        if (!verified) {
            logger.warn('paymobWebhookIngest: HMAC verification failed', { hasBody: !!req.body });
            res.status(401).send('Invalid signature');
            return;
        }

        try {
            const messageBuffer = Buffer.from(JSON.stringify(event));
            await pubsub.topic(PAYMOB_EVENTS_TOPIC).publishMessage({ data: messageBuffer });
            res.status(200).send('OK');
        } catch (err) {
            logger.error('paymobWebhookIngest: failed to publish event', err);
            // Still nothing Firestore-side happened — safe to let Paymob retry.
            res.status(500).send('Internal error');
        }
    }
);

// ---------------------------------------------------------------------------
// processPaymobEvent — Pub/Sub-triggered worker, not internet-facing.
// ---------------------------------------------------------------------------
exports.processPaymobEvent = onMessagePublished(
    { region: REGION, topic: PAYMOB_EVENTS_TOPIC, secrets: [resendApiKey, googleCalendarKey] },
    async function (cloudEvent) {
        // Already the normalized { orderId, providerTransactionId, status,
        // amount, currency, raw } shape from PaymobGateway.verifyWebhook —
        // paymobWebhookIngest publishes the normalized event, not Paymob's
        // raw payload, specifically so this function stays gateway-agnostic
        // and would work unchanged if the active gateway were ever swapped.
        await handlePaymentEvent(db, cloudEvent.data.message.json, {
            resendApiKey: resendApiKey.value(),
            calendarKey: googleCalendarKey.value()
        });
    }
);

// ---------------------------------------------------------------------------
// Managing a booking — the page a customer reaches from the "Manage my
// booking" link in their email. The private token in that link (48 hex
// characters, unguessable) is the only credential; there are no accounts.
//   GET  manageBooking     ?bookingId&token          the booking, and what can be done
//   GET  manageSlots       ?bookingId&token          free times for rescheduling
//   POST manageCancel      { bookingId, token }      cancel (+ refund if paid)
//   POST manageReschedule  { bookingId, token, date, slotId }
// Every wrong id/token gets the same generic 404, and repeated wrong guesses
// from one address are rate-limited.
// ---------------------------------------------------------------------------
const BOOKING_ID_PATTERN = /^[A-Za-z0-9]{10,40}$/;
const MANAGE_TOKEN_PATTERN = /^[a-f0-9]{48}$/;

// Gateway used to refund. In the local emulator with the placeholder Paymob key
// (no real account yet) refunds go to the fake gateway.
function buildRefundGateway() {
    const key = paymobSecretKey.value();
    if (IS_EMULATOR && key === 'placeholder') return new MockGateway({ checkoutBase: '' });
    return new PaymobGateway({ secretKey: key });
}

// Returns the booking if the request carries a valid id + token; otherwise
// answers the request itself (404, or 429 for repeated bad guesses) and
// returns null.
async function loadManagedBooking(req, res) {
    const source = req.method === 'GET' ? (req.query || {}) : (req.body || {});
    const bookingId = String(source.bookingId || '');
    const token = String(source.token || '');

    if (BOOKING_ID_PATTERN.test(bookingId) && MANAGE_TOKEN_PATTERN.test(token)) {
        const snap = await db.collection('bookings').doc(bookingId).get();
        if (snap.exists && manage.tokenMatches(snap.data(), token)) {
            return Object.assign({ id: bookingId }, snap.data());
        }
    }

    const ip = clientIpFromForwardedFor(req.headers['x-forwarded-for'], req.ip);
    const allowed = await checkAndRecord(db, 'manage-fail-' + ip);
    if (allowed) res.status(404).json({ error: 'This link is not valid.' });
    else res.status(429).json({ error: 'Too many attempts. Please try again later.' });
    return null;
}

// Common start of the four customer endpoints. Returns the booking, or null
// after replying (preflight, wrong method, bad link).
async function startManageRequest(req, res, method) {
    applyCors(req, res);
    res.set('Cache-Control', 'no-store');
    if (req.method === 'OPTIONS') { res.status(204).send(''); return null; }
    if (req.method !== method) { res.status(405).json({ error: 'Method not allowed' }); return null; }
    return loadManagedBooking(req, res);
}

exports.manageBooking = onRequest({ region: REGION }, async function (req, res) {
    try {
        const booking = await startManageRequest(req, res, 'GET');
        if (!booking) return;
        res.status(200).json(manage.toManageView(booking, new Date()));
    } catch (err) {
        logger.error('manageBooking failed', err);
        res.status(500).json({ error: 'Internal error' });
    }
});

exports.manageSlots = onRequest({ region: REGION }, async function (req, res) {
    try {
        const booking = await startManageRequest(req, res, 'GET');
        if (!booking) return;
        if (!manage.isChangeable(booking, new Date())) { res.status(409).json({ error: 'This booking can no longer be changed.' }); return; }

        const view = manage.toManageView(booking, new Date());
        const slotsByDate = await computeSlotsByDate(db, { durationMinutes: view.durationMinutes, excludeBookingId: booking.id });
        res.status(200).json({ slotsByDate: slotsByDate, durationMinutes: view.durationMinutes });
    } catch (err) {
        logger.error('manageSlots failed', err);
        res.status(500).json({ error: 'Internal error' });
    }
});

// Shared by the customer and admin cancel endpoints.
function sendCancelResult(res, result) {
    if (result.outcome === 'cancelled' || result.outcome === 'already_cancelled') {
        res.status(200).json({ outcome: result.outcome, refundAmount: result.refundAmount, refundStatus: result.refundStatus });
    } else if (result.outcome === 'not_found') {
        res.status(404).json({ error: 'Booking not found.', outcome: result.outcome });
    } else if (result.outcome === 'too_late') {
        res.status(409).json({ error: 'This session has already started, so it can no longer be cancelled.', outcome: result.outcome });
    } else {
        res.status(409).json({ error: 'This booking can\'t be cancelled.', outcome: result.outcome });
    }
}

exports.manageCancel = onRequest(
    { region: REGION, secrets: [paymobSecretKey, resendApiKey, googleCalendarKey] },
    async function (req, res) {
        try {
            const booking = await startManageRequest(req, res, 'POST');
            if (!booking) return;
            const result = await manage.cancelBooking(db, booking.id, {
                by: 'customer',
                refundGateway: buildRefundGateway(),
                calendarKey: googleCalendarKey.value(),
                resendApiKey: resendApiKey.value()
            });
            sendCancelResult(res, result);
        } catch (err) {
            logger.error('manageCancel failed', err);
            res.status(500).json({ error: 'Something went wrong. Please try again.' });
        }
    }
);

exports.manageReschedule = onRequest(
    { region: REGION, secrets: [resendApiKey, googleCalendarKey] },
    async function (req, res) {
        try {
            const booking = await startManageRequest(req, res, 'POST');
            if (!booking) return;

            const dateKey = String((req.body || {}).date || '');
            const slotId = String((req.body || {}).slotId || '');
            const prefix = dateKey + '-';
            const time = slotId.startsWith(prefix) ? slotId.slice(prefix.length) : '';

            const result = await manage.rescheduleBooking(db, booking.id, {
                dateKey: dateKey,
                time: time,
                calendarKey: googleCalendarKey.value(),
                resendApiKey: resendApiKey.value()
            });

            if (result.outcome === 'rescheduled') {
                res.status(200).json({
                    outcome: 'rescheduled',
                    slotDateLabel: result.view.slotDateLabel,
                    slotTimeLabel: result.view.slotTimeLabel,
                    meetLink: result.view.meetLink
                });
            } else if (result.outcome === 'slot_taken') {
                res.status(409).json({ error: 'That time was just taken. Please pick another.', outcome: result.outcome });
            } else if (result.outcome === 'invalid_slot' || result.outcome === 'same_slot') {
                res.status(400).json({ error: result.outcome === 'same_slot' ? 'That is already your booked time.' : 'Invalid or unavailable time.', outcome: result.outcome });
            } else {
                res.status(409).json({ error: 'This booking can no longer be changed.', outcome: result.outcome });
            }
        } catch (err) {
            logger.error('manageReschedule failed', err);
            res.status(500).json({ error: 'Something went wrong. Please try again.' });
        }
    }
);

// ---------------------------------------------------------------------------
// adminCancelBooking — POST { bookingId }, the admin panel's Cancel button.
// Needs a Firebase sign-in token (Authorization: Bearer ...); the person must
// have a verified email on the admin list (the same rule as firestore.rules).
// Cancellation involves a refund and emails, so it can't be a plain database
// write from the browser.
// ---------------------------------------------------------------------------
exports.adminCancelBooking = onRequest(
    { region: REGION, secrets: [paymobSecretKey, resendApiKey, googleCalendarKey] },
    async function (req, res) {
        applyCors(req, res, { origins: ADMIN_ORIGINS, headers: 'Content-Type, Authorization' });
        res.set('Cache-Control', 'no-store');
        if (req.method === 'OPTIONS') { res.status(204).send(''); return; }
        if (req.method !== 'POST') { res.status(405).json({ error: 'Method not allowed' }); return; }

        try {
            const idToken = bearerToken(req.headers.authorization);
            if (!idToken) { res.status(401).json({ error: 'Sign in required.' }); return; }

            let decoded;
            try {
                decoded = await admin.auth().verifyIdToken(idToken);
            } catch (err) {
                res.status(401).json({ error: 'Sign in required.' }); return;
            }
            if (!isAdminToken(decoded, ADMIN_EMAILS)) { res.status(403).json({ error: 'Not allowed.' }); return; }

            const bookingId = String((req.body || {}).bookingId || '');
            if (!BOOKING_ID_PATTERN.test(bookingId)) { res.status(400).json({ error: 'Invalid booking.' }); return; }

            const result = await manage.cancelBooking(db, bookingId, {
                by: 'admin',
                refundGateway: buildRefundGateway(),
                calendarKey: googleCalendarKey.value(),
                resendApiKey: resendApiKey.value()
            });
            sendCancelResult(res, result);
        } catch (err) {
            logger.error('adminCancelBooking failed', err);
            res.status(500).json({ error: 'Something went wrong. Please try again.' });
        }
    }
);

// ---------------------------------------------------------------------------
// bookingStatus — GET ?bookingId=...  What the page shown after payment needs:
// is this booking confirmed yet? Only non-personal fields are returned (see
// lib/bookingStatus.js); the 20-character random id is the only key.
// ---------------------------------------------------------------------------
exports.bookingStatus = onRequest({ region: REGION }, async function (req, res) {
    applyCors(req, res);
    res.set('Cache-Control', 'no-store');
    if (req.method === 'OPTIONS') { res.status(204).send(''); return; }
    if (req.method !== 'GET') { res.status(405).json({ error: 'Method not allowed' }); return; }

    const bookingId = String(req.query.bookingId || '');
    if (!/^[A-Za-z0-9]{10,40}$/.test(bookingId)) { res.status(400).json({ error: 'Invalid booking.' }); return; }

    try {
        const snap = await db.collection('bookings').doc(bookingId).get();
        if (!snap.exists) { res.status(404).json({ error: 'Booking not found.' }); return; }
        res.status(200).json(toPublicStatus(snap.data()));
    } catch (err) {
        logger.error('bookingStatus failed', err);
        res.status(500).json({ error: 'Internal error' });
    }
});

// ---------------------------------------------------------------------------
// LOCAL DEMO ONLY — these two functions are only defined inside the emulator,
// so they do not exist in a deployed project.
//   demoCompletePayment  POST { bookingId, outcome: 'paid' | 'declined' }
//   demoMailbox          GET   the captured emails, newest first
// ---------------------------------------------------------------------------
if (IS_EMULATOR) {
    exports.demoCompletePayment = onRequest(
        { region: REGION, secrets: [resendApiKey, googleCalendarKey] },
        async function (req, res) {
            applyCors(req, res);
            if (req.method === 'OPTIONS') { res.status(204).send(''); return; }
            if (req.method !== 'POST') { res.status(405).json({ error: 'Method not allowed' }); return; }

            const bookingId = String((req.body || {}).bookingId || '');
            const outcome = String((req.body || {}).outcome || '');
            if (!/^[A-Za-z0-9]{10,40}$/.test(bookingId) || (outcome !== 'paid' && outcome !== 'declined')) {
                res.status(400).json({ error: 'bookingId and outcome (paid | declined) required.' }); return;
            }

            try {
                if (outcome === 'declined') {
                    // A real declined payment just never confirms; the hourly sweep
                    // frees the slot. The demo frees it straight away.
                    await abandonPendingBooking(db, bookingId);
                    res.status(200).json({ result: 'declined' });
                    return;
                }
                const result = await handlePaymentEvent(db, {
                    orderId: bookingId, providerTransactionId: 'demo-txn-' + Date.now(), status: 'paid'
                }, { resendApiKey: resendApiKey.value(), calendarKey: googleCalendarKey.value() });
                res.status(200).json({ result: result });
            } catch (err) {
                logger.error('demoCompletePayment failed', err);
                res.status(500).json({ error: 'Internal error' });
            }
        }
    );

    exports.demoMailbox = onRequest({ region: REGION }, async function (req, res) {
        applyCors(req, res);
        res.set('Cache-Control', 'no-store');
        if (req.method === 'OPTIONS') { res.status(204).send(''); return; }
        try {
            const snap = await db.collection('demoOutbox').orderBy('sentAt', 'desc').limit(50).get();
            res.status(200).json({ emails: snap.docs.map(function (d) {
                const x = d.data();
                return { id: d.id, to: x.to, subject: x.subject, html: x.html, sentAt: x.sentAt.toDate().toISOString() };
            }) });
        } catch (err) {
            logger.error('demoMailbox failed', err);
            res.status(500).json({ error: 'Internal error' });
        }
    });
}

// ---------------------------------------------------------------------------
// expirePendingBookings — hourly sweep
// ---------------------------------------------------------------------------
exports.expirePendingBookings = onSchedule({ region: REGION, schedule: 'every 60 minutes' }, async function () {
    const count = await expireStalePending(db);
    if (count > 0) logger.info('expirePendingBookings: expired ' + count + ' stale booking(s)');
});
