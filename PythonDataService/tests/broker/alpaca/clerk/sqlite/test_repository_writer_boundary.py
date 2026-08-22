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
_CONVENTIONAL_REPOSITORY_NAMES = frozenset({"repo", "repository"})
_CONVENTIONAL_FACADE_NAMES = frozenset({"facade"})


@dataclass(frozen=True, order=True)
class _WriterCallSite:
    path: str
    owner: str
    call: str


class _WriterCallVisitor(ast.NodeVisitor):
    def __init__(self, *, source_root: Path) -> None:
        self._source_root = source_root
        self._scope: list[str] = []
        self._repository_aliases = set(_CONVENTIONAL_REPOSITORY_NAMES)
        self._facade_aliases = set(_CONVENTIONAL_FACADE_NAMES)
        self.calls: set[_WriterCallSite] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._record_aliases(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._record_aliases([node.target], node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        call = _writer_call_name(
            node,
            repository_aliases=self._repository_aliases,
            facade_aliases=self._facade_aliases,
        )
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
        repository_aliases = self._repository_aliases.copy()
        facade_aliases = self._facade_aliases.copy()
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()
        self._repository_aliases = repository_aliases
        self._facade_aliases = facade_aliases

    def _record_aliases(self, targets: list[ast.expr], value: ast.expr) -> None:
        is_repository = _is_repository(
            value,
            repository_aliases=self._repository_aliases,
            facade_aliases=self._facade_aliases,
        )
        is_facade = _is_facade(value, facade_aliases=self._facade_aliases)
        for target in targets:
            self._record_alias(target, is_repository=is_repository, is_facade=is_facade)

    def _record_alias(self, target: ast.expr, *, is_repository: bool, is_facade: bool) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._record_alias(element, is_repository=is_repository, is_facade=is_facade)
            return
        if not isinstance(target, ast.Name):
            return
        if target.id not in _CONVENTIONAL_REPOSITORY_NAMES:
            self._repository_aliases.discard(target.id)
        if target.id not in _CONVENTIONAL_FACADE_NAMES:
            self._facade_aliases.discard(target.id)
        if is_repository:
            self._repository_aliases.add(target.id)
        if is_facade:
            self._facade_aliases.add(target.id)


def _writer_call_name(
    node: ast.Call,
    *,
    repository_aliases: set[str],
    facade_aliases: set[str],
) -> str | None:
    if isinstance(node.func, ast.Attribute):
        if node.func.attr in REPOSITORY_MUTATION_METHODS and _is_repository(
            node.func.value,
            repository_aliases=repository_aliases,
            facade_aliases=facade_aliases,
        ):
            return node.func.attr
        if node.func.attr in FACADE_WORKFLOW_METHODS and _is_facade(
            node.func.value,
            facade_aliases=facade_aliases,
        ):
            return node.func.attr
        if node.func.attr == "append" and isinstance(node.func.value, ast.Name) and node.func.value.id == "receipts":
            return "append_decision_receipt"
        if (
            node.func.attr == "update_final_outcome"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "receipts"
        ):
            return "update_decision_final_outcome"
        return None
    if (
        isinstance(node.func, ast.Name)
        and node.func.id in REPOSITORY_MUTATION_HELPERS
        and any(
            _is_repository(
                argument,
                repository_aliases=repository_aliases,
                facade_aliases=facade_aliases,
            )
            for argument in (*node.args, *(keyword.value for keyword in node.keywords))
        )
    ):
        return node.func.id
    if (
        isinstance(node.func, ast.Name)
        and node.func.id in FACADE_WORKFLOW_HELPERS
        and any(
            _is_facade(argument, facade_aliases=facade_aliases)
            for argument in (*node.args, *(keyword.value for keyword in node.keywords))
        )
    ):
        return node.func.id
    return None


def _is_repository(
    node: ast.AST,
    *,
    repository_aliases: set[str],
    facade_aliases: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in repository_aliases
    if isinstance(node, ast.Attribute):
        if _is_facade(node.value, facade_aliases=facade_aliases) or (
            isinstance(node.value, ast.Name) and node.value.id == "clerk"
        ):
            return node.attr == "repository"
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            return node.attr == "_repo"
    return False


def _is_facade(node: ast.AST, *, facade_aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in facade_aliases
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


def test_writer_census_detects_decision_final_outcome_updates() -> None:
    """The visitor still recognizes `receipts.update_final_outcome(...)`.

    No production caller uses this pattern anymore: decision receipts are
    now captured atomically inside the custody transaction
    (`append_atomic_decision_receipt_row` via `_commit_transition_row`), so
    the two-step provisional-then-final write this pattern used to protect
    (`bot_trade_strategy._record_blocked_decision`) was deleted. This test
    is now a detector self-test, matching the sibling alias fixture below,
    so the visitor's `update_decision_final_outcome` branch keeps coverage.
    """
    source = (
        "def blocked_decision(receipts):\n"
        "    receipts.update_final_outcome(bar_ref='X', outcome='blocked', order_ref=None, facts={})\n"
    )
    visitor = _WriterCallVisitor(source_root=Path("fixture/decision_final_outcome.py"))
    visitor.visit(ast.parse(source, filename="fixture/decision_final_outcome.py"))

    assert (
        _WriterCallSite(
            "fixture/decision_final_outcome.py",
            "blocked_decision",
            "update_decision_final_outcome",
        )
        in visitor.calls
    )


def test_writer_census_detects_direct_repository_and_facade_aliases() -> None:
    source = (
        "async def aliased_writer(facade):\n"
        "    repository_alias = facade.repository\n"
        "    repository_alias.append_transition(None)\n"
        "    facade_alias = facade\n"
        "    await facade_alias.reconcile_account()\n"
        "    await execute_recovery_action(reconciler=facade_alias)\n"
    )
    visitor = _WriterCallVisitor(source_root=Path("fixture/aliased_writer.py"))
    visitor.visit(ast.parse(source, filename="fixture/aliased_writer.py"))

    assert visitor.calls == {
        _WriterCallSite("fixture/aliased_writer.py", "aliased_writer", "append_transition"),
        _WriterCallSite("fixture/aliased_writer.py", "aliased_writer", "reconcile_account"),
        _WriterCallSite("fixture/aliased_writer.py", "aliased_writer", "execute_recovery_action"),
    }


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
