"""Checks the migration runner's rules that need no database: file discovery, transaction control and the ledger.

The runner itself is ``scripts/migrate.py``. Its behaviour against a real database is in
``test_migrate_postgres.py``.
"""

from pathlib import Path

import pytest

from scripts.migrate import (
    URL_ENV,
    Migration,
    MigrationError,
    controls_transaction,
    discover,
    main,
    pending,
)

ROOT = Path(__file__).resolve().parent.parent


def write(directory: Path, name: str, sql: str = "SELECT 1;") -> None:
    """Create a migration file.

    Args:
        directory: Where to put it.
        name: The file name.
        sql: Its text.
    """
    (directory / name).write_text(sql, encoding="utf-8")


def migrations(*versions: str) -> list[Migration]:
    """Build in-memory migrations for the ledger checks.

    Args:
        versions: Versions such as ``0001_a``, in order.

    Returns:
        The migrations. Their paths are not read.
    """
    return [Migration(int(version[:4]), version, Path(f"{version}.sql")) for version in versions]


def test_the_repository_files_are_found_in_order_and_none_controls_its_own_transaction() -> None:
    """Every file in ``migrations/`` is accepted by the runner: named, numbered and free of BEGIN or COMMIT."""
    found = discover(ROOT / "migrations")
    assert [migration.number for migration in found] == list(range(1, len(found) + 1))
    assert found[14].version == "0015_migration_ledger"


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN;\nCREATE TABLE t (id int);\nCOMMIT;",
        "CREATE TABLE t (id int);\ncommit;",
        "START TRANSACTION;\nSELECT 1;",
        "SELECT 1;\n  ROLLBACK;",
        "SAVEPOINT s;",
        "SELECT 1;\nEND;",
        "/* a -- b */ COMMIT;",
        "-- a /* b\nCOMMIT;",
    ],
)
def test_a_top_level_transaction_statement_is_found(sql: str) -> None:
    """BEGIN, START TRANSACTION, COMMIT, END, ROLLBACK and SAVEPOINT outside a block are reported."""
    assert controls_transaction(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DO $$\nBEGIN\n    CREATE TABLE t (id int);\nEND\n$$;",
        "DO $body$\nBEGIN\n    IF true THEN\n        SELECT 1;\n    END IF;\nEND;\n$body$;",
        "-- BEGIN;\nSELECT 1;",
        "/* COMMIT; */\nSELECT 1;",
        "CREATE TABLE commits (ended_at timestamptz);",
        "COMMENT ON TABLE t IS 'note:\nCOMMIT;';",
    ],
)
def test_transaction_words_inside_blocks_comments_or_names_are_ignored(sql: str) -> None:
    """A ``DO`` block's own BEGIN and END, comments and identifiers that start with the words are not reported."""
    assert not controls_transaction(sql)


def test_a_badly_named_file_is_refused(tmp_path: Path) -> None:
    """A file not named ``NNNN_words.sql`` stops discovery."""
    write(tmp_path, "0001_ok.sql")
    write(tmp_path, "2_bad.sql")
    with pytest.raises(MigrationError, match="2_bad.sql"):
        discover(tmp_path)


def test_a_gap_in_the_numbers_is_refused(tmp_path: Path) -> None:
    """Files must run from 0001 with nothing missing."""
    write(tmp_path, "0001_a.sql")
    write(tmp_path, "0003_c.sql")
    with pytest.raises(MigrationError, match="numbered"):
        discover(tmp_path)


def test_a_file_with_its_own_transaction_is_refused(tmp_path: Path) -> None:
    """The runner owns the transaction, so a file that commits is refused before anything runs."""
    write(tmp_path, "0001_a.sql", "BEGIN;\nSELECT 1;\nCOMMIT;")
    with pytest.raises(MigrationError, match="0001_a.sql"):
        discover(tmp_path)


def test_pending_files_follow_the_applied_ones() -> None:
    """Files after the last recorded one are pending, in order."""
    files = migrations("0001_a", "0002_b", "0003_c")
    assert [migration.version for migration in pending(files, {"0001_a"})] == ["0002_b", "0003_c"]
    assert pending(files, {"0001_a", "0002_b", "0003_c"}) == []


def test_a_ledger_version_the_repository_does_not_have_is_refused() -> None:
    """A database ahead of the repository, or with a renamed file, is not touched."""
    with pytest.raises(MigrationError, match="0004_d"):
        pending(migrations("0001_a", "0002_b"), {"0001_a", "0004_d"})


def test_a_ledger_with_a_gap_is_refused() -> None:
    """Recorded files must be an unbroken run from 0001."""
    with pytest.raises(MigrationError, match="unbroken"):
        pending(migrations("0001_a", "0002_b", "0003_c"), {"0001_a", "0003_c"})


def test_the_runner_stops_without_a_connection_string(capsys: pytest.CaptureFixture[str]) -> None:
    """With no ``MIGRATION_DATABASE_URL`` the command exits 1 and says why."""
    assert main([], {}) == 1
    assert URL_ENV in capsys.readouterr().err


def test_an_unencrypted_connection_is_refused_without_printing_it(capsys: pytest.CaptureFixture[str]) -> None:
    """The same encryption rules as the API apply, and the connection string never appears in the output."""
    secret = "postgresql://owner:not-a-real-password@db.example.com/postgres?sslmode=disable"
    assert main([], {URL_ENV: secret}) == 1
    output = capsys.readouterr()
    assert URL_ENV in output.err
    assert "not-a-real-password" not in output.err + output.out
