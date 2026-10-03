// Tests firestore.rules against the real Firestore emulator.
// Run from this folder:  npm test   (needs Java on PATH — the emulator is a Java program)
//
// The rules ship with a placeholder admin email. To exercise the REAL rules
// logic, the test swaps in its own test emails and loads the resulting text —
// the only thing changed is the address list.

const fs = require('fs');
const path = require('path');
const { initializeTestEnvironment, assertSucceeds, assertFails } = require('@firebase/rules-unit-testing');
const { doc, getDoc, getDocs, setDoc, updateDoc, deleteDoc, collection, writeBatch, setLogLevel } = require('firebase/firestore');

// Denials are the point of most of these tests; don't let the SDK log each one.
setLogLevel('silent');

// RULES_PATH lets a deliberately-broken copy of the rules be tested, to prove
// this suite really fails when the rules are wrong (mutation check).
const rulesPath = process.env.RULES_PATH || path.join(__dirname, '..', 'firestore.rules');
const shippedRules = fs.readFileSync(rulesPath, 'utf8');

// The real admin list is swapped for test accounts, so these tests don't depend
// on which real emails are in firestore.rules.
const ADMIN_LIST = /(request\.auth\.token\.email\.lower\(\) in \[)[^\]]*(\])/;

if (!ADMIN_LIST.test(shippedRules)) {
    console.error('The admin email list was not found in firestore.rules — update this test.');
    process.exit(2);
}

const testRules = shippedRules.replace(ADMIN_LIST, "$1'admin@test.example', 'second@test.example'$2");

let pass = 0;
let fail = 0;

async function expectOk(name, promise) {
    try { await assertSucceeds(promise); pass++; } catch (e) { fail++; console.log('FAIL (should be allowed):', name, '-', e.message.split('\n')[0]); }
}
async function expectDenied(name, promise) {
    try { await assertFails(promise); pass++; } catch (e) { fail++; console.log('FAIL (should be denied):', name, '-', e.message.split('\n')[0]); }
}

const goodService = { name: 'Business Review', description: 'A review.', durationMinutes: 60, amount: 50000, active: true, sortOrder: 1 };
const goodRule = { dayOfWeek: 0, startTime: '09:00', endTime: '17:00' };

async function run() {
    const env = await initializeTestEnvironment({ projectId: 'demo-booking', firestore: { rules: testRules } });
    const withData = function (fn) { return env.withSecurityRulesDisabled(async function (ctx) { await fn(ctx.firestore()); }); };

    const anon = env.unauthenticatedContext().firestore();
    const stranger = env.authenticatedContext('u1', { email: 'stranger@test.example', email_verified: true }).firestore();
    const unverified = env.authenticatedContext('u2', { email: 'admin@test.example', email_verified: false }).firestore();
    const noEmail = env.authenticatedContext('u4', { email_verified: true }).firestore();
    const admin = env.authenticatedContext('u3', { email: 'admin@test.example', email_verified: true }).firestore();
    const admin2 = env.authenticatedContext('u5', { email: 'second@test.example', email_verified: true }).firestore();
    const adminMixedCase = env.authenticatedContext('u6', { email: 'Admin@Test.Example', email_verified: true }).firestore();

    await env.withSecurityRulesDisabled(async function (ctx) {
        const db = ctx.firestore();
        await setDoc(doc(db, 'services', 's1'), goodService);
        await setDoc(doc(db, 'availabilityRules', 'r1'), goodRule);
        await setDoc(doc(db, 'blockedDates', '2026-10-03'), { reason: 'Holiday' });
        await setDoc(doc(db, 'settings', 'general'), { meetLink: 'https://meet.google.com/abc-defg-hij' });
        await setDoc(doc(db, 'bookings', 'b-confirmed'), { status: 'confirmed', customerName: 'A', isPaid: true, amount: 50000 });
        await setDoc(doc(db, 'bookings', 'b-pending'), { status: 'pending_payment', customerName: 'B' });
        await setDoc(doc(db, 'bookings', 'b-cancelled'), { status: 'cancelled', customerName: 'C' });
        await setDoc(doc(db, 'rateLimits', '1.2.3.4'), { count: 1, windowStart: 1 });
    });

    // ---------------- who is an admin ----------------
    await expectDenied('anonymous cannot read services', getDoc(doc(anon, 'services', 's1')));
    await expectDenied('anonymous cannot read bookings', getDoc(doc(anon, 'bookings', 'b-confirmed')));
    await expectDenied('anonymous cannot list bookings', getDocs(collection(anon, 'bookings')));
    await expectDenied('signed-in stranger cannot read services', getDoc(doc(stranger, 'services', 's1')));
    await expectDenied('signed-in stranger cannot read bookings', getDoc(doc(stranger, 'bookings', 'b-confirmed')));
    await expectDenied('stranger cannot write services', setDoc(doc(stranger, 'services', 'x'), goodService));
    await expectDenied('admin email but UNVERIFIED cannot read', getDoc(doc(unverified, 'services', 's1')));
    await expectDenied('admin email but UNVERIFIED cannot write', setDoc(doc(unverified, 'services', 'x'), goodService));
    await expectDenied('token without an email claim is denied (and does not crash)', getDoc(doc(noEmail, 'services', 's1')));
    await expectOk('verified admin can read services', getDoc(doc(admin, 'services', 's1')));
    await expectOk('second listed admin can read services', getDoc(doc(admin2, 'services', 's1')));
    await expectOk('email match is case-insensitive', getDoc(doc(adminMixedCase, 'services', 's1')));
    await expectOk('admin can list bookings', getDocs(collection(admin, 'bookings')));
    await expectOk('admin can read a document that does not exist yet (settings on first run)', getDoc(doc(admin, 'settings', 'not-created-yet')));
    await expectDenied('a stranger is refused even for a document that does not exist (no probing)', getDoc(doc(stranger, 'settings', 'not-created-yet')));

    // ---------------- services ----------------
    await expectOk('admin creates a valid service', setDoc(doc(admin, 'services', 'new1'), goodService));
    await expectOk('admin can create a FREE service (amount 0)', setDoc(doc(admin, 'services', 'free1'), Object.assign({}, goodService, { amount: 0 })));
    await expectOk('admin updates a service', updateDoc(doc(admin, 'services', 's1'), { amount: 60000 }));
    await expectOk('admin deletes a service', deleteDoc(doc(admin, 'services', 'new1')));
    await expectDenied('service: negative price', setDoc(doc(admin, 'services', 'bad'), Object.assign({}, goodService, { amount: -1 })));
    await expectDenied('service: fractional price', setDoc(doc(admin, 'services', 'bad'), Object.assign({}, goodService, { amount: 10.5 })));
    await expectDenied('service: price as string', setDoc(doc(admin, 'services', 'bad'), Object.assign({}, goodService, { amount: '500' })));
    await expectDenied('service: zero duration', setDoc(doc(admin, 'services', 'bad'), Object.assign({}, goodService, { durationMinutes: 0 })));
    await expectDenied('service: absurd duration', setDoc(doc(admin, 'services', 'bad'), Object.assign({}, goodService, { durationMinutes: 9999 })));
    await expectDenied('service: empty name', setDoc(doc(admin, 'services', 'bad'), Object.assign({}, goodService, { name: '' })));
    await expectDenied('service: missing name', setDoc(doc(admin, 'services', 'bad'), { description: '', durationMinutes: 60, amount: 0, active: true, sortOrder: 1 }));
    await expectDenied('service: unexpected extra field', setDoc(doc(admin, 'services', 'bad'), Object.assign({}, goodService, { isAdmin: true })));
    await expectDenied('service: active not a boolean', setDoc(doc(admin, 'services', 'bad'), Object.assign({}, goodService, { active: 'yes' })));

    // ---------------- working hours ----------------
    await expectOk('admin creates a valid rule', setDoc(doc(admin, 'availabilityRules', 'r2'), { dayOfWeek: 3, startTime: '09:30', endTime: '13:00' }));
    await expectOk('admin deletes a rule', deleteDoc(doc(admin, 'availabilityRules', 'r1')));
    await expectDenied('rule: end before start', setDoc(doc(admin, 'availabilityRules', 'bad'), { dayOfWeek: 1, startTime: '17:00', endTime: '09:00' }));
    await expectDenied('rule: start equals end', setDoc(doc(admin, 'availabilityRules', 'bad'), { dayOfWeek: 1, startTime: '09:00', endTime: '09:00' }));
    await expectDenied('rule: day 7', setDoc(doc(admin, 'availabilityRules', 'bad'), { dayOfWeek: 7, startTime: '09:00', endTime: '17:00' }));
    await expectDenied('rule: day -1', setDoc(doc(admin, 'availabilityRules', 'bad'), { dayOfWeek: -1, startTime: '09:00', endTime: '17:00' }));
    await expectDenied('rule: time 24:00', setDoc(doc(admin, 'availabilityRules', 'bad'), { dayOfWeek: 1, startTime: '09:00', endTime: '24:00' }));
    await expectDenied('rule: time without leading zero', setDoc(doc(admin, 'availabilityRules', 'bad'), { dayOfWeek: 1, startTime: '9:00', endTime: '17:00' }));
    await expectDenied('rule: garbage time', setDoc(doc(admin, 'availabilityRules', 'bad'), { dayOfWeek: 1, startTime: 'nine', endTime: '17:00' }));
    await expectDenied('rule: extra field', setDoc(doc(admin, 'availabilityRules', 'bad'), { dayOfWeek: 1, startTime: '09:00', endTime: '17:00', note: 'x' }));
    await expectDenied('stranger cannot write rules', setDoc(doc(stranger, 'availabilityRules', 'x'), goodRule));

    // atomic replace of the whole weekly schedule (what the admin panel does)
    {
        const batch = writeBatch(admin);
        batch.delete(doc(admin, 'availabilityRules', 'r2'));
        batch.set(doc(admin, 'availabilityRules', 'day-0-0'), { dayOfWeek: 0, startTime: '09:00', endTime: '12:00' });
        batch.set(doc(admin, 'availabilityRules', 'day-0-1'), { dayOfWeek: 0, startTime: '13:00', endTime: '17:00' });
        await expectOk('admin replaces the schedule in one batch', batch.commit());
    }
    {
        const batch = writeBatch(admin);
        batch.set(doc(admin, 'availabilityRules', 'ok-in-batch'), goodRule);
        batch.set(doc(admin, 'availabilityRules', 'bad-in-batch'), { dayOfWeek: 1, startTime: '17:00', endTime: '09:00' });
        await expectDenied('a batch containing one invalid rule is rejected as a whole', batch.commit());
    }

    // ---------------- blocked dates ----------------
    await expectOk('admin blocks a date with a reason', setDoc(doc(admin, 'blockedDates', '2026-12-25'), { reason: 'Holiday' }));
    await expectOk('admin blocks a date without a reason', setDoc(doc(admin, 'blockedDates', '2026-12-26'), {}));
    await expectOk('admin unblocks a date', deleteDoc(doc(admin, 'blockedDates', '2026-10-03')));
    await expectDenied('blocked date: malformed id', setDoc(doc(admin, 'blockedDates', 'christmas'), { reason: 'x' }));
    await expectDenied('blocked date: id without zero padding', setDoc(doc(admin, 'blockedDates', '2026-1-5'), {}));
    await expectDenied('blocked date: extra field', setDoc(doc(admin, 'blockedDates', '2026-12-27'), { reason: 'x', other: 1 }));
    await expectDenied('blocked date: reason not a string', setDoc(doc(admin, 'blockedDates', '2026-12-27'), { reason: 5 }));
    await expectDenied('blocked date: over-long reason', setDoc(doc(admin, 'blockedDates', '2026-12-27'), { reason: 'x'.repeat(201) }));
    await expectDenied('stranger cannot block dates', setDoc(doc(stranger, 'blockedDates', '2026-12-28'), {}));

    // ---------------- bookings: refund follow-up ----------------
    await withData(async function (db) {
        await setDoc(doc(db, 'bookings', 'cancelledManual'), { status: 'cancelled', refundStatus: 'needs_manual', amount: 50000, customerName: 'C' });
        await setDoc(doc(db, 'bookings', 'cancelledPending'), { status: 'cancelled', refundStatus: 'pending', amount: 50000 });
        await setDoc(doc(db, 'bookings', 'cancelledDone'), { status: 'cancelled', refundStatus: 'refunded', amount: 50000 });
        await setDoc(doc(db, 'bookings', 'cancelledNoField'), { status: 'cancelled', amount: 50000 });
        await setDoc(doc(db, 'bookings', 'conflict'), { status: 'paid_slot_conflict', amount: 50000 });
        await setDoc(doc(db, 'bookings', 'confirmedOne'), { status: 'confirmed', amount: 50000 });
    });
    await expectOk('admin marks a failed-refund cancellation as refunded by hand', updateDoc(doc(admin, 'bookings', 'cancelledManual'), { refundStatus: 'refunded_manually' }));
    await expectOk('...also when the refund was left "pending"', updateDoc(doc(admin, 'bookings', 'cancelledPending'), { refundStatus: 'refunded_manually' }));
    await expectDenied('...but not one already refunded', updateDoc(doc(admin, 'bookings', 'cancelledDone'), { refundStatus: 'refunded_manually' }));
    await expectDenied('...nor a cancelled booking with no refund record', updateDoc(doc(admin, 'bookings', 'cancelledNoField'), { refundStatus: 'refunded_manually' }));
    await expectOk('admin marks a lost-slot payment as refunded', updateDoc(doc(admin, 'bookings', 'conflict'), { status: 'refunded' }));
    await withData(async function (db) {
        await setDoc(doc(db, 'bookings', 'cancelledManual'), { status: 'cancelled', refundStatus: 'needs_manual', amount: 50000, customerName: 'C' });
        await setDoc(doc(db, 'bookings', 'conflict'), { status: 'paid_slot_conflict', amount: 50000 });
    });
    await expectDenied('a refund mark cannot also change another field', updateDoc(doc(admin, 'bookings', 'cancelledManual'), { refundStatus: 'refunded_manually', amount: 1 }));
    await expectDenied('refund follow-up cannot be set to any other value', updateDoc(doc(admin, 'bookings', 'cancelledManual'), { refundStatus: 'refunded' }));
    await expectDenied('a conflict cannot be turned into confirmed', updateDoc(doc(admin, 'bookings', 'conflict'), { status: 'confirmed' }));
    await expectDenied('a conflict cannot be turned into cancelled', updateDoc(doc(admin, 'bookings', 'conflict'), { status: 'cancelled' }));
    await expectDenied('a confirmed booking cannot be cancelled straight from the browser', updateDoc(doc(admin, 'bookings', 'confirmedOne'), { status: 'cancelled' }));
    await expectDenied('a confirmed booking cannot be given a refund status', updateDoc(doc(admin, 'bookings', 'confirmedOne'), { refundStatus: 'refunded_manually' }));
    await expectDenied('a stranger cannot mark a refund', updateDoc(doc(stranger, 'bookings', 'cancelledManual'), { refundStatus: 'refunded_manually' }));

    // ---------------- settings (Meet link) ----------------
    await expectOk('admin sets an https meet link', setDoc(doc(admin, 'settings', 'general'), { meetLink: 'https://meet.google.com/xyz-abcd-efg' }));
    await expectOk('admin clears the meet link', setDoc(doc(admin, 'settings', 'general'), { meetLink: '' }));
    await expectDenied('meet link: http (not https)', setDoc(doc(admin, 'settings', 'general'), { meetLink: 'http://meet.google.com/abc' }));
    await expectDenied('meet link: javascript: url', setDoc(doc(admin, 'settings', 'general'), { meetLink: 'javascript:alert(1)' }));
    await expectDenied('meet link: not a url', setDoc(doc(admin, 'settings', 'general'), { meetLink: 'meet.google.com/abc' }));
    await expectDenied('meet link: bare https://', setDoc(doc(admin, 'settings', 'general'), { meetLink: 'https://' }));
    await expectDenied('meet link: over-long', setDoc(doc(admin, 'settings', 'general'), { meetLink: 'https://x.com/' + 'a'.repeat(400) }));
    await expectDenied('settings: some other document id', setDoc(doc(admin, 'settings', 'other'), { meetLink: '' }));
    await expectDenied('settings: extra field', setDoc(doc(admin, 'settings', 'general'), { meetLink: '', evil: true }));
    await expectOk('admin sets a 15-minute break', setDoc(doc(admin, 'settings', 'general'), { meetLink: '', breakMinutes: 15 }));
    await expectOk('break: 0 (none) and 120 (max) allowed', setDoc(doc(admin, 'settings', 'general'), { breakMinutes: 0 }).then(function () { return setDoc(doc(admin, 'settings', 'general'), { breakMinutes: 120 }); }));
    await expectOk('break can be saved on its own without the Meet link', setDoc(doc(admin, 'settings', 'general'), { breakMinutes: 30 }));
    await expectOk('a merge write of just the Meet link keeps working alongside the break', setDoc(doc(admin, 'settings', 'general'), { meetLink: 'https://meet.google.com/abc-defg-hij' }, { merge: true }));
    await expectOk('a merge write of just the break works too', setDoc(doc(admin, 'settings', 'general'), { breakMinutes: 20 }, { merge: true }));
    await expectDenied('break: negative', setDoc(doc(admin, 'settings', 'general'), { breakMinutes: -5 }));
    await expectDenied('break: over 120', setDoc(doc(admin, 'settings', 'general'), { breakMinutes: 121 }));
    await expectDenied('break: decimal', setDoc(doc(admin, 'settings', 'general'), { breakMinutes: 7.5 }));
    await expectDenied('break: text', setDoc(doc(admin, 'settings', 'general'), { breakMinutes: '15' }));
    await expectDenied('break: null', setDoc(doc(admin, 'settings', 'general'), { breakMinutes: null }));
    await expectDenied('a merge write cannot smuggle a bad break in', setDoc(doc(admin, 'settings', 'general'), { breakMinutes: 999 }, { merge: true }));
    await expectDenied('stranger cannot change the break', setDoc(doc(stranger, 'settings', 'general'), { breakMinutes: 0 }));
    await expectDenied('stranger cannot read settings', getDoc(doc(stranger, 'settings', 'general')));
    await expectDenied('stranger cannot change the meet link', setDoc(doc(stranger, 'settings', 'general'), { meetLink: 'https://evil.example/x' }));

    // ---------------- bookings ----------------
    await expectOk('admin marks a confirmed booking as no-show', updateDoc(doc(admin, 'bookings', 'b-confirmed'), { status: 'no_show' }));
    await expectDenied('cannot mark a pending booking as no-show', updateDoc(doc(admin, 'bookings', 'b-pending'), { status: 'no_show' }));
    await expectDenied('cannot mark a cancelled booking as no-show', updateDoc(doc(admin, 'bookings', 'b-cancelled'), { status: 'no_show' }));
    await expectDenied('a no-show booking cannot be changed again', updateDoc(doc(admin, 'bookings', 'b-confirmed'), { status: 'confirmed' }));

    await env.withSecurityRulesDisabled(async function (ctx) {
        await setDoc(doc(ctx.firestore(), 'bookings', 'b2'), { status: 'confirmed', customerName: 'D', isPaid: true, amount: 50000, manageToken: 'tok' });
    });
    await expectDenied('cannot cancel directly (must go through the cloud function)', updateDoc(doc(admin, 'bookings', 'b2'), { status: 'cancelled' }));
    await expectDenied('cannot set an arbitrary status', updateDoc(doc(admin, 'bookings', 'b2'), { status: 'refunded' }));
    await expectDenied('cannot change the price', updateDoc(doc(admin, 'bookings', 'b2'), { amount: 1 }));
    await expectDenied('cannot change the customer', updateDoc(doc(admin, 'bookings', 'b2'), { customerName: 'Someone' }));
    await expectDenied('cannot smuggle extra changes in with a no-show', updateDoc(doc(admin, 'bookings', 'b2'), { status: 'no_show', amount: 0 }));
    await expectDenied('cannot overwrite the customer\'s manage token', updateDoc(doc(admin, 'bookings', 'b2'), { manageToken: 'mine' }));
    await expectDenied('admin cannot create bookings', setDoc(doc(admin, 'bookings', 'forged'), { status: 'confirmed' }));
    await expectDenied('admin cannot delete bookings', deleteDoc(doc(admin, 'bookings', 'b2')));
    await expectDenied('stranger cannot mark no-show', updateDoc(doc(stranger, 'bookings', 'b2'), { status: 'no_show' }));
    await expectDenied('anonymous cannot mark no-show', updateDoc(doc(anon, 'bookings', 'b2'), { status: 'no_show' }));

    // ---------------- everything else stays closed ----------------
    await expectDenied('admin cannot read rateLimits', getDoc(doc(admin, 'rateLimits', '1.2.3.4')));
    await expectDenied('admin cannot write rateLimits', setDoc(doc(admin, 'rateLimits', '9.9.9.9'), { count: 0 }));
    await expectDenied('admin cannot use an unlisted collection', setDoc(doc(admin, 'somethingElse', 'x'), { a: 1 }));
    await expectDenied('admin cannot read an unlisted collection', getDoc(doc(admin, 'somethingElse', 'x')));

    await env.cleanup();

    console.log('passed:', pass, 'failed:', fail);
    process.exit(fail ? 1 : 0);
}

run().catch(function (e) { console.error('TEST CRASHED:', e); process.exit(2); });
