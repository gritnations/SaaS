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

import logging
import re
import unicodedata
from collections.abc import Sequence
from functools import partial
from typing import Annotated, Any, Literal, TypeVar

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

__all__ = [
    "AwareDatetime",
    "BUSINESS_SLUG_PATTERN",
    "BookingStatus",
    "BreakMinutes",
    "BusinessName",
    "BusinessSlug",
    "CancelledBy",
    "Currency",
    "CustomerEmail",
    "CustomerName",
    "CustomerNote",
    "CustomerPhone",
    "CutoffMinutes",
    "DisplayName",
    "DurationMinutes",
    "EMAIL_PATTERN",
    "EventKind",
    "IdempotencyKey",
    "ManageToken",
    "MeetingLink",
    "NoticeMinutes",
    "ORDER_REFERENCE_PATTERN",
    "OrderReference",
    "OwnerAddress",
    "OwnerBusinessName",
    "OwnerBusinessType",
    "OwnerEmail",
    "OwnerName",
    "OwnerServiceName",
    "PHONE_PATTERN",
    "PaidAmountMinor",
    "PaymentStatus",
    "Plan",
    "PlanStatus",
    "PriceMinor",
    "ProviderId",
    "ProviderName",
    "Reference",
    "ReportedAmountMinor",
    "ReturnUrl",
    "Role",
    "SERVICE_SLUG_PATTERN",
    "SITE_URL_PATTERN",
    "STRICT",
    "SecureUrl",
    "ServiceName",
    "ServiceSlug",
    "SiteUrl",
    "SortOrder",
    "StoredAddress",
    "StoredEmail",
    "StoredLink",
    "StoredPhone",
    "TextRefusedError",
    "TimeOffNote",
    "TimeZoneName",
    "Weekday",
    "WindowDays",
    "clean_business_name",
    "clean_email",
    "clean_name",
    "clean_note",
    "from_row",
    "scripts",
]

LOGGER = logging.getLogger(__name__)

RecordT = TypeVar("RecordT", bound=BaseModel)

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

SITE_URL_PATTERN = r"^(https://[a-z0-9.-]+|http://(localhost|127\.0\.0\.1)(:\d{1,5})?)(/[A-Za-z0-9._~/-]*)?$"
"""A site the platform links customers back to: ``https://``, or ``http://localhost`` in development. No query or
fragment, because links to pages are built by adding to it."""

SiteUrl = Annotated[str, StringConstraints(pattern=SITE_URL_PATTERN)]
"""A site URL that matches :data:`SITE_URL_PATTERN`."""

BUSINESS_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$"
"""A business's web name: 3 to 40 lower-case letters, digits or hyphens, not starting or ending with a hyphen. The
same rule as the database's check on ``businesses.slug``."""

SERVICE_SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$"
"""A service's web name: 2 to 40 characters, otherwise as a business's. The same rule as the database's check on
``services.slug``."""

BusinessSlug = Annotated[str, StringConstraints(pattern=BUSINESS_SLUG_PATTERN)]
"""A business's web name."""

ServiceSlug = Annotated[str, StringConstraints(pattern=SERVICE_SLUG_PATTERN)]
"""A service's web name."""

Reference = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{10}$")]
"""A booking's public reference."""

ManageToken = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{48}$")]
"""The code that lets a customer manage one booking. Only its hash is stored."""

IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,128}$")]
"""The random key a booking page sends with one attempt, so that a repeat returns the booking already made."""

BookingStatus = Literal["pending_payment", "confirmed", "cancelled", "expired"]
"""A booking's status. Must equal the database's check list on ``bookings.status``."""

PaymentStatus = Literal["not_required", "awaiting", "paid", "at_venue", "refunded"]
"""A booking's payment status. Must equal the database's check list on ``bookings.payment_status``."""

Plan = Literal["free", "starter", "pro"]
"""A business's plan. Must equal the database's check list on ``businesses.plan``."""

PriceMinor = Annotated[int, Field(ge=0, le=MAX_AMOUNT_MINOR)]
"""A service's price in minor units. ``0`` means free."""

DurationMinutes = Annotated[int, Field(ge=5, le=480)]
"""A service's session length in minutes. The same limits as the database."""

SortOrder = Annotated[int, Field(ge=0, le=10_000)]
"""A service's display position. The same limits as the owner API."""

Weekday = Annotated[int, Field(ge=0, le=6)]
"""A day of the week: 0 = Sunday ... 6 = Saturday, as the database stores it."""

BreakMinutes = Annotated[int, Field(ge=0, le=240)]
"""The break between sessions, in minutes. The same limits as the database."""

NoticeMinutes = Annotated[int, Field(ge=0, le=43_200)]
"""How long before a session it may still be booked, in minutes. The same limits as the database."""

WindowDays = Annotated[int, Field(ge=1, le=365)]
"""How many days ahead, counting today, are open for booking. The same limits as the database."""

BusinessName = Annotated[str, StringConstraints(min_length=1, max_length=120)]
"""A business's display name, as stored. Shape only: the text rules apply where the name is entered."""

ServiceName = Annotated[str, StringConstraints(min_length=1, max_length=120)]
"""A service's display name, as stored. Shape only: the text rules apply where the name is entered."""

PlanStatus = Literal["active", "past_due"]
"""Whether a business's plan is paid up. Must equal the database's check list on ``businesses.plan_status``."""

Role = Literal["owner"]
"""An account's role in a business. Must equal the database's check list on ``business_members.role``."""

CutoffMinutes = Annotated[int, Field(ge=0, le=43_200)]
"""How long before a session a customer may still cancel or move it online, in minutes. The same limits as the
database."""

TimeZoneName = Annotated[str, StringConstraints(min_length=1, max_length=64)]
"""An IANA time zone name, as stored. Length only: an unknown zone is handled where it is used."""

StoredEmail = Annotated[str, StringConstraints(min_length=3, max_length=254)]
"""An email address, as stored. Shape only: the text rules apply where the address is entered."""

StoredAddress = Annotated[str, StringConstraints(min_length=3, max_length=200)]
"""A public address, as stored. Shape only: the text rules apply where the address is entered."""

StoredPhone = Annotated[str, StringConstraints(min_length=5, max_length=32)]
"""A phone number, as stored. The same limits as the database."""

StoredLink = Annotated[str, StringConstraints(pattern=r"^https://", max_length=500)]
"""A meeting link, as stored: ``https://`` and up to 500 characters, the same check as the database."""

DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=100)]
"""An owner's name, as stored. Shape only: the text rules apply where the name is entered."""

MeetingLink = Annotated[str, StringConstraints(pattern=r"^https://\S{4,}$", max_length=500)]
"""A meeting link as an owner enters it: ``https://``, no spaces, 12 to 500 characters."""

EventKind = Literal["confirmed", "cancelled", "moved"]
"""A booking event that sends emails. Must equal the database's check list on ``email_log.kind``."""

CancelledBy = Literal["customer", "owner"]
"""Who cancelled a booking. Must equal the database's check list on ``bookings.cancelled_by``."""

EMAIL_PATTERN = r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$"
"""A plausible email address: a local part, one ``@``, and a domain with a dot."""

PHONE_PATTERN = r"^[0-9+()\- ]{5,32}$"
"""A phone number: 5 to 32 digits, spaces and common phone punctuation."""

_EMAIL_RE = re.compile(EMAIL_PATTERN)

_DIRECTION_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
"""Characters that change the direction text is shown in. They can make text look different from what it is."""

_ZERO_WIDTH = frozenset("\u00ad\u200b\u200c\u200d\u2060\ufeff")
"""Invisible characters removed from names: soft hyphen, zero-width space, joiners, word joiner, byte-order mark."""

_NOTE_CONTROLS_ALLOWED = frozenset("\n\r\t")
"""The control characters a note may contain."""


class TextRefusedError(ValueError):
    """Raised when text breaks a hidden- or look-alike-character rule. Its message never contains the text."""


def _refuse(field: str, reason: str) -> TextRefusedError:
    """Log a refusal by field and reason only, never the value, and build the error to raise.

    Args:
        field: The field refused, such as ``email``.
        reason: Why, such as ``mixed_scripts``.

    Returns:
        The error to raise.
    """
    LOGGER.warning("text refused field=%s reason=%s", field, reason)
    return TextRefusedError(f"{field} refused: {reason.replace('_', ' ')}")


def _has_controls(value: str, allowed: frozenset[str] = frozenset()) -> bool:
    """Check text for control characters other than those allowed.

    Args:
        value: The text.
        allowed: Control characters that are allowed.

    Returns:
        ``True`` when the text contains another control character.
    """
    return any(unicodedata.category(char) == "Cc" and char not in allowed for char in value)


def scripts(value: str) -> set[str]:
    """Name the writing systems the letters in some text come from.

    The script is read from the start of each letter's Unicode name (``LATIN SMALL LETTER A`` is Latin). Digits,
    punctuation, symbols and spaces belong to no script.

    Args:
        value: The text.

    Returns:
        The scripts, such as ``{"LATIN"}`` or ``{"LATIN", "CYRILLIC"}``.
    """
    return {
        unicodedata.name(char, "UNKNOWN").split(" ", 1)[0]
        for char in value
        if unicodedata.category(char).startswith("L")
    }


def clean_name(value: str, field: str = "name", max_length: int = 100, min_length: int = 1) -> str:
    """Normalise a person's name and refuse hidden characters.

    The text is normalised (NFKC), so look-alike forms such as full-width letters become ordinary ones, and
    zero-width characters are removed. Control characters and direction controls are refused. Mixed scripts are
    allowed, because names written in two scripts are real.

    Args:
        value: The name as typed.
        field: The field name used in the log and the error.
        max_length: The longest name accepted once cleaned.
        min_length: The shortest name accepted once cleaned.

    Returns:
        The cleaned name.

    Raises:
        TextRefusedError: If the name has control or direction characters, or is empty, too short or too long once
            cleaned.
    """
    text = unicodedata.normalize("NFKC", value)
    if _has_controls(text):
        raise _refuse(field, "control_characters")
    if any(char in _DIRECTION_CONTROLS for char in text):
        raise _refuse(field, "direction_controls")
    text = "".join(char for char in text if char not in _ZERO_WIDTH).strip()
    if not text:
        raise _refuse(field, "empty")
    if len(text) < min_length:
        raise _refuse(field, "too_short")
    if len(text) > max_length:
        raise _refuse(field, "too_long")
    return text


def clean_business_name(value: str, field: str = "business_name") -> str:
    """Clean a business name as a person's name, and refuse a word that mixes scripts.

    Each word must be in one script, but different words may use different scripts. A bilingual name such as
    ``Noor Salon`` followed by its Arabic form is accepted; a Latin word with one Cyrillic letter in it, which
    looks like another business's name, is refused.

    Args:
        value: The name as typed.
        field: The field name used in the log and the error.

    Returns:
        The cleaned name, 1 to 120 characters.

    Raises:
        TextRefusedError: If the name breaks a rule of :func:`clean_name`, or a word mixes scripts.
    """
    text = clean_name(value, field, 120)
    if any(len(scripts(word)) > 1 for word in text.split()):
        raise _refuse(field, "mixed_scripts_in_a_word")
    return text


def clean_email(value: str, field: str = "email") -> str:
    """Normalise an email address and refuse hidden or look-alike characters.

    The address is normalised (NFKC). Any invisible, control or direction character is refused, and so is an address
    whose letters come from more than one script, such as a Cyrillic letter inside a Latin address. An address in a
    single script, such as Arabic, is accepted.

    Args:
        value: The address as typed.
        field: The field name used in the log and the error.

    Returns:
        The normalised address.

    Raises:
        TextRefusedError: If the address breaks a rule, or is not a plausible address once normalised.
    """
    text = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(char) in ("Cc", "Cf") for char in text):
        raise _refuse(field, "invisible_characters")
    if len(scripts(text)) > 1:
        raise _refuse(field, "mixed_scripts")
    if len(text) > 254 or not _EMAIL_RE.match(text):
        raise _refuse(field, "not_an_email_address")
    return text


def clean_note(value: str, field: str = "note", max_length: int = 1000) -> str:
    """Refuse hidden characters in free text, without changing it.

    Notes are not normalised, so emoji and formatting stay as typed. Control characters other than new lines and
    tabs, and direction controls, are refused.

    Args:
        value: The text as typed.
        field: The field name used in the log and the error.
        max_length: The longest text accepted.

    Returns:
        The text, unchanged.

    Raises:
        TextRefusedError: If the text has a refused character or is too long.
    """
    if _has_controls(value, _NOTE_CONTROLS_ALLOWED):
        raise _refuse(field, "control_characters")
    if any(char in _DIRECTION_CONTROLS for char in value):
        raise _refuse(field, "direction_controls")
    if len(value) > max_length:
        raise _refuse(field, "too_long")
    return value


REMINDER_NOTE_MAX_LENGTH = 200
"""The longest note a business may add to its reminders."""

_LINK = re.compile(
    r"://|\bwww\.|\b[a-z0-9-]+\.(?:com|co|uk|net|org|io|app|link|ly|me|info|biz|sa|ae|eg|xyz|shop|site|online)\b",
    re.IGNORECASE,
)
"""Text a mail program would turn into a link: a scheme, ``www.`` or a name ending in a common domain."""


def clean_reminder_note(value: str) -> str:
    """Normalise a business's reminder note and refuse hidden characters and links.

    The note goes into emails to customers, so it follows the rules for a name (one line, no hidden characters) and
    may not contain a web or email address.

    Args:
        value: The note as typed.

    Returns:
        The cleaned note.

    Raises:
        TextRefusedError: If it breaks the name rule, is longer than :data:`REMINDER_NOTE_MAX_LENGTH` or holds a link.
    """
    text = clean_name(value, "reminder_note", REMINDER_NOTE_MAX_LENGTH)
    if _LINK.search(text):
        raise _refuse("reminder_note", "links")
    return text


OwnerReminderNote = Annotated[str, AfterValidator(clean_reminder_note)]
"""A reminder note as an owner enters it: one line, up to 200 characters, no links."""

StoredReminderNote = Annotated[str, StringConstraints(min_length=1, max_length=REMINDER_NOTE_MAX_LENGTH)]
"""A reminder note, as stored. The same limits as the database."""

ReminderEarlyHours = Literal[48, 72, 168]
"""How early the earlier reminder is sent, in hours. Must equal the database's check list."""

CustomerName = Annotated[str, AfterValidator(clean_name)]
"""A customer's name: normalised, 1 to 100 characters, no hidden characters."""

CustomerEmail = Annotated[str, AfterValidator(clean_email)]
"""A customer's email address: normalised, one script only, no hidden characters."""

CustomerNote = Annotated[str, AfterValidator(clean_note)]
"""A customer's note: up to 1,000 characters, no hidden characters, otherwise as typed."""

CustomerPhone = Annotated[str, StringConstraints(pattern=PHONE_PATTERN)]
"""A customer's phone number."""

OwnerBusinessName = Annotated[str, AfterValidator(clean_business_name)]
"""A business name as an owner enters it: normalised, 1 to 120 characters, no hidden characters, one script per
word."""

OwnerName = Annotated[str, AfterValidator(partial(clean_name, field="owner_name", max_length=100))]
"""An owner's name as entered: as a customer's name."""

OwnerBusinessType = Annotated[str, AfterValidator(partial(clean_name, field="business_type_other", max_length=60))]
"""The description of a business whose type is ``other``: as a person's name, up to 60 characters."""

OwnerServiceName = Annotated[str, AfterValidator(partial(clean_name, field="service_name", max_length=120))]
"""A service name as an owner enters it: as a person's name, up to 120 characters."""

OwnerAddress = Annotated[str, AfterValidator(partial(clean_name, field="public_address", max_length=200, min_length=3))]
"""A public address as an owner enters it: as a person's name, 3 to 200 characters, on one line."""

OwnerEmail = Annotated[str, AfterValidator(partial(clean_email, field="owner_email"))]
"""An email address an owner enters: as a customer's email address."""

TimeOffNote = Annotated[str, AfterValidator(partial(clean_note, field="time_off_note", max_length=100))]
"""A private note on days off: as a customer's note, up to 100 characters."""


def from_row(record: type[RecordT], row: Sequence[Any]) -> RecordT:
    """Build a record from a database row whose columns are in the record's field order.

    A row may leave out trailing fields, which then take their defaults.

    Args:
        record: The record type.
        row: The row's values, in field order.

    Returns:
        The record, checked.

    Raises:
        ValueError: If the row has more columns than the record has fields.
    """
    fields = list(record.model_fields)
    if len(row) > len(fields):
        raise ValueError(f"{record.__name__} has {len(fields)} fields, the row has {len(row)} columns")
    return record(**dict(zip(fields, row)))
