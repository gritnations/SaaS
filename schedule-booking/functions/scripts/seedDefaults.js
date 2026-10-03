// One-time bootstrap: gives a fresh deployment a working schedule so the
// booking page has something to show before the admin panel has been used.
//
// SAFE BY DESIGN: it only writes into a collection that is completely empty,
// so re-running it can never overwrite anything the client has since edited
// in the admin panel.
//
// Run (needs credentials for the Firebase project — either
// `gcloud auth application-default login`, or GOOGLE_APPLICATION_CREDENTIALS
// pointing at a service-account key, or FIRESTORE_EMULATOR_HOST for a local
// emulator):
//
//   node scripts/seedDefaults.js <firebase-project-id>
//
// What it seeds (mirrors the behavior of the original hardcoded setup):
//   services            one paid 60-minute service — EDIT THE PRICE before going live
//   availabilityRules   Sunday-Thursday, 09:00-17:00 (Fri/Sat off)
//   settings/general    empty meetLink, to be filled in from the admin panel

const DEFAULT_SERVICE = {
    name: 'Executive Advisory Session',
    description: 'A dedicated one-to-one working session.',
    durationMinutes: 60,
    amount: 50000, // SAR 500.00 in halalas — placeholder, confirm the real rate with the client
    active: true,
    sortOrder: 1
};

const WORKING_DAYS = [0, 1, 2, 3, 4]; // Sun-Thu

async function isEmpty(db, collectionName) {
    const snap = await db.collection(collectionName).get();
    return snap.empty;
}

// Returns a report of what was written vs skipped. Takes `db` so it can be
// tested against a stand-in without any real credentials.
async function seedIfEmpty(db) {
    const report = { seeded: [], skipped: [] };

    if (await isEmpty(db, 'services')) {
        await db.collection('services').doc('default-session').set(DEFAULT_SERVICE);
        report.seeded.push('services');
    } else {
        report.skipped.push('services (already has data)');
    }

    if (await isEmpty(db, 'availabilityRules')) {
        for (const day of WORKING_DAYS) {
            await db.collection('availabilityRules').doc('default-day-' + day).set({
                dayOfWeek: day,
                startTime: '09:00',
                endTime: '17:00'
            });
        }
        report.seeded.push('availabilityRules');
    } else {
        report.skipped.push('availabilityRules (already has data)');
    }

    if (await isEmpty(db, 'settings')) {
        await db.collection('settings').doc('general').set({ meetLink: '' });
        report.seeded.push('settings');
    } else {
        report.skipped.push('settings (already has data)');
    }

    return report;
}

module.exports = { seedIfEmpty, DEFAULT_SERVICE, WORKING_DAYS };

if (require.main === module) {
    const projectId = process.argv[2];
    if (!projectId) {
        console.error('Usage: node scripts/seedDefaults.js <firebase-project-id>');
        process.exit(1);
    }

    const admin = require('firebase-admin');
    admin.initializeApp({ projectId: projectId });

    seedIfEmpty(admin.firestore())
        .then(function (report) {
            console.log('Seeded:', report.seeded.length ? report.seeded.join(', ') : 'nothing');
            report.skipped.forEach(function (s) { console.log('Skipped:', s); });
            process.exit(0);
        })
        .catch(function (err) {
            console.error('Seeding failed:', err.message);
            process.exit(1);
        });
}
