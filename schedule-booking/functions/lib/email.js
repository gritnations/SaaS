// Sends booking emails via Resend's REST API directly (no SDK dependency —
// it's a single plain HTTPS POST, not worth adding a package for). Chosen
// over a Google Workspace mailbox because Workspace status for
// example.com wasn't confirmed; swap this module out if that changes
// later without touching anything that calls it.

const FROM_ADDRESS = 'Example Business <bookings@example.com>';
const NOTIFY_ADDRESS = 'contact@example.com'; // the owner's copy on every booking event

// LOCAL DEMO ONLY: when a capture function is registered (index.js does this
// only inside the emulator) and the API key is still the placeholder, emails
// are handed to it instead of Resend. With a real key, they really send.
let captureEmail = null;
function setEmailCapture(fn) { captureEmail = fn; }

async function sendEmail(apiKey, { to, subject, html }) {
    if (captureEmail && (!apiKey || apiKey === 'placeholder')) {
        await captureEmail({ to, subject, html });
        return;
    }

    const response = await fetch('https://api.resend.com/emails', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            Authorization: 'Bearer ' + apiKey
        },
        body: JSON.stringify({ from: FROM_ADDRESS, to: [to], subject, html })
    });

    if (!response.ok) {
        const text = await response.text().catch(function () { return ''; });
        throw new Error('Resend send failed (' + response.status + '): ' + text);
    }
}

// Everything interpolated into email HTML comes from customer-typed fields or
// admin-entered service names, so it is always escaped.
function escapeHtml(value) {
    return String(value === undefined || value === null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// Only ever place a URL into an href if it's http(s) — an admin-editable
// Meet-link setting must not be able to inject a javascript: URL.
function safeUrl(url) {
    return /^https?:\/\//i.test(String(url || '')) ? escapeHtml(url) : '';
}

function formatMoney(amount, currency) {
    return currency + ' ' + (amount / 100).toFixed(2);
}

function button(url, label) {
    return '<p><a href="' + url + '" style="display:inline-block;padding:10px 20px;background:#044233;color:#ffffff;text-decoration:none;border-radius:4px;">' + escapeHtml(label) + '</a></p>';
}

// booking: { id, customerName, customerEmail, customerMobile, serviceName,
//   isPaid, amount, currency, slotDateLabel, slotTimeLabel, meetLink, manageUrl }
function bookingConfirmedHtml(booking) {
    const meet = safeUrl(booking.meetLink);
    const manage = safeUrl(booking.manageUrl);

    return '<div style="font-family: Helvetica, Arial, sans-serif; color: #141414;">'
        + '<p>Hi ' + escapeHtml(booking.customerName) + ',</p>'
        + '<p>Your session is confirmed:</p>'
        + '<p><strong>' + escapeHtml(booking.serviceName) + '</strong><br>'
        + escapeHtml(booking.slotDateLabel) + ' — ' + escapeHtml(booking.slotTimeLabel) + '</p>'
        + (booking.isPaid ? '<p>Amount paid: ' + escapeHtml(formatMoney(booking.amount, booking.currency)) + '</p>' : '')
        + (meet ? '<p>Join by video call:</p>' + button(meet, 'Join Google Meet') : '')
        + '<p>Booking reference: ' + escapeHtml(booking.id) + '</p>'
        + (manage ? '<p>Need to change plans? You can cancel or reschedule here:</p>' + button(manage, 'Manage my booking') : '')
        + '<p>We look forward to speaking with you.</p>'
        + '<p>Example Business</p>'
        + '</div>';
}

function ownerNotificationHtml(booking, heading) {
    return '<div style="font-family: Helvetica, Arial, sans-serif; color: #141414;">'
        + '<p>' + escapeHtml(heading) + '</p>'
        + '<p><strong>' + escapeHtml(booking.serviceName) + '</strong><br>'
        + escapeHtml(booking.slotDateLabel) + ' — ' + escapeHtml(booking.slotTimeLabel) + '</p>'
        + '<p>' + escapeHtml(booking.customerName) + ' &lt;' + escapeHtml(booking.customerEmail) + '&gt; · ' + escapeHtml(booking.customerMobile) + '</p>'
        + (booking.isPaid ? '<p>Paid: ' + escapeHtml(formatMoney(booking.amount, booking.currency)) + '</p>' : '<p>Free booking</p>')
        + '<p>Booking reference: ' + escapeHtml(booking.id) + '</p>'
        + '</div>';
}

// What a cancelled customer is told about their money. refundStatus:
//   'none'          nothing was paid (free booking)
//   'refunded'      the refund was sent to the payment provider
//   'needs_manual'  it couldn't be sent automatically; the owner refunds it by hand
function refundParagraph(booking, info) {
    if (!info || !(info.refundAmount > 0)) return '';
    const amount = escapeHtml(formatMoney(info.refundAmount, booking.currency));
    if (info.refundStatus === 'refunded') {
        return '<p>A refund of ' + amount + ' has been issued to your original payment method. Banks can take several working days to show it.</p>';
    }
    return '<p>We are processing your refund of ' + amount + ' and will email you as soon as it has been sent.</p>';
}

// info: { refundAmount, refundStatus, by: 'customer' | 'admin' }
function bookingCancelledHtml(booking, info) {
    const book = safeUrl(booking.bookUrl);
    return '<div style="font-family: Helvetica, Arial, sans-serif; color: #141414;">'
        + '<p>Hi ' + escapeHtml(booking.customerName) + ',</p>'
        + '<p>Your session has been cancelled:</p>'
        + '<p><strong>' + escapeHtml(booking.serviceName) + '</strong><br>'
        + escapeHtml(booking.slotDateLabel) + ' — ' + escapeHtml(booking.slotTimeLabel) + '</p>'
        + refundParagraph(booking, info)
        + (book ? '<p>You are welcome to book another time whenever suits you:</p>' + button(book, 'Book a session') : '')
        + '<p>Booking reference: ' + escapeHtml(booking.id) + '</p>'
        + '<p>Example Business</p>'
        + '</div>';
}

// old: { slotDateLabel, slotTimeLabel } — the time it was moved FROM.
function bookingRescheduledHtml(booking, old) {
    const meet = safeUrl(booking.meetLink);
    const manage = safeUrl(booking.manageUrl);
    return '<div style="font-family: Helvetica, Arial, sans-serif; color: #141414;">'
        + '<p>Hi ' + escapeHtml(booking.customerName) + ',</p>'
        + '<p>Your session has been moved. Your new time:</p>'
        + '<p><strong>' + escapeHtml(booking.serviceName) + '</strong><br>'
        + escapeHtml(booking.slotDateLabel) + ' — ' + escapeHtml(booking.slotTimeLabel) + '</p>'
        + '<p style="color:#666;">Previously: ' + escapeHtml(old.slotDateLabel) + ' — ' + escapeHtml(old.slotTimeLabel) + '</p>'
        + (meet ? '<p>Join by video call:</p>' + button(meet, 'Join Google Meet') : '')
        + '<p>Booking reference: ' + escapeHtml(booking.id) + '</p>'
        + (manage ? '<p>Need to change it again?</p>' + button(manage, 'Manage my booking') : '')
        + '<p>Example Business</p>'
        + '</div>';
}

function ownerRefundLine(booking, info) {
    if (!info || !(info.refundAmount > 0)) return 'No payment to refund.';
    const amount = formatMoney(info.refundAmount, booking.currency);
    return info.refundStatus === 'refunded'
        ? 'Refund of ' + amount + ' was sent automatically.'
        : 'ACTION NEEDED: refund ' + amount + ' to the customer in Paymob (automatic refund did not go through).';
}

async function sendCancellationEmails(apiKey, booking, info) {
    await sendEmail(apiKey, {
        to: booking.customerEmail,
        subject: 'Your session has been cancelled',
        html: bookingCancelledHtml(booking, info)
    });

    const who = info && info.by === 'admin' ? 'by you (admin panel)' : 'by the customer';
    await sendEmail(apiKey, {
        to: NOTIFY_ADDRESS,
        subject: (info && info.refundStatus === 'needs_manual' ? 'ACTION NEEDED: ' : '') + 'Booking cancelled: ' + booking.slotDateLabel + ' ' + booking.slotTimeLabel,
        html: ownerNotificationHtml(booking, 'Booking cancelled ' + who + '. ' + ownerRefundLine(booking, info))
    });
}

async function sendRescheduleEmails(apiKey, booking, old) {
    await sendEmail(apiKey, {
        to: booking.customerEmail,
        subject: 'Your session has been rescheduled',
        html: bookingRescheduledHtml(booking, old)
    });

    await sendEmail(apiKey, {
        to: NOTIFY_ADDRESS,
        subject: 'Booking rescheduled: ' + booking.slotDateLabel + ' ' + booking.slotTimeLabel,
        html: ownerNotificationHtml(booking, 'Booking moved by the customer (was ' + old.slotDateLabel + ', ' + old.slotTimeLabel + '):')
    });
}

async function sendBookingConfirmationEmails(apiKey, booking) {
    await sendEmail(apiKey, {
        to: booking.customerEmail,
        subject: 'Your session is confirmed',
        html: bookingConfirmedHtml(booking)
    });

    await sendEmail(apiKey, {
        to: NOTIFY_ADDRESS,
        subject: 'New booking: ' + booking.slotDateLabel + ' ' + booking.slotTimeLabel,
        html: ownerNotificationHtml(booking, 'New booking confirmed:')
    });
}

// Something needs a human — e.g. a customer paid for a time that was taken
// by someone else meanwhile and must be refunded.
async function sendOwnerAlert(apiKey, subject, html) {
    await sendEmail(apiKey, { to: NOTIFY_ADDRESS, subject: subject, html: html });
}

module.exports = {
    setEmailCapture,
    sendBookingConfirmationEmails,
    sendOwnerAlert,
    sendCancellationEmails,
    sendRescheduleEmails,
    bookingCancelledHtml,
    bookingRescheduledHtml,
    ownerNotificationHtml,
    bookingConfirmedHtml,
    escapeHtml,
    safeUrl
};
