// Simple per-IP rate limit for bookingRequest, backed by Firestore since
// Cloud Functions instances aren't guaranteed to share in-memory state.
// Deliberately basic (fixed window, not sliding) — this only needs to stop
// obvious slot-spam abuse, not survive a serious attacker.

const WINDOW_MS = 10 * 60 * 1000; // 10 minutes
const MAX_REQUESTS_PER_WINDOW = 10;

// x-forwarded-for is "<whatever the caller sent>, <address Google saw>": the
// caller controls everything BEFORE the last entry, so only the last is
// trustworthy. Keying on the whole header would let anyone dodge the limit by
// sending a different fake prefix each time.
function clientIpFromForwardedFor(header, fallback) {
    const parts = String(header || '').split(',').map(function (p) { return p.trim(); }).filter(Boolean);
    return parts.length > 0 ? parts[parts.length - 1] : (fallback || 'unknown');
}

// Returns true if the request should be ALLOWED to proceed.
async function checkAndRecord(db, ip) {
    const safeId = String(ip).replace(/[^a-zA-Z0-9_.:-]/g, '_') || 'unknown';
    const ref = db.collection('rateLimits').doc(safeId);
    const now = Date.now();

    return db.runTransaction(async function (tx) {
        const snap = await tx.get(ref);
        const data = snap.exists ? snap.data() : null;

        if (!data || now - data.windowStart > WINDOW_MS) {
            tx.set(ref, { windowStart: now, count: 1 });
            return true;
        }

        if (data.count >= MAX_REQUESTS_PER_WINDOW) {
            return false;
        }

        tx.update(ref, { count: data.count + 1 });
        return true;
    });
}

module.exports = { checkAndRecord, clientIpFromForwardedFor };
