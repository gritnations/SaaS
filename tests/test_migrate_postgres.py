"""Tests for the migration runner against a real PostgreSQL server.

Skipped unless ``BOOKING_TEST_DATABASE_URL`` is set. Each test creates a new, empty database on that server, runs
the runner against it, and drops it afterwards; the database the URL names is not changed. The account needs
permission to create databases; if it has none, the tests are skipped.

What these tests establish: an empty database gets every file and a full ledger; a second run applies nothing; a
file that fails, or that does not record itself, leaves nothing behind and stops the run; a database with tables but
no ledger is refused; ``--status`` changes nothing. They do not establish how the runner behaves on the hosted
database, or with two runs at the same time.
"""

import io
import os
import secrets
import shutil
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_manage_postgres import URL_ENV

from postgres import DatabaseSettings
from scripts.migrate import DEFAULT_DIRECTORY, MigrationError, run

pytestmark = pytest.mark.skipif(not os.environ.get(URL_ENV), reason=f"{URL_ENV} is not set")

Connection = psycopg.Connection[tuple[object, ...]]
FILE_COUNT = len(list(DEFAULT_DIRECTORY.glob("*.sql")))


@pytest.fixture
def scratch() -> Iterator[Connection]:
    """Create an empty database, yield an autocommit owner connection to it, then drop it.

    Yields:
        The connection to the new database.
    """
    conninfo = DatabaseSettings.from_env({"DATABASE_URL": os.environ[URL_ENV]}).conninfo
    name = "migrate_test_" + secrets.token_hex(6)
    with psycopg.connect(conninfo, autocommit=True) as admin:
        try:
            admin.execute(f'CREATE DATABASE "{name}"')
        except errors.InsufficientPrivilege:
            pytest.skip("The test account cannot create databases.")
    params = conninfo_to_dict(conninfo)
    params["dbname"] = name
    try:
        connection: Connection = psycopg.connect(make_conninfo("", **params), autocommit=True)
        try:
            yield connection
        finally:
            connection.close()
    finally:
        with psycopg.connect(conninfo, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def ledger(connection: Connection) -> list[str]:
    """Read the ledger in order.

    Args:
        connection: A connection to a database that has the ledger.

    Returns:
        The recorded versions.
    """
    return [str(row[0]) for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]


def table_exists(connection: Connection, name: str) -> bool:
    """Tell whether a table is in the public schema.

    Args:
        connection: An open connection.
        name: The table name.

    Returns:
        True if it exists.
    """
    row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",)).fetchone()
    return bool(row and row[0])


def with_extra(tmp_path: Path, name: str, sql: str) -> Path:
    """Copy the repository's migrations and add one more file.

    Args:
        tmp_path: A temporary folder.
        name: The extra file's name.
        sql: Its text.

    Returns:
        The folder holding the copy.
    """
    folder = tmp_path / "migrations"
    shutil.copytree(DEFAULT_DIRECTORY, folder)
    (folder / name).write_text(sql, encoding="utf-8")
    return folder


def test_an_empty_database_gets_every_file_and_a_second_run_applies_nothing(scratch: Connection) -> None:
    """All files are applied and recorded; running again changes nothing."""
    out = io.StringIO()
    assert run(scratch, DEFAULT_DIRECTORY, out=out) == FILE_COUNT
    assert ledger(scratch) == sorted(path.stem for path in DEFAULT_DIRECTORY.glob("*.sql"))
    assert table_exists(scratch, "bookings")
    again = io.StringIO()
    assert run(scratch, DEFAULT_DIRECTORY, out=again) == 0
    assert again.getvalue() == "Nothing to apply.\n"


def test_a_failing_file_leaves_nothing_behind(scratch: Connection, tmp_path: Path) -> None:
    """A statement error rolls back the whole file, including what ran before the error, and stops the run."""
    run(scratch, DEFAULT_DIRECTORY, out=io.StringIO())
    folder = with_extra(
        tmp_path,
        f"{FILE_COUNT + 1:04d}_broken.sql",
        "CREATE TABLE runner_probe (id int);\nSELECT 1 / 0;\n"
        f"INSERT INTO schema_migrations (version) VALUES ('{FILE_COUNT + 1:04d}_broken');\n",
    )
    with pytest.raises(MigrationError, match="rolled back"):
        run(scratch, folder, out=io.StringIO())
    assert not table_exists(scratch, "runner_probe")
    assert len(ledger(scratch)) == FILE_COUNT


def test_a_file_that_does_not_record_itself_is_rolled_back(scratch: Connection, tmp_path: Path) -> None:
    """Without its own ledger row a file is undone, so the ledger cannot fall behind the schema."""
    run(scratch, DEFAULT_DIRECTORY, out=io.StringIO())
    folder = with_extra(tmp_path, f"{FILE_COUNT + 1:04d}_silent.sql", "CREATE TABLE runner_probe (id int);\n")
    with pytest.raises(MigrationError, match="did not record itself"):
        run(scratch, folder, out=io.StringIO())
    assert not table_exists(scratch, "runner_probe")


def test_a_failure_in_the_first_fifteen_leaves_an_empty_database(scratch: Connection, tmp_path: Path) -> None:
    """0001 to 0015 run as one transaction, so an error in them leaves no tables and no ledger."""
    folder = tmp_path / "migrations"
    shutil.copytree(DEFAULT_DIRECTORY, folder)
    ledger_file = folder / "0015_migration_ledger.sql"
    ledger_file.write_text(ledger_file.read_text(encoding="utf-8") + "\nSELECT 1 / 0;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="rolled back"):
        run(scratch, folder, out=io.StringIO())
    assert not table_exists(scratch, "bookings")
    assert not table_exists(scratch, "schema_migrations")


def test_a_database_with_tables_but_no_ledger_is_refused(scratch: Connection) -> None:
    """An unknown state is not guessed at: nothing runs."""
    scratch.execute("CREATE TABLE something_else (id int)")
    with pytest.raises(MigrationError, match="no schema_migrations ledger"):
        run(scratch, DEFAULT_DIRECTORY, out=io.StringIO())
    assert not table_exists(scratch, "bookings")


def test_status_lists_pending_files_and_changes_nothing(scratch: Connection) -> None:
    """``--status`` on an empty database lists every file and creates nothing."""
    out = io.StringIO()
    assert run(scratch, DEFAULT_DIRECTORY, status_only=True, out=out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0] == f"Applied: 0 of {FILE_COUNT}."
    assert len(lines) == FILE_COUNT + 1
    assert not table_exists(scratch, "schema_migrations")
