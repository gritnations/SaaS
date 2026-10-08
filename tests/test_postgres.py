"""Tests for the database boundary. No real PostgreSQL server is used.

What these tests establish: configuration is validated, encryption is required, nothing connects before
first use, and an unreachable database fails safely without leaking a credential. What they do not
establish: a successful connection, a successful query, or certificate verification against a real server.
"""

import logging

import pytest

from postgres import Database, DatabaseConfigError, DatabaseSettings, DatabaseUnavailableError

SECRET = "s3cr3t-pw"
USER = "db_user_x"
UNREACHABLE = f"postgresql://{USER}:{SECRET}@127.0.0.1:1/appdb"
"""A connection string that points at a closed local port, so that every attempt is refused at once."""

UNKNOWN_HOST = f"postgresql://{USER}:{SECRET}@no-such-host.invalid:5432/appdb"
"""A connection string whose host can never resolve. The ``.invalid`` domain is reserved for this."""


def test_missing_configuration_fails_clearly() -> None:
    """With no connection string the error names the variable."""
    with pytest.raises(DatabaseConfigError) as caught:
        DatabaseSettings.from_env({})
    assert "DATABASE_URL" in str(caught.value)


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_an_unencrypted_mode_is_refused_without_leaking_the_string(mode: str) -> None:
    """A mode that permits an unencrypted connection is refused, and the error holds no credential."""
    with pytest.raises(DatabaseConfigError) as caught:
        DatabaseSettings.from_env({"DATABASE_URL": f"{UNREACHABLE}?sslmode={mode}"})
    assert SECRET not in str(caught.value)
    assert USER not in str(caught.value)


def test_encryption_is_required_when_no_mode_is_given() -> None:
    """A connection string with no ``sslmode`` is given ``require``."""
    settings = DatabaseSettings.from_env({"DATABASE_URL": UNREACHABLE})
    assert "sslmode=require" in settings.conninfo


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_a_secure_mode_is_kept(mode: str) -> None:
    """A secure mode chosen by the operator is preserved."""
    settings = DatabaseSettings.from_env({"DATABASE_URL": f"{UNREACHABLE}?sslmode={mode}"})
    assert f"sslmode={mode}" in settings.conninfo


def test_a_string_without_a_host_is_refused() -> None:
    """A connection string that names no host is refused, because it would not use TLS."""
    with pytest.raises(DatabaseConfigError):
        DatabaseSettings.from_env({"DATABASE_URL": "dbname=appdb user=someone"})


def test_an_unparseable_string_is_refused_without_leaking_it() -> None:
    """A malformed connection string is refused, and the error does not repeat it."""
    with pytest.raises(DatabaseConfigError) as caught:
        DatabaseSettings.from_env({"DATABASE_URL": f"{USER} {SECRET}"})
    assert SECRET not in str(caught.value)
    assert USER not in str(caught.value)


@pytest.mark.parametrize("size", ["0", "21", "many", "-1", "1.5", "²", "٢"])
def test_an_invalid_pool_size_is_refused(size: str) -> None:
    """A pool size outside 1 to 20, or not a whole number, is refused."""
    with pytest.raises(DatabaseConfigError):
        DatabaseSettings.from_env({"DATABASE_URL": UNREACHABLE, "DATABASE_POOL_MAX_SIZE": size})


def test_the_pool_size_is_read() -> None:
    """A valid pool size is used, and the default is 4."""
    assert DatabaseSettings.from_env({"DATABASE_URL": UNREACHABLE, "DATABASE_POOL_MAX_SIZE": "7"}).pool_max_size == 7
    assert DatabaseSettings.from_env({"DATABASE_URL": UNREACHABLE}).pool_max_size == 4


def test_settings_do_not_show_the_secret_when_printed() -> None:
    """The printed form of the settings leaves out the connection string."""
    settings = DatabaseSettings.from_env({"DATABASE_URL": UNREACHABLE})
    assert SECRET not in repr(settings)
    assert USER not in repr(settings)


def test_creating_the_database_opens_nothing() -> None:
    """Constructing the boundary does not open the pool. It would fail here if it tried to connect."""
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": UNREACHABLE}))
    assert database._pool.closed is True
    database.close()


@pytest.mark.parametrize("url", [UNREACHABLE, UNKNOWN_HOST])
def test_an_unreachable_database_fails_safely_and_leaks_nothing(url: str, caplog: pytest.LogCaptureFixture) -> None:
    """An unreachable database is unhealthy, raises a fixed error, and no log or error holds a credential.

    The unknown-host case matters: without redaction the connection pool logs the host name.
    """
    caplog.set_level(logging.DEBUG)
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": url}), timeout_seconds=1.0)
    try:
        assert database.is_healthy() is False
        with pytest.raises(DatabaseUnavailableError) as caught:
            with database.connection():
                pass
    finally:
        database.close()
    assert str(caught.value) == "The database is unavailable."
    assert caught.value.__cause__ is None
    logged = "\n".join(record.getMessage() for record in caplog.records) + caplog.text
    assert "database pool event (details withheld)" in logged
    for forbidden in (SECRET, USER, "127.0.0.1", "no-such-host", "appdb"):
        assert forbidden not in logged
        assert forbidden not in str(caught.value)


def test_a_closed_database_is_unavailable() -> None:
    """After ``close`` the boundary refuses to hand out a connection and reports unhealthy."""
    database = Database(DatabaseSettings.from_env({"DATABASE_URL": UNREACHABLE}), timeout_seconds=0.5)
    database.close()
    database.close()
    assert database.is_healthy() is False
    with pytest.raises(DatabaseUnavailableError):
        with database.connection():
            pass
