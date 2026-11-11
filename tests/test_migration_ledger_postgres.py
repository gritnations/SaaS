"""Tests for the migration ledger against a real PostgreSQL database.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. The tests connect as the database owner. If the ledger
table is not there yet, migration 0015 is run inside the test's own transaction, and everything is rolled back
afterwards, so the database is not changed. If the table is already there, the tests check the real one.

What these tests establish: the ledger holds a row for every migration file up to 0015; its version rule
refuses a repeated or badly named version; and no Data API role and not the limited API role can read or
change it. They do not establish that the migrations were applied to the database in the right order.
"""

import os

import psycopg
import pytest
from psycopg import errors
from test_app_api_role_postgres import ROLE, as_limited, owner_connection  # noqa: F401
from test_manage_postgres import URL_ENV
from test_migrations import FILES, LEDGER_FIRST, number

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

LEDGER_FILE = next(path for path in FILES if number(path) == LEDGER_FIRST)
Connection = psycopg.Connection[tuple[object, ...]]


def with_ledger(connection: Connection) -> None:
    """Make sure the ledger exists in the open transaction, creating it from the migration file if needed.

    Args:
        connection: The owner's connection.
    """
    exists = connection.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL").fetchone()
    if exists is not None and not exists[0]:
        connection.execute(LEDGER_FILE.read_text(encoding="utf-8"))


def test_the_ledger_holds_a_row_for_every_migration_up_to_the_ledger(
    owner_connection: Connection,
) -> None:  # noqa: F811
    """Every file from 0001 to 0015 has a row, spelled as its file name."""
    with_ledger(owner_connection)
    rows = {row[0] for row in owner_connection.execute("SELECT version FROM schema_migrations").fetchall()}
    assert {path.stem for path in FILES if number(path) <= LEDGER_FIRST} <= rows


def test_the_ledger_refuses_a_repeated_or_badly_named_version(owner_connection: Connection) -> None:  # noqa: F811
    """A version is unique, and must look like ``NNNN_words``."""
    with_ledger(owner_connection)
    with pytest.raises(errors.UniqueViolation):
        owner_connection.execute("INSERT INTO schema_migrations (version) VALUES ('0001_booking')")
    owner_connection.rollback()
    with_ledger(owner_connection)
    for bad in ("1_x", "0016 spaces", "0016_UPPER", ""):
        with pytest.raises(errors.CheckViolation):
            owner_connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (bad,))
        owner_connection.rollback()
        with_ledger(owner_connection)


def test_the_ledger_is_closed_to_every_role_but_the_owner(owner_connection: Connection) -> None:  # noqa: F811
    """Row-level security is on, and the Data API roles and the limited API role hold no privilege on it."""
    with_ledger(owner_connection)
    secured = owner_connection.execute(
        "SELECT relrowsecurity FROM pg_class WHERE oid = 'public.schema_migrations'::regclass"
    ).fetchone()
    assert secured == (True,)
    for role in ("anon", "authenticated", ROLE):
        known = owner_connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if known is None:
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            allowed = owner_connection.execute(
                "SELECT has_table_privilege(%s, 'public.schema_migrations', %s)", (role, privilege)
            ).fetchone()
            assert allowed == (False,), f"{role} holds {privilege}"


def test_the_limited_api_role_cannot_read_the_ledger(owner_connection: Connection) -> None:  # noqa: F811
    """Switched to the limited role, a read of the ledger is refused."""
    with_ledger(owner_connection)
    as_limited(owner_connection)
    with pytest.raises(errors.InsufficientPrivilege):
        owner_connection.execute("SELECT version FROM schema_migrations").fetchall()
