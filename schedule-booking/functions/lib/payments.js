// What happens when a payment event arrives. Shared by the real Pub/Sub worker
// (processPaymobEvent) and the local-only demo checkout, so the demo exercises
// exactly the same confirmation / conflict logic as production.

const logger = require('firebase-functions/logger');
const { confirmPaidBooking } = require('./bookings');
const { finalizeConfirmedBooking, toEmailBooking } = require('./confirmation');
const { sendOwnerAlert, ownerNotificationHtml } = require('./email');

// paymentEvent is the gateway-normalised shape:
//   { orderId, providerTransactionId, status, amount, currency, raw }
// Returns one of: 'ignored' | 'invalid' | 'confirmed' | 'conflict' |
// 'already_handled' | 'not_found'. Never throws for an email problem.
async function handlePaymentEvent(db, paymentEvent, secrets) {
    if (!paymentEvent) { logger.error('handlePaymentEvent: empty event payload'); return 'invalid'; }

    if (paymentEvent.status !== 'paid') {
        logger.info('handlePaymentEvent: ignoring non-paid event', { status: paymentEvent.status, providerTransactionId: paymentEvent.providerTransactionId });
        return 'ignored';
    }

    const orderId = paymentEvent.orderId;
    if (!orderId) { logger.error('handlePaymentEvent: event missing orderId', { providerTransactionId: paymentEvent.providerTransactionId }); return 'invalid'; }

    const result = await confirmPaidBooking(db, orderId, paymentEvent.providerTransactionId);

    if (result.outcome === 'confirmed') {
        await finalizeConfirmedBooking(db, result.booking, secrets.resendApiKey, secrets.calendarKey);
        return 'confirmed';
    }

    if (result.outcome === 'conflict') {
        // The customer paid, but their time was taken by someone else after
        // their hold lapsed. Don't double-book — flag it loudly so the payment
        // gets refunded.
        logger.error('handlePaymentEvent: paid booking conflicts with another booking', { orderId, providerTransactionId: paymentEvent.providerTransactionId });
        try {
            await sendOwnerAlert(
                secrets.resendApiKey,
                'ACTION NEEDED: paid booking needs a refund',
                ownerNotificationHtml(toEmailBooking(result.booking, ''), 'A customer paid for a time that was taken by another booking before their payment arrived. It was NOT confirmed and needs a refund (Paymob transaction ' + paymentEvent.providerTransactionId + '):')
            );
        } catch (err) {
            logger.error('handlePaymentEvent: owner alert email failed', err);
        }
        return 'conflict';
    }

    logger.info('handlePaymentEvent: nothing to do', { orderId, outcome: result.outcome });
    return result.outcome;
}

module.exports = { handlePaymentEvent };
