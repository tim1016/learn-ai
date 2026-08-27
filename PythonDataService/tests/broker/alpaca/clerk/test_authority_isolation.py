"""S5.3: the active-SQLite product surface never falls back to a legacy reader.

**Import-graph** — none of the modules that make up the active-SQLite
product surface (the ``sqlite/`` package plus the two product-facing
modules that adapt it to HTTP) statically import a JSONL custody reader,
the Postgres Clerk projection store, or an in-memory rollup-of-JSONL
reader.

The prior "Runtime" proof here (killing the SQLite read must never fall
back to the Postgres Clerk projection store) tested the explicit
``broker=ibkr`` compatibility branch in ``clerk_transactions.py``. That
branch, ``get_clerk_transaction_store()``, and the Postgres store itself
were retired with the rest of IBKR account authority (PR-A of #1813) —
the router now only ever reaches the active-SQLite path, so the fallback
this proved against is no longer constructible, not merely proven absent
at runtime.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# The active-SQLite product surface: the sqlite/ package is sole-authority by
# definition, plus the two product-facing modules that adapt it to HTTP.
_SOLE_AUTHORITY_ROOT = Path(__file__).resolve().parents[4] / "app"
_SOLE_AUTHORITY_MODULES = (
    *sorted((_SOLE_AUTHORITY_ROOT / "broker" / "alpaca" / "clerk" / "sqlite").glob("*.py")),
    _SOLE_AUTHORITY_ROOT / "services" / "sqlite_clerk_transaction_projection.py",
    _SOLE_AUTHORITY_ROOT / "routers" / "clerk_transactions.py",
)

# Concrete symbols this program's earlier slices identified as legacy
# readers: the Postgres Clerk projection store (S0-S4), the JSONL decision
# journal (S5.1 retired its producer), the in-memory rollup-of-JSONL fill
# reader, and the JSONL order-journal row model it reads. Banned by symbol,
# not by whole module: `decision_journal` and `rollup_cache` also export
# shared, non-legacy types (`DecisionOutcome`, `BotRollup`) that sole-authority
# modules legitimately reuse.
_BANNED_SYMBOLS = frozenset({
    "PostgresClerkTransactionProjectionStore",
    "DecisionJournal",
    "project_instance_fills",
    "OrderJournalEntry",
    "BotRollupCache",
})
# `clerk_transaction_projection_store` is single-purpose (only the Postgres
# store) — banned wholesale as defense-in-depth against a bare `import`.
_BANNED_MODULES = frozenset({
    "app.services.clerk_transaction_projection_store",
})


def _module_scope_imports(path: Path) -> tuple[set[str], set[str]]:
    """Return (imported symbol names, imported module dotted paths) at module scope.

    Only ``ast.Module.body`` top-level import statements count — a
    function-local ``import`` is invisible here by design.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    symbols: set[str] = set()
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            symbols.update(alias.name for alias in node.names)
    return symbols, modules


@pytest.mark.parametrize("module_path", _SOLE_AUTHORITY_MODULES, ids=lambda p: p.stem)
def test_sole_authority_module_never_imports_a_legacy_reader_at_module_scope(
    module_path: Path,
) -> None:
    symbols, modules = _module_scope_imports(module_path)
    banned_symbols_found = symbols & _BANNED_SYMBOLS
    banned_modules_found = {
        module for module in modules if any(module.startswith(banned) for banned in _BANNED_MODULES)
    }
    assert not banned_symbols_found, (
        f"{module_path} imports legacy reader symbol(s) {banned_symbols_found} at module scope; "
        "the sole-authority surface must not statically depend on a retired reader."
    )
    assert not banned_modules_found, (
        f"{module_path} imports legacy reader module(s) {banned_modules_found} at module scope."
    )
