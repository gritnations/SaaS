"""Database boundary for the platform.

This module is the only place that knows how to reach PostgreSQL. It reads its configuration from the
environment, refuses connections that are not encrypted, and hands out typed connections from a pool.

It holds no domain schema and no booking logic. The only SQL here is the health probe.

Trust path: ``main.py -> api/main.py -> bounded service -> postgres.py -> PostgreSQL``.

Dependencies:
    psycopg (PostgreSQL driver) and psycopg_pool (connection pool). Both work with any PostgreSQL server,
    including a Supabase-hosted one. The Supabase client library is deliberately not used.

Constraints:
    * Importing this module, and constructing :class:`Database`, perform no network I/O.
    * No credential may appear in an exception, a log record or a return value.
"""

import logging
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeAlias

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

LOGGER = logging.getLogger(__name__)

DATABASE_URL_ENV = "DATABASE_URL"
"""Environment variable holding the PostgreSQL connection string. It is a secret."""

POOL_MAX_SIZE_ENV = "DATABASE_POOL_MAX_SIZE"
"""Environment variable for the largest number of pooled connections. Optional."""

SECURE_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
"""The ``sslmode`` values that guarantee an encrypted connection."""

DEFAULT_SSL_MODE = "require"
"""Applied when the connection string names no ``sslmode``."""

DEFAULT_POOL_MAX_SIZE = 4
MAX_POOL_MAX_SIZE = 20
CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 3.0
"""How long to wait for a pooled connection before the database is treated as unavailable."""

PgConnection: TypeAlias = psycopg.Connection[tuple[Any, ...]]
"""The typed connection handed to callers. Rows are plain tuples."""


class DatabaseConfigError(RuntimeError):
    """Raised when the database configuration is absent or unsafe.

    The message names the environment variable and the problem. It never contains the value.
    """


class DatabaseUnavailableError(RuntimeError):
    """Raised when a connection cannot be obtained.

    The message is fixed. The driver's own error text is deliberately dropped, because it can name
    the host and the database user.
    """


class _PoolLogRedactor(logging.Filter):
    """Removes driver detail from the connection pool's own log records.

    The pool logs failed connection attempts with the driver's error text, which can name the host
    and the database user. This filter keeps the level and replaces the text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Replace the record's message with fixed text.

        Args:
            record: The log record produced by ``psycopg.pool``.

        Returns:
            Always ``True``. The record is kept, with its detail removed.
        """
        record.msg = "database pool event (details withheld)"
        record.args = None
        record.exc_info = None
        record.exc_text = None
        return True


def _install_log_redaction() -> None:
    """Attach :class:`_PoolLogRedactor` to the ``psycopg.pool`` logger once.

    Calling it again has no further effect.
    """
    pool_logger = logging.getLogger("psycopg.pool")
    if not any(isinstance(existing, _PoolLogRedactor) for existing in pool_logger.filters):
        pool_logger.addFilter(_PoolLogRedactor())


@dataclass(frozen=True)
class DatabaseSettings:
    """Validated database configuration.

    Attributes:
        conninfo: The PostgreSQL connection string, with a secure ``sslmode`` enforced. It is a secret,
            so it is left out of ``repr()``.
        pool_max_size: The largest number of pooled connections, from 1 to 20.
    """

    conninfo: str = field(repr=False)
    pool_max_size: int = DEFAULT_POOL_MAX_SIZE

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DatabaseSettings":
        """Build settings from environment variables.

        Reads ``DATABASE_URL`` (required) and ``DATABASE_POOL_MAX_SIZE`` (optional). A connection string
        with no ``sslmode`` gets ``require``. A weaker mode is refused, as is a string with no host.

        Args:
            environ: The mapping to read. ``None`` means the process environment.

        Returns:
            The validated settings.

        Raises:
            DatabaseConfigError: If ``DATABASE_URL`` is missing or cannot be parsed, names no host, asks
                for an unencrypted connection, or if the pool size is not a whole number from 1 to 20.
        """
        env = os.environ if environ is None else environ
        raw = env.get(DATABASE_URL_ENV, "").strip()
        if not raw:
            raise DatabaseConfigError(
                f"{DATABASE_URL_ENV} is not set. The database boundary cannot be used without it."
            )
        try:
            params = dict(conninfo_to_dict(raw))
        except psycopg.Error:
            raise DatabaseConfigError(f"{DATABASE_URL_ENV} is not a valid PostgreSQL connection string.") from None
        if not params.get("host") and not params.get("hostaddr"):
            raise DatabaseConfigError(f"{DATABASE_URL_ENV} must name a host, so that the connection is encrypted.")
        sslmode = str(params.get("sslmode") or DEFAULT_SSL_MODE)
        if sslmode not in SECURE_SSL_MODES:
            raise DatabaseConfigError(
                f"{DATABASE_URL_ENV} must use sslmode require, verify-ca or verify-full. An unencrypted mode is refused."
            )
        params["sslmode"] = sslmode
        params.setdefault("connect_timeout", CONNECT_TIMEOUT_SECONDS)

        raw_size = env.get(POOL_MAX_SIZE_ENV, "").strip()
        if not raw_size:
            pool_max_size = DEFAULT_POOL_MAX_SIZE
        elif raw_size.isdigit() and 1 <= int(raw_size) <= MAX_POOL_MAX_SIZE:
            pool_max_size = int(raw_size)
        else:
            raise DatabaseConfigError(f"{POOL_MAX_SIZE_ENV} must be a whole number from 1 to {MAX_POOL_MAX_SIZE}.")
        return cls(conninfo=make_conninfo("", **params), pool_max_size=pool_max_size)


class Database:
    """Typed access to PostgreSQL through a connection pool.

    The pool starts empty and opens connections on demand, so the same code suits a short-lived
    serverless process and a long-running server. Server-side prepared statements are turned off so
    that it also works behind a transaction-mode pooler.

    Constructing an instance performs no I/O. The first connection is attempted on first use.
    """

    def __init__(self, settings: DatabaseSettings, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        """Create the pool without opening it.

        Args:
            settings: Validated configuration from :meth:`DatabaseSettings.from_env`.
            timeout_seconds: How long to wait for a connection before giving up.
        """
        _install_log_redaction()
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._closed = False
        self._pool: ConnectionPool[PgConnection] = ConnectionPool(
            conninfo=settings.conninfo,
            min_size=0,
            max_size=settings.pool_max_size,
            open=False,
            timeout=timeout_seconds,
            kwargs={"prepare_threshold": None},
            check=ConnectionPool.check_connection,
            name="platform-db",
        )

    def _ensure_open(self) -> None:
        """Open the pool on first use.

        Raises:
            DatabaseUnavailableError: If :meth:`close` has already been called.
        """
        with self._lock:
            if self._closed:
                raise DatabaseUnavailableError("The database is unavailable.")
            if self._pool.closed:
                self._pool.open()

    @contextmanager
    def connection(self) -> Iterator[PgConnection]:
        """Borrow a connection for the length of a ``with`` block.

        The transaction commits when the block ends normally and rolls back if it raises. The
        connection then returns to the pool.

        Yields:
            A typed psycopg connection.

        Raises:
            DatabaseUnavailableError: If no connection can be obtained in time. Errors raised by the
                caller's own statements inside the block are not converted. They propagate unchanged.
        """
        with ExitStack() as stack:
            try:
                self._ensure_open()
                connection = stack.enter_context(self._pool.connection(timeout=self._timeout))
            except (psycopg.Error, PoolTimeout, PoolClosed, OSError) as exc:
                LOGGER.warning("database unavailable (%s)", type(exc).__name__)
                raise DatabaseUnavailableError("The database is unavailable.") from None
            yield connection

    def is_healthy(self) -> bool:
        """Report whether the database answers a trivial query.

        Runs ``SELECT 1``. It never raises, and it reveals nothing about why a check failed.

        Returns:
            ``True`` if the database answered correctly, otherwise ``False``.
        """
        try:
            with self.connection() as connection:
                row = connection.execute("SELECT 1").fetchone()
        except (DatabaseUnavailableError, psycopg.Error) as exc:
            LOGGER.warning("database health check failed (%s)", type(exc).__name__)
            return False
        return row == (1,)

    def close(self) -> None:
        """Close the pool and release its connections.

        Safe to call more than once. After this, :meth:`connection` raises
        :class:`DatabaseUnavailableError`.
        """
        with self._lock:
            self._closed = True
            self._pool.close()
