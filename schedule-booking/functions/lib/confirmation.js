// The single "a booking just became confirmed" step, shared by BOTH paths:
//   - paid bookings, after the payment webhook is verified (processPaymobEvent)
//   - free bookings, immediately at booking time (bookingRequest)
// Keeping it in one place means the Meet link, manage link, and emails are
// built identically no matter how a booking got confirmed.

const logger = require('firebase-functions/logger');
const { PUBLIC_SITE_ORIGIN } = require('./config');
const { getGeneralSettings } = require('./settings');
const { sendBookingConfirmationEmails } = require('./email');
const { dateFromKey, formatSessionLabel, WEEKDAY_SHORT, MONTH_SHORT } = require('./availability');
const { getInterval } = require('./bookings');
const { createMeeting } = require('./meeting');

function formatDateLabel(dateKey) {
    const d = dateFromKey(dateKey);
    return WEEKDAY_SHORT[d.getDay()] + ', ' + d.getDate() + ' ' + MONTH_SHORT[d.getMonth()];
}

function buildManageUrl(booking) {
    return PUBLIC_SITE_ORIGIN + '/pages/manage-booking.html?bookingId=' + encodeURIComponent(booking.id)
        + '&token=' + encodeURIComponent(booking.manageToken);
}

// Builds the values the email templates need from a booking doc.
function toEmailBooking(booking, meetLink) {
    const interval = getInterval(booking);
    return {
        id: booking.id,
        customerName: booking.customerName,
        customerEmail: booking.customerEmail,
        customerMobile: booking.customerMobile,
        serviceName: booking.serviceName || 'Advisory session',
        isPaid: !!booking.isPaid,
        amount: booking.amount,
        currency: booking.currency,
        slotDateLabel: formatDateLabel(booking.slotDate),
        slotTimeLabel: interval
            ? formatSessionLabel(interval.startMinutes, interval.endMinutes - interval.startMinutes)
            : booking.slotTime,
        meetLink: meetLink,
        manageUrl: buildManageUrl(booking),
        bookUrl: PUBLIC_SITE_ORIGIN + '/pages/book-a-session.html'
    };
}

// booking must already be in the confirmed state. Never throws for an email
// problem: the booking IS confirmed (and any payment taken), so a failed
// email must not be retried as though the booking itself had failed.
//
// Meet link: a unique one from a Calendar event when that works, otherwise the
// static link from settings (so a Calendar outage never leaves a customer
// without a link). `options.requestCalendar` is for tests.
async function finalizeConfirmedBooking(db, booking, resendApiKey, calendarKey, options) {
    let meetLink = '';
    const update = {};

    const meeting = await createMeeting(calendarKey, booking, options && options.requestCalendar);
    if (meeting) {
        meetLink = meeting.meetLink;
        update.calendarEventId = meeting.eventId;
    } else {
        try {
            meetLink = (await getGeneralSettings(db)).meetLink;
        } catch (err) {
            logger.error('finalizeConfirmedBooking: could not read static meet link', err);
        }
    }

    try {
        // Snapshot the link on the booking so later edits to the setting
        // don't change what this customer was told.
        update.meetLink = meetLink || null;
        await db.collection('bookings').doc(booking.id).update(update);
    } catch (err) {
        logger.error('finalizeConfirmedBooking: could not store meet link', err);
    }

    try {
        await sendBookingConfirmationEmails(resendApiKey, toEmailBooking(booking, meetLink));
    } catch (err) {
        logger.error('finalizeConfirmedBooking: booking confirmed but email send failed', { bookingId: booking.id, err: String(err) });
    }

    return { meetLink: meetLink, manageUrl: buildManageUrl(booking) };
}

module.exports = { finalizeConfirmedBooking, toEmailBooking, buildManageUrl, formatDateLabel };
