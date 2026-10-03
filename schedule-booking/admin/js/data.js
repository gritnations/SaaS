// All Firestore reads/writes the admin panel performs, in one place. The
// views never touch the SDK directly. What each write is allowed to contain
// is enforced server-side by firestore.rules; the validators in lib/ exist so
// the admin gets a clear message before a write is even attempted.

import {
    auth, db, collection, doc, getDoc, getDocs, setDoc, updateDoc, deleteDoc, query, where, writeBatch
} from './firebase.js';
import { functionsBase } from './config.js';
import { monthRange, quarterRange } from './lib/format.js';
import { normalizeBooking } from './lib/bookingsModel.js';

// ---- bookings ------------------------------------------------------------

async function loadBookingsBetween(start, end) {
    const snap = await getDocs(query(
        collection(db, 'bookings'),
        where('slotDate', '>=', start),
        where('slotDate', '<=', end)
    ));
    return snap.docs.map(function (d) { return normalizeBooking(d.id, d.data()); });
}

export async function loadMonthBookings(year, month) {
    const { start, end } = monthRange(year, month);
    return loadBookingsBetween(start, end);
}

// Three months in ONE read (the Bookings tab shows a quarter at a time).
export async function loadQuarterBookings(year, quarter) {
    const { start, end } = quarterRange(year, quarter);
    return loadBookingsBetween(start, end);
}

// The only direct booking edit the rules allow (confirmed -> no_show).
export async function markNoShow(bookingId) {
    await updateDoc(doc(db, 'bookings', bookingId), { status: 'no_show' });
}

// Cancelling goes through a Cloud Function (refund + calendar + emails), not a
// direct database write. The function checks the signed-in admin's token.
export async function cancelBooking(bookingId) {
    const user = auth.currentUser;
    if (!user) throw new Error('You are signed out. Please sign in again.');
    const token = await user.getIdToken();

    let response;
    try {
        response = await fetch(functionsBase + '/adminCancelBooking', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
            body: JSON.stringify({ bookingId: bookingId })
        });
    } catch (err) {
        throw new Error('Couldn’t reach the server. Check your connection and try again.');
    }

    let body = {};
    try { body = await response.json(); } catch (err) { /* keep {} */ }
    if (!response.ok) throw new Error(body.error || 'The booking could not be cancelled.');
    return body; // { outcome, refundAmount, refundStatus }
}

// The owner has refunded the customer in Paymob by hand. One field changes; the rules
// allow nothing else.
export async function markRefunded(booking) {
    const patch = booking.status === 'paid_slot_conflict' ? { status: 'refunded' } : { refundStatus: 'refunded_manually' };
    await updateDoc(doc(db, 'bookings', booking.id), patch);
}

export async function countActiveBookingsOn(dateKey) {
    const snap = await getDocs(query(collection(db, 'bookings'), where('slotDate', '==', dateKey)));
    return snap.docs.filter(function (d) {
        const s = d.data().status;
        return s === 'confirmed' || s === 'pending_payment';
    }).length;
}

// ---- services ------------------------------------------------------------

export async function loadServices() {
    const snap = await getDocs(collection(db, 'services'));
    return snap.docs
        .map(function (d) { return Object.assign({ id: d.id }, d.data()); })
        .sort(function (a, b) {
            return (Number(a.sortOrder) || 0) - (Number(b.sortOrder) || 0) || String(a.name).localeCompare(String(b.name));
        });
}

// id === null creates a new service (auto-generated id). Returns the id.
export async function saveService(id, value) {
    const ref = id ? doc(db, 'services', id) : doc(collection(db, 'services'));
    await setDoc(ref, value);
    return ref.id;
}

export async function setServiceActive(id, active) {
    await updateDoc(doc(db, 'services', id), { active: !!active });
}

export async function deleteService(id) {
    await deleteDoc(doc(db, 'services', id));
}

// ---- weekly hours --------------------------------------------------------

export async function loadRules() {
    const snap = await getDocs(collection(db, 'availabilityRules'));
    return snap.docs.map(function (d) { return Object.assign({ id: d.id }, d.data()); });
}

// Replaces the whole weekly schedule in ONE batch, so a failure part-way can
// never leave the booking page with half a schedule. newRules: [{ id, data }].
export async function replaceRules(existingIds, newRules) {
    const keep = new Set(newRules.map(function (r) { return r.id; }));
    const batch = writeBatch(db);

    existingIds.forEach(function (id) {
        if (!keep.has(id)) batch.delete(doc(db, 'availabilityRules', id));
    });
    newRules.forEach(function (r) {
        batch.set(doc(db, 'availabilityRules', r.id), r.data);
    });

    await batch.commit();
}

// ---- blocked dates -------------------------------------------------------

export async function loadBlockedDates() {
    const snap = await getDocs(collection(db, 'blockedDates'));
    return snap.docs
        .map(function (d) { return { dateKey: d.id, reason: d.data().reason || '' }; })
        .sort(function (a, b) { return a.dateKey < b.dateKey ? -1 : 1; });
}

export async function blockDate(dateKey, reason) {
    await setDoc(doc(db, 'blockedDates', dateKey), reason ? { reason: reason } : {});
}

export async function unblockDate(dateKey) {
    await deleteDoc(doc(db, 'blockedDates', dateKey));
}

// ---- settings ------------------------------------------------------------

export async function loadMeetLink() {
    const snap = await getDoc(doc(db, 'settings', 'general'));
    return snap.exists() && typeof snap.data().meetLink === 'string' ? snap.data().meetLink : '';
}

// merge: true so saving one setting never wipes the other.
export async function saveMeetLink(value) {
    await setDoc(doc(db, 'settings', 'general'), { meetLink: value }, { merge: true });
}

export async function loadBreakMinutes() {
    const snap = await getDoc(doc(db, 'settings', 'general'));
    const n = snap.exists() ? snap.data().breakMinutes : 0;
    return Number.isInteger(n) && n > 0 ? n : 0;
}

export async function saveBreakMinutes(minutes) {
    await setDoc(doc(db, 'settings', 'general'), { breakMinutes: minutes }, { merge: true });
}

// Used at sign-in to find out whether this account is an admin: the rules
// let ONLY admins read settings/general (even when it doesn't exist yet), so
// a permission-denied here means "not an admin".
export async function checkAdminAccess() {
    try {
        await getDoc(doc(db, 'settings', 'general'));
        return 'admin';
    } catch (err) {
        if (err && err.code === 'permission-denied') return 'denied';
        throw err;
    }
}
