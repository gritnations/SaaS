// Run with: node test/hardening.test.js
const { clientIpFromForwardedFor, checkAndRecord } = require('../lib/rateLimit');
const { createFakeDb } = require('./fakeFirestore');
const { bookingWindowStart, dateKeyFromDate } = require('../lib/availability');

let pass = 0;
let fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.log('FAIL:', name); } }

(async function run() {
    check('single address', clientIpFromForwardedFor('203.0.113.5') === '203.0.113.5');
    check('uses the LAST entry, not the caller-supplied prefix', clientIpFromForwardedFor('1.1.1.1, 9.9.9.9, 203.0.113.5') === '203.0.113.5');
    check('trims whitespace', clientIpFromForwardedFor('  1.1.1.1 ,  203.0.113.5  ') === '203.0.113.5');
    check('falls back when header missing', clientIpFromForwardedFor(undefined, '10.0.0.1') === '10.0.0.1');
    check('unknown when nothing at all', clientIpFromForwardedFor('', undefined) === 'unknown');

    // Spoofed prefixes must all land in the same bucket and hit the limit.
    const db = createFakeDb();
    let allowed = 0;
    for (let i = 0; i < 15; i++) {
        if (await checkAndRecord(db, clientIpFromForwardedFor('fake-' + i + ', 203.0.113.5'))) allowed++;
    }
    check('rotating the fake prefix does not dodge the limit (10 allowed of 15)', allowed === 10);

    // First bookable day is tomorrow in SAUDI time, whatever the server clock says.
    const key = function (iso) { return dateKeyFromDate(bookingWindowStart(new Date(iso))); };
    check('midday UTC 23 Sep -> 24 Sep', key('2026-09-23T12:00:00Z') === '2026-09-24');
    check('00:36 Saudi (21:36 UTC, still 23 Sep in UTC) -> 25 Sep, not 24 Sep', key('2026-09-23T21:36:00Z') === '2026-09-25');
    check('02:59 Saudi is still Saudi 24 Sep -> 25 Sep', key('2026-09-23T23:59:00Z') === '2026-09-25');
    check('21:00 Saudi (18:00 UTC) -> 25 Sep', key('2026-09-24T18:00:00Z') === '2026-09-25');
    check('month rollover', key('2026-09-29T22:00:00Z') === '2026-10-01');

    console.log('passed: ' + pass + ' failed: ' + fail);
    process.exit(fail === 0 ? 0 : 1);
})().catch(function (e) { console.error(e); process.exit(1); });
