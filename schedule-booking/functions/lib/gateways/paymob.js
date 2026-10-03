// Paymob adapter — implements the PaymentGateway interface documented in
// the shared payments-gateway-module README. This file is a
// COPY of that shared module's gateways/paymob.js (per the copy-paste
// distribution model — see the shared module's README for why). If you fix
// a bug here, copy the fix back to the shared module and any other project
// using it too, since there's no automatic syncing.
//
// VERIFY BEFORE GOING LIVE (carried over from the original implementation,
// still true here — nothing about this migration re-validated it against a
// real account): the Create Intention request/response shape and the HMAC
// field list/order below come from Paymob's published docs and third-party
// references, not a tested call. Confirm both against a real TEST-mode
// transaction before trusting this with real payments. In particular:
//   - That `obj.order.merchant_order_id` is really where Paymob echoes back
//     the `special_reference` sent when creating the intention.
//   - That the HMAC you compute here matches the `hmac` query parameter on
//     a real callback.

const { computeOrderedHmac, safeEqual } = require('../hmacUtil');

const DEFAULT_BASE_URL = 'https://ksa.paymob.com'; // override via config.baseUrl for other Paymob regions

// Exact, ordered field list Paymob uses for the Transaction Processed
// Callback HMAC (HMAC-SHA512, no separators between values, sent as an
// `hmac` query parameter on the callback URL — not a header, not over the
// raw body).
const HMAC_FIELD_ORDER = [
    'amount_cents', 'created_at', 'currency', 'error_occured',
    'has_parent_transaction', 'id', 'integration_id', 'is_3d_secure',
    'is_auth', 'is_capture', 'is_refunded', 'is_standalone_payment',
    'is_voided', 'order.id', 'owner', 'pending',
    'source_data.pan', 'source_data.sub_type', 'source_data.type', 'success'
];

class PaymobGateway {
    // Credentials are validated per-method, not all up front — a caller
    // that only ever calls verifyWebhook() (e.g. a webhook-ingest function
    // kept deliberately least-privilege) shouldn't be forced to also hold
    // the payment-creation secretKey/publicKey just to construct this class.
    constructor(config) {
        this.secretKey = config && config.secretKey;
        this.publicKey = config && config.publicKey;
        this.hmacSecret = config && config.hmacSecret;
        this.baseUrl = (config && config.baseUrl) || DEFAULT_BASE_URL;
        // Paymob "integration IDs" identify which payment methods (Mada,
        // Visa/Mastercard, wallets, etc.) are offered at checkout — these
        // come from the merchant dashboard, not something this module can
        // know in advance. Empty until a real merchant account provides them.
        this.paymentMethods = (config && config.paymentMethods) || [];
    }

    async createCheckout(checkoutRequest) {
        if (!this.secretKey || !this.publicKey) {
            throw new Error('PaymobGateway.createCheckout requires secretKey and publicKey in its config.');
        }

        const { amount, currency, orderId, customer, notificationUrl, redirectUrl, paymentMethods } = checkoutRequest;

        const response = await fetch(this.baseUrl + '/v1/intention/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                Authorization: 'Token ' + this.secretKey
            },
            body: JSON.stringify({
                amount: amount,
                currency: currency,
                payment_methods: paymentMethods || this.paymentMethods,
                items: [
                    { name: 'Payment', amount: amount, quantity: 1 }
                ],
                billing_data: {
                    first_name: (customer && customer.name) || 'Guest',
                    last_name: 'Guest',
                    phone_number: customer && customer.mobile,
                    email: customer && customer.email
                },
                special_reference: orderId,
                notification_url: notificationUrl,
                redirection_url: redirectUrl
            })
        });

        if (!response.ok) {
            const text = await response.text().catch(function () { return ''; });
            throw new Error('Paymob createIntention failed (' + response.status + '): ' + text);
        }

        const data = await response.json();
        const checkoutUrl = this.baseUrl + '/unifiedcheckout/?publicKey=' + encodeURIComponent(this.publicKey) + '&clientSecret=' + encodeURIComponent(data.client_secret);

        return { checkoutUrl: checkoutUrl, providerReference: data.id };
    }

    // Refunds (part of) a captured payment. UNVERIFIED against a live Paymob
    // account — written from Paymob's published "refund transaction" docs
    // (POST /api/acceptance/void_refund/refund with the numeric transaction id
    // and the amount in cents). Check with a real TEST-mode refund before relying
    // on it. Callers must treat any failure as "refund manually", never as
    // "the cancellation failed".
    async refund({ providerTransactionId, amount }) {
        if (!this.secretKey) {
            throw new Error('PaymobGateway.refund requires secretKey in its config.');
        }
        if (!providerTransactionId || !(amount > 0)) {
            throw new Error('PaymobGateway.refund needs the payment\'s transaction id and a positive amount.');
        }

        const response = await fetch(this.baseUrl + '/api/acceptance/void_refund/refund', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                Authorization: 'Token ' + this.secretKey
            },
            body: JSON.stringify({ transaction_id: providerTransactionId, amount_cents: amount })
        });

        if (!response.ok) {
            const text = await response.text().catch(function () { return ''; });
            throw new Error('Paymob refund failed (' + response.status + '): ' + text);
        }

        const data = await response.json().catch(function () { return {}; });
        return { refundId: data && data.id !== undefined ? String(data.id) : '' };
    }

    verifyWebhook({ body, query }) {
        if (!this.hmacSecret) {
            throw new Error('PaymobGateway.verifyWebhook requires hmacSecret in its config.');
        }

        const providedHmac = query && query.hmac;
        const txn = body && body.obj;

        if (!providedHmac || !txn) return { verified: false, event: null };

        const expected = computeOrderedHmac(txn, HMAC_FIELD_ORDER, this.hmacSecret, 'sha512');
        if (!safeEqual(expected, providedHmac)) return { verified: false, event: null };

        const orderId = txn.order && txn.order.merchant_order_id;
        let status = 'pending';
        if (txn.success === true && txn.pending !== true) status = 'paid';
        else if (txn.success === false) status = 'failed';

        return {
            verified: true,
            event: {
                orderId: orderId,
                providerTransactionId: txn.id,
                status: status,
                amount: txn.amount_cents,
                currency: txn.currency,
                raw: body
            }
        };
    }
}

module.exports = PaymobGateway;
