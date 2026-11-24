"""Tests for the reminder run route, with a fake reminder service and no database.

What these tests establish: the route answers 404 and runs nothing without ``REMINDERS_TOKEN``; it refuses a
missing or wrong token; with the right token it runs the reminders and returns the counts; it answers 503 when the
database is unavailable or absent; a short token stops the API from starting; and the reminder email itself carries
no manage link and escapes what the customer typed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN, TRUSTED_HOST

from api.main import ApiConfigError, create_api
from booking.notify import Context, reminder_message
from booking.reminders import RunResult
from main import create_app
from postgres import DatabaseUnavailableError

TOKEN = "r" * 40
PATH = "/internal/reminders/run"


class FakeReminders:
    """Records runs and returns fixed counts, or fails on demand.

    Attributes:
        runs: How many times :meth:`run` was called.
        fail: When ``True``, :meth:`run` raises :class:`postgres.DatabaseUnavailableError`.
    """

    def __init__(self) -> None:
        """Create the fake."""
        self.runs = 0
        self.fail = False

    def run(self) -> RunResult:
        """Record a run.

        Returns:
            Fixed counts.

        Raises:
            DatabaseUnavailableError: When :attr:`fail` is set.
        """
        if self.fail:
            raise DatabaseUnavailableError("The database is unavailable.")
        self.runs += 1
        return RunResult(claimed=2, sent=1, failed=1)


def client(reminders: FakeReminders | None, **overrides: str) -> TestClient:
    """Build the full application around a fake reminder service.

    Args:
        reminders: The fake, or ``None`` for no reminder service.
        **overrides: Environment values to add.

    Returns:
        A test client.
    """
    environ = {"APP_ENV": "production", "ALLOWED_HOSTS": TRUSTED_HOST, "CORS_ALLOWED_ORIGINS": ORIGIN, **overrides}
    return TestClient(create_app(environ, api=create_api(environ, reminders=reminders)), raise_server_exceptions=False)


def test_the_route_answers_404_without_a_token() -> None:
    """No ``REMINDERS_TOKEN``: 404, and nothing runs."""
    fake = FakeReminders()
    response = client(fake).post(PATH, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 404
    assert fake.runs == 0


@pytest.mark.parametrize("header", [None, "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN}x"])
def test_a_missing_or_wrong_token_is_refused(header: str | None) -> None:
    """Any header other than the exact bearer token: 401, and nothing runs."""
    fake = FakeReminders()
    headers = {"Authorization": header} if header else {}
    response = client(fake, REMINDERS_TOKEN=TOKEN).post(PATH, headers=headers)
    assert response.status_code == 401
    assert fake.runs == 0


def test_the_right_token_runs_the_reminders() -> None:
    """The exact token: one run, and its counts in the answer."""
    fake = FakeReminders()
    response = client(fake, REMINDERS_TOKEN=TOKEN).post(PATH, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {"claimed": 2, "sent": 1, "skipped": 0, "failed": 1, "dropped": 0}
    assert fake.runs == 1


def test_an_unavailable_database_answers_503() -> None:
    """The run cannot reach the database: 503, so the clock tries again next time."""
    fake = FakeReminders()
    fake.fail = True
    response = client(fake, REMINDERS_TOKEN=TOKEN).post(PATH, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 503


def test_no_database_answers_503() -> None:
    """A token but no database and no reminder service: 503."""
    response = client(None, REMINDERS_TOKEN=TOKEN).post(PATH, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 503


def test_a_short_token_stops_the_api_from_starting() -> None:
    """A token under 32 characters is a configuration error, and the message does not contain it."""
    with pytest.raises(ApiConfigError) as caught:
        create_api({"APP_ENV": "test", "REMINDERS_TOKEN": "short-secret"})
    assert "short-secret" not in str(caught.value)
    assert "REMINDERS_TOKEN" in str(caught.value)


def test_the_reminder_has_no_manage_link_and_escapes_the_customer_name() -> None:
    """Even with a manage link loaded, the reminder leaves it out; typed text is escaped in the HTML."""
    start = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
    context = Context(
        kind="confirmed",
        business_name="Test & Co",
        business_slug="test-co",
        time_zone="Europe/London",
        contact_email="owner@example.com",
        meeting_link=None,
        reference="ZA2JBN2C5B",
        service_name="Hour",
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        customer_name="Pat <b>Customer</b>",
        customer_email="pat@example.com",
        customer_phone=None,
        customer_note=None,
        cancelled_by=None,
        sequence=0,
        manage_url="https://book.example.com/#manage=test-co.ZA2JBN2C5B." + "a" * 48,
        payment_status="at_venue",
        price_minor=2500,
        currency="GBP",
    )
    message = reminder_message(context)
    assert message.to == "pat@example.com"
    assert message.subject == "Reminder: Hour with Test & Co"
    assert "#manage" not in message.html and "#manage" not in message.text
    assert "Pat &lt;b&gt;Customer&lt;/b&gt;" in message.html
    assert "Tue 1 Dec 2026, 09:00 to 10:00 (UK time)" in message.text
    assert "Pay at the venue: £25.00" in message.text
    assert message.attachments == ()
