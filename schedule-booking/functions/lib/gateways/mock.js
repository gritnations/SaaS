// LOCAL DEMO ONLY. A stand-in payment gateway whose "checkout" is a fake page
// (booking-backend/demo/checkout.html) that has a Pay and a Decline button.
// It exists so the whole paid journey can be shown and tested before a real
// Paymob account exists.
//
// It refuses to be constructed anywhere except the Firebase emulator
// (FUNCTIONS_EMULATOR is set by the emulator and never in a deployed
// function), so it can never take the place of the real gateway in production.

class MockGateway {
    constructor(config) {
        if (process.env.FUNCTIONS_EMULATOR !== 'true') {
            throw new Error('MockGateway can only be used in the local emulator.');
        }
        this.checkoutBase = String((config && config.checkoutBase) || '');
    }

    async createCheckout(checkoutRequest) {
        const url = this.checkoutBase + '?bookingId=' + encodeURIComponent(checkoutRequest.orderId);
        return { checkoutUrl: url, providerReference: 'demo-intention-' + checkoutRequest.orderId };
    }

    async refund({ providerTransactionId }) {
        return { refundId: 'demo-refund-' + providerTransactionId };
    }
}

module.exports = MockGateway;
