"""Tests for the online payment routes: a paid booking's answer, the owner's payment set-up, and the Stripe
webhook. Fakes stand in for the database and the payment service; webhook bodies are signed here.

What these tests establish: a paid booking answers with its checkout link and no manage code; the owner
routes need sign-in and map outcomes to answers; the webhook records only notifications whose signature
verifies, and asks the provider to retry when the database is down; live keys stop the API from starting.
Database behaviour is covered by ``test_payments_postgres.py``.
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN, TRUSTED_HOST, FakeDatabase
from test_owner_api import GOOD, FakeOwners, FakeVerifier
from test_stripe import PAID_SESSION, SECRET, event, sign

from api.main import create_api
from booking.availability import Slot
from booking.payments import PaymentSettings
from booking.service import Business, Confirmation, DayAvailability, NotFoundError, Service
from main import create_app
from payments.base import PaymentEvent, ProviderUnavailableError
from payments.stripe import StripeConfigError
from postgres import DatabaseUnavailableError

STRIPE_ENV = {"STRIPE_SECRET_KEY": "sk_test_placeholder", "STRIPE_WEBHOOK_SECRET": SECRET}
SERVICE = Service("internal", "cut", "Haircut", 30, 2500)
SLOT = Slot(
    "10:00",
    "10:00 to 10:30",
    datetime(2026, 11, 3, 10, 0, tzinfo=UTC),
    datetime(2026, 11, 3, 10, 30, tzinfo=UTC),
    True,
)


class FakeBookings:
    """A booking service that answers every booking with a held, paid booking."""

    def list_services(self, business_slug: str) -> tuple[Business, list[Service]]:
        """Unused.

        Args:
            business_slug: Unused.

        Returns:
            Never returns.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError

    def availability(
        self, business_slug: str, service_slug: str, start: date, days: int
    ) -> tuple[Business, Service, list[DayAvailability]]:
        """Unused.

        Args:
            business_slug: Unused.
            service_slug: Unused.
            start: Unused.
            days: Unused.

        Returns:
            Never returns.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError

    def create_booking(self, business_slug: str, request: Any) -> Confirmation:
        """Answer with a hold waiting for payment.

        Args:
            business_slug: Unused.
            request: Unused.

        Returns:
            The hold.
        """
        return Confirmation("ABCDEFGH23", SERVICE, SLOT, None, "pending_payment", "https://checkout.stripe.com/c/x")


class FakePayments:
    """A payment service answering from fixed data, recording calls, or raising a chosen outcome.

    Attributes:
        fail_with: If set, every call raises it.
        calls: ``(method, args)`` for each call.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        """Create the fake.

        Args:
            fail_with: An exception for every call to raise.
        """
        self.fail_with = fail_with
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _record(self, name: str, *args: Any) -> None:
        """Record a call, then raise the configured outcome, if any.

        Args:
            name: The method.
            *args: Its arguments.

        Raises:
            Exception: The configured outcome.
        """
        self.calls.append((name, args))
        if self.fail_with is not None:
            raise self.fail_with

    def settings(self, account: str, slug: str) -> PaymentSettings:
        """Answer with a connected account that is not ready yet.

        Args:
            account: The account.
            slug: The business.

        Returns:
            The set-up.
        """
        self._record("settings", account, slug)
        return PaymentSettings("stripe", True, False)

    def connect(self, account: str, slug: str) -> str:
        """Answer with a sign-up link.

        Args:
            account: The account.
            slug: The business.

        Returns:
            The link.
        """
        self._record("connect", account, slug)
        return "https://connect.stripe.com/setup/test"

    def record(self, event: PaymentEvent) -> bool:
        """Record the event and report that a booking changed.

        Args:
            event: The verified event.

        Returns:
            ``True``.
        """
        self._record("record", event)
        return True


def client(payments: FakePayments | None = None, stripe: bool = True, **extra: Any) -> TestClient:
    """Build a test client around the full application.

    Args:
        payments: The fake payment service, or ``None``.
        stripe: Whether the Stripe settings are present.
        **extra: Other services for :func:`api.main.create_api`.

    Returns:
        A client that reports server errors as responses.
    """
    environ = {"APP_ENV": "test", "ALLOWED_HOSTS": TRUSTED_HOST, "CORS_ALLOWED_ORIGINS": ORIGIN}
    if stripe:
        environ.update(STRIPE_ENV)
    api = create_api(
        environ, database=FakeDatabase(), owners=FakeOwners(), verifier=FakeVerifier(), payments=payments, **extra
    )
    return TestClient(create_app(environ, api=api), raise_server_exceptions=False)


def test_a_paid_booking_answers_with_its_checkout_link_and_no_manage_code() -> None:
    """The customer is sent to pay; the manage link comes by email once the payment is confirmed."""
    body = {"service": "cut", "date": "2026-11-03", "time": "10:00", "name": "Sam", "email": "sam@example.com"}
    response = client(bookings=FakeBookings()).post("/businesses/fade/bookings", json=body)
    assert response.status_code == 201
    answer = response.json()
    assert (answer["status"], answer["checkout_url"], answer["manage_token"]) == (
        "pending_payment",
        "https://checkout.stripe.com/c/x",
        None,
    )


def test_the_owner_payment_routes_need_sign_in() -> None:
    """Without a token nothing reaches the payment service."""
    payments = FakePayments()
    api = client(payments)
    assert api.get("/owner/businesses/fade/payments").status_code == 401
    assert api.post("/owner/businesses/fade/payments/connect").status_code == 401
    assert payments.calls == []


def test_the_owner_sees_the_set_up_and_gets_a_sign_up_link() -> None:
    """The set-up and the link come from the payment service, for the signed-in account."""
    payments = FakePayments()
    api = client(payments)
    assert api.get("/owner/businesses/fade/payments", headers=GOOD).json() == {
        "available": True,
        "provider": "stripe",
        "connected": True,
        "enabled": False,
    }
    response = api.post("/owner/businesses/fade/payments/connect", headers=GOOD)
    assert (response.status_code, response.json()) == (200, {"url": "https://connect.stripe.com/setup/test"})
    assert [name for name, _ in payments.calls] == ["settings", "connect"]


def test_without_online_payments_the_set_up_says_so() -> None:
    """With no payment service the set-up is unavailable and connecting answers 503."""
    api = client(None, stripe=False)
    assert api.get("/owner/businesses/fade/payments", headers=GOOD).json() == {
        "available": False,
        "provider": None,
        "connected": False,
        "enabled": False,
    }
    assert api.post("/owner/businesses/fade/payments/connect", headers=GOOD).status_code == 503


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (NotFoundError("Business not found."), 404),
        (ProviderUnavailableError("Stripe could not create account."), 503),
        (DatabaseUnavailableError("down"), 503),
    ],
)
def test_connect_outcomes_map_to_answers(failure: Exception, status: int) -> None:
    """Another owner's business is 404; a provider or database failure is 503 with a fixed message."""
    response = client(FakePayments(failure)).post("/owner/businesses/fade/payments/connect", headers=GOOD)
    assert response.status_code == status
    assert "Stripe could not" not in response.text


def test_a_verified_notification_is_recorded() -> None:
    """A correctly signed paid notification reaches the payment service with its verified fields."""
    payments = FakePayments()
    body = event("checkout.session.completed", PAID_SESSION)
    response = client(payments).post("/payments/stripe/webhook", content=body, headers=sign(body))
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "provider": "stripe", "live": False, "recorded": True}
    recorded = payments.calls[0][1][0]
    assert (recorded.status, recorded.checkout_id, recorded.amount_minor) == ("paid", "cs_test_1", 2500)


def test_a_badly_signed_notification_is_rejected_and_not_recorded() -> None:
    """A wrong signature is a 400 and the payment service is never called."""
    payments = FakePayments()
    body = event("checkout.session.completed", PAID_SESSION)
    response = client(payments).post("/payments/stripe/webhook", content=body, headers=sign(body, "whsec_other"))
    assert response.status_code == 400
    assert payments.calls == []


def test_a_notification_is_retried_when_the_database_is_down() -> None:
    """A database failure answers 503, so Stripe sends the notification again later."""
    body = event("checkout.session.completed", PAID_SESSION)
    payments = FakePayments(DatabaseUnavailableError("down"))
    response = client(payments).post("/payments/stripe/webhook", content=body, headers=sign(body))
    assert response.status_code == 503


def test_without_stripe_settings_the_stripe_webhook_does_not_exist() -> None:
    """Stripe is registered only when configured."""
    body = event("checkout.session.completed", PAID_SESSION)
    response = client(FakePayments(), stripe=False).post("/payments/stripe/webhook", content=body, headers=sign(body))
    assert response.status_code == 404


def test_live_keys_stop_the_api_from_starting() -> None:
    """Only test-mode keys are accepted until live payments are approved."""
    with pytest.raises(StripeConfigError):
        create_api({"APP_ENV": "test", "STRIPE_SECRET_KEY": "sk_live_x", "STRIPE_WEBHOOK_SECRET": SECRET})
