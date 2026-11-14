"""Tests for the limited database role the API is meant to connect as, against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The database must have every migration applied,
including 0014. The tests connect as the database owner and switch to ``app_api`` with ``SET ROLE``, so no
password for the limited role is needed, and everything the permission checks do is rolled back.

What these tests establish: ``app_api`` holds exactly the table privileges the code uses and nothing more; it
cannot create or drop anything, create roles, delete bookings, or read or change the email log beyond adding to
it; and the real service code (free booking, manage link, owner screens, days off, hours, sign-up, emails)
works end to end when every statement runs as ``app_api``. They do not establish that the live API works with
the new connection string: that is checked by hand on staging after the switch.
"""

import os
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, time, timedelta

import psycopg
import pytest
from psycopg import errors
from test_manage_postgres import URL_ENV, World, world  # noqa: F401
from test_signup_postgres import new

from booking.manage import ManageService
from booking.notify import Notifier
from booking.owner import HoursBlock, OwnerService, OwnerServiceRecord
from booking.service import BookingRequest, BookingService
from postgres import Database, DatabaseSettings, PgConnection

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

ROLE = "app_api"
EXPECTED = {
    "bookings": {"SELECT", "INSERT", "UPDATE"},
    "businesses": {"SELECT", "INSERT", "UPDATE"},
    "services": {"SELECT", "INSERT", "UPDATE"},
    "business_members": {"SELECT", "INSERT"},
    "email_log": {"INSERT"},
    "payment_events": {"INSERT"},
    "time_off": {"SELECT", "INSERT", "DELETE"},
    "weekly_hours": {"SELECT", "INSERT", "DELETE"},
}
ALL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@pytest.fixture
def owner_connection() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """Open a connection as the database owner and roll everything back afterwards.

    Yields:
        The connection.
    """
    settings = DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]})
    connection: psycopg.Connection[tuple[object, ...]] = psycopg.connect(settings.conninfo)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def as_limited(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    """Switch the open transaction to the limited role.

    Args:
        connection: The owner's connection.
    """
    connection.execute(f"GRANT {ROLE} TO CURRENT_USER WITH SET TRUE")
    connection.execute(f"SET LOCAL ROLE {ROLE}")


def test_the_role_has_exactly_the_privileges_the_code_uses(
    owner_connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    """Every public table is checked for every table privilege, so a table added later without a decision shows up."""
    tables = [
        row[0]
        for row in owner_connection.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"
        ).fetchall()
    ]
    held = {
        table: {
            privilege
            for privilege in ALL_PRIVILEGES
            if owner_connection.execute(
                "SELECT has_table_privilege(%s, format('public.%%I', %s::text), %s)", (ROLE, table, privilege)
            ).fetchone()[
                0
            ]  # type: ignore[index]
        }
        for table in tables
    }
    assert {table: privileges for table, privileges in held.items() if privileges} == EXPECTED
    # The migration ledger (0015) is owner-only: it may exist, and the loop above already shows it holds nothing.
    assert set(held) - {"schema_migrations"} == set(
        EXPECTED
    ), "a table exists that this test and migration 0014 do not know about"


def test_the_role_has_no_powers_beyond_its_tables(owner_connection: psycopg.Connection[tuple[object, ...]]) -> None:
    """It is not a superuser, cannot create roles or databases, and cannot create objects in the public schema."""
    row = owner_connection.execute(
        "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolcanlogin "
        "FROM pg_roles WHERE rolname = %s",
        (ROLE,),
    ).fetchone()
    assert row == (False, False, False, False, True, True)
    usage, create = owner_connection.execute(
        "SELECT has_schema_privilege(%s, 'public', 'USAGE'), has_schema_privilege(%s, 'public', 'CREATE')",
        (ROLE, ROLE),
    ).fetchone()  # type: ignore[misc]
    assert (usage, create) == (True, False)


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE public.email_log",
        "TRUNCATE public.bookings",
        "DELETE FROM public.bookings WHERE false",
        "DELETE FROM public.businesses WHERE false",
        "UPDATE public.email_log SET status = status WHERE false",
        "SELECT count(*) FROM public.email_log",
        "UPDATE public.business_members SET role = role WHERE false",
        "ALTER TABLE public.bookings ADD COLUMN probe text",
        "CREATE TABLE public.probe (id int)",
        "CREATE ROLE probe_role",
        "CREATE DATABASE probe_db",
    ],
)
def test_the_role_is_refused_what_the_api_never_does(
    owner_connection: psycopg.Connection[tuple[object, ...]], statement: str
) -> None:
    """Each statement the API never runs is refused for the limited role."""
    as_limited(owner_connection)
    with pytest.raises((errors.InsufficientPrivilege, errors.ActiveSqlTransaction)):
        owner_connection.execute(statement)


def test_the_role_reads_every_row_despite_row_level_security(
    owner_connection: psycopg.Connection[tuple[object, ...]],
) -> None:
    """Row-level security stays on, with no policies; the role bypasses it on purpose, as the owner does today."""
    expected = owner_connection.execute("SELECT count(*) FROM public.bookings").fetchone()[0]  # type: ignore[index]
    as_limited(owner_connection)
    assert owner_connection.execute("SELECT count(*) FROM public.bookings").fetchone()[0] == expected  # type: ignore[index]


class LimitedDatabase(Database):
    """A database boundary whose every transaction runs as ``app_api``, for exercising the real service code."""

    @contextmanager
    def connection(self) -> Iterator[PgConnection]:
        """Borrow a connection and make its transaction run as the limited role.

        Yields:
            A typed psycopg connection.
        """
        with super().connection() as connection:
            connection.execute(f"GRANT {ROLE} TO CURRENT_USER WITH SET TRUE")
            connection.execute(f"SET LOCAL ROLE {ROLE}")
            yield connection


def test_the_service_code_works_end_to_end_as_the_limited_role(world: World) -> None:  # noqa: F811
    """A free booking, its manage link, the owner's screens and the emails all run with only app_api's privileges."""
    limited = LimitedDatabase(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    try:
        notifier = Notifier(limited, world.mailer, "https://book.example.com")
        bookings = BookingService(limited, notifier=notifier)
        manage = ManageService(limited, notifier=notifier)
        owners = OwnerService(limited, notifier=notifier)
        slug, owner, day = world.slug, world.owner, world.day
        _, _, days = bookings.availability(slug, "hour", day, 1)  # type: ignore[arg-type]
        assert any(s.available for s in days[0].slots)
        confirmed = bookings.create_booking(
            slug, BookingRequest("hour", day, "10:00", "Pat Customer", "pat@example.com", None, None)  # type: ignore[arg-type]
        )
        assert confirmed.manage_token is not None
        assert manage.view(slug, confirmed.reference, confirmed.manage_token).status == "confirmed"
        assert manage.move(slug, confirmed.reference, confirmed.manage_token, day, "11:00").status == "confirmed"  # type: ignore[arg-type]
        assert manage.cancel(slug, confirmed.reference, confirmed.manage_token).status == "cancelled"
        second = bookings.create_booking(
            slug, BookingRequest("hour", day, "13:00", "Sam Customer", "sam@example.com", None, None)  # type: ignore[arg-type]
        )
        _, listed = owners.bookings(owner, slug, day, 1)  # type: ignore[arg-type]
        assert second.reference in {b.reference for b in listed}
        owners.cancel_booking(owner, slug, second.reference)
        owners.update_settings(owner, slug, {"window_days": 20})
        owners.replace_hours(owner, slug, [HoursBlock(1, time(9, 0), time(17, 0))])
        added = owners.create_service(owner, slug, OwnerServiceRecord("extra", "Extra", 30, 0, True, 0))
        owners.update_service(owner, slug, added.slug, {"active": False})
        entries, _ = owners.add_time_off(
            owner, slug, date.today() + timedelta(days=40), date.today() + timedelta(days=41), "probe"
        )
        owners.remove_time_off(owner, slug, entries[0].id)
        assert len(world.mailer.sent) >= 4
        with world.database.connection() as connection:
            logged = connection.execute(
                "SELECT count(*) FROM email_log l JOIN businesses b ON b.id = l.business_id WHERE b.slug = %s", (slug,)
            ).fetchone()
        assert logged is not None and logged[0] >= 4
    finally:
        limited.close()


def test_sign_up_works_as_the_limited_role() -> None:
    """Creating a business, with its owner, free service and hours, needs nothing beyond the granted privileges."""
    limited = LimitedDatabase(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    owner_db = Database(DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}))
    tag = secrets.token_hex(3)
    try:
        owners = OwnerService(limited)
        account = str(uuid.uuid4())
        made = owners.create_business(account, new(f"Signup Test {tag} Limited"))
        assert [m.slug for m in owners.memberships(account)] == [made.slug]
        assert [s.slug for s in owners.services(account, made.slug)] == ["intro"]
    finally:
        with owner_db.connection() as connection:
            ids = [
                str(row[0])
                for row in connection.execute("SELECT id FROM businesses WHERE slug LIKE 'signup-test-%'").fetchall()
            ]
            for table in ("email_log", "bookings", "business_members", "weekly_hours", "services"):
                connection.execute(f"DELETE FROM {table} WHERE business_id = ANY(%s::uuid[])", (ids,))
            connection.execute("DELETE FROM businesses WHERE id = ANY(%s::uuid[])", (ids,))
        owner_db.close()
        limited.close()
