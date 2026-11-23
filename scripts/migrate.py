"""Apply the files in ``migrations/`` to a database, each one completely or not at all.

Run from the repository root, as the database owner::

    MIGRATION_DATABASE_URL=<owner connection string> python -m scripts.migrate            # apply what is pending
    MIGRATION_DATABASE_URL=<owner connection string> python -m scripts.migrate --status   # list only, change nothing

How it works:
    * The ledger table ``schema_migrations`` (created by migration 0015) says which files a database has had.
    * An empty database gets 0001 to 0015 in a single transaction. 0015 creates the ledger and records all
      fifteen, so either all of them are applied and recorded, or none.
    * After that, each pending file runs in its own transaction, in order. The file must record its own version
      in the ledger (a test checks this for every file from 0016 on). If it does not, or if any statement fails,
      the transaction is rolled back, nothing from that file is left behind, and the run stops.
    * A database that has tables but no ledger is refused: its state is unknown, so it is checked by hand first.
    * A ledger that names a file the repository does not have, or that skips one, is refused as well.
    * Each transaction first takes a database lock, so two runs at the same time cannot apply the same file.

Environment variables read:
    * ``MIGRATION_DATABASE_URL``: the database owner's connection string. A secret; it is never printed. The same
      rules as ``DATABASE_URL`` apply: a host is required, and the connection must be encrypted.

Constraints:
    * Migration files must not open or close their own transaction; the runner owns it. A file that does is refused
      before anything runs.
    * Nothing is applied with ``--status``.
"""

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import psycopg

from postgres import DatabaseConfigError, DatabaseSettings

URL_ENV = "MIGRATION_DATABASE_URL"
DEFAULT_DIRECTORY = Path(__file__).resolve().parent.parent / "migrations"
LEDGER_NUMBER = 15
"""The migration that creates the ledger. Files up to and including it are applied together on an empty database."""

LOCK_KEY = 4_729_015
"""Key of the transaction-level advisory lock taken before each transaction. Any fixed number; only this tool uses it."""

FILE_NAME = re.compile(r"([0-9]{4})_[a-z0-9_]+\.sql")
NOT_CODE = re.compile(
    r"\$([A-Za-z_][A-Za-z0-9_]*|)\$.*?\$\1\$"  # a dollar-quoted body, such as a DO block
    r"|/\*.*?\*/"  # a block comment
    r"|--[^\n]*"  # a line comment
    r"|'(?:[^']|'')*'",  # a string literal
    re.DOTALL,
)
"""Text that is not top-level SQL. One pattern scanned left to right, so whichever opens first is removed whole."""
TRANSACTION_CONTROL = re.compile(
    r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b[^;]*;", re.IGNORECASE | re.MULTILINE
)

Connection = psycopg.Connection[tuple[object, ...]]


class MigrationError(Exception):
    """The run was refused or stopped. The message says why, and never contains the connection string."""


@dataclass(frozen=True)
class Migration:
    """One migration file.

    Attributes:
        number: The leading number, for example 15.
        version: The file name without ``.sql``, as recorded in the ledger.
        path: Where the file is.
    """

    number: int
    version: str
    path: Path

    def sql(self) -> str:
        """Read the file.

        Returns:
            Its text.
        """
        return self.path.read_text(encoding="utf-8")


def controls_transaction(sql: str) -> bool:
    """Tell whether a file opens or closes a transaction itself, outside any ``DO`` block or function body.

    Args:
        sql: The text of a migration file.

    Returns:
        True if a top-level BEGIN, START TRANSACTION, COMMIT, END, ROLLBACK, SAVEPOINT or RELEASE is found.
    """
    return TRANSACTION_CONTROL.search(NOT_CODE.sub(" ", sql)) is not None


def discover(directory: Path) -> list[Migration]:
    """List the migration files in order, and check their names.

    Args:
        directory: The folder holding ``NNNN_words.sql`` files.

    Returns:
        The files, ordered by number.

    Raises:
        MigrationError: If a name is malformed, the numbers do not run from 0001 without a gap or repeat, or a file
            controls its own transaction.
    """
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = FILE_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"{path.name} is not named NNNN_words.sql.")
        migrations.append(Migration(int(match.group(1)), path.stem, path))
    if [migration.number for migration in migrations] != list(range(1, len(migrations) + 1)):
        raise MigrationError("The migration files must be numbered from 0001 with no number missing or repeated.")
    for migration in migrations:
        if controls_transaction(migration.sql()):
            raise MigrationError(f"{migration.path.name} opens or closes a transaction itself; the runner owns it.")
    return migrations


def ledger_exists(connection: Connection) -> bool:
    """Tell whether the ledger table is there.

    Args:
        connection: An open connection.

    Returns:
        True if ``public.schema_migrations`` exists.
    """
    row = connection.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL").fetchone()
    return bool(row and row[0])


def has_tables(connection: Connection) -> bool:
    """Tell whether the public schema holds any table.

    Args:
        connection: An open connection.

    Returns:
        True if at least one table exists in ``public``.
    """
    row = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public')",
    ).fetchone()
    return bool(row and row[0])


def applied_versions(connection: Connection) -> set[str]:
    """Read the ledger.

    Args:
        connection: An open connection to a database that has the ledger.

    Returns:
        Every recorded version.
    """
    return {str(row[0]) for row in connection.execute("SELECT version FROM schema_migrations").fetchall()}


def pending(migrations: Sequence[Migration], applied: set[str]) -> list[Migration]:
    """Work out which files still need to run, and check that the ledger agrees with the repository.

    Args:
        migrations: Every file, in order.
        applied: The versions in the ledger.

    Returns:
        The files not yet applied, in order.

    Raises:
        MigrationError: If the ledger names a version the repository does not have, or the applied files are not
            an unbroken run from 0001.
    """
    known = {migration.version for migration in migrations}
    unknown = sorted(applied - known)
    if unknown:
        raise MigrationError(f"The ledger records versions this repository does not have: {', '.join(unknown)}.")
    count = len(applied)
    if {migration.version for migration in migrations[:count]} != applied:
        raise MigrationError("The ledger does not record an unbroken run of files from 0001. Check it by hand.")
    return list(migrations[count:])


def recorded(connection: Connection, versions: Sequence[str]) -> bool:
    """Tell whether every given version is in the ledger, inside the current transaction.

    Args:
        connection: An open connection with a transaction in progress.
        versions: The versions to look for.

    Returns:
        True if all are recorded.
    """
    row = connection.execute(
        "SELECT count(*) FROM schema_migrations WHERE version = ANY(%s)", (list(versions),)
    ).fetchone()
    return row is not None and row[0] == len(set(versions))


def apply(connection: Connection, batch: Sequence[Migration]) -> None:
    """Run one or more files in a single transaction, and roll it back unless all of them are recorded.

    Args:
        connection: An open connection in autocommit mode.
        batch: The files to run together, in order.

    Raises:
        MigrationError: If a statement fails or a file does not record itself. Nothing from the batch remains.
    """
    names = ", ".join(migration.path.name for migration in batch)
    try:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            if ledger_exists(connection) and recorded(connection, [migration.version for migration in batch]):
                return
            for migration in batch:
                connection.execute(migration.sql())
            if not recorded(connection, [migration.version for migration in batch]):
                raise MigrationError(f"{names} did not record itself in schema_migrations. Rolled back.")
    except psycopg.Error as error:
        raise MigrationError(f"{names} failed and was rolled back: {type(error).__name__}: {error}") from None


def plan(connection: Connection, migrations: Sequence[Migration]) -> list[list[Migration]]:
    """Decide what a run would apply, as a list of transactions.

    Args:
        connection: An open connection.
        migrations: Every file, in order.

    Returns:
        One list of files per transaction, in order. Empty when nothing is pending.

    Raises:
        MigrationError: If the database has tables but no ledger, or the ledger disagrees with the repository.
    """
    if not ledger_exists(connection):
        if has_tables(connection):
            raise MigrationError(
                "The database has tables but no schema_migrations ledger, so its state is unknown. Check it by hand."
            )
        first = [migration for migration in migrations if migration.number <= LEDGER_NUMBER]
        rest = [migration for migration in migrations if migration.number > LEDGER_NUMBER]
        return ([first] if first else []) + [[migration] for migration in rest]
    return [[migration] for migration in pending(migrations, applied_versions(connection))]


def connect(environ: Mapping[str, str]) -> Connection:
    """Open an autocommit connection as the owner, with the same encryption rules as the API.

    Args:
        environ: The environment to read ``MIGRATION_DATABASE_URL`` from.

    Returns:
        The connection.

    Raises:
        MigrationError: If the variable is missing or invalid, or the database cannot be reached.
    """
    if not environ.get(URL_ENV, "").strip():
        raise MigrationError(f"{URL_ENV} is not set.")
    try:
        settings = DatabaseSettings.from_env({"DATABASE_URL": environ[URL_ENV]})
    except DatabaseConfigError as error:
        raise MigrationError(str(error).replace("DATABASE_URL", URL_ENV)) from None
    try:
        return psycopg.connect(settings.conninfo, autocommit=True)
    except psycopg.Error as error:
        raise MigrationError(f"Could not connect to the database: {type(error).__name__}.") from None


def run(connection: Connection, directory: Path, status_only: bool = False, out: TextIO | None = None) -> int:
    """Apply what is pending, or only report it.

    Args:
        connection: An open autocommit connection as the database owner.
        directory: The migrations folder.
        status_only: When True, list applied and pending files and change nothing.
        out: Where to write progress. Defaults to standard output.

    Returns:
        How many files were applied (0 with ``status_only``).

    Raises:
        MigrationError: If the run is refused or a file fails. Files applied before the failure stay applied.
    """
    write = (out or sys.stdout).write
    migrations = discover(directory)
    batches = plan(connection, migrations)
    waiting = [migration for batch in batches for migration in batch]
    if status_only:
        done = len(migrations) - len(waiting)
        write(f"Applied: {done} of {len(migrations)}.\n")
        for migration in waiting:
            write(f"Pending: {migration.path.name}\n")
        return 0
    if not waiting:
        write("Nothing to apply.\n")
        return 0
    for batch in batches:
        apply(connection, batch)
        for migration in batch:
            write(f"Applied: {migration.path.name}\n")
    return len(waiting)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: The arguments, without the program name. Defaults to ``sys.argv[1:]``.
        environ: The environment. Defaults to ``os.environ``.

    Returns:
        The exit code: 0 on success, 1 if the run was refused or a file failed.
    """
    parser = argparse.ArgumentParser(prog="python -m scripts.migrate", description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true", help="list applied and pending files; change nothing")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIRECTORY, help="migrations folder (default: migrations/)")
    arguments = parser.parse_args(argv)
    try:
        connection = connect(os.environ if environ is None else environ)
        try:
            run(connection, arguments.dir, status_only=arguments.status)
        finally:
            connection.close()
    except MigrationError as error:
        sys.stderr.write(f"Stopped: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
