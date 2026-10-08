"""Enforces the documentation standard: every module, class and function has a docstring."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
"""The repository root."""

SOURCES = [ROOT / "main.py", ROOT / "postgres.py"] + sorted(
    path for folder in ("api", "payments", "tests") for path in (ROOT / folder).glob("*.py")
)
"""Every Python file that belongs to the platform, including these tests."""


def undocumented(path: Path) -> list[str]:
    """List what lacks a docstring in one file.

    Args:
        path: The Python file to inspect.

    Returns:
        One entry per module, class or function without a docstring, as ``file:name``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing = [] if ast.get_docstring(tree) else [f"{path.name}:<module>"]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and not ast.get_docstring(node):
            missing.append(f"{path.name}:{node.name}")
    return missing


def test_every_module_class_and_function_is_documented() -> None:
    """No platform file may contain an undocumented module, class or function."""
    assert len(SOURCES) >= 12
    missing = [entry for path in SOURCES for entry in undocumented(path)]
    assert missing == []
