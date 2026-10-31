"""Plan billing: a business's monthly subscription to a paid plan, paid to the platform.

Trust path: ``api -> booking.billing -> postgres.Database -> PostgreSQL``, with the provider reached through
``payments.base.SubscriptionBilling``.

Every business starts on the free plan with a trial, and upgrades from its dashboard. The business stays
open throughout: the upgrade applies only when a verified subscription event says the plan is paid. The
price of each plan is decided here, on the server (:data:`booking.owner.PLAN_PRICES`). Recording is
idempotent, and an event changes a business only when its business and subscription match what is stored.

State transitions of a business's plan:
    * free (trial or not) + subscription active -> the plan the subscription was started for, ``active``.
    * ``active`` + renewal failed -> ``past_due``; ``past_due`` + paid again -> ``active``.
    * any paid plan + subscription ended -> the free plan, ``active``. Online payments then stop counting.
    * a subscription set to cancel later keeps its plan; ``plan_ends_at`` records when it will end.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from booking.owner import PLAN_CURRENCY, PLAN_PRICES, InvalidChangeError
from booking.service import NotFoundError
from payments.base import PaymentEvent, SubscriptionBilling, SubscriptionOrder
from postgres import Database

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PlanRow:
    """A business's plan fields, loaded for one operation.

    Attributes:
        id: Internal identifier.
        slug: Public identifier.
        name: Display name.
        currency: ISO 4217 code.
        contact_email: Contact email, or ``None``.
        plan: The plan.
        plan_status: The plan's status.
        customer_id: The provider's customer record, or ``None``.
        subscription_id: The provider's subscription, or ``None``.
        role: The account's role.
        display_name: The owner's private name, or ``None``.
    """

    id: str
    slug: str
    name: str
    currency: str
    contact_email: str | None
    plan: str
    plan_status: str
    customer_id: str | None
    subscription_id: str | None
    role: str
    display_name: str | None


class BillingBackend(Protocol):
    """What the API needs from the billing service. Tests substitute a fake."""

    def upgrade(self, account: str, slug: str, plan: str) -> str:
        """See :meth:`BillingService.upgrade`.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            plan: The paid plan wanted.

        Returns:
            The checkout link.
        """
        ...

    def portal(self, account: str, slug: str) -> str:
        """See :meth:`BillingService.portal`.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The portal link.
        """
        ...

    def record(self, event: PaymentEvent) -> bool:
        """See :meth:`BillingService.record`.

        Args:
            event: A verified subscription event.

        Returns:
            Whether a business changed.
        """
        ...


class BillingService:
    """Plan subscriptions, backed by PostgreSQL and one billing provider."""

    def __init__(self, database: Database, provider: SubscriptionBilling, site_url: str) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
            provider: The billing provider.
            site_url: The public site, without a trailing slash, that the provider sends owners back to.
        """
        self._database = database
        self._provider = provider
        self._site_url = site_url

    @staticmethod
    def _member(connection: psycopg.Connection[Any], account: str, slug: str) -> _PlanRow:
        """Load the plan fields of a business the account manages, locked until the transaction ends.

        Args:
            connection: An open connection.
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The business.

        Raises:
            NotFoundError: If the business does not exist or the account does not manage it.
        """
        row = connection.execute(
            "SELECT b.id::text, b.slug, b.name, b.currency, b.contact_email, b.plan, b.plan_status, "
            "b.billing_customer_id, b.billing_subscription_id, m.role, m.display_name "
            "FROM businesses b JOIN business_members m ON m.business_id = b.id "
            "WHERE b.slug = %s AND m.user_id = %s FOR UPDATE OF b",
            (slug, account),
        ).fetchone()
        if row is None:
            raise NotFoundError("Business not found.")
        return _PlanRow(*row)

    def upgrade(self, account: str, slug: str, plan: str) -> str:
        """Start the subscription checkout that moves a free business (trial or not) to a paid plan.

        The business keeps its current plan until the subscription is confirmed.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            plan: The paid plan wanted.

        Returns:
            The provider's checkout link.

        Raises:
            NotFoundError: If the account does not manage the business.
            InvalidChangeError: If the plan is not a paid plan, or the business already has one: plans are
                changed or cancelled through the provider's portal.
            payments.base.ProviderUnavailableError: If the provider could not be reached.
        """
        if plan not in PLAN_PRICES:
            raise InvalidChangeError("That plan is not available.")
        with self._database.connection() as connection:
            business = self._member(connection, account, slug)
        if business.plan != "free" or business.subscription_id is not None:
            raise InvalidChangeError("Change or cancel your plan from Manage subscription.")
        plan_name, amount = PLAN_PRICES[plan]
        order = SubscriptionOrder(
            business_id=business.id,
            plan=plan,
            plan_name=plan_name,
            amount_minor=amount,
            currency=PLAN_CURRENCY,
            customer_email=business.contact_email,
            customer_id=business.customer_id,
            success_url=f"{self._site_url}/#billing=done",
            cancel_url=f"{self._site_url}/#billing=cancelled",
        )
        checkout = self._provider.start_subscription(order)
        LOGGER.info("plan checkout started business=%s plan=%s", slug, plan)
        return checkout.checkout_url

    def portal(self, account: str, slug: str) -> str:
        """Get the provider's page where the business manages its subscription.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The provider's link.

        Raises:
            NotFoundError: If the account does not manage the business.
            InvalidChangeError: If the business has never subscribed.
            payments.base.ProviderUnavailableError: If the provider could not be reached.
        """
        with self._database.connection() as connection:
            business = self._member(connection, account, slug)
        if business.customer_id is None:
            raise InvalidChangeError("This business has no subscription to manage.")
        return self._provider.billing_portal_url(business.customer_id, f"{self._site_url}/#billing=portal")

    def record(self, event: PaymentEvent) -> bool:
        """Apply a verified subscription event to its business.

        Args:
            event: An event whose signature the provider adapter has verified.

        Returns:
            ``True`` when a business changed, ``False`` when the event needed nothing or did not match.

        Raises:
            postgres.DatabaseUnavailableError: If the database cannot be reached, so the provider retries.
        """
        if event.status not in ("subscription_active", "subscription_past_due", "subscription_ended"):
            return False
        try:
            business_id = str(uuid.UUID(event.order_reference))
        except ValueError:
            LOGGER.warning("subscription event without a business provider=%s", event.provider)
            return False
        if not event.subscription_id:
            return False
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT slug, plan, plan_status, billing_subscription_id, plan_ends_at FROM businesses "
                "WHERE id = %s FOR UPDATE",
                (business_id,),
            ).fetchone()
            if row is None:
                LOGGER.warning("subscription event for an unknown business provider=%s", event.provider)
                return False
            slug, plan, status, subscription, ends_at = row
            same = subscription == event.subscription_id
            new_ends = event.ends_at if event.ends_known else ends_at
            if event.status == "subscription_active":
                if event.plan not in PLAN_PRICES:
                    LOGGER.error("subscription event names no paid plan business=%s", slug)
                    return False
                if not same and subscription is not None:
                    LOGGER.error("second subscription for a business already on a plan business=%s", slug)
                    return False
                if same and status == "active" and plan == event.plan and new_ends == ends_at:
                    return False
                connection.execute(
                    "UPDATE businesses SET plan = %s, plan_status = 'active', billing_subscription_id = %s, "
                    "billing_customer_id = COALESCE(%s, billing_customer_id), plan_ends_at = %s WHERE id = %s",
                    (
                        event.plan,
                        event.subscription_id,
                        event.customer_id,
                        new_ends if same else event.ends_at,
                        business_id,
                    ),
                )
            elif not same:
                return False
            elif event.status == "subscription_past_due":
                if status == "past_due" and new_ends == ends_at:
                    return False
                connection.execute(
                    "UPDATE businesses SET plan_status = 'past_due', plan_ends_at = %s WHERE id = %s",
                    (new_ends, business_id),
                )
            else:
                connection.execute(
                    "UPDATE businesses SET plan = 'free', plan_status = 'active', billing_subscription_id = NULL, "
                    "plan_ends_at = NULL WHERE id = %s",
                    (business_id,),
                )
        LOGGER.info("plan %s business=%s", event.status, slug)
        return True
