"""Static enforcement for SQLite Clerk repository writers outside the package."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.repository_boundary import (
    EXTERNAL_REPOSITORY_WRITER_CENSUS,
    FACADE_WORKFLOW_HELPERS,
    FACADE_WORKFLOW_METHODS,
    FILL_GUARDED_APPENDS,
    FILL_TRANSITION_KINDS,
    FILL_TRANSITION_PRODUCER_CENSUS,
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


@dataclass(frozen=True, order=True)
class _FillTransitionSite:
    path: str
    owner: str
    kind: str


class _FillTransitionVisitor(ast.NodeVisitor):
    """Every call that names a fill kind as its ``transition_kind``, by the function that owns it.

    The owner is the outermost function around the call, under its class: a
    nested builder or a lambda belongs to the function that hands it on.
    The kind may be spelled as a string or as a module-level name bound to
    one. ``sites`` counts each site, so a second one in a listed owner shows.
    ``called`` is every method each owner calls, by name.
    """

    def __init__(self, *, source_root: Path) -> None:
        self._source_root = source_root
        self._classes: list[str] = []
        self._owner: str | None = None
        self._module_strings: dict[str, str] = {}
        self.sites: Counter[_FillTransitionSite] = Counter()
        self.called: dict[str, set[str]] = {}

    def visit_Module(self, node: ast.Module) -> None:
        for statement in node.body:
            targets: list[ast.expr] = []
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            else:
                continue
            value = statement.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for target in targets:
                    if isinstance(target, ast.Name):
                        self._module_strings[target.id] = value.value
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._classes.append(node.name)
        self.generic_visit(node)
        self._classes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        owner = self._owner or ""
        if isinstance(node.func, ast.Attribute):
            self.called.setdefault(owner, set()).add(node.func.attr)
        for keyword in node.keywords:
            if keyword.arg != "transition_kind":
                continue
            kind = self._kind_named(keyword.value)
            if kind in FILL_TRANSITION_KINDS:
                self.sites[_FillTransitionSite(self._source_root.as_posix(), owner, kind)] += 1
        self.generic_visit(node)

    def _kind_named(self, value: ast.expr) -> object:
        if isinstance(value, ast.Constant):
            return value.value
        if isinstance(value, ast.Name):
            return self._module_strings.get(value.id)
        return None

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outermost = self._owner is None
        if outermost:
            self._owner = ".".join([*self._classes, node.name])
        self.generic_visit(node)
        if outermost:
            self._owner = None


def _fill_transition_visitor(source: str, *, source_root: Path) -> _FillTransitionVisitor:
    visitor = _FillTransitionVisitor(source_root=source_root)
    visitor.visit(ast.parse(source, filename=str(source_root)))
    return visitor


def _assert_fill_census(
    observed: Counter[_FillTransitionSite],
    expected: Counter[_FillTransitionSite],
) -> None:
    assert observed == expected, (
        "A fill transition must be built once where the census names, and appended through "
        "append_fill so a discharge that counted it withholds its credit (#2854). "
        f"Unlisted: {sorted((observed - expected).elements())!r}; "
        f"stale: {sorted((expected - observed).elements())!r}"
    )


def test_fill_transitions_are_built_only_where_the_census_names_and_go_through_a_guarded_append() -> None:
    observed: Counter[_FillTransitionSite] = Counter()
    called: dict[tuple[str, str], set[str]] = {}
    for path in _PRODUCTION_ROOT.rglob("*.py"):
        source_root = path.relative_to(_PYTHON_DATA_SERVICE_ROOT)
        visitor = _fill_transition_visitor(path.read_text(encoding="utf-8"), source_root=source_root)
        observed.update(visitor.sites)
        for owner, names in visitor.called.items():
            called[(source_root.as_posix(), owner)] = names

    _assert_fill_census(
        observed,
        Counter(
            _FillTransitionSite(entry.path, entry.owner, entry.kind) for entry in FILL_TRANSITION_PRODUCER_CENSUS
        ),
    )
    for entry in FILL_TRANSITION_PRODUCER_CENSUS:
        assert entry.through in FILL_GUARDED_APPENDS, entry
        assert entry.through in called[(entry.path, entry.owner)], entry


def test_an_unlisted_fill_producer_fails_the_census_even_from_a_nested_builder() -> None:
    source = (
        "def unlisted_producer(repo, effect):\n"
        "    def build():\n"
        "        return TransitionInput(transition_kind=\"ORDER_FILL_OBSERVED\", order_ref=effect.order_ref)\n"
        "    repo.append_transition(build())\n"
    )
    visitor = _fill_transition_visitor(source, source_root=Path("fixture/unlisted_producer.py"))

    site = _FillTransitionSite("fixture/unlisted_producer.py", "unlisted_producer", "ORDER_FILL_OBSERVED")
    assert visitor.sites == Counter({site: 1})
    with pytest.raises(AssertionError, match="Unlisted"):
        _assert_fill_census(visitor.sites, Counter())


def test_a_fill_kind_held_in_a_module_constant_is_still_a_producer() -> None:
    source = (
        "KIND = \"EXECUTION_SLICE_FILLED\"\n"
        "def constant_producer(repo, effect):\n"
        "    repo.append_transition(TransitionInput(transition_kind=KIND, order_ref=effect.order_ref))\n"
    )
    visitor = _fill_transition_visitor(source, source_root=Path("fixture/constant_producer.py"))

    site = _FillTransitionSite("fixture/constant_producer.py", "constant_producer", "EXECUTION_SLICE_FILLED")
    assert visitor.sites == Counter({site: 1})


def test_a_second_fill_site_in_a_listed_owner_fails_the_census() -> None:
    source = (
        "def listed_owner(repo, effect):\n"
        "    repo.append_fill(TransitionInput(transition_kind=\"ORDER_FILL_OBSERVED\"), symbol=\"SPY\")\n"
        "    repo.append_transition(TransitionInput(transition_kind=\"ORDER_FILL_OBSERVED\"))\n"
    )
    visitor = _fill_transition_visitor(source, source_root=Path("fixture/listed_owner.py"))

    site = _FillTransitionSite("fixture/listed_owner.py", "listed_owner", "ORDER_FILL_OBSERVED")
    with pytest.raises(AssertionError, match="Unlisted"):
        _assert_fill_census(visitor.sites, Counter({site: 1}))
