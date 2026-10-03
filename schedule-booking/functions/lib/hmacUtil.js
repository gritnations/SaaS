// Generic HMAC helpers shared across gateway adapters. Nothing here is
// Paymob-specific — the field list/order/algorithm a gateway uses lives in
// that gateway's own adapter file, not here.

const crypto = require('crypto');

function getByPath(obj, path) {
    return path.split('.').reduce(function (acc, key) {
        return acc && typeof acc === 'object' ? acc[key] : undefined;
    }, obj);
}

// Serializes a value the way most gateways expect when building a
// concatenated string to sign: lowercase "true"/"false" for booleans, empty
// string for missing values, plain String() for everything else. If a
// gateway serializes differently, override this in that gateway's adapter
// rather than changing the shared default.
function defaultToHmacString(value) {
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (value === undefined || value === null) return '';
    return String(value);
}

// Computes an HMAC over an ordered list of field paths pulled out of `obj`,
// concatenated with no separator (the common convention) and hashed with
// the given algorithm (e.g. 'sha512', 'sha256').
function computeOrderedHmac(obj, fieldOrder, secret, algorithm, toStringFn) {
    const serialize = toStringFn || defaultToHmacString;
    const concatenated = fieldOrder
        .map(function (path) { return serialize(getByPath(obj, path)); })
        .join('');

    return crypto.createHmac(algorithm, secret).update(concatenated).digest('hex');
}

// Constant-time comparison — always use this instead of `===` when checking
// a computed signature against one supplied by a webhook caller, so a
// timing attack can't be used to guess the correct value byte-by-byte.
function safeEqual(a, b) {
    const bufA = Buffer.from(String(a), 'utf8');
    const bufB = Buffer.from(String(b), 'utf8');
    if (bufA.length !== bufB.length) return false;
    return crypto.timingSafeEqual(bufA, bufB);
}

module.exports = { getByPath, defaultToHmacString, computeOrderedHmac, safeEqual };
