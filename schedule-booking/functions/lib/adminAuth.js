// Is this signed-in user an admin? Same rule as firestore.rules (isAdmin):
// verified email, and on the allow-list. The list lives in config.ADMIN_EMAILS
// and MUST match the one in firestore.rules — the panel's own writes are
// checked by the rules, but actions that go through a Cloud Function (cancel +
// refund + email) have to be checked here.

// decodedToken: the result of admin.auth().verifyIdToken(...)
function isAdminToken(decodedToken, adminEmails) {
    if (!decodedToken || decodedToken.email_verified !== true) return false;
    if (typeof decodedToken.email !== 'string') return false;
    const email = decodedToken.email.toLowerCase();
    return (adminEmails || []).some(function (a) { return String(a).toLowerCase() === email; });
}

// 'Authorization: Bearer <token>' -> '<token>' or ''.
function bearerToken(headerValue) {
    const m = /^Bearer\s+(\S+)$/i.exec(String(headerValue || ''));
    return m ? m[1] : '';
}

module.exports = { isAdminToken, bearerToken };
