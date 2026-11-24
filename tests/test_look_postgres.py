"""Tests for booking page looks and logos against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied, including
``0021_booking_look.sql``. Each test creates its own businesses with random slugs and deletes everything it created.

What these tests establish: a draft is private until published; publishing needs a draft, a plan or trial that
includes branding, and a readable look; customers see the published look only while branding is included and the
default look otherwise; saving or publishing the same thing twice changes nothing more; reset goes back to the
default; a logo must be an accepted image whose bytes match its type, is served only under its own business, and
cannot be used by another business; logos no look uses are removed, but never one that a change to the same business
still in progress has put in a look; a business that no longer exists has no branding; the database refuses values outside the fixed
sets even if the application were bypassed; another account cannot see or change the look; and everything works
with only the API role's privileges.
"""

import os
import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors
from test_app_api_role_postgres import LimitedDatabase

from booking.look import LOGO_MAX_BYTES, Look, LookService
from booking.owner import InvalidChangeError
from booking.service import NotFoundError
from postgres import Database, DatabaseSettings

URL_ENV = "BOOKING_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MIDNIGHT = Look(accent="#e0aa55", background="#0c1726", text="#f3f5f8", font="classic", corners="round")
UNREADABLE = Look(accent="#e0aa55", background="#ffffff", text="#f0f0f0", font="modern", corners="soft")


@dataclass
class Shop:
    """Two temporary businesses, each with its own owner.

    Attributes:
        database: The database boundary.
        slug: The first business.
        account: Its owner.
        other_slug: The second business.
        other_account: Its owner.
        ids: Every business created, for cleanup.
    """

    database: Database
    slug: str
    account: str
    other_slug: str
    other_account: str
    ids: list[str] = field(default_factory=list)


def make_business(database: Database, trial_days: int | None = 7) -> tuple[str, str, str]:
    """Create a business with an owner, on the free plan.

    Args:
        database: The database boundary.
        trial_days: Days of trial left, negative for an ended trial, or ``None`` for none.

    Returns:
        ``(slug, owner account, business id)``.
    """
    slug = "look-" + secrets.token_hex(6)
    account = str(uuid.uuid4())
    trial = None if trial_days is None else datetime.now(UTC) + timedelta(days=trial_days)
    with database.connection() as connection:
        row = connection.execute(
            "INSERT INTO businesses (slug, name, time_zone, trial_ends_at) VALUES (%s, 'Look test', 'Europe/London', %s) "
            "RETURNING id::text",
            (slug, trial),
        ).fetchone()
        assert row is not None
        connection.execute(
            "INSERT INTO business_members (business_id, user_id, role) VALUES (%s, %s, 'owner')", (row[0], account)
        )
    return slug, account, row[0]


@pytest.fixture
def shop() -> Iterator[Shop]:
    """Create two businesses in their trial, then remove them and everything the test added.

    Yields:
        The businesses.
    """
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    slug, account, first = make_business(database)
    other_slug, other_account, second = make_business(database)
    made = Shop(database, slug, account, other_slug, other_account, [first, second])
    try:
        yield made
    finally:
        with database.connection() as connection:
            for table in ("booking_looks", "business_logos", "business_members"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = ANY(%s::uuid[])", (made.ids,))
            connection.execute("DELETE FROM businesses WHERE id = ANY(%s::uuid[])", (made.ids,))
        database.close()


def set_plan(shop: Shop, plan: str, trial_days: int | None) -> None:
    """Change the first business's plan and trial.

    Args:
        shop: The test businesses.
        plan: ``free``, ``starter`` or ``pro``.
        trial_days: Days of trial left, negative for ended, or ``None`` for none.
    """
    trial = None if trial_days is None else datetime.now(UTC) + timedelta(days=trial_days)
    with shop.database.connection() as connection:
        connection.execute(
            "UPDATE businesses SET plan = %s, trial_ends_at = %s WHERE id = %s", (plan, trial, shop.ids[0])
        )


def logo_count(shop: Shop) -> int:
    """Count the first business's stored logos.

    Args:
        shop: The test businesses.

    Returns:
        How many.
    """
    with shop.database.connection() as connection:
        row = connection.execute(
            "SELECT count(*) FROM business_logos WHERE business_id = %s", (shop.ids[0],)
        ).fetchone()
        assert row is not None
        return int(row[0])


def test_a_draft_is_private_until_published(shop: Shop) -> None:
    """Saving a draft leaves customers on the default look; publishing shows it; repeats change nothing more."""
    looks = LookService(shop.database)
    state = looks.save_draft(shop.account, shop.slug, MIDNIGHT)
    assert state.allowed and state.draft == MIDNIGHT and state.published is None
    assert looks.public_look(shop.slug) is None
    looks.save_draft(shop.account, shop.slug, MIDNIGHT)
    state = looks.publish(shop.account, shop.slug)
    assert state.published == MIDNIGHT
    assert looks.publish(shop.account, shop.slug).published == MIDNIGHT
    assert looks.public_look(shop.slug) == MIDNIGHT
    assert looks.public_look(shop.other_slug) is None
    with shop.database.connection() as connection:
        row = connection.execute("SELECT count(*) FROM booking_looks WHERE business_id = %s", (shop.ids[0],)).fetchone()
        assert row is not None and row[0] == 2


def test_publishing_needs_a_draft_a_plan_and_a_readable_look(shop: Shop) -> None:
    """No draft, no branding on the plan, or a hard-to-read draft: refused, and nothing is published."""
    looks = LookService(shop.database)
    with pytest.raises(InvalidChangeError, match="Save a draft"):
        looks.publish(shop.account, shop.slug)
    looks.save_draft(shop.account, shop.slug, UNREADABLE)
    with pytest.raises(InvalidChangeError, match="hard to read"):
        looks.publish(shop.account, shop.slug)
    set_plan(shop, "free", -1)
    looks.save_draft(shop.account, shop.slug, MIDNIGHT)
    with pytest.raises(InvalidChangeError, match="paid plans"):
        looks.publish(shop.account, shop.slug)
    assert looks.state(shop.account, shop.slug).published is None


def test_customers_see_the_look_only_while_branding_is_included(shop: Shop) -> None:
    """When the trial ends the default look returns; a paid plan brings the published look back."""
    looks = LookService(shop.database)
    looks.save_draft(shop.account, shop.slug, MIDNIGHT)
    looks.publish(shop.account, shop.slug)
    set_plan(shop, "free", -1)
    assert looks.public_look(shop.slug) is None
    assert looks.state(shop.account, shop.slug).allowed is False
    set_plan(shop, "starter", None)
    assert looks.public_look(shop.slug) == MIDNIGHT


def test_reset_goes_back_to_the_default_and_removes_the_logos(shop: Shop) -> None:
    """No draft, no published look, no logo left."""
    looks = LookService(shop.database)
    logo = looks.upload_logo(shop.account, shop.slug, "image/png", PNG)
    looks.save_draft(shop.account, shop.slug, MIDNIGHT.model_copy(update={"logo_id": logo}))
    looks.publish(shop.account, shop.slug)
    state = looks.reset(shop.account, shop.slug)
    assert state.draft is None and state.published is None
    assert looks.public_look(shop.slug) is None
    assert logo_count(shop) == 0


def test_a_logo_is_stored_and_served_under_its_own_business_only(shop: Shop) -> None:
    """The bytes and type come back unchanged; the other business's slug finds nothing."""
    looks = LookService(shop.database)
    logo = looks.upload_logo(shop.account, shop.slug, "image/jpeg", JPEG)
    found = looks.logo(shop.slug, logo)
    assert found.content_type == "image/jpeg" and found.data == JPEG
    with pytest.raises(NotFoundError):
        looks.logo(shop.other_slug, logo)
    with pytest.raises(NotFoundError):
        looks.logo(shop.slug, str(uuid.uuid4()))
    with pytest.raises(NotFoundError):
        looks.logo(shop.slug, "not-a-uuid")


@pytest.mark.parametrize(
    ("content_type", "data", "message"),
    [
        ("image/svg+xml", b"<svg/>", "PNG, JPEG or WebP"),
        ("image/gif", b"GIF89a", "PNG, JPEG or WebP"),
        ("image/png", b"", "empty"),
        ("image/png", JPEG, "not the image type"),
        ("image/png", b"<html><script>alert(1)</script></html>", "not the image type"),
        ("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * LOGO_MAX_BYTES, "larger than"),
    ],
    ids=["svg", "gif", "empty", "jpeg-as-png", "html-as-png", "too-large"],
)
def test_a_bad_logo_is_refused_and_nothing_is_stored(shop: Shop, content_type: str, data: bytes, message: str) -> None:
    """Wrong type, empty, mismatched bytes or too large: refused before anything is written."""
    with pytest.raises(InvalidChangeError, match=message):
        LookService(shop.database).upload_logo(shop.account, shop.slug, content_type, data)
    assert logo_count(shop) == 0


def test_a_business_cannot_use_another_business_logo(shop: Shop) -> None:
    """A draft naming the other business's logo is refused."""
    looks = LookService(shop.database)
    theirs = looks.upload_logo(shop.other_account, shop.other_slug, "image/png", PNG)
    with pytest.raises(InvalidChangeError, match="logo was not found"):
        looks.save_draft(shop.account, shop.slug, MIDNIGHT.model_copy(update={"logo_id": theirs}))
    assert looks.state(shop.account, shop.slug).draft is None


def test_logos_no_look_uses_are_removed(shop: Shop) -> None:
    """Repeated uploads keep only the newest unused one; the published logo stays while a new draft changes it."""
    looks = LookService(shop.database)
    first = looks.upload_logo(shop.account, shop.slug, "image/png", PNG)
    looks.upload_logo(shop.account, shop.slug, "image/png", PNG)
    second = looks.upload_logo(shop.account, shop.slug, "image/png", PNG)
    assert logo_count(shop) == 1
    looks.save_draft(shop.account, shop.slug, MIDNIGHT.model_copy(update={"logo_id": second}))
    looks.publish(shop.account, shop.slug)
    third = looks.upload_logo(shop.account, shop.slug, "image/jpeg", JPEG)
    looks.save_draft(shop.account, shop.slug, MIDNIGHT.model_copy(update={"logo_id": third}))
    assert logo_count(shop) == 2
    assert looks.logo(shop.slug, second).data == PNG
    with pytest.raises(NotFoundError):
        looks.logo(shop.slug, first)
    looks.save_draft(shop.account, shop.slug, MIDNIGHT)
    assert logo_count(shop) == 1


def test_a_change_waits_for_another_change_to_the_same_business(shop: Shop) -> None:
    """A second change runs after an unfinished one, so its clean-up never strips a logo from a look.

    The unfinished change is played by a second connection that holds the business's row, as every look change
    does, and publishes a look using a logo nothing uses yet. A reset started meanwhile must wait for it and then
    remove both the look and the logo. Without the wait, the reset's clean-up would delete the logo first and leave
    the new published look without it. Reset is used because nothing else in it waits on the business's row.
    """
    looks = LookService(shop.database)
    logo = looks.upload_logo(shop.account, shop.slug, "image/png", PNG)
    outcome: dict[str, object] = {}

    def reset() -> None:
        """Reset the look, which removes the logos no look uses."""
        try:
            outcome["state"] = looks.reset(shop.account, shop.slug)
        except Exception as error:  # noqa: BLE001 - reported to the test below
            outcome["error"] = error

    settings = DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]})
    with psycopg.connect(settings.conninfo) as other:
        other.execute("SELECT 1 FROM businesses WHERE id = %s FOR UPDATE", (shop.ids[0],))
        other.execute(
            "INSERT INTO booking_looks (business_id, state, accent, background, text_colour, font, corners, logo_id) "
            "VALUES (%s, 'published', '#e0aa55', '#0c1726', '#f3f5f8', 'classic', 'round', %s)",
            (shop.ids[0], logo),
        )
        second = threading.Thread(target=reset)
        second.start()
        time.sleep(1.0)
        assert second.is_alive(), "the reset did not wait for the unfinished change"
        other.commit()
    second.join(timeout=10)
    assert not second.is_alive() and "error" not in outcome
    assert looks.public_look(shop.slug) is None
    assert logo_count(shop) == 0


def test_a_business_that_no_longer_exists_has_no_branding(shop: Shop) -> None:
    """The plan check answers ``False`` rather than failing when the business row is gone."""
    looks = LookService(shop.database)
    with shop.database.connection() as connection:
        assert looks._allowed(connection, str(uuid.uuid4())) is False


def test_another_account_cannot_see_or_change_the_look(shop: Shop) -> None:
    """Every owner operation on a business the account does not manage is not found."""
    looks = LookService(shop.database)
    looks.save_draft(shop.account, shop.slug, MIDNIGHT)
    stranger = shop.other_account
    for call in (
        lambda: looks.state(stranger, shop.slug),
        lambda: looks.save_draft(stranger, shop.slug, UNREADABLE),
        lambda: looks.publish(stranger, shop.slug),
        lambda: looks.reset(stranger, shop.slug),
        lambda: looks.upload_logo(stranger, shop.slug, "image/png", PNG),
    ):
        with pytest.raises(NotFoundError):
            call()
    assert looks.state(shop.account, shop.slug).draft == MIDNIGHT
    with pytest.raises(NotFoundError):
        looks.public_look("no-such-business-" + secrets.token_hex(4))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("accent", "#ABCDEF"),
        ("accent", "red"),
        ("background", "#000;}x{"),
        ("font", "Papyrus"),
        ("corners", "pill"),
        ("state", "preview"),
    ],
)
def test_the_database_refuses_values_outside_the_fixed_sets(shop: Shop, column: str, value: str) -> None:
    """Even a direct insert cannot store a colour, font, corner style or state outside its set."""
    values = {"state": "draft", "accent": "#13355a", "background": "#ffffff", "font": "modern", "corners": "soft"}
    values[column] = value
    with pytest.raises(errors.CheckViolation):
        with shop.database.connection() as connection:
            connection.execute(
                "INSERT INTO booking_looks (business_id, state, accent, background, text_colour, font, corners) "
                "VALUES (%s, %s, %s, %s, '#111111', %s, %s)",
                (
                    shop.ids[0],
                    values["state"],
                    values["accent"],
                    values["background"],
                    values["font"],
                    values["corners"],
                ),
            )


@pytest.mark.parametrize(
    ("content_type", "size"),
    [("image/svg+xml", 10), ("text/html", 10), ("image/png", 0), ("image/png", LOGO_MAX_BYTES + 1)],
)
def test_the_database_refuses_a_logo_outside_the_limits(shop: Shop, content_type: str, size: int) -> None:
    """Even a direct insert cannot store another type, an empty file or one over the size limit."""
    with pytest.raises(errors.CheckViolation):
        with shop.database.connection() as connection:
            connection.execute(
                "INSERT INTO business_logos (business_id, content_type, data) VALUES (%s, %s, %s)",
                (shop.ids[0], content_type, b"\x00" * size),
            )


def test_everything_works_as_the_limited_role(shop: Shop) -> None:
    """Upload, draft, publish, the public look, the logo and reset need nothing beyond the API role's privileges."""
    limited = LimitedDatabase(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    try:
        looks = LookService(limited)
        looks.upload_logo(shop.account, shop.slug, "image/jpeg", JPEG)
        logo = looks.upload_logo(shop.account, shop.slug, "image/png", PNG)
        looks.save_draft(shop.account, shop.slug, MIDNIGHT.model_copy(update={"logo_id": logo}))
        looks.publish(shop.account, shop.slug)
        assert looks.public_look(shop.slug) == MIDNIGHT.model_copy(update={"logo_id": logo})
        assert looks.logo(shop.slug, logo).data == PNG
        assert looks.reset(shop.account, shop.slug).published is None
    finally:
        limited.close()


def test_the_data_api_roles_cannot_read_the_tables() -> None:
    """Where the hosted database's public roles exist, they hold no privilege on either table."""
    settings = DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]})
    with psycopg.connect(settings.conninfo) as connection:
        roles = [
            r[0]
            for r in connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')"
            ).fetchall()
        ]
        for role in roles:
            for table in ("booking_looks", "business_logos"):
                row = connection.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, table)).fetchone()
                assert row is not None and row[0] is False
