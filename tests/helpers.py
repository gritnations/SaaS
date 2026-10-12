"""Shared helpers for the platform tests. No test lives in this module."""

from fastapi.testclient import TestClient

from api.main import create_api
from main import create_app

TRUSTED_HOST = "testserver"
"""The host name the test client sends, listed in ``ALLOWED_HOSTS`` by :func:`build_client`."""

ORIGIN = "https://site.example"
"""The one browser origin allowed by :func:`build_client`."""


class FakeDatabase:
    """A stand-in for the database boundary, so that no test needs a real PostgreSQL server.

    Attributes:
        healthy: The answer :meth:`is_healthy` gives.
        fail_with: If set, :meth:`is_healthy` raises it instead of answering.
        closed: Set to ``True`` once :meth:`close` has been called.
    """

    def __init__(self, healthy: bool = True, fail_with: Exception | None = None) -> None:
        """Create the fake.

        Args:
            healthy: The health answer to give.
            fail_with: An exception to raise from the health check, to simulate an unexpected fault.
        """
        self.healthy = healthy
        self.fail_with = fail_with
        self.closed = False

    def is_healthy(self) -> bool:
        """Give the configured health answer.

        Returns:
            The value of :attr:`healthy`.

        Raises:
            Exception: Whatever was passed as ``fail_with``.
        """
        if self.fail_with is not None:
            raise self.fail_with
        return self.healthy

    def close(self) -> None:
        """Record that the application released the database."""
        self.closed = True


def build_client(database: FakeDatabase | None = None, **overrides: str) -> TestClient:
    """Build a test client around the full application: root ingress plus API.

    Args:
        database: The fake database to attach. ``None`` leaves the database unconfigured.
        **overrides: Environment values that replace or extend the test defaults.

    Returns:
        A client that reports server errors as responses and does not raise them.
    """
    environ = {"APP_ENV": "test", "ALLOWED_HOSTS": TRUSTED_HOST, "CORS_ALLOWED_ORIGINS": ORIGIN, **overrides}
    api = create_api(environ, database=database)
    return TestClient(create_app(environ, api=api), raise_server_exceptions=False)
