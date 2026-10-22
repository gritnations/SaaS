"""Tests for customer links: the product pages layout and the staging page layout."""

import pytest

from api.main import ApiConfigError, create_api
from booking.links import CustomerLinks, as_links
from booking.notify import NotifyConfigError, site_url_from_environment

TOKEN = "ab" * 24


def test_product_pages_layout() -> None:
    """With product pages, booking and manage links use their own paths; the code stays after ``#``."""
    links = CustomerLinks("https://book.example.com", pages=True)
    assert links.booking("fade") == "https://book.example.com/book/fade"
    assert links.manage("fade", "ZA2JBN2C5B", TOKEN) == f"https://book.example.com/manage/#fade.ZA2JBN2C5B.{TOKEN}"


def test_staging_layout_is_unchanged() -> None:
    """Without product pages, links keep the staging page layout that emails already use."""
    links = CustomerLinks("https://book.example.com")
    assert links.booking("fade") == "https://book.example.com/?b=fade"
    assert links.manage("fade", "ZA2JBN2C5B", TOKEN) == f"https://book.example.com/#manage=fade.ZA2JBN2C5B.{TOKEN}"


def test_plain_site_url_means_staging_layout() -> None:
    """A plain site URL becomes staging links; links and ``None`` pass through."""
    assert as_links("https://book.example.com") == CustomerLinks("https://book.example.com")
    ready = CustomerLinks("https://book.example.com", pages=True)
    assert as_links(ready) is ready
    assert as_links(None) is None


def test_pages_url_is_validated_like_the_site_url() -> None:
    """``BOOKING_PAGES_URL`` must be https, and the error names that variable."""
    assert site_url_from_environment({"BOOKING_PAGES_URL": "https://pages.example.com/"}, "BOOKING_PAGES_URL") == (
        "https://pages.example.com"
    )
    with pytest.raises(NotifyConfigError, match="BOOKING_PAGES_URL"):
        site_url_from_environment({"BOOKING_PAGES_URL": "http://pages.example.com"}, "BOOKING_PAGES_URL")


def test_api_builds_without_a_database_when_pages_url_is_set() -> None:
    """Setting the pages URL with the site URL does not need a database or change start-up."""
    api = create_api(
        {
            "APP_ENV": "test",
            "BOOKING_PAGES_URL": "https://pages.example.com",
            "PUBLIC_SITE_URL": "https://site.example.com",
        }
    )
    assert api.title


def test_pages_url_without_site_url_is_refused() -> None:
    """Payment notifications are recorded only with the site URL, so the pages URL alone is refused."""
    with pytest.raises(ApiConfigError, match="BOOKING_PAGES_URL needs PUBLIC_SITE_URL"):
        create_api({"APP_ENV": "test", "BOOKING_PAGES_URL": "https://pages.example.com"})
    for blank in (" ", "/", " / "):
        with pytest.raises(ApiConfigError):
            create_api({"APP_ENV": "test", "BOOKING_PAGES_URL": "https://pages.example.com", "PUBLIC_SITE_URL": blank})
