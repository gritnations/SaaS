"""Shared value types for the platform's internal contracts.

Each common format is defined here once, so that every internal record checks it the same way. The records
themselves stay with the code that owns them (for example the payment records in ``payments/base.py``).

Every internal record uses :data:`STRICT`:
    * ``frozen``: a record cannot be changed after it is built, as with the frozen dataclasses it replaces.
    * ``strict``: no silent conversion. The string ``"1500"`` is not accepted where an integer is expected.
    * ``extra="forbid"``: an unexpected field is an error, not ignored.

The API's request and response models keep their own settings. These types are for records passed inside the
platform, after the API has accepted a request or a provider adapter has verified a notification.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, StringConstraints

__all__ = [
    "AwareDatetime",
    "Currency",
    "ORDER_REFERENCE_PATTERN",
    "OrderReference",
    "PaidAmountMinor",
    "ProviderId",
    "ProviderName",
    "ReportedAmountMinor",
    "ReturnUrl",
    "STRICT",
    "SecureUrl",
]

STRICT = ConfigDict(frozen=True, strict=True, extra="forbid")
"""The settings every internal contract uses."""

ORDER_REFERENCE_PATTERN = r"^[A-Za-z0-9_-]{6,64}$"
"""The accepted shape of an order reference: 6 to 64 letters, digits, hyphens or underscores."""

MAX_AMOUNT_MINOR = 10_000_000
"""The largest price the platform accepts, in minor units. The same limit the owner API applies to prices."""

Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
"""An upper-case ISO 4217 currency code, such as ``GBP``."""

PaidAmountMinor = Annotated[int, Field(gt=0, le=MAX_AMOUNT_MINOR)]
"""An amount the platform asks a customer or business to pay, in minor units. Never zero."""

ReportedAmountMinor = Annotated[int, Field(ge=0)]
"""An amount as a payment provider reports it, in minor units. It has no upper limit here: it is compared with the
price in the database, and a mismatch is handled there."""

ProviderName = Literal["stripe", "paymob", "stub"]
"""The payment providers the platform knows."""

ProviderId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]{1,255}$")]
"""An identifier issued by a payment provider: a checkout, payment, account, customer, subscription or event."""

OrderReference = Annotated[str, StringConstraints(pattern=ORDER_REFERENCE_PATTERN)]
"""A reference to an order, as accepted at the payment boundary."""

SecureUrl = Annotated[str, StringConstraints(pattern=r"^https://[^\s/?#]+(?:[/?#]\S*)?$", max_length=2048)]
"""An ``https://`` link, such as a provider's hosted checkout page."""

ReturnUrl = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:https://[^\s/?#]+|http://(?:localhost|127\.0\.0\.1)(?::\d{1,5})?)(?:[/?#]\S*)?$",
        max_length=2048,
    ),
]
"""Where a provider sends the payer back to: ``https://``, or ``http://localhost`` in development."""
