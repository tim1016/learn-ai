"""S5.3: the active-SQLite product surface never falls back to a legacy reader.

Two proofs:

1. **Import-graph** — none of the modules that make up the active-SQLite
   product surface (the ``sqlite/`` package plus the two product-facing
   modules that adapt it to HTTP) statically import a JSONL custody reader,
   the Postgres Clerk projection store, or an in-memory rollup-of-JSONL
   reader. A module that needs one of these for a *legacy* (non-SQLite)
   authority code path must do so via a function-local import, which this
   test cannot see — that is the escape hatch, not a loophole: it means the
   symbol is unreachable unless the function actually runs.
2. **Runtime** — killing the SQLite read (simulating a corrupt/unavailable
   authority) surfaces as ``projection_unavailable``/503, never a silent
   read of the legacy Postgres store.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.routers.clerk_transactions as clerk_transactions_router
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    ClerkStartupFailure,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.routers.clerk_transactions import router
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable

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
    function-local ``import`` (used deliberately to defer a legacy-path
    dependency, e.g. ``get_clerk_transaction_store``) is invisible here by
    design.
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
        "use a function-local import if this is genuinely a legacy-authority fallback."
    )
    assert not banned_modules_found, (
        f"{module_path} imports legacy reader module(s) {banned_modules_found} at module scope."
    )


async def test_killing_the_active_sqlite_read_returns_unavailable_never_legacy_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adversarial verify (S5): a broken active-SQLite read must not fall through."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-KILL", artifacts_root=tmp_path)

    class _UnreachableBroker:
        broker_id = "alpaca"

        async def list_orders(self, **_kwargs: object) -> list:
            return []

        async def list_positions(self) -> list:
            return []

    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_UnreachableBroker(),  # type: ignore[arg-type]
        trade=_UnreachableBroker(),  # type: ignore[arg-type]
    )
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))

    def killed_sqlite_read(**_kwargs: object) -> None:
        raise ClerkTransactionProjectionUnavailable("SQLite authority is unreachable (simulated kill).")

    monkeypatch.setattr(clerk_transactions_router, "sqlite_transaction_history", killed_sqlite_read)

    postgres_calls: list[str] = []

    class _PoisonedLegacyStore:
        """Fails the test if the router ever reaches for legacy data while SQLite is active."""

        async def history_page(self, **_kwargs: object):
            postgres_calls.append("history_page")
            raise AssertionError("router must not consult the legacy store while SQLite is active")

        async def transaction_detail(self, **_kwargs: object):
            postgres_calls.append("transaction_detail")
            raise AssertionError("router must not consult the legacy store while SQLite is active")

    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(
        clerk_transactions_router,
        "get_clerk_transaction_store",
        lambda: _PoisonedLegacyStore(),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/PA-KILL/transactions")
    finally:
        set_active_clerk_runtime(None)
        repo.close()

    assert response.status_code == 200
    body = response.json()
    assert body["projection_available"] is False
    assert body["feed_state"] == "projection_unavailable"
    assert body["rows"] == []
    assert postgres_calls == []


async def test_selected_but_unavailable_sqlite_authority_never_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An activation record means startup failure is unavailable, not legacy."""
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="unavailable",
            startup_failure=ClerkStartupFailure(
                reason_code="SQLITE_CLERK_STARTUP_FAILED",
                account_id="PA-UNAVAILABLE",
                scope="ACCOUNT_CLERK",
                impact="SQLite authority could not start.",
                recovery="Repair the activated SQLite authority.",
                observed_at_ms=1,
                activation_detected=True,
                authority_generation=1,
                db_identity_token="identity-1",
            ),
        )
    )
    legacy_calls: list[str] = []

    class _PoisonedLegacyStore:
        async def history_page(self, **_kwargs: object) -> None:
            legacy_calls.append("history_page")
            raise AssertionError("activated SQLite startup failure must not read legacy history")

    monkeypatch.setattr(
        clerk_transactions_router,
        "get_clerk_transaction_store",
        lambda: _PoisonedLegacyStore(),
    )
    app = FastAPI()
    app.include_router(router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/PA-UNAVAILABLE/transactions")
    finally:
        set_active_clerk_runtime(None)

    assert response.status_code == 200
    assert response.json()["feed_state"] == "projection_unavailable"
    assert legacy_calls == []
