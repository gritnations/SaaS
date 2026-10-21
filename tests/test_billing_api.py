"""Tests for the plan routes and the routing of subscription notifications. Fakes stand in for the database
and the billing service; webhook bodies are signed here.

What these tests establish: sign-up accepts only the free plan and answers with the trial; upgrading and
the subscription portal need sign-in, reach the billing service and map outcomes to answers; a paid plan
cannot be chosen when billing is not configured; a verified subscription notification reaches the billing
service and not the booking payment service. Database behaviour is in ``test_billing_postgres.py``.
"""

from datetime import UTC, datetime
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
TRIAL_END = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class TrialOwners(FakeOwners):
    """An owner service whose new business starts free with a trial."""

    def create_business(self, account: str, new: NewBusiness) -> Membership:
        """Record the call and answer with the new business on its trial.

        Args:
            account: The account.
            new: The sign-up details.

        Returns:
            The membership.
        """
        self.calls.append(("create_business", (account, new)))
        return Membership("new-company", new.name, "owner", new.owner_name, "free", "active", TRIAL_END, True)


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

    def upgrade(self, account: str, slug: str, plan: str) -> str:
        """Answer with a checkout link.

        Args:
            account: The account.
            slug: The business.
            plan: The plan wanted.

        Returns:
            The link.
        """
        self._record("upgrade", account, slug, plan)
        return "https://checkout.stripe.com/c/pay/cs_test_plan"

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
        owners=owners or TrialOwners(),
        verifier=FakeVerifier(),
        payments=payments,
        billing=billing,
    )
    return TestClient(create_app(environ, api=api), raise_server_exceptions=False)


def test_sign_up_starts_free_with_a_trial() -> None:
    """The free plan is the default; the answer carries the trial end and online payments."""
    body = {key: value for key, value in VALID.items() if key != "plan"}
    response = client(FakeBilling()).post("/owner/businesses", headers=GOOD, json=body)
    assert response.status_code == 201
    answer = response.json()
    assert (answer["plan"], answer["plan_status"], answer["online_payments"]) == ("free", "active", True)
    assert answer["trial_ends_at"] == TRIAL_END.isoformat()


@pytest.mark.parametrize("plan", ["starter", "pro"])
def test_a_paid_plan_cannot_be_chosen_at_sign_up(plan: str) -> None:
    """Paid plans are bought later, from the dashboard; sign-up refuses them before the service is called."""
    owners = TrialOwners()
    response = client(FakeBilling(), owners=owners).post(
        "/owner/businesses", headers=GOOD, json={**VALID, "plan": plan}
    )
    assert response.status_code == 422
    assert owners.calls == []


def test_the_plan_routes_need_sign_in() -> None:
    """Without a token nothing reaches the billing service."""
    billing = FakeBilling()
    api = client(billing)
    assert api.post("/owner/businesses/fade/billing/upgrade", json={"plan": "starter"}).status_code == 401
    assert api.post("/owner/businesses/fade/billing/portal").status_code == 401
    assert billing.calls == []


def test_upgrade_and_portal_answer_from_the_billing_service() -> None:
    """Upgrade starts a checkout for the plan asked; the portal opens for the business."""
    billing = FakeBilling()
    api = client(billing)
    response = api.post("/owner/businesses/fade/billing/upgrade", headers=GOOD, json={"plan": "pro"})
    assert response.json() == {"url": "https://checkout.stripe.com/c/pay/cs_test_plan"}
    assert api.post("/owner/businesses/fade/billing/portal", headers=GOOD).json() == {
        "url": "https://billing.stripe.com/p/session/test"
    }
    assert billing.calls == [("upgrade", (ACCOUNT, "fade", "pro")), ("portal", (ACCOUNT, "fade"))]


@pytest.mark.parametrize("body", [{"plan": "free"}, {"plan": "enterprise"}, {"plan": "pro", "price": 1}, {}])
def test_an_upgrade_names_one_paid_plan_and_nothing_else(body: dict[str, Any]) -> None:
    """Only starter or pro is accepted, and no price can be sent."""
    billing = FakeBilling()
    response = client(billing).post("/owner/businesses/fade/billing/upgrade", headers=GOOD, json=body)
    assert response.status_code == 422
    assert billing.calls == []


def test_without_billing_upgrades_are_unavailable() -> None:
    """With no billing service the plan routes answer 503."""
    response = client(None).post("/owner/businesses/fade/billing/upgrade", headers=GOOD, json={"plan": "starter"})
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (NotFoundError("Business not found."), 404),
        (InvalidChangeError("Change or cancel your plan from Manage subscription."), 422),
        (ProviderUnavailableError("Stripe could not start subscription."), 503),
    ],
)
def test_plan_route_outcomes_map_to_answers(failure: Exception, status: int) -> None:
    """Another owner's business is 404; already on a paid plan is 422; a provider failure is 503."""
    response = client(FakeBilling(failure)).post(
        "/owner/businesses/fade/billing/upgrade", headers=GOOD, json={"plan": "starter"}
    )
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


def test_online_payments_are_on_every_plan_until_the_plan_rule_is_switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off (today): every plan, trial or not. On: only paid plans and a running trial."""
    from booking.service import online_payments_allowed

    now = TRIAL_END
    ended = TRIAL_END.replace(year=2025)
    assert online_payments_allowed("free", None, now) and online_payments_allowed("free", ended, now)
    monkeypatch.setattr("booking.service.ONLINE_PAYMENTS_NEED_PAID_PLAN", True)
    assert not online_payments_allowed("free", None, now) and not online_payments_allowed("free", ended, now)
    assert online_payments_allowed("starter", None, now) and online_payments_allowed(
        "free", TRIAL_END.replace(year=2027), now
    )
