"""Tests for the reminder note's text rules and the reminder change record. No database is needed.

What these tests establish: an ordinary note is accepted and cleaned; hidden characters, line breaks, links, empty
and overlong notes are refused; and only the note may be cleared.
"""

import pytest

from booking.owner import ReminderChanges
from contracts import TextRefusedError, clean_reminder_note


@pytest.mark.parametrize(
    "note",
    [
        "Please arrive 5 minutes early.",
        "Free parking behind the shop, e.g. bays 1.2 and 3.",
        "Open 9 a.m. to 5 p.m.",
        "يرجى الحضور قبل الموعد بخمس دقائق",
        "x" * 200,
    ],
)
def test_ordinary_notes_are_accepted(note: str) -> None:
    """Sentences, times, numbers and other alphabets are not mistaken for links."""
    assert clean_reminder_note(note) == note


def test_a_note_is_trimmed() -> None:
    """Spaces around the note are removed."""
    assert clean_reminder_note("  Bring your ID  ") == "Bring your ID"


@pytest.mark.parametrize(
    "note",
    [
        "See https://example.com",
        "See ftp://files",
        "Visit www.example",
        "Visit example.co.uk today",
        "Write to owner@example.com",
        "Pay at PAYNOW.APP",
        "Visit example\uff0ecom",
        "Visit exam\u200bple.com",
        "two\nlines",
        "hidden \u202e direction",
        "   ",
        "x" * 201,
    ],
)
def test_links_and_hidden_characters_are_refused(note: str) -> None:
    """A note cannot carry a link, even disguised, or characters that change how it reads."""
    with pytest.raises(TextRefusedError):
        clean_reminder_note(note)


def test_only_the_note_may_be_cleared() -> None:
    """``None`` clears the note and is refused for the switches and the timing."""
    assert ReminderChanges(note=None).model_fields_set == {"note"}
    for name in ("day_enabled", "early_enabled", "early_hours"):
        with pytest.raises(ValueError):
            ReminderChanges(**{name: None})
