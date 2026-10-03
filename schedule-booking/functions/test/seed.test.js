// Run with: node test/seed.test.js
// The seed script must fill an empty project and must NEVER overwrite data
// the client has since edited.

const { createFakeDb } = require('./fakeFirestore');
const { seedIfEmpty, WORKING_DAYS } = require('../scripts/seedDefaults');
const { normalizeService } = require('../lib/services');
const { getRulesByDay, getGeneralSettings } = require('../lib/settings');

let pass = 0;
let fail = 0;
function check(name, cond) { if (cond) { pass++; } else { fail++; console.log('FAIL:', name); } }

(async function run() {
    // fresh project
    {
        const db = createFakeDb();
        const report = await seedIfEmpty(db);
        check('fresh: seeds all three areas', report.seeded.length === 3 && report.skipped.length === 0);

        const svc = normalizeService('default-session', db.raw('services', 'default-session'));
        check('seeded service is valid per the app\'s own parser', svc && svc.durationMinutes === 60 && svc.amount === 50000 && svc.active);

        const rules = await getRulesByDay(db);
        check('seeded rules are Sun-Thu 09:00-17:00 per the app\'s own reader',
            WORKING_DAYS.every(function (d) { return rules[d] && rules[d][0].startTime === '09:00' && rules[d][0].endTime === '17:00'; })
            && !rules[5] && !rules[6]);

        const settings = await getGeneralSettings(db);
        check('seeded settings readable, empty meet link', settings.meetLink === '');
    }

    // re-running is a no-op
    {
        const db = createFakeDb();
        await seedIfEmpty(db);
        const again = await seedIfEmpty(db);
        check('second run seeds nothing', again.seeded.length === 0 && again.skipped.length === 3);
    }

    // client-edited data is never overwritten
    {
        const db = createFakeDb();
        db.seed('services', 'my-own', { name: 'Custom', durationMinutes: 45, amount: 0, active: true });
        db.seed('settings', 'general', { meetLink: 'https://meet.google.com/abc-defg-hij' });
        const report = await seedIfEmpty(db);
        check('existing services left alone', !db.raw('services', 'default-session') && db.raw('services', 'my-own').name === 'Custom');
        check('existing settings left alone', db.raw('settings', 'general').meetLink === 'https://meet.google.com/abc-defg-hij');
        check('only the empty area was seeded', report.seeded.length === 1 && report.seeded[0] === 'availabilityRules');
    }

    console.log('passed:', pass, 'failed:', fail);
    process.exit(fail ? 1 : 0);
})().catch(function (e) { console.error('TEST CRASHED:', e); process.exit(2); });
