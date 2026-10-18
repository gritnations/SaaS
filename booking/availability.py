"""Availability rules for booking. Pure: no I/O, no clock, no database.

The rules follow the existing engine's tested behaviour:

* Session starts sit on a grid inside each working block. Starts are spaced by the session length plus
  the break between sessions, and every session must end by the end of its block. The last session of a
  block needs no break after it.
* Several working blocks on one day give that day a gap, such as lunch. The grid restarts after a gap.
* Intervals are half-open, so back-to-back sessions do not overlap.
* An existing booking blocks ``[starts_at, blocked_until)``, where ``blocked_until`` is its end plus the
  break that applied when it was made. A new session overlapping that range is unavailable, whatever the
  durations involved. The database enforces the same rule with an exclusion constraint.

Times inside a day are whole minutes since local midnight. Conversion to aware datetimes happens in the
business's time zone, so daylight-saving changes are respected.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class Block:
    """One working block on a weekday, in minutes since local midnight.

    Attributes:
        start: First minute of the block.
        end: Minute the block closes. A session must end at or before it.
    """

    start: int
    end: int


@dataclass(frozen=True)
class Slot:
    """One session start on one day.

    Attributes:
        time: Local start time as ``HH:MM``.
        label: The session as people read it, such as ``9:00 – 10:00 AM``.
        starts_at: Aware start, in the business's time zone.
        ends_at: Aware end, in the business's time zone.
        available: ``True`` if the session can be booked now.
    """

    time: str
    label: str
    starts_at: datetime
    ends_at: datetime
    available: bool


def normalize_break(minutes: object) -> int:
    """Read a break length, treating anything other than a positive whole number as no break.

    Args:
        minutes: The configured break.

    Returns:
        The break in minutes, or ``0``.
    """
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return 0
    return minutes if minutes > 0 else 0


def minutes_to_time(minutes: int) -> str:
    """Format minutes since midnight as ``HH:MM``.

    Args:
        minutes: Minutes since midnight, 0 to 1439.

    Returns:
        The time, zero-padded.
    """
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def time_to_minutes(value: str) -> int | None:
    """Parse ``HH:MM`` into minutes since midnight.

    Args:
        value: The text to parse.

    Returns:
        The minutes, or ``None`` if the text is not a valid 24-hour time.
    """
    if len(value) != 5 or value[2] != ":" or not (value[:2] + value[3:]).isdigit():
        return None
    hours, minutes = int(value[:2]), int(value[3:])
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def _twelve_hour(minutes: int) -> tuple[str, str]:
    """Split minutes since midnight into a 12-hour clock text and its AM/PM suffix.

    Args:
        minutes: Minutes since midnight. Values past midnight wrap.

    Returns:
        ``(text, suffix)``, such as ``("9:00", "AM")``.
    """
    hour24 = (minutes // 60) % 24
    hour12 = hour24 % 12 or 12
    return f"{hour12}:{minutes % 60:02d}", "AM" if hour24 < 12 else "PM"


def session_label(start: int, duration: int) -> str:
    """Describe a session as people read it.

    Args:
        start: Start, in minutes since midnight.
        duration: Length in minutes.

    Returns:
        ``9:00 – 10:00 AM`` when both ends share a suffix, otherwise ``11:30 AM – 12:30 PM``.
    """
    start_text, start_suffix = _twelve_hour(start)
    end_text, end_suffix = _twelve_hour(start + duration)
    if start_suffix == end_suffix:
        return f"{start_text} – {end_text} {end_suffix}"
    return f"{start_text} {start_suffix} – {end_text} {end_suffix}"


def slot_starts(blocks: Iterable[Block], duration: int, break_minutes: int) -> list[int]:
    """List every session start that fits inside the working blocks of one day.

    Args:
        blocks: The day's working blocks. Invalid blocks are ignored.
        duration: Session length in minutes. A non-positive length gives no starts.
        break_minutes: Break between sessions. Non-positive values mean no break.

    Returns:
        Distinct starts in minutes since midnight, in ascending order.
    """
    if duration <= 0:
        return []
    step = duration + normalize_break(break_minutes)
    starts: set[int] = set()
    for block in blocks:
        if not (0 <= block.start < block.end <= MINUTES_PER_DAY):
            continue
        minute = block.start
        while minute + duration <= block.end:
            starts.add(minute)
            minute += step
    return sorted(starts)


def intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Report whether two half-open intervals overlap.

    Args:
        a_start: Start of the first interval.
        a_end: End of the first interval, excluded.
        b_start: Start of the second interval.
        b_end: End of the second interval, excluded.

    Returns:
        ``True`` if they share any instant. Back-to-back intervals do not.
    """
    return a_start < b_end and a_end > b_start


def local_datetime(day: date, minutes: int, zone: ZoneInfo) -> datetime | None:
    """Build the aware datetime for a wall-clock time in a time zone.

    Args:
        day: The local date.
        minutes: Minutes since local midnight.
        zone: The business's time zone.

    Returns:
        The aware datetime, or ``None`` if that wall-clock time does not exist on that day because the
        clocks go forward.
    """
    naive = datetime.combine(day, time(minutes // 60, minutes % 60))
    aware = naive.replace(tzinfo=zone)
    if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return aware


def day_slots(
    day: date,
    blocks: Iterable[Block],
    duration: int,
    break_minutes: int,
    zone: ZoneInfo,
    booked: Sequence[tuple[datetime, datetime]],
    earliest: datetime,
) -> list[Slot]:
    """Build one day's sessions for a service, marking which can be booked.

    A session is available when it starts at or after ``earliest`` and does not overlap any booked
    range. Sessions are listed even when unavailable, so a page can show them as taken.

    Args:
        day: The local date.
        blocks: The working blocks for that date's weekday.
        duration: Session length in minutes.
        break_minutes: The business's current break between sessions.
        zone: The business's time zone.
        booked: Ranges already taken, as ``(starts_at, blocked_until)``.
        earliest: The first instant a session may start, after the notice period.

    Returns:
        The sessions in start order. A wall-clock time skipped by a clock change is left out.
    """
    slots = []
    for start in slot_starts(blocks, duration, break_minutes):
        starts_at = local_datetime(day, start, zone)
        if starts_at is None:
            continue
        ends_at = starts_at + timedelta(minutes=duration)
        blocked_until = ends_at + timedelta(minutes=normalize_break(break_minutes))
        taken = any(intervals_overlap(starts_at, blocked_until, b_start, b_end) for b_start, b_end in booked)
        slots.append(
            Slot(
                time=minutes_to_time(start),
                label=session_label(start, duration),
                starts_at=starts_at,
                ends_at=ends_at,
                available=starts_at >= earliest and not taken,
            )
        )
    return slots


def booking_window(now: datetime, zone: ZoneInfo, window_days: int) -> tuple[date, date]:
    """Find the first and last local dates a customer may book.

    Args:
        now: The current instant, aware.
        zone: The business's time zone.
        window_days: How many days ahead, counting today, are open for booking.

    Returns:
        ``(first, last)``, both inclusive. ``first`` is today in the business's time zone.
    """
    today = now.astimezone(zone).date()
    return today, today + timedelta(days=max(window_days, 1) - 1)


def weekday_index(day: date) -> int:
    """Give a date's weekday with Sunday as 0, matching the stored working hours.

    Args:
        day: The date.

    Returns:
        0 for Sunday through 6 for Saturday.
    """
    return (day.weekday() + 1) % 7
