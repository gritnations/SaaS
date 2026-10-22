"""Links that send customers back to the website: manage links in emails and checkout returns.

Two layouts exist while the product pages replace the single staging page:
    * Product pages, when ``BOOKING_PAGES_URL`` is set: ``/book/{slug}`` and ``/manage/#{slug}.{ref}.{token}``.
    * The staging page otherwise, from ``PUBLIC_SITE_URL``: ``/?b={slug}`` and ``/#manage={slug}.{ref}.{token}``.

The manage code always travels after ``#``, so it never reaches a server's logs. Owner links (payments and
plans) are not built here; they still return to ``PUBLIC_SITE_URL``.
"""

from dataclasses import dataclass

PAGES_URL_ENV = "BOOKING_PAGES_URL"
"""Where the product pages are served. When set, customer links use the product pages layout."""


@dataclass(frozen=True)
class CustomerLinks:
    """Builds customer-facing links.

    Attributes:
        base: The site, without a trailing slash.
        pages: ``True`` for the product pages layout, ``False`` for the staging page layout.
    """

    base: str
    pages: bool = False

    def booking(self, slug: str) -> str:
        """Link to a business's booking page.

        Args:
            slug: Business slug.

        Returns:
            The link.
        """
        return f"{self.base}/book/{slug}" if self.pages else f"{self.base}/?b={slug}"

    def manage(self, slug: str, reference: str, token: str) -> str:
        """Link to manage one booking.

        Args:
            slug: Business slug.
            reference: Booking reference.
            token: Manage code.

        Returns:
            The link.
        """
        if self.pages:
            return f"{self.base}/manage/#{slug}.{reference}.{token}"
        return f"{self.base}/#manage={slug}.{reference}.{token}"


def as_links(value: str | CustomerLinks | None) -> CustomerLinks | None:
    """Accept a plain site URL (staging layout) or ready-made links.

    Args:
        value: A site URL without a trailing slash, links, or ``None``.

    Returns:
        Links, or ``None``.
    """
    if value is None or isinstance(value, CustomerLinks):
        return value
    return CustomerLinks(value)
