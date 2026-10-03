// Services = the bookable offerings (name, duration, price). Stored in
// Firestore so the client can add/edit them from the admin panel — this
// replaces the old single hardcoded session length and price.
//
// amount is in the smallest currency unit (halalas). amount === 0 means the
// service is FREE — bookings for it confirm instantly with no payment step,
// which is what lets one codebase serve both paid and free-booking clients.

// Normalizes one Firestore doc into a service object, or returns null if the
// doc is malformed. A hand-edited bad doc should be skipped, not take down
// the whole services list for every visitor.
function normalizeService(id, data) {
    if (!data) return null;

    const durationMinutes = Number(data.durationMinutes);
    const amount = data.amount === undefined || data.amount === null ? 0 : Number(data.amount);

    if (typeof data.name !== 'string' || !data.name.trim()) return null;
    if (!Number.isInteger(durationMinutes) || durationMinutes <= 0) return null;
    if (!Number.isInteger(amount) || amount < 0) return null;

    return {
        id: id,
        name: data.name.trim(),
        description: typeof data.description === 'string' ? data.description.trim() : '',
        durationMinutes: durationMinutes,
        amount: amount,
        active: data.active !== false, // default to active unless explicitly turned off
        sortOrder: Number.isFinite(Number(data.sortOrder)) ? Number(data.sortOrder) : 0
    };
}

function isPaidService(service) {
    return !!service && service.amount > 0;
}

async function getActiveServices(db) {
    const snap = await db.collection('services').get();
    return snap.docs
        .map(function (doc) { return normalizeService(doc.id, doc.data()); })
        .filter(function (s) { return s && s.active; })
        .sort(function (a, b) { return a.sortOrder - b.sortOrder || a.name.localeCompare(b.name); });
}

// Returns the normalized service, or null if it doesn't exist / is malformed.
// Callers deciding whether a customer may BOOK it must also check `.active`.
async function getServiceById(db, serviceId) {
    if (typeof serviceId !== 'string' || !serviceId) return null;
    const snap = await db.collection('services').doc(serviceId).get();
    if (!snap.exists) return null;
    return normalizeService(snap.id, snap.data());
}

// What the public frontend needs to render a service card — deliberately
// omits anything internal.
function toPublicService(service) {
    return {
        id: service.id,
        name: service.name,
        description: service.description,
        durationMinutes: service.durationMinutes,
        amount: service.amount,
        isFree: !isPaidService(service)
    };
}

module.exports = { normalizeService, isPaidService, getActiveServices, getServiceById, toPublicService };
