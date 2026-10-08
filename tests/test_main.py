"""Tests for the root application: the ingress controls that apply to every request."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN, FakeDatabase, build_client

import main
from main import MAX_BODY_BYTES, SECURITY_HEADERS, IngressConfigError, create_app


def test_the_module_imports_and_serves_with_no_configuration() -> None:
    """With an empty environment the application starts, is live, and is honestly not ready."""
    app = create_app({})
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").status_code == 503


def test_the_module_level_app_exists() -> None:
    """``uvicorn main:app`` has an application to serve."""
    assert main.app is not None


def test_an_untrusted_host_is_refused() -> None:
    """A request for a host that is not listed is refused before it reaches the API."""
    response = build_client().get("/health", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_security_headers_are_set_on_success_and_on_refusal() -> None:
    """Every response carries the security headers, including an ingress refusal."""
    client = build_client()
    for response in (client.get("/health"), client.get("/health", headers={"Host": "evil.example"})):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value


def test_cors_allows_only_the_listed_origin() -> None:
    """A listed origin is echoed back. Any other origin gets no CORS header."""
    client = build_client()
    assert client.get("/health", headers={"Origin": ORIGIN}).headers["access-control-allow-origin"] == ORIGIN
    assert (
        "access-control-allow-origin" not in client.get("/health", headers={"Origin": "https://evil.example"}).headers
    )


def test_cors_preflight_is_answered_for_the_listed_origin_only() -> None:
    """A preflight from a listed origin succeeds. One from another origin is refused."""
    client = build_client()
    headers = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    allowed = client.options("/payments/stub/checkout", headers={"Origin": ORIGIN, **headers})
    refused = client.options("/payments/stub/checkout", headers={"Origin": "https://evil.example", **headers})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert refused.status_code == 400


def test_no_origin_is_allowed_by_default() -> None:
    """With no CORS configuration, no origin receives a CORS header."""
    client = TestClient(create_app({"ALLOWED_HOSTS": "testserver"}))
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": ORIGIN}).headers


@pytest.mark.parametrize(
    "environ",
    [
        {"ALLOWED_HOSTS": "*"},
        {"CORS_ALLOWED_ORIGINS": "*"},
        {"CORS_ALLOWED_ORIGINS": "site.example"},
        {"CORS_ALLOWED_ORIGINS": "https://site.example/"},
        {"CORS_ALLOWED_ORIGINS": "https://*.example"},
    ],
)
def test_unsafe_ingress_configuration_stops_the_application(environ: dict[str, str]) -> None:
    """A wildcard host, a wildcard origin or a malformed origin refuses to start."""
    with pytest.raises(IngressConfigError):
        create_app(environ)


def test_an_oversized_body_is_refused() -> None:
    """A body over the limit is refused with 413 before the API sees it."""
    response = build_client().post("/payments/stub/webhook", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413


def test_a_body_without_a_declared_length_is_refused() -> None:
    """A body sent without ``Content-Length`` is refused with 411."""

    def chunks() -> Iterator[bytes]:
        """Yield the body in pieces, so that the client sends it without a declared length.

        Yields:
            The body, one piece at a time.
        """
        yield b'{"order_reference": "order-123", '
        yield b'"status": "paid"}'

    response = build_client().post("/payments/stub/webhook", content=chunks())
    assert response.status_code == 411


@pytest.mark.parametrize("length", ["-500", "²"])
def test_an_invalid_content_length_is_refused_with_security_headers(length: str) -> None:
    """Invalid lengths receive a controlled 400 with every security header."""
    response = build_client().get("/health", headers={b"Content-Length": length.encode("latin-1")})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid Content-Length."}
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_an_unhandled_error_is_generic_and_carries_the_headers() -> None:
    """An unexpected fault becomes a plain 500 that shows nothing internal."""
    database = FakeDatabase(fail_with=RuntimeError("password=hunter2 host=db.internal"))
    response = build_client(database).get("/health/ready")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "hunter2" not in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_shutdown_releases_the_database() -> None:
    """Stopping the application closes the database boundary."""
    database = FakeDatabase()
    with build_client(database) as client:
        assert client.get("/health").status_code == 200
        assert database.closed is False
    assert database.closed is True
