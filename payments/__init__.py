"""Payment boundary: a provider-neutral interface and its provider adapters.

The API talks only to :class:`payments.base.PaymentProvider`. Provider-specific code lives in one adapter
module per provider, so a provider can be added or replaced without touching the API routes.

No provider is live. Signature verification, replay protection, idempotency, server-side amount
verification and payment-state transitions are not implemented yet.
"""
