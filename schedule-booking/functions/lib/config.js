// Central config: things that are genuinely deployment-level, not business
// data. Business data (services/prices/durations, weekly hours, blocked
// dates, the Meet link) lives in Firestore so the client can manage it from
// the admin panel without a code deploy — see lib/services.js and
// lib/settings.js.
//
// Secrets are declared with defineSecret so their values never live in code
// or environment files — Firebase Functions v2 pulls them from Secret
// Manager at runtime and only injects them into the functions that
// explicitly request them (see the `secrets: [...]` option in index.js).

const { defineSecret } = require('firebase-functions/params');

// How many minutes a slot stays reserved for an unpaid booking before
// expirePendingBookings frees it back up. Only applies to PAID services —
// free services confirm instantly and never hold.
const HOLD_WINDOW_MINUTES = 20;

// All services in one deployment share a currency. Amounts are stored in the
// smallest unit (halalas for SAR — same convention Paymob calls "amount_cents").
const CURRENCY = 'SAR';

// How far ahead customers can book, in days.
const BOOKING_WINDOW_DAYS = 42;

// Public site origin — used to build the customer-facing manage-booking link
// in confirmation emails.
const PUBLIC_SITE_ORIGIN = 'https://example.com';

// Session times are Saudi local time (no daylight saving, so a fixed offset is exact).
const TIMEZONE = 'Asia/Riyadh';
const TIMEZONE_OFFSET = '+03:00';

// The Google Workspace user whose calendar holds the sessions (and who owns
// the Meet rooms). The service account acts as this user.
const CALENDAR_OWNER_EMAIL = 'owner@example.com';

// Admin emails allowed to run admin actions that go through a Cloud Function
// (cancel + refund). MUST match the list in firestore.rules.
const ADMIN_EMAILS = ['owner@example.com'];

// Where the admin panel is served from (it calls adminCancelBooking).
const ADMIN_ORIGINS = [
    'https://your-project-id.web.app',
    'https://your-project-id.firebaseapp.com',
    'https://admin.example.com'
];

// Only these origins may call the HTTP endpoints.
const ALLOWED_ORIGINS = [
    'https://example.com',
    'http://localhost:8899' // local static preview only — remove before going live
];

const paymobSecretKey = defineSecret('PAYMOB_SECRET_KEY');
const paymobPublicKey = defineSecret('PAYMOB_PUBLIC_KEY');
const paymobHmacSecret = defineSecret('PAYMOB_HMAC_SECRET');
// Shared secret WE choose and configure as a custom header in the Paymob
// merchant portal's webhook settings — Paymob's callback carries no
// signature of its own beyond the HMAC over transaction fields, so this
// header is what proves a request actually came from Paymob's configured
// webhook call and not some other caller hitting this public URL.
const paymobWebhookSharedSecret = defineSecret('PAYMOB_WEBHOOK_SHARED_SECRET');
const resendApiKey = defineSecret('RESEND_API_KEY');
// Service-account key JSON (calendar-meet) used to create per-booking Meet links.
const googleCalendarKey = defineSecret('GOOGLE_CALENDAR_KEY');

module.exports = {
    ADMIN_EMAILS,
    ADMIN_ORIGINS,
    TIMEZONE,
    TIMEZONE_OFFSET,
    CALENDAR_OWNER_EMAIL,
    googleCalendarKey,
    HOLD_WINDOW_MINUTES,
    CURRENCY,
    BOOKING_WINDOW_DAYS,
    PUBLIC_SITE_ORIGIN,
    ALLOWED_ORIGINS,
    paymobSecretKey,
    paymobPublicKey,
    paymobHmacSecret,
    paymobWebhookSharedSecret,
    resendApiKey
};
