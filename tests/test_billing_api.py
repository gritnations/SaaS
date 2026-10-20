"""Tests for the paid-plan routes and the routing of subscription notifications. Fakes stand in for the
database and the billing service; webhook bodies are signed here.

What these tests establish: a paid plan is refused when billing is not configured; choosing one answers
with its checkout link, and a provider failure leaves the business waiting rather than failing sign-up;
the plan routes need sign-in and map outcomes to answers; a verified subscription notification reaches the
billing service and not the booking payment service. Database behaviour is in ``test_billing_postgres.py``.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN, TRUSTED_HOST, FakeDatabase
from test_owner_api import ACCOUNT, GOOD, FakeOwners, FakeVerifier
from test_payments_api import FakePayments
from test_signup_api import VALID
from test_stripe import SECRET, sign
from test_stripe_billing import BILLING_SECRET, SUBSCRIBED, platform_event

from api.main import create_api
from booking.owner import InvalidChangeError, Membership, NewBusiness
from booking.service import NotFoundError
from main import create_app
from payments.base import PaymentEvent, ProviderUnavailableError

ENV = {
    "STRIPE_SECRET_KEY": "sk_test_placeholder",
    "STRIPE_WEBHOOK_SECRET": SECRET,
    "STRIPE_BILLING_WEBHOOK_SECRET": BILLING_SECRET,
}


class PlanOwners(FakeOwners):
    """An owner service whose new business keeps the chosen plan, waiting for payment when it is paid."""

    def create_business(self, account: str, new: NewBusiness) -> Membership:
        """Record the call and answer with the new business on its plan.

        Args:
            account: The account.
            new: The sign-up details.

        Returns:
            The membership.
        """
        self.calls.append(("create_business", (account, new)))
        status = "active" if new.plan == "free" else "awaiting_payment"
        return Membership("new-company", new.name, "owner", new.owner_name, new.plan, status)


class FakeBilling:
    """A billing service recording calls, answering fixed links, or raising a chosen outcome.

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

    def start_checkout(self, account: str, slug: str) -> str:
        """Answer with a checkout link.

        Args:
            account: The account.
            slug: The business.

        Returns:
            The link.
        """
        self._record("start_checkout", account, slug)
        return "https://checkout.stripe.com/c/pay/cs_test_plan"

    def choose_free(self, account: str, slug: str) -> Membership:
        """Answer with the business on the free plan.

        Args:
            account: The account.
            slug: The business.

        Returns:
            The membership.
        """
        self._record("choose_free", account, slug)
        return Membership(slug, "Fade & Co", "owner", None, "free", "active")

    def portal(self, account: str, slug: str) -> str:
        """Answer with a portal link.

        Args:
            account: The account.
            slug: The business.

        Returns:
            The link.
        """
        self._record("portal", account, slug)
        return "https://billing.stripe.com/p/session/test"

    def record(self, event: PaymentEvent) -> bool:
        """Record the event.

        Args:
            event: The verified event.

        Returns:
            ``True``.
        """
        self._record("record", event)
        return True


def client(
    billing: FakeBilling | None, payments: FakePayments | None = None, owners: FakeOwners | None = None
) -> TestClient:
    """Build a test client around the full application with Stripe configured.

    Args:
        billing: The fake billing service, or ``None`` for paid plans not offered.
        payments: The fake payment service.
        owners: The fake owner service.

    Returns:
        A client that reports server errors as responses.
    """
    environ = {"APP_ENV": "test", "ALLOWED_HOSTS": TRUSTED_HOST, "CORS_ALLOWED_ORIGINS": ORIGIN, **ENV}
    api = create_api(
        environ,
        database=FakeDatabase(),
        owners=owners or PlanOwners(),
        verifier=FakeVerifier(),
        payments=payments,
        billing=billing,
    )
    return TestClient(create_app(environ, api=api), raise_server_exceptions=False)


def test_a_paid_plan_is_refused_when_billing_is_not_offered() -> None:
    """Without billing a paid plan answers 503 and no business is created."""
    owners = PlanOwners()
    response = client(None, owners=owners).post("/owner/businesses", headers=GOOD, json={**VALID, "plan": "pro"})
    assert response.status_code == 503
    assert owners.calls == []


def test_choosing_a_paid_plan_answers_with_its_checkout() -> None:
    """The business waits for payment and the owner is sent to the checkout."""
    billing = FakeBilling()
    response = client(billing).post("/owner/businesses", headers=GOOD, json={**VALID, "plan": "starter"})
    assert response.status_code == 201
    answer = response.json()
    assert (answer["plan"], answer["plan_status"], answer["checkout_url"]) == (
        "starter",
        "awaiting_payment",
        "https://checkout.stripe.com/c/pay/cs_test_plan",
    )
    assert billing.calls == [("start_checkout", (ACCOUNT, "new-company"))]


def test_a_provider_failure_leaves_the_business_waiting_instead_of_failing_sign_up() -> None:
    """Sign-up still succeeds; the owner pays later from the dashboard."""
    billing = FakeBilling(ProviderUnavailableError("Stripe could not start subscription."))
    response = client(billing).post("/owner/businesses", headers=GOOD, json={**VALID, "plan": "pro"})
    assert response.status_code == 201
    assert (response.json()["plan_status"], response.json()["checkout_url"]) == ("awaiting_payment", None)


def test_the_free_plan_needs_no_billing() -> None:
    """Free sign-up works with or without billing and starts no checkout."""
    billing = FakeBilling()
    response = client(billing).post("/owner/businesses", headers=GOOD, json=VALID)
    assert (response.status_code, response.json()["plan_status"], response.json()["checkout_url"]) == (
        201,
        "active",
        None,
    )
    assert billing.calls == []


def test_the_plan_routes_need_sign_in() -> None:
    """Without a token nothing reaches the billing service."""
    billing = FakeBilling()
    api = client(billing)
    for path in ("checkout", "free", "portal"):
        assert api.post(f"/owner/businesses/fade/billing/{path}").status_code == 401
    assert billing.calls == []


def test_the_plan_routes_answer_from_the_billing_service() -> None:
    """Pay now, switch to free and manage subscription each reach the service for the signed-in account."""
    billing = FakeBilling()
    api = client(billing)
    assert api.post("/owner/businesses/fade/billing/checkout", headers=GOOD).json() == {
        "url": "https://checkout.stripe.com/c/pay/cs_test_plan"
    }
    assert api.post("/owner/businesses/fade/billing/free", headers=GOOD).json()["plan"] == "free"
    assert api.post("/owner/businesses/fade/billing/portal", headers=GOOD).json() == {
        "url": "https://billing.stripe.com/p/session/test"
    }
    assert [name for name, _ in billing.calls] == ["start_checkout", "choose_free", "portal"]


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (NotFoundError("Business not found."), 404),
        (InvalidChangeError("This business is not waiting to pay for a plan."), 422),
        (ProviderUnavailableError("Stripe could not start subscription."), 503),
    ],
)
def test_plan_route_outcomes_map_to_answers(failure: Exception, status: int) -> None:
    """Another owner's business is 404; nothing to pay is 422; a provider failure is 503."""
    response = client(FakeBilling(failure)).post("/owner/businesses/fade/billing/checkout", headers=GOOD)
    assert response.status_code == status
    assert "Stripe could not" not in response.text


def test_a_subscription_notification_reaches_billing_not_booking_payments() -> None:
    """A verified platform event is recorded by the billing service only."""
    billing, payments = FakeBilling(), FakePayments()
    body = platform_event("checkout.session.completed", SUBSCRIBED)
    response = client(billing, payments).post(
        "/payments/stripe/webhook", content=body, headers=sign(body, BILLING_SECRET)
    )
    assert (response.status_code, response.json()["recorded"]) == (202, True)
    assert [name for name, _ in billing.calls] == ["record"] and payments.calls == []
    assert billing.calls[0][1][0].status == "subscription_active"
