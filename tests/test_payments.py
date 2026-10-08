"""Tests for the payment boundary: the interface, the registry and the two adapters."""

import pytest

from payments.base import (
    PaymentProvider,
    PaymentRegistry,
    ProviderNotLiveError,
    UnknownProviderError,
    WebhookRejectedError,
    is_valid_order_reference,
)
from payments.paymob import PaymobProvider
from payments.stub import StubProvider


@pytest.mark.parametrize("provider", [PaymobProvider(), StubProvider()])
def test_both_adapters_satisfy_the_interface(provider: PaymentProvider) -> None:
    """Paymob and the stub both implement the provider interface, and neither is live."""
    assert isinstance(provider, PaymentProvider)
    assert provider.live is False


def test_paymob_refuses_every_operation() -> None:
    """The Paymob adapter never pretends to work."""
    provider = PaymobProvider()
    with pytest.raises(ProviderNotLiveError):
        provider.create_checkout("order-123")
    with pytest.raises(ProviderNotLiveError):
        provider.parse_webhook({}, b"{}")


def test_the_stub_returns_non_live_results() -> None:
    """The stub answers, and marks every answer as not live."""
    provider = StubProvider()
    session = provider.create_checkout("order-123")
    assert (session.provider, session.order_reference, session.checkout_url, session.live) == (
        "stub",
        "order-123",
        None,
        False,
    )
    event = provider.parse_webhook({}, b'{"order_reference": "order-123", "status": "failed"}')
    assert (event.provider, event.order_reference, event.status, event.live) == ("stub", "order-123", "failed", False)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\xff\xfe",
        b'"text"',
        b'{"order_reference": "order-123", "status": "refunded"}',
        b'{"order_reference": 123456, "status": "paid"}',
        b'{"order_reference": "order-123", "status": "paid", "amount": 5}',
    ],
)
def test_the_stub_rejects_malformed_notifications(body: bytes) -> None:
    """Anything other than the exact expected JSON object is rejected."""
    with pytest.raises(WebhookRejectedError):
        StubProvider().parse_webhook({}, body)


def test_the_registry_finds_providers_by_name() -> None:
    """The registry returns the adapter registered under a name and lists names in order."""
    registry = PaymentRegistry([StubProvider(), PaymobProvider()])
    assert registry.names() == ["paymob", "stub"]
    assert isinstance(registry.get("paymob"), PaymobProvider)
    with pytest.raises(UnknownProviderError):
        registry.get("other")


def test_the_registry_refuses_duplicate_names() -> None:
    """Two providers with the same name cannot be registered."""
    with pytest.raises(ValueError):
        PaymentRegistry([StubProvider(), StubProvider()])


@pytest.mark.parametrize(
    ("reference", "expected"),
    [("order-123", True), ("A_b-9x", True), ("short", False), ("has space", False), ("x" * 65, False), ("", False)],
)
def test_order_reference_shape(reference: str, expected: bool) -> None:
    """Only 6 to 64 letters, digits, hyphens or underscores are accepted."""
    assert is_valid_order_reference(reference) is expected
