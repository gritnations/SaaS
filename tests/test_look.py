"""Tests for the booking page look rules that need no database.

What these tests establish: colours are only ``#rrggbb`` in lower case and fonts, corners and logo identifiers only
come from their fixed sets, so nothing else can reach a customer's page; the contrast arithmetic matches WCAG; a
hard-to-read look is named as such; button text picks the more readable of white and near-black; branding is
included on paid plans and during the trial only; and only PNG, JPEG and WebP bytes are recognised as images.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from booking.look import (
    DEFAULT_LOOK,
    Look,
    branding_allowed,
    contrast,
    on_accent,
    readability_problems,
    sniff_image,
)

GOOD: dict[str, Any] = {
    "accent": "#13355a",
    "background": "#ffffff",
    "text": "#1c2733",
    "font": "modern",
    "corners": "soft",
}
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def test_a_valid_look_is_accepted() -> None:
    """Every field from its set, with and without a logo."""
    assert Look(**GOOD).logo_id is None
    assert Look(**GOOD, logo_id="0b6c3f0e-6a8e-4d3e-9a51-0f3c1f2d9a10").logo_id is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("accent", "#13355A"),
        ("accent", "13355a"),
        ("accent", "#1335"),
        ("accent", "red"),
        ("background", "#ffffff;}body{display:none"),
        ("text", "url(https://example.com/x)"),
        ("font", "Comic Sans"),
        ("font", "modern;"),
        ("corners", "huge"),
        ("logo_id", "../../etc/passwd"),
        ("logo_id", "0B6C3F0E-6A8E-4D3E-9A51-0F3C1F2D9A10"),
    ],
)
def test_anything_outside_the_fixed_sets_is_refused(field: str, value: str) -> None:
    """Upper-case or short colours, CSS fragments, unknown fonts or corners, and odd identifiers never validate."""
    with pytest.raises(ValidationError):
        Look(**{**GOOD, field: value})


def test_an_extra_field_is_refused() -> None:
    """A look carries exactly its fields."""
    with pytest.raises(ValidationError):
        Look(**{**GOOD, "css": "body{}"})


def test_contrast_matches_the_wcag_reference_values() -> None:
    """Black on white is 21:1, a colour on itself is 1:1, and the order of the two colours does not matter."""
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#13355a", "#13355a") == pytest.approx(1.0)
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
    assert contrast("#ffffff", "#767676") == contrast("#767676", "#ffffff")


def test_button_text_is_the_more_readable_of_white_and_near_black() -> None:
    """Dark accents get white text, light accents get near-black text."""
    assert on_accent("#13355a") == "#ffffff"
    assert on_accent("#e0aa55") == "#111111"


def test_the_default_look_is_readable() -> None:
    """The look shown when nothing is published passes its own rules."""
    assert readability_problems(DEFAULT_LOOK) == []


def test_each_readability_problem_is_named() -> None:
    """Text, buttons and highlights are checked separately."""
    assert readability_problems(Look(**{**GOOD, "text": "#f0f0f0"})) == ["text on the background is hard to read"]
    assert readability_problems(Look(**{**GOOD, "accent": "#ffffff"})) == [
        "highlights barely stand out from the background"
    ]
    assert "button text is hard to read" in readability_problems(Look(**{**GOOD, "accent": "#777777"}))


@pytest.mark.parametrize(
    ("plan", "trial_ends_at", "allowed"),
    [
        ("starter", None, True),
        ("pro", None, True),
        ("free", NOW + timedelta(days=1), True),
        ("free", NOW - timedelta(seconds=1), False),
        ("free", NOW, False),
        ("free", None, False),
    ],
)
def test_branding_is_included_on_paid_plans_and_during_the_trial(
    plan: str, trial_ends_at: datetime | None, allowed: bool
) -> None:
    """A paid plan always; a free plan only before its trial ends."""
    assert branding_allowed(plan, trial_ends_at, NOW) is allowed


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', None),
        (b"<html><script>alert(1)</script></html>", None),
        (b"GIF89a" + b"\x00" * 8, None),
        (b"RIFF\x10\x00\x00\x00WAVE", None),
        (b"", None),
    ],
)
def test_only_png_jpeg_and_webp_bytes_are_recognised(data: bytes, kind: str | None) -> None:
    """SVG, HTML, GIF and other RIFF files are not images we accept, whatever they are called."""
    assert sniff_image(data) == kind
