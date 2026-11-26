"""The owner pages know what sign-up creates, so that the set-up box does not tick steps by itself.

These tests fail if the pages' copy of those defaults and the backend's values drift apart.
"""

import re
from pathlib import Path

from booking.owner import DEFAULT_HOURS, DEFAULT_SERVICE

SCRIPT = (Path(__file__).resolve().parents[1] / "site" / "assets" / "setup-model.js").read_text(encoding="utf-8")


def _constant(name: str) -> str:
    """Return the source text assigned to an exported constant in the pages' script."""
    found = re.search(rf"export const {name} = (.+?);", SCRIPT)
    assert found, name
    return found.group(1)


def test_the_pages_know_the_default_service() -> None:
    """The pages' default service is the one sign-up creates."""
    slug, name, minutes, price = DEFAULT_SERVICE
    expected = f'{{ slug: "{slug}", name: "{name}", duration_minutes: {minutes}, price_minor: {price} }}'
    assert _constant("DEFAULT_SERVICE") == expected


def test_the_pages_know_the_default_hours() -> None:
    """The pages' default days and times are the ones sign-up creates."""
    start, end = DEFAULT_HOURS[0][1], DEFAULT_HOURS[0][2]
    assert {(opens, closes) for _, opens, closes in DEFAULT_HOURS} == {(start, end)}
    assert _constant("DEFAULT_DAYS") == str([day for day, _, _ in DEFAULT_HOURS])
    assert _constant("DEFAULT_START") == f'"{start:%H:%M}"'
    assert _constant("DEFAULT_END") == f'"{end:%H:%M}"'
