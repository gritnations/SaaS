"""The look of a business's booking pages: colours, font, corners and logo.

Trust path: ``api -> booking.look -> postgres.Database -> PostgreSQL``.

An owner keeps a draft look and publishes it; customers see the published look only while the business's plan or
trial includes branding (:func:`branding_allowed`), and the default look otherwise. Every value comes from a fixed
set or is a ``#rrggbb`` colour, so an owner cannot put code into a customer's page. A look is published only when
its text and buttons are readable (:func:`readability_problems`).

A logo is a PNG, JPEG or WebP image of at most :data:`LOGO_MAX_BYTES`. Its declared type must match its own bytes
(:func:`sniff_image`); SVG is refused because an SVG opened on its own can run scripts. Logos are served from the
API's own origin, so the customer pages load no image from anywhere else.

Owner operations start from an account the API has verified and use :meth:`booking.owner.OwnerService._member_business`,
so an account that does not manage the business gets the same not-found answer as for a business that does not exist.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import psycopg
from pydantic import BaseModel, StringConstraints

from booking.owner import InvalidChangeError, OwnerService
from booking.service import PAID_PLANS, NotFoundError
from contracts import STRICT
from postgres import Database

LOGO_MAX_BYTES = 61_440
"""The largest logo accepted, in bytes. It stays under the API's request body limit."""

LOGO_TYPES = ("image/png", "image/jpeg", "image/webp")
"""The logo formats accepted."""

Colour = Annotated[str, StringConstraints(pattern=r"^#[0-9a-f]{6}$")]
"""A colour as ``#rrggbb`` in lower case."""

LOGO_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
"""A logo identifier: a UUID in its lower-case text form, as the database gives it."""

LogoId = Annotated[str, StringConstraints(pattern=LOGO_ID_PATTERN)]
"""A logo identifier."""

Font = Literal["modern", "classic", "rounded", "geometric"]
"""The fonts an owner can choose from. The pages map each to a font stack."""

Corners = Literal["sharp", "soft", "round"]
"""The corner styles an owner can choose from."""

MIN_TEXT_CONTRAST = 4.5
"""The least contrast for text on the background and on buttons (WCAG AA, normal text)."""

MIN_ACCENT_CONTRAST = 3.0
"""The least contrast between highlights and the background (WCAG AA, user-interface components)."""


class Look(BaseModel):
    """A booking page look.

    Attributes:
        accent: Buttons and highlights.
        background: Page background.
        text: Text colour.
        font: Font choice.
        corners: Corner style.
        logo_id: The logo shown in the header, or ``None`` to show the business name.
    """

    model_config = STRICT

    accent: Colour
    background: Colour
    text: Colour
    font: Font
    corners: Corners
    logo_id: LogoId | None = None


DEFAULT_LOOK = Look(accent="#13355a", background="#ffffff", text="#1c2733", font="modern", corners="soft")
"""The look customers see when a business has published none, or its plan does not include branding."""


class LookState(BaseModel):
    """What the owner's editor needs.

    Attributes:
        allowed: The business's plan or trial includes branding now.
        draft: The saved draft, or ``None``.
        published: The published look, or ``None`` for the default.
    """

    model_config = STRICT

    allowed: bool
    draft: Look | None = None
    published: Look | None = None


class Logo(BaseModel):
    """A stored logo.

    Attributes:
        content_type: Its image type.
        data: Its bytes.
    """

    model_config = STRICT

    content_type: Literal["image/png", "image/jpeg", "image/webp"]
    data: bytes


def branding_allowed(plan: str, trial_ends_at: datetime | None, now: datetime) -> bool:
    """Decide whether a business's plan includes a branded booking page.

    Args:
        plan: The business's plan.
        trial_ends_at: When its trial ends, or ``None``.
        now: The current instant.

    Returns:
        ``True`` on a paid plan or during the trial.
    """
    return plan in PAID_PLANS or (trial_ends_at is not None and trial_ends_at > now)


def _luminance(colour: str) -> float:
    """Give a colour's relative luminance as WCAG defines it.

    Args:
        colour: ``#rrggbb``.

    Returns:
        A value from 0 (black) to 1 (white).
    """
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    """Give the WCAG contrast ratio of two colours.

    Args:
        first: ``#rrggbb``.
        second: ``#rrggbb``.

    Returns:
        A ratio from 1 to 21.
    """
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def on_accent(accent: str) -> str:
    """Choose the button text colour for an accent: white or near-black, whichever reads better.

    Args:
        accent: ``#rrggbb``.

    Returns:
        ``#ffffff`` or ``#111111``.
    """
    return "#ffffff" if contrast(accent, "#ffffff") >= contrast(accent, "#111111") else "#111111"


def readability_problems(look: Look) -> list[str]:
    """List why a look would be hard to read.

    Args:
        look: The look.

    Returns:
        Plain-English problems; empty when the look can be published.
    """
    problems = []
    if contrast(look.text, look.background) < MIN_TEXT_CONTRAST:
        problems.append("text on the background is hard to read")
    if contrast(on_accent(look.accent), look.accent) < MIN_TEXT_CONTRAST:
        problems.append("button text is hard to read")
    if contrast(look.accent, look.background) < MIN_ACCENT_CONTRAST:
        problems.append("highlights barely stand out from the background")
    return problems


def sniff_image(data: bytes) -> str | None:
    """Recognise an accepted image format from its first bytes.

    Args:
        data: The file's bytes.

    Returns:
        ``image/png``, ``image/jpeg`` or ``image/webp``, or ``None`` for anything else.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_LOOK_COLUMNS = "accent, background, text_colour, font, corners, logo_id::text"


class LookService:
    """Booking page looks and logos backed by PostgreSQL."""

    def __init__(self, database: Database, clock: Callable[[], datetime] | None = None) -> None:
        """Create the service. Performs no I/O.

        Args:
            database: The database boundary.
            clock: Returns the current aware instant. ``None`` means the system clock in UTC.
        """
        self._database = database
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def _allowed(self, connection: psycopg.Connection[Any], business_id: str) -> bool:
        """Check whether a business's plan or trial includes branding now.

        Args:
            connection: An open connection.
            business_id: The business's identifier.

        Returns:
            Whether branding is included.
        """
        row: Any = connection.execute(
            "SELECT plan, trial_ends_at FROM businesses WHERE id = %s", (business_id,)
        ).fetchone()
        return branding_allowed(row[0], row[1], self._clock())

    @staticmethod
    def _look(connection: psycopg.Connection[Any], business_id: str, state: str) -> Look | None:
        """Read one of a business's looks.

        Args:
            connection: An open connection.
            business_id: The business's identifier.
            state: ``draft`` or ``published``.

        Returns:
            The look, or ``None``.
        """
        row = connection.execute(
            f"SELECT {_LOOK_COLUMNS} FROM booking_looks WHERE business_id = %s AND state = %s",
            (business_id, state),
        ).fetchone()
        if row is None:
            return None
        return Look(accent=row[0], background=row[1], text=row[2], font=row[3], corners=row[4], logo_id=row[5])

    @staticmethod
    def _write(connection: psycopg.Connection[Any], business_id: str, state: str, look: Look) -> None:
        """Insert or replace one of a business's looks.

        Args:
            connection: An open connection.
            business_id: The business's identifier.
            state: ``draft`` or ``published``.
            look: The look.
        """
        connection.execute(
            "INSERT INTO booking_looks (business_id, state, accent, background, text_colour, font, corners, logo_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (business_id, state) DO UPDATE SET accent = EXCLUDED.accent, "
            "background = EXCLUDED.background, text_colour = EXCLUDED.text_colour, font = EXCLUDED.font, "
            "corners = EXCLUDED.corners, logo_id = EXCLUDED.logo_id, updated_at = now()",
            (business_id, state, look.accent, look.background, look.text, look.font, look.corners, look.logo_id),
        )

    @staticmethod
    def _prune_logos(connection: psycopg.Connection[Any], business_id: str, keep: str | None = None) -> None:
        """Remove a business's logos that no look uses.

        Args:
            connection: An open connection.
            business_id: The business's identifier.
            keep: A logo to keep even if unused, such as one just uploaded.
        """
        connection.execute(
            "DELETE FROM business_logos l WHERE l.business_id = %s AND l.id IS DISTINCT FROM %s::uuid "
            "AND NOT EXISTS (SELECT 1 FROM booking_looks k WHERE k.logo_id = l.id)",
            (business_id, keep),
        )

    def _state(self, connection: psycopg.Connection[Any], business_id: str) -> LookState:
        """Read the editor state of a loaded business.

        Args:
            connection: An open connection.
            business_id: The business's identifier.

        Returns:
            The state.
        """
        return LookState(
            allowed=self._allowed(connection, business_id),
            draft=self._look(connection, business_id, "draft"),
            published=self._look(connection, business_id, "published"),
        )

    def state(self, account: str, slug: str) -> LookState:
        """Read the draft and published looks.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The editor state.

        Raises:
            NotFoundError: If the account does not manage the business.
        """
        with self._database.connection() as connection:
            business = OwnerService._member_business(connection, account, slug)
            return self._state(connection, business.id)

    def save_draft(self, account: str, slug: str, look: Look) -> LookState:
        """Save the draft. Customers still see the published look. Saving the same draft twice changes nothing more.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            look: The draft.

        Returns:
            The editor state.

        Raises:
            NotFoundError: If the account does not manage the business.
            InvalidChangeError: If the draft names a logo the business does not have.
        """
        with self._database.connection() as connection:
            business = OwnerService._member_business(connection, account, slug)
            if look.logo_id is not None:
                owned = connection.execute(
                    "SELECT 1 FROM business_logos WHERE id = %s::uuid AND business_id = %s",
                    (look.logo_id, business.id),
                ).fetchone()
                if owned is None:
                    raise InvalidChangeError("That logo was not found. Upload it again.")
            self._write(connection, business.id, "draft", look)
            self._prune_logos(connection, business.id)
            return self._state(connection, business.id)

    def publish(self, account: str, slug: str) -> LookState:
        """Make the draft the look customers see. Publishing the same draft twice changes nothing more.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The editor state.

        Raises:
            NotFoundError: If the account does not manage the business.
            InvalidChangeError: If there is no draft, the plan does not include branding, or the draft is hard
                to read.
        """
        with self._database.connection() as connection:
            business = OwnerService._member_business(connection, account, slug)
            draft = self._look(connection, business.id, "draft")
            if draft is None:
                raise InvalidChangeError("Save a draft before publishing.")
            if not self._allowed(connection, business.id):
                raise InvalidChangeError("A branded booking page is part of the paid plans. Upgrade to publish it.")
            problems = readability_problems(draft)
            if problems:
                raise InvalidChangeError("This look can't be published yet: " + "; ".join(problems) + ".")
            self._write(connection, business.id, "published", draft)
            self._prune_logos(connection, business.id)
            return self._state(connection, business.id)

    def reset(self, account: str, slug: str) -> LookState:
        """Go back to the default look: remove the draft, the published look and the logos.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.

        Returns:
            The editor state.

        Raises:
            NotFoundError: If the account does not manage the business.
        """
        with self._database.connection() as connection:
            business = OwnerService._member_business(connection, account, slug)
            connection.execute("DELETE FROM booking_looks WHERE business_id = %s", (business.id,))
            self._prune_logos(connection, business.id)
            return self._state(connection, business.id)

    def upload_logo(self, account: str, slug: str, content_type: str, data: bytes) -> str:
        """Store a logo for use in a draft. It is removed again if no draft or published look uses it.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            content_type: The declared type, without parameters.
            data: The image's bytes.

        Returns:
            The logo's identifier.

        Raises:
            NotFoundError: If the account does not manage the business.
            InvalidChangeError: If the file is empty, too large, not an accepted type, or its bytes do not match
                its declared type.
        """
        if content_type not in LOGO_TYPES:
            raise InvalidChangeError("Upload a PNG, JPEG or WebP image.")
        if not data:
            raise InvalidChangeError("The image is empty.")
        if len(data) > LOGO_MAX_BYTES:
            raise InvalidChangeError(f"The image is larger than {LOGO_MAX_BYTES // 1024} KB.")
        if sniff_image(data) != content_type:
            raise InvalidChangeError("The file is not the image type it claims to be.")
        with self._database.connection() as connection:
            business = OwnerService._member_business(connection, account, slug)
            row: Any = connection.execute(
                "INSERT INTO business_logos (business_id, content_type, data) VALUES (%s, %s, %s) RETURNING id::text",
                (business.id, content_type, data),
            ).fetchone()
            logo_id: str = row[0]
            self._prune_logos(connection, business.id, keep=logo_id)
            return logo_id

    def public_look(self, slug: str) -> Look | None:
        """Give the look customers see.

        Args:
            slug: The business's public identifier.

        Returns:
            The published look while the plan or trial includes branding; ``None`` for the default look.

        Raises:
            NotFoundError: If the business does not exist.
        """
        with self._database.connection() as connection:
            row: Any = connection.execute("SELECT id::text FROM businesses WHERE slug = %s", (slug,)).fetchone()
            if row is None:
                raise NotFoundError("Business not found.")
            if not self._allowed(connection, row[0]):
                return None
            return self._look(connection, row[0], "published")

    def logo(self, slug: str, logo_id: str) -> Logo:
        """Give a logo that a business's draft or published look uses.

        A draft's logo is served too, so the owner's preview can show it; logo identifiers are random and a
        logo is shown to customers anyway once published.

        Args:
            slug: The business's public identifier.
            logo_id: The logo's identifier.

        Returns:
            The logo.

        Raises:
            NotFoundError: If the business or the logo does not exist, or the logo belongs to another business.
        """
        if re.fullmatch(LOGO_ID_PATTERN, logo_id) is None:
            raise NotFoundError("Logo not found.")
        with self._database.connection() as connection:
            row: Any = connection.execute(
                "SELECT l.content_type, l.data FROM business_logos l JOIN businesses b ON b.id = l.business_id "
                "WHERE b.slug = %s AND l.id = %s::uuid",
                (slug, logo_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("Logo not found.")
            return Logo(content_type=row[0], data=bytes(row[1]))
