"""Checks the migration files themselves: their numbering, and that each one records itself in the ledger.

These tests read the files in ``migrations/`` and need no database. The ledger table is created by migration
0015; from that file on, every migration must end by inserting its own version into ``schema_migrations``.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted((ROOT / "migrations").glob("*.sql"))
"""Every migration file, in order."""

LEDGER_FIRST = 15
"""The number of the migration that creates the ledger. Earlier files cannot record themselves."""


def number(path: Path) -> int:
    """Read the leading number of a migration file name.

    Args:
        path: A migration file.

    Returns:
        The number, for example 15 for ``0015_migration_ledger.sql``.
    """
    return int(path.name[:4])


def test_migrations_are_numbered_without_gaps_or_repeats() -> None:
    """The files are named ``NNNN_words.sql`` and run from 0001 with no number missing or used twice."""
    for path in FILES:
        assert re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", path.name), path.name
    assert [number(path) for path in FILES] == list(range(1, len(FILES) + 1))


def test_the_ledger_migration_lists_every_earlier_file() -> None:
    """Migration 0015 writes a row for each of 0001 to 0015, spelled as the file names are."""
    ledger = next(path for path in FILES if number(path) == LEDGER_FIRST)
    listed = re.findall(r"\('([0-9]{4}_[a-z0-9_]+)'\)", ledger.read_text(encoding="utf-8"))
    assert listed == [path.stem for path in FILES if number(path) <= LEDGER_FIRST]


def test_every_later_migration_records_itself() -> None:
    """From 0016 on, each file inserts its own version into ``schema_migrations``, so the ledger cannot fall behind."""
    for path in FILES:
        if number(path) <= LEDGER_FIRST:
            continue
        text = path.read_text(encoding="utf-8")
        assert re.search(
            rf"INSERT\s+INTO\s+schema_migrations\s*\(version\)\s*VALUES\s*\(\s*'{re.escape(path.stem)}'\s*\)", text
        ), f"{path.name} does not record itself in schema_migrations"
