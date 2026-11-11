"""Checks that no log call in the platform can write a secret, a credential or a raw provider payload.

The scan reads the source of every platform module and inspects the arguments of each ``LOGGER`` call.
It is a static check: it proves what the code is written to log, not what a running process emits.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCES = [ROOT / "main.py", ROOT / "mail.py", ROOT / "postgres.py"] + sorted(
    path for folder in ("api", "booking", "payments") for path in (ROOT / folder).glob("*.py")
)
"""Every platform file that may log. The tests are not scanned."""

FORBIDDEN = re.compile(
    r"token|secret|password|passwd|api_?key|conninfo|database_url|dsn|signature|authorization|credential"
    r"|payload|raw_?body|headers|manage_code|\burl\b|_url$",
    re.IGNORECASE,
)
"""Argument names that must never be logged."""

REVIEWED_EXCEPTION_TEXT = {
    ("api/main.py", "database is not configured: %s"),
    ("booking/notify.py", "email to %s failed: %s"),
}
"""Calls that log an exception's own text. Each was read and its message is fixed text that holds no secret:
``DatabaseConfigError`` names only an environment variable, and ``MailError`` names only an HTTP status or an
exception class. Any new call of this kind fails the test until it is reviewed and added here."""


def _names(node: ast.AST) -> list[str]:
    """List every variable, attribute and keyword name used inside an expression.

    ``request.url.path`` is read as ``path``: the path holds only a business slug and a booking reference
    on every route (no route takes a credential in its path), and it leaves out the query string.

    Args:
        node: The expression to walk.

    Returns:
        The names found, in the order met.
    """
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "url"
    ):
        return ["path"]
    found: list[str] = []
    if isinstance(node, ast.Name):
        found.append(node.id)
    elif isinstance(node, ast.Attribute):
        found.append(node.attr)
    for child in ast.iter_child_nodes(node):
        found.extend(_names(child))
    return found


def problems(path: Path, source: str | None = None) -> list[str]:
    """Find the log calls in one file that could write a secret.

    Args:
        path: The file, used for reporting and for the reviewed list.
        source: The code to scan instead of reading ``path``. Used to test the scanner itself.

    Returns:
        One entry per risky call, as ``file:line: reason``.
    """
    relative = path.relative_to(ROOT).as_posix() if path.is_absolute() and ROOT in path.parents else path.name
    tree = ast.parse(source if source is not None else path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ("LOGGER", "logging")
            and node.func.attr in ("debug", "info", "warning", "error", "exception", "critical", "log")
        ):
            continue
        where = f"{relative}:{node.lineno}"
        message = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
        for argument in node.args[1:]:
            for name in _names(argument):
                if FORBIDDEN.search(name):
                    found.append(f"{where}: logs '{name}'")
            raw_exception = isinstance(argument, ast.Name) and argument.id in ("exc", "error", "err", "e")
            if raw_exception and (relative, message) not in REVIEWED_EXCEPTION_TEXT:
                found.append(f"{where}: logs an exception's text that has not been reviewed")
        if node.func.attr == "exception":
            found.append(f"{where}: LOGGER.exception writes a traceback")
        if any(keyword.arg == "exc_info" for keyword in node.keywords):
            found.append(f"{where}: exc_info writes a traceback")
        if isinstance(node.args[0] if node.args else None, ast.JoinedStr):
            found.append(f"{where}: logs an f-string, so the arguments are not inspected")
    return found


def test_no_platform_log_call_can_write_a_secret() -> None:
    """No log call names a secret-like value, a traceback, an unreviewed exception text or an f-string."""
    found = [line for path in SOURCES for line in problems(path)]
    assert found == []


def test_the_scanner_catches_what_it_is_meant_to_catch() -> None:
    """The scanner flags each kind of risky call, so a passing scan means something."""
    bad = (
        "import logging\n"
        "LOGGER = logging.getLogger(__name__)\n"
        "def f(manage_token, exc, secret_key):\n"
        "    LOGGER.info('a %s', manage_token)\n"
        "    LOGGER.error('b %s', exc)\n"
        "    LOGGER.warning(f'c {secret_key}')\n"
        "    LOGGER.error('d', exc_info=True)\n"
        "    LOGGER.exception('e')\n"
        "    LOGGER.info('f %s', request.url)\n"
    )
    found = problems(ROOT / "bad.py", bad)
    assert len(found) == 6, found


def test_the_scanner_sees_the_real_log_calls() -> None:
    """The scan is not vacuous: it finds log calls in the platform files it covers."""
    count = 0
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count += sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "LOGGER"
            for node in ast.walk(tree)
        )
    assert count >= 25
