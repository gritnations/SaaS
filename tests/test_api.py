"""Tests for the API routes: health, readiness, metrics and the payment boundary."""

import pytest
from helpers import FakeDatabase, build_client

from api.main import ApiConfigError, create_api

TOKEN = "t" * 40


def test_health_is_ok_without_a_database() -> None:
    """Liveness answers even when no database is configured."""
    response = build_client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_ready_when_the_database_answers() -> None:
    """Readiness is 200 when the database health check passes."""
    response = build_client(FakeDatabase(healthy=True)).get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.parametrize("database", [None, FakeDatabase(healthy=False)])
def test_ready_reports_not_ready_with_only_a_coarse_status(database: FakeDatabase | None) -> None:
    """Readiness is 503 with no reason when the database is absent or unhealthy."""
    response = build_client(database).get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_liveness_does_not_depend_on_readiness() -> None:
    """An unhealthy database leaves liveness untouched."""
    client = build_client(FakeDatabase(healthy=False))
    assert client.get("/health/ready").status_code == 503
    assert client.get("/health").status_code == 200


def test_metrics_are_open_outside_production_and_count_responses() -> None:
    """Outside production the metrics are readable and reflect the responses sent."""
    client = build_client()
    client.get("/health")
    client.get("/health/ready")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'platform_info{version="0.1.0"} 1' in response.text
    assert 'platform_http_requests_total{status_class="2xx"} 1' in response.text
    assert 'platform_http_requests_total{status_class="5xx"} 1' in response.text


def test_metrics_do_not_exist_in_production_without_a_token() -> None:
    """In production, with no token configured, the metrics route answers 404."""
    assert build_client(APP_ENV="production").get("/metrics").status_code == 404


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic abc", TOKEN])
def test_metrics_refuse_a_missing_or_wrong_token_in_production(header: str | None) -> None:
    """In production a missing, malformed or wrong token is refused with 401."""
    headers = {} if header is None else {"Authorization": header}
    response = build_client(APP_ENV="production", METRICS_TOKEN=TOKEN).get("/metrics", headers=headers)
    assert response.status_code == 401
    assert "platform_info" not in response.text


def test_metrics_accept_the_right_token_in_production() -> None:
    """In production the configured bearer token unlocks the metrics."""
    client = build_client(APP_ENV="production", METRICS_TOKEN=TOKEN)
    response = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert "platform_uptime_seconds" in response.text


def test_a_short_metrics_token_is_refused_at_start() -> None:
    """A token under 32 characters stops the API from being built."""
    with pytest.raises(ApiConfigError) as caught:
        create_api({"APP_ENV": "production", "METRICS_TOKEN": "short-token-value"})
    assert "short-token-value" not in str(caught.value)


def test_an_unknown_environment_is_refused_at_start() -> None:
    """An unrecognised ``APP_ENV`` stops the API from being built."""
    with pytest.raises(ApiConfigError):
        create_api({"APP_ENV": "staging"})


def test_a_missing_environment_means_production() -> None:
    """With ``APP_ENV`` unset the API behaves as production: no schema and no stub provider."""
    client = build_client(APP_ENV="")
    assert client.get("/openapi.json").status_code == 404
    assert client.post("/payments/stub/checkout", json={"order_reference": "order-123"}).status_code == 404


def test_the_schema_is_served_only_in_development() -> None:
    """The OpenAPI schema exists in development and nowhere else."""
    assert build_client(APP_ENV="development").get("/openapi.json").status_code == 200
    assert build_client(APP_ENV="test").get("/openapi.json").status_code == 404
    assert build_client(APP_ENV="production").get("/openapi.json").status_code == 404


def test_paymob_is_not_live() -> None:
    """Paymob refuses both a checkout and a notification with 501."""
    client = build_client()
    assert client.post("/payments/paymob/checkout", json={"order_reference": "order-123"}).status_code == 501
    notification = client.post("/payments/paymob/webhook", content=b'{"anything": true}')
    assert notification.status_code == 501


def test_the_stub_goes_through_the_same_routes() -> None:
    """The stub provider answers through the same routes, which proves the routes are provider-neutral."""
    client = build_client()
    checkout = client.post("/payments/stub/checkout", json={"order_reference": "order-123"})
    assert checkout.status_code == 200
    assert checkout.json() == {"provider": "stub", "order_reference": "order-123", "checkout_url": None, "live": False}
    notification = client.post("/payments/stub/webhook", json={"order_reference": "order-123", "status": "paid"})
    assert notification.status_code == 202
    assert notification.json() == {"accepted": True, "provider": "stub", "live": False, "recorded": False}


def test_the_stub_is_absent_in_production() -> None:
    """In production the stub is not registered, so its routes answer 404."""
    client = build_client(APP_ENV="production")
    assert client.post("/payments/stub/checkout", json={"order_reference": "order-123"}).status_code == 404
    assert (
        client.post("/payments/stub/webhook", json={"order_reference": "order-123", "status": "paid"}).status_code
        == 404
    )


def test_an_unknown_provider_is_not_found() -> None:
    """A provider name that is not registered answers 404."""
    assert build_client().post("/payments/other/checkout", json={"order_reference": "order-123"}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"order_reference": "x"},
        {"order_reference": "order-123", "amount": 1},
        {"order_reference": "<script>alert(1)</script>"},
        {},
    ],
)
def test_an_invalid_checkout_body_is_refused_without_an_echo(body: dict[str, object]) -> None:
    """A bad reference, an extra field such as an amount, or a missing field is 422 and never echoed."""
    response = build_client().post("/payments/stub/checkout", json=body)
    assert response.status_code == 422
    assert "script" not in response.text
    assert '"amount":1' not in response.text.replace(" ", "")


@pytest.mark.parametrize(
    "content",
    [b"not json", b'{"order_reference": "order-123"}', b'{"order_reference": "x", "status": "paid"}', b"[]"],
)
def test_a_malformed_stub_notification_is_rejected(content: bytes) -> None:
    """A stub notification that is not the expected JSON object is refused with 400."""
    assert build_client().post("/payments/stub/webhook", content=content).status_code == 400
