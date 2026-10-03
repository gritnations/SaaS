// Input validation for the admin forms. These mirror firestore.rules (which
// is the real enforcement) so the admin gets a clear message instead of a
// bare "permission denied". Pure functions — no DOM, no Firebase.

// "500" -> 50000, "500.5" -> 50050, "0" -> 0. Works on the text, never via
// floating point, so 19.99 can't become 1998.9999.
export function parsePriceToMinorUnits(text) {
    const s = String(text === undefined || text === null ? '' : text).trim();
    if (!/^\d+(\.\d{1,2})?$/.test(s)) return null;
    const [whole, frac = ''] = s.split('.');
    return Number(whole) * 100 + Number((frac + '00').slice(0, 2));
}

// fields: raw form strings/booleans. Returns { ok, errors: {field: message}, value }.
// `value` is exactly the document to store (matches firestore.rules validService).
export function validateService(fields) {
    const errors = {};

    const name = String(fields.name || '').trim();
    if (!name) errors.name = 'Give the service a name.';
    else if (name.length > 120) errors.name = 'Keep the name under 120 characters.';

    const description = String(fields.description || '').trim();
    if (description.length > 500) errors.description = 'Keep the description under 500 characters.';

    const durationText = String(fields.durationMinutes === undefined ? '' : fields.durationMinutes).trim();
    const durationMinutes = /^\d+$/.test(durationText) ? Number(durationText) : NaN;
    if (!Number.isInteger(durationMinutes) || durationMinutes < 5) errors.durationMinutes = 'Enter the length in whole minutes (at least 5).';
    else if (durationMinutes > 480) errors.durationMinutes = 'Sessions can be at most 480 minutes (8 hours).';

    const amount = parsePriceToMinorUnits(fields.price);
    if (amount === null) errors.price = 'Enter a price like 500 or 149.50 — or 0 to make the service free.';

    const sortText = String(fields.sortOrder === undefined || fields.sortOrder === '' ? '0' : fields.sortOrder).trim();
    const sortOrder = /^-?\d+$/.test(sortText) ? Number(sortText) : NaN;
    if (!Number.isInteger(sortOrder)) errors.sortOrder = 'Order must be a whole number.';

    const ok = Object.keys(errors).length === 0;
    return {
        ok,
        errors,
        value: ok ? { name, description, durationMinutes, amount, active: !!fields.active, sortOrder } : null
    };
}

// Empty is allowed (it switches the Meet link off). Otherwise https only —
// the link is placed in customer emails.
export function validateMeetLink(text) {
    const s = String(text || '').trim();
    if (s === '') return { ok: true, value: '' };
    if (!/^https:\/\/.{1,300}$/.test(s)) return { ok: false, error: 'Paste the full link, starting with https:// (e.g. https://meet.google.com/abc-defg-hij).' };
    return { ok: true, value: s };
}

// Minutes kept free between sessions: a whole number from 0 to 120.
export function validateBreakMinutes(text) {
    const s = String(text === undefined || text === null ? '' : text).trim();
    if (!/^\d{1,3}$/.test(s) || Number(s) > 120) return { ok: false, error: 'Enter a whole number of minutes from 0 to 120 (0 = no break).' };
    return { ok: true, value: Number(s) };
}

export function validateDateKey(text) {
    const s = String(text || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
    const [y, m, d] = s.split('-').map(Number);
    const date = new Date(y, m - 1, d);
    return date.getFullYear() === y && date.getMonth() === m - 1 && date.getDate() === d;
}
