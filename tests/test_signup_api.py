"""Tests for the create-business route used by self sign-up. A fake owner service stands in for the database.

What these tests establish: the route needs a valid sign-in, validates every field before the service is
called, offers only the listed business types and the free plan, and maps outcomes to answers. Database
behaviour is covered by ``test_signup_postgres.py``.
"""

from typing import Any

import pytest
from test_owner_api import GOOD, FakeOwners, client_for

from booking.owner import ConflictError, InvalidChangeError

VALID = {
    "name": "Fade & Co Barbers",
    "business_type": "barbershop",
    "owner_name": "Sample Owner",
    "plan": "free",
    "contact_email": "owner@example.com",
}


def signed_in(owners: FakeOwners) -> Any:
    """Build a client with sign-in configured.

    Args:
        owners: The fake owner service.

    Returns:
        The client.
    """
    from test_owner_api import FakeVerifier

    return client_for(owners, FakeVerifier())


def test_creating_a_business_needs_sign_in() -> None:
    """Without a token the route answers 401 and nothing is created."""
    owners = FakeOwners()
    response = signed_in(owners).post("/owner/businesses", json=VALID)
    assert response.status_code == 401
    assert owners.calls == []


def test_a_valid_sign_up_creates_the_business() -> None:
    """The details reach the service cleaned, and the answer names the new business."""
    owners = FakeOwners()
    response = signed_in(owners).post(
        "/owner/businesses", headers=GOOD, json={**VALID, "name": "  Fade & Co Barbers  ", "business_type_other": ""}
    )
    assert response.status_code == 201
    assert response.json() == {
        "slug": "new-company",
        "name": "Fade & Co Barbers",
        "role": "owner",
        "display_name": "Sample Owner",
    }
    new = owners.calls[0][1][1]
    assert (new.name, new.business_type, new.business_type_other, new.plan) == (
        "Fade & Co Barbers",
        "barbershop",
        None,
        "free",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"plan": "pro"},
        {"plan": "paid"},
        {"business_type": "casino"},
        {"business_type": "Barbershop"},
        {"name": ""},
        {"name": "x" * 121},
        {"owner_name": ""},
        {"contact_email": "not-an-email"},
        {"business_type_other": "x" * 61},
        {"slug": "chosen-by-client"},
    ],
)
def test_invalid_sign_ups_are_rejected_before_the_service(change: dict[str, Any]) -> None:
    """Paid or unknown plans, unknown types, bad names or email, or a client-chosen slug are refused."""
    owners = FakeOwners()
    response = signed_in(owners).post("/owner/businesses", headers=GOOD, json={**VALID, **change})
    assert response.status_code == 422
    assert owners.calls == []


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (ConflictError("This account already has a business."), 409),
        (InvalidChangeError("Describe the business when its type is other."), 422),
    ],
)
def test_sign_up_outcomes_map_to_answers(outcome: Exception, status: int) -> None:
    """A second business is a conflict; an undescribed other type is invalid."""
    response = signed_in(FakeOwners(fail_with=outcome)).post("/owner/businesses", headers=GOOD, json=VALID)
    assert response.status_code == status
    assert response.json() == {"detail": str(outcome)}
