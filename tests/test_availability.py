"""Tests for the pure availability rules.

The cases are the existing engine's tested behaviour, used as acceptance criteria: the session grid,
breaks between sessions, overlaps across different durations, lunch gaps, the booking window, and the
daylight-saving changes that a fixed-offset engine never met.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking.availability import (
    Block,
    Slot,
    booking_window,
    day_slots,
    intervals_overlap,
    local_datetime,
    normalize_break,
    session_label,
    slot_starts,
    time_to_minutes,
    weekday_index,
)

LONDON = ZoneInfo("Europe/London")
NINE_TO_FIVE = [Block(9 * 60, 17 * 60)]
DAY = date(2026, 11, 3)
"""A Tuesday outside any clock change."""

PAST = datetime(2000, 1, 1, tzinfo=UTC)
"""An ``earliest`` value that never hides a session."""


def times(starts: list[int]) -> list[str]:
    """Format minute offsets as ``HH:MM`` for readable assertions.

    Args:
        starts: Minutes since midnight.

    Returns:
        The times.
    """
    return [f"{m // 60:02d}:{m % 60:02d}" for m in starts]


def at(hhmm: str, day: date = DAY) -> datetime:
    """Build an aware London datetime on the test day.

    Args:
        hhmm: Wall-clock time.
        day: Local date.

    Returns:
        The aware datetime.
    """
    minutes = time_to_minutes(hhmm)
    assert minutes is not None
    result = local_datetime(day, minutes, LONDON)
    assert result is not None
    return result


def available(slots: list[Slot], hhmm: str) -> bool:
    """Report whether the session at a time is available.

    Args:
        slots: Sessions from :func:`day_slots`.
        hhmm: The start time.

    Returns:
        The session's availability.
    """
    return next(s.available for s in slots if s.time == hhmm)


@pytest.mark.parametrize(("duration", "count", "last"), [(60, 8, "16:00"), (30, 16, "16:30"), (90, 5, "15:00")])
def test_grid_fits_inside_working_hours(duration: int, count: int, last: str) -> None:
    """Sessions fill 09:00-17:00 on a grid of their own length, and the last one ends by closing."""
    starts = times(slot_starts(NINE_TO_FIVE, duration, 0))
    assert len(starts) == count
    assert starts[0] == "09:00"
    assert starts[-1] == last


def test_break_spaces_the_grid() -> None:
    """45-minute sessions with a 15-minute break start every hour."""
    assert times(slot_starts(NINE_TO_FIVE, 45, 15)) == [f"{h:02d}:00" for h in range(9, 17)]


def test_break_with_sixty_minute_sessions() -> None:
    """60 + 15 gives 09:00, 10:15, 11:30, 12:45, 14:00, 15:15 in a 09:00-17:00 day."""
    assert times(slot_starts(NINE_TO_FIVE, 60, 15)) == ["09:00", "10:15", "11:30", "12:45", "14:00", "15:15"]


def test_last_session_needs_no_break_after_it() -> None:
    """A 16:00-16:45 session fits a 16:45 close even with a break configured."""
    assert times(slot_starts([Block(9 * 60, 16 * 60 + 45)], 45, 15))[-1] == "16:00"


def test_grid_restarts_after_a_lunch_gap() -> None:
    """A split day gives 3 + 4 sessions and none at 12:00."""
    starts = times(slot_starts([Block(9 * 60, 12 * 60), Block(13 * 60, 17 * 60)], 60, 0))
    assert len(starts) == 7
    assert "12:00" not in starts
    assert "13:00" in starts


def test_overlapping_blocks_do_not_duplicate_sessions() -> None:
    """Two blocks that overlap produce each start once."""
    starts = slot_starts([Block(9 * 60, 13 * 60), Block(11 * 60, 15 * 60)], 60, 0)
    assert len(starts) == len(set(starts))


@pytest.mark.parametrize(
    ("blocks", "duration"),
    [([], 60), (NINE_TO_FIVE, 0), (NINE_TO_FIVE, -30), ([Block(17 * 60, 9 * 60)], 60), ([Block(-5, 60)], 30)],
)
def test_no_sessions_from_empty_or_invalid_input(blocks: list[Block], duration: int) -> None:
    """Empty hours, a non-positive duration or an invalid block give no sessions and never loop."""
    assert slot_starts(blocks, duration, 0) == []


@pytest.mark.parametrize("value", [-15, 2.5, "15", None, True])
def test_invalid_break_values_count_as_no_break(value: object) -> None:
    """Negative, fractional, text, missing or boolean breaks are treated as zero."""
    assert normalize_break(value) == 0
    assert normalize_break(15) == 15


@pytest.mark.parametrize(
    ("start", "duration", "label"),
    [(9 * 60, 60, "9:00 – 10:00 AM"), (11 * 60 + 30, 60, "11:30 AM – 12:30 PM"), (14 * 60, 45, "2:00 – 2:45 PM")],
)
def test_session_labels(start: int, duration: int, label: str) -> None:
    """Labels show the session, with the suffix once unless it crosses noon."""
    assert session_label(start, duration) == label


def test_overlap_is_half_open() -> None:
    """Same, partial and contained intervals overlap; back-to-back ones do not."""
    assert intervals_overlap(at("09:00"), at("10:00"), at("09:00"), at("10:00"))
    assert intervals_overlap(at("09:00"), at("10:00"), at("09:30"), at("10:30"))
    assert intervals_overlap(at("09:00"), at("12:00"), at("10:00"), at("11:00"))
    assert not intervals_overlap(at("09:00"), at("10:00"), at("10:00"), at("11:00"))


def test_bookings_block_sessions_of_other_durations() -> None:
    """A 60-minute booking at 09:00 blocks a 30-minute session at 09:30 but not 10:00."""
    booked = [(at("09:00"), at("10:00"))]
    thirty = day_slots(DAY, NINE_TO_FIVE, 30, 0, LONDON, booked, PAST)
    assert not available(thirty, "09:00")
    assert not available(thirty, "09:30")
    assert available(thirty, "10:00")
    sixty = day_slots(DAY, NINE_TO_FIVE, 60, 0, LONDON, [(at("09:30"), at("10:00"))], PAST)
    assert not available(sixty, "09:00")
    assert available(sixty, "10:00")


def test_break_keeps_sessions_clear_on_both_sides() -> None:
    """With a 15-minute break, a 30-minute session cannot sit inside the break around a 10:00-10:45 booking."""
    booked = [(at("10:00"), at("10:45") + timedelta(minutes=15))]
    slots = day_slots(DAY, NINE_TO_FIVE, 30, 15, LONDON, booked, PAST)
    assert not available(slots, "09:45")
    assert available(slots, "09:00")
    assert available(slots, "11:15")


def test_taken_sessions_are_listed_but_unavailable() -> None:
    """A page can grey out a taken session, because it is still listed."""
    slots = day_slots(DAY, NINE_TO_FIVE, 60, 0, LONDON, [(at("09:00"), at("10:00"))], PAST)
    assert [s.time for s in slots][0] == "09:00"
    assert not slots[0].available


def test_notice_period_hides_sessions_that_start_too_soon() -> None:
    """Sessions before ``earliest`` are unavailable; the first one at or after it is available."""
    slots = day_slots(DAY, NINE_TO_FIVE, 60, 0, LONDON, [], at("11:00"))
    assert not available(slots, "10:00")
    assert available(slots, "11:00")


def test_sessions_carry_the_correct_offset_in_summer_and_winter() -> None:
    """09:00 in London is 08:00 UTC in summer and 09:00 UTC in winter."""
    summer = day_slots(date(2026, 7, 7), NINE_TO_FIVE, 60, 0, LONDON, [], PAST)[0]
    winter = day_slots(DAY, NINE_TO_FIVE, 60, 0, LONDON, [], PAST)[0]
    assert summer.starts_at.astimezone(UTC).hour == 8
    assert winter.starts_at.astimezone(UTC).hour == 9


def test_a_wall_clock_time_skipped_by_the_clocks_going_forward_is_left_out() -> None:
    """01:00-02:00 does not exist in London on the last Sunday of March."""
    slots = day_slots(date(2026, 3, 29), [Block(0, 4 * 60)], 60, 0, LONDON, [], PAST)
    assert [s.time for s in slots] == ["00:00", "02:00", "03:00"]


def test_booking_window_starts_today_in_the_business_time_zone() -> None:
    """At 23:30 UTC in summer it is already tomorrow in London, and the window counts from there."""
    first, last = booking_window(datetime(2026, 7, 6, 23, 30, tzinfo=UTC), LONDON, 30)
    assert first == date(2026, 7, 7)
    assert last == date(2026, 8, 5)


def test_weekday_index_counts_from_sunday() -> None:
    """Sunday is 0 and Saturday is 6, matching the stored working hours."""
    assert weekday_index(date(2026, 11, 1)) == 0
    assert weekday_index(date(2026, 11, 7)) == 6


@pytest.mark.parametrize("value", ["9:00", "24:00", "12:60", "ab:cd", "12-00", ""])
def test_time_parsing_rejects_malformed_values(value: str) -> None:
    """Only zero-padded 24-hour ``HH:MM`` parses."""
    assert time_to_minutes(value) is None
