"""Reads what an owner types into the bookings search box, using fixed rules.

No model or outside service is involved: the same words on the same day always give the same search. This
module performs no I/O; ``booking.owner`` turns the result into a database query.

The rules, applied in this order:
    * text in double quotes is searched for exactly as written, whatever the words mean;
    * the full name of one of the business's services, singular or with a simple plural, selects it;
    * date words and UK dates select days, in the business's time zone;
    * ``confirmed``, ``cancelled``, ``upcoming`` and ``past`` filter the bookings;
    * a word with ``@`` is part of an email address;
    * a word ending in ``*``, with any unrecognised words just before it, is the start of a customer's name;
    * words such as "list", "show" and "bookings" are ignored;
    * anything left is part of a customer name or a service name, or a whole booking reference.

Only English words are understood.
"""

import re
from collections.abc import Sequence
from datetime import date, timedelta

from pydantic import BaseModel

from contracts import STRICT

MAX_QUERY_LENGTH = 200
"""The longest search text accepted."""

_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTHS = {name: number for number, name in enumerate(_MONTH_NAMES, start=1)}
# Short forms are read only next to a day number: on their own "Jan" and "Mar" are more likely names.
_MONTHS_WITH_A_DAY = _MONTHS | {name[:3]: number for name, number in _MONTHS.items()} | {"sept": 9}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_NEARBY_DAYS = {"yesterday": -1, "today": 0, "tomorrow": 1}
_PERIOD_OFFSETS = {"last": -1, "this": 0, "next": 1}
_STATUSES = {"confirmed": "confirmed", "cancelled": "cancelled", "canceled": "cancelled"}
_TIMES = ("upcoming", "past")
_FILLER = frozenset(
    "a all an and any appointment appointments booking bookings called customer customers find for from in list "
    "me my named of on please search service show the with".split()
)
_QUOTED = re.compile(r'["“”]([^"“”]*)["“”]')
_TRAILING = ",.;:?!"
_DAY_NUMBER = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$")
_YEAR = re.compile(r"^20\d{2}$")
_SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2}|20\d{2}))?$")


class DayRange(BaseModel):
    """A run of whole local days.

    Attributes:
        first: First day.
        last: Last day, included.
        label: How the search describes it back to the owner.
    """

    model_config = STRICT

    first: date
    last: date
    label: str


class SearchPlan(BaseModel):
    """What a search asks for. Different kinds of condition must all hold; several of one kind widen it.

    The exception is ``texts``, ``emails`` and ``name_starts``: every entry must hold.

    Attributes:
        services: Identifiers of the services asked for.
        service_names: Their names, in the same order.
        days: The days asked for.
        statuses: ``confirmed`` and/or ``cancelled``.
        timing: ``upcoming`` or ``past``, or ``None`` for both.
        emails: Parts of the customer's email address.
        name_starts: Starts of the customer's name.
        texts: Parts of a customer name or service name, or a whole booking reference.
    """

    model_config = STRICT

    services: tuple[str, ...] = ()
    service_names: tuple[str, ...] = ()
    days: tuple[DayRange, ...] = ()
    statuses: tuple[str, ...] = ()
    timing: str | None = None
    emails: tuple[str, ...] = ()
    name_starts: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()

    def understood(self) -> list[str]:
        """Describe the search back to the owner, so that a wrong reading can be seen.

        Returns:
            One short line for each condition. Empty when the text asked for every booking.
        """
        lines = [f"Service: {name}" for name in dict.fromkeys(self.service_names)]
        lines += [f"Date: {day.label}" for day in self.days]
        lines += [f"Status: {status}" for status in self.statuses]
        if self.timing:
            lines.append(f"When: {self.timing}")
        lines += [f"Email contains: {text}" for text in self.emails]
        lines += [f"Name starts with: {text}" for text in self.name_starts]
        lines += [f"Name, service or reference: {text}" for text in self.texts]
        return lines


def _written(day: date) -> str:
    """Write a date the UK way.

    Args:
        day: The date.

    Returns:
        For example ``26 Nov 2026``.
    """
    return f"{day.day} {_MONTH_NAMES[day.month - 1][:3].title()} {day.year}"


def _range(first: date, last: date, name: str = "") -> DayRange:
    """Build a run of days with its description.

    Args:
        first: First day.
        last: Last day.
        name: The words the owner used, when they need showing beside the dates.

    Returns:
        The range.
    """
    dates = _written(first) if first == last else f"{_written(first)} to {_written(last)}"
    return DayRange(first=first, last=last, label=f"{name}, {dates}" if name else dates)


def _month_range(year: int, month: int, name: str = "") -> DayRange:
    """Build the range covering one month.

    Args:
        year: Year.
        month: Month, 1 to 12.
        name: The words the owner used.

    Returns:
        The range.
    """
    following = date(year + month // 12, month % 12 + 1, 1)
    return _range(date(year, month, 1), following - timedelta(days=1), name)


def _calendar_day(day: int, month: int, year: int | None, today: date) -> date | None:
    """Make a date from its parts. Without a year, the one closest to today is meant.

    Args:
        day: Day of the month.
        month: Month.
        year: Year, or ``None``.
        today: Today in the business's time zone.

    Returns:
        The date, or ``None`` if there is no such day.
    """
    found = []
    for candidate in (year,) if year is not None else (today.year - 1, today.year, today.year + 1):
        try:
            found.append(date(candidate, month, day))
        except ValueError:
            continue
    return min(found, key=lambda one: abs((one - today).days), default=None)


def _read_days(words: Sequence[str], today: date) -> tuple[DayRange | None, int] | None:
    """Read a date at the start of some words.

    Args:
        words: The remaining words, lower case.
        today: Today in the business's time zone.

    Returns:
        The days and how many words they took, or ``None`` if the words do not start with a date. The days are
        ``None`` for a day number and month that make no real date, such as 31 February.
    """
    first = words[0]
    second = words[1] if len(words) > 1 else ""
    third = words[2] if len(words) > 2 else ""
    if first in _NEARBY_DAYS:
        day = today + timedelta(days=_NEARBY_DAYS[first])
        return _range(day, day, first), 1
    if first in _PERIOD_OFFSETS and second in ("week", "month"):
        offset = _PERIOD_OFFSETS[first]
        name = f"{first} {second}"
        if second == "week":
            monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
            return _range(monday, monday + timedelta(days=6), name), 2
        months = today.year * 12 + today.month - 1 + offset
        return _month_range(months // 12, months % 12 + 1, name), 2
    if first == "next" and second in _WEEKDAYS:
        ahead = (_WEEKDAYS.index(second) - today.weekday() - 1) % 7 + 1
        day = today + timedelta(days=ahead)
        return _range(day, day, f"next {second.title()}"), 2
    if first in _WEEKDAYS:
        day = today + timedelta(days=(_WEEKDAYS.index(first) - today.weekday()) % 7)
        return _range(day, day, first.title()), 1
    slashed = _SLASH_DATE.match(first)
    if slashed:
        year = int(slashed[3]) if slashed[3] else None
        if year is not None and year < 100:
            year += 2000
        found = _calendar_day(int(slashed[1]), int(slashed[2]), year, today)
        return (_range(found, found), 1) if found else None
    for number, month, used in ((first, second, 2), (second, first, 2)):
        matched = _DAY_NUMBER.match(number)
        if matched and month in _MONTHS_WITH_A_DAY:
            year = int(third) if _YEAR.match(third) else None
            found = _calendar_day(int(matched[1]), _MONTHS_WITH_A_DAY[month], year, today)
            return (_range(found, found) if found else None), used + (year is not None)
    if first in _MONTHS:
        year = int(second) if _YEAR.match(second) else None
        return _month_range(year or today.year, _MONTHS[first]), 1 + (year is not None)
    return None


def _service_forms(name: str) -> list[tuple[str, ...]]:
    """List the ways a service name may be typed.

    Args:
        name: The service's name.

    Returns:
        Its words in lower case, as they are and with a simple plural on the last one.
    """
    words = tuple(word.strip(_TRAILING) for word in name.casefold().split())
    if not words:
        return []
    return [words, words[:-1] + (words[-1] + "s",), words[:-1] + (words[-1] + "es",)]


def read_query(query: str, services: Sequence[tuple[str, str]], today: date) -> SearchPlan:
    """Turn search text into conditions.

    Args:
        query: What the owner typed.
        services: The business's services as ``(identifier, name)``, inactive ones included.
        today: Today in the business's time zone.

    Returns:
        The conditions to search with.
    """
    texts = [" ".join(part.split()) for part in _QUOTED.findall(query) if part.strip()]
    typed = [word.strip(_TRAILING) for word in _QUOTED.sub(" | ", query).replace('"', " ").split()]
    typed = [word for word in typed if word]
    lowered = [word.casefold() for word in typed]
    forms: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for slug, name in services:
        for form in _service_forms(name):
            forms.setdefault(form, []).append((slug, name))
    longest_first = sorted(forms, key=len, reverse=True)

    chosen: dict[str, str] = {}
    days: list[DayRange] = []
    statuses: dict[str, None] = {}
    timings: set[str] = set()
    emails: list[str] = []
    name_starts: list[str] = []
    run: list[str] = []

    def end_run() -> None:
        """Keep the unrecognised words gathered so far as one piece of text."""
        if run:
            texts.append(" ".join(run))
            run.clear()

    position = 0
    while position < len(typed):
        word, plain = typed[position], lowered[position]
        service = next((f for f in longest_first if tuple(lowered[position : position + len(f)]) == f), None)
        if service is not None:
            end_run()
            chosen.update(forms[service])
            position += len(service)
            continue
        dated = _read_days(lowered[position:], today)
        if dated is not None:
            found, used = dated
            if found is None:
                run.extend(typed[position : position + used])
            else:
                end_run()
                days.append(found)
            position += used
            continue
        position += 1
        if plain in _STATUSES:
            statuses[_STATUSES[plain]] = None
        elif plain in _TIMES:
            timings.add(plain)
        elif "@" in word:
            emails.append(word.strip("*").casefold())
        elif word.endswith("*"):
            if word.strip("*"):
                # Words typed just before it are the earlier part of the same name, as in "Sara Kh*".
                name_starts.append(" ".join([*run, word.strip("*")]))
                run.clear()
        elif plain in _FILLER or word == "|":
            pass
        else:
            run.append(word)
            continue
        end_run()
    end_run()

    return SearchPlan(
        services=tuple(chosen),
        service_names=tuple(chosen.values()),
        days=tuple(days),
        statuses=tuple(statuses),
        timing=timings.pop() if len(timings) == 1 else None,
        emails=tuple(email for email in emails if email),
        name_starts=tuple(name_starts),
        texts=tuple(texts),
    )
