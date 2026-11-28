"""Tests for the rules that read the owner's search text. No database is needed.

What these tests establish: the named phrases select the intended services, days, statuses and customer
conditions; quotes force a text search; and each reading is described back to the owner. The database
query built from a reading is covered by ``test_owner_postgres.py``.
"""

from datetime import date

import pytest

from booking.search import SearchPlan, read_query

TODAY = date(2026, 11, 26)
"""A Thursday."""

SERVICES = [("cut", "Haircut"), ("intro", "Introductory call"), ("club", "Monday Club"), ("wash", "Wash")]


def read(query: str, today: date = TODAY) -> SearchPlan:
    """Read search text for a business with the sample services.

    Args:
        query: The text.
        today: Today.

    Returns:
        The conditions.
    """
    return read_query(query, SERVICES, today)


def days(query: str, today: date = TODAY) -> list[tuple[date, date]]:
    """Return only the days a search asks for.

    Args:
        query: The text.
        today: Today.

    Returns:
        ``(first, last)`` pairs.
    """
    return [(found.first, found.last) for found in read(query, today).days]


def test_list_all_of_a_service_for_today() -> None:
    """The sentence selects the service and today, and nothing else."""
    plan = read("list all haircut for today")
    assert plan.services == ("cut",)
    assert days("list all haircut for today") == [(TODAY, TODAY)]
    assert plan.texts == ()
    assert plan.understood() == ["Service: Haircut", "Date: today, 26 Nov 2026"]


@pytest.mark.parametrize(
    ("query", "services"),
    [
        ("HAIRCUTS", ("cut",)),
        ("washes", ("wash",)),
        ("introductory calls tomorrow", ("intro",)),
        ("haircut and wash", ("cut", "wash")),
        ("Monday Club", ("club",)),
    ],
)
def test_service_names_are_matched_whole_with_simple_plurals(query: str, services: tuple[str, ...]) -> None:
    """A service's full name wins over any other meaning of its words."""
    assert read(query).services == services


def test_part_of_a_service_name_is_searched_as_text() -> None:
    """Text is matched against customer and service names alike."""
    assert read("introductory").texts == ("introductory",)


def test_services_with_the_same_name_are_all_chosen() -> None:
    """Two services called the same are both searched, and described once."""
    plan = read_query("trim", [("a", "Trim"), ("b", "Trim")], TODAY)
    assert plan.services == ("a", "b")
    assert plan.understood() == ["Service: Trim"]


@pytest.mark.parametrize(
    ("query", "first", "last"),
    [
        ("yesterday", date(2026, 11, 25), date(2026, 11, 25)),
        ("tomorrow", date(2026, 11, 27), date(2026, 11, 27)),
        ("this week", date(2026, 11, 23), date(2026, 11, 29)),
        ("next week", date(2026, 11, 30), date(2026, 12, 6)),
        ("last week", date(2026, 11, 16), date(2026, 11, 22)),
        ("this month", date(2026, 11, 1), date(2026, 11, 30)),
        ("next month", date(2026, 12, 1), date(2026, 12, 31)),
        ("last month", date(2026, 10, 1), date(2026, 10, 31)),
        ("thursday", TODAY, TODAY),
        ("next thursday", date(2026, 12, 3), date(2026, 12, 3)),
        ("friday", date(2026, 11, 27), date(2026, 11, 27)),
        ("monday", date(2026, 11, 30), date(2026, 11, 30)),
        ("26 Nov", TODAY, TODAY),
        ("3rd December", date(2026, 12, 3), date(2026, 12, 3)),
        ("December 3", date(2026, 12, 3), date(2026, 12, 3)),
        ("3 Dec 2027", date(2027, 12, 3), date(2027, 12, 3)),
        ("26/11", TODAY, TODAY),
        ("1/2/27", date(2027, 2, 1), date(2027, 2, 1)),
        ("05/01/2026", date(2026, 1, 5), date(2026, 1, 5)),
        ("5 Jan", date(2027, 1, 5), date(2027, 1, 5)),
        ("March", date(2026, 3, 1), date(2026, 3, 31)),
        ("December 2027", date(2027, 12, 1), date(2027, 12, 31)),
    ],
)
def test_date_words_and_uk_dates(query: str, first: date, last: date) -> None:
    """Weeks run Monday to Sunday; a date without a year means the closest one."""
    assert days(query) == [(first, last)]


def test_months_cross_the_year() -> None:
    """Next month in December is January; last month in January is December."""
    assert days("next month", date(2026, 12, 15)) == [(date(2027, 1, 1), date(2027, 1, 31))]
    assert days("last month", date(2027, 1, 15)) == [(date(2026, 12, 1), date(2026, 12, 31))]


def test_several_dates_are_all_kept() -> None:
    """Today and tomorrow both count."""
    assert len(days("today and tomorrow")) == 2


@pytest.mark.parametrize("query", ["31/02", "31 February", "13/13", "31/12/9999", "Jan", "Mar"])
def test_what_is_not_a_date_is_searched_as_text(query: str) -> None:
    """An impossible date, or a short month on its own, is not read as a date."""
    plan = read(query)
    assert plan.days == ()
    assert plan.texts


@pytest.mark.parametrize("query", ["name@example.com", "NAME@EXAMPLE.COM", "show bookings for name@example.com."])
def test_an_email_address(query: str) -> None:
    """The address is searched in any letter case, without the punctuation after it."""
    plan = read(query)
    assert plan.emails == ("name@example.com",)
    assert plan.texts == ()


def test_part_of_an_email_address() -> None:
    """A domain on its own is enough."""
    assert read("@example.com").emails == ("@example.com",)


def test_a_star_means_the_name_starts_with() -> None:
    """``Moh*`` asks for names starting with Moh; a star alone asks for nothing."""
    plan = read("Moh*")
    assert plan.name_starts == ("Moh",)
    assert plan.texts == ()
    assert plan.understood() == ["Name starts with: Moh"]
    assert read("*") == SearchPlan()


@pytest.mark.parametrize(
    ("query", "start"),
    [("Sara Kh*", "Sara Kh"), ("haircut Moh*", "Moh"), ("bookings for Sara Kh* today", "Sara Kh")],
)
def test_words_before_a_star_are_the_start_of_the_same_name(query: str, start: str) -> None:
    """A first name and part of a surname are one start of a name."""
    plan = read(query)
    assert (plan.name_starts, plan.texts) == ((start,), ())


def test_a_name_with_a_date() -> None:
    """A name and a period are separate conditions."""
    plan = read("Sara Khan next week")
    assert plan.texts == ("Sara Khan",)
    assert len(plan.days) == 1


def test_statuses_and_timing() -> None:
    """Status words filter; asking for both past and upcoming limits nothing."""
    plan = read("cancelled haircut this month")
    assert (plan.statuses, plan.services) == (("cancelled",), ("cut",))
    assert read("canceled").statuses == ("cancelled",)
    assert read("upcoming confirmed").timing == "upcoming"
    assert read("past").timing == "past"
    assert read("past and upcoming").timing is None


@pytest.mark.parametrize("query", ['"May"', "“May”", 'bookings for "May"'])
def test_quotes_force_a_text_search(query: str) -> None:
    """A quoted word is a name even when it could be a month."""
    plan = read(query)
    assert (plan.texts, plan.days) == (("May",), ())


def test_an_unclosed_quote_is_ignored() -> None:
    """A single quote mark does not swallow the search."""
    assert read('"Sara').texts == ("Sara",)


def test_a_reference_is_text() -> None:
    """A reference is searched as written."""
    assert read("H49FV82SPR").texts == ("H49FV82SPR",)


def test_only_filler_asks_for_everything() -> None:
    """With no condition, every booking is wanted."""
    plan = read("Show me all the bookings, please")
    assert plan == SearchPlan()
    assert plan.understood() == []


def test_apostrophes_and_other_alphabets_are_kept() -> None:
    """Names are not changed by the rules."""
    assert read("O'Neil").texts == ("O'Neil",)
    assert read("محمد").texts == ("محمد",)
