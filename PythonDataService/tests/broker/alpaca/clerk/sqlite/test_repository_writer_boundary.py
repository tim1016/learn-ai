"""Static enforcement for SQLite Clerk repository writers outside the package."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.repository_boundary import (
    EXTERNAL_REPOSITORY_WRITER_CENSUS,
    FACADE_WORKFLOW_HELPERS,
    FACADE_WORKFLOW_METHODS,
    REPOSITORY_MUTATION_HELPERS,
    REPOSITORY_MUTATION_METHODS,
)

_PYTHON_DATA_SERVICE_ROOT = Path(__file__).parents[5]
_PRODUCTION_ROOT = _PYTHON_DATA_SERVICE_ROOT / "app"
_SQLITE_PACKAGE = _PRODUCTION_ROOT / "broker/alpaca/clerk/sqlite"


@dataclass(frozen=True, order=True)
class _WriterCallSite:
    path: str
    owner: str
    call: str


class _WriterCallVisitor(ast.NodeVisitor):
    def __init__(self, *, source_root: Path) -> None:
        self._source_root = source_root
        self._scope: list[str] = []
        self.calls: set[_WriterCallSite] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        call = _writer_call_name(node)
        if call is not None:
            self.calls.add(
                _WriterCallSite(
                    path=self._source_root.as_posix(),
                    owner=".".join(self._scope),
                    call=call,
                )
            )
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()


def _writer_call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        if node.func.attr in REPOSITORY_MUTATION_METHODS and _is_repository(node.func.value):
            return node.func.attr
        if node.func.attr in FACADE_WORKFLOW_METHODS and _is_facade(node.func.value):
            return node.func.attr
        if node.func.attr == "append" and isinstance(node.func.value, ast.Name) and node.func.value.id == "receipts":
            return "append_decision_receipt"
        return None
    if (
        isinstance(node.func, ast.Name)
        and node.func.id in REPOSITORY_MUTATION_HELPERS
        and any(_is_repository(argument) for argument in node.args)
    ):
        return node.func.id
    if (
        isinstance(node.func, ast.Name)
        and node.func.id in FACADE_WORKFLOW_HELPERS
        and any(_is_facade(argument) for argument in node.args)
    ):
        return node.func.id
    return None


def _is_repository(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {"repo", "repository"}
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id in {"facade", "clerk"}:
            return node.attr == "repository"
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            return node.attr == "_repo"
    return False


def _is_facade(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "facade"
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == "_reconciler"
    )


def _writer_calls(root: Path) -> set[_WriterCallSite]:
    calls: set[_WriterCallSite] = set()
    for path in root.rglob("*.py"):
        if _SQLITE_PACKAGE in path.parents:
            continue
        visitor = _WriterCallVisitor(source_root=path.relative_to(_PYTHON_DATA_SERVICE_ROOT))
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        calls.update(visitor.calls)
    return calls


def _assert_census(
    observed: set[_WriterCallSite],
    expected: set[_WriterCallSite],
) -> None:
    assert observed == expected, (
        "External SQLite repository writers require an explicit census entry. "
        f"Unclassified: {sorted(observed - expected)!r}; stale: {sorted(expected - observed)!r}"
    )


def test_external_repository_writers_match_the_explicit_census() -> None:
    expected = {
        _WriterCallSite(entry.path, entry.owner, entry.call)
        for entry in EXTERNAL_REPOSITORY_WRITER_CENSUS
    }

    _assert_census(_writer_calls(_PRODUCTION_ROOT), expected)


def test_unknown_external_repository_mutator_fails_until_classified(tmp_path: Path) -> None:
    fixture = tmp_path / "unknown_writer.py"
    fixture.write_text(
        "def unsafe_writer(repo):\n    repo.append_transition(None)\n",
        encoding="utf-8",
    )
    visitor = _WriterCallVisitor(source_root=Path("fixture/unknown_writer.py"))
    visitor.visit(ast.parse(fixture.read_text(encoding="utf-8"), filename=str(fixture)))

    with pytest.raises(AssertionError, match="Unclassified"):
        _assert_census(visitor.calls, set())
