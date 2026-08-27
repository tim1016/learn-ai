"""Structural regression guard for the IBKR feed/control-plane boundary.

Issue #1813 (IBKR control-plane decommission), Slice 0. Walks the
retained feed-side modules' import statements and asserts none of them
reach into the account/order/session bucket, except two named,
temporary exceptions — each with a second live consumer outside this
slice's scope, tracked to close in a later slice. See
``docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md``.

This is deliberately a source-level import scan (``ast``), not a
runtime ``sys.modules`` inspection — it catches an import statement
even in a code path that isn't exercised by the rest of the test suite.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Iterable
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# Modules whose entire import graph must stay clear of the account/order/
# session bucket (module dotted-path prefixes below), except the named
# exceptions in _ALLOWED_EXCEPTIONS.
RETAINED_FEED_MODULES = [
    "app.marketdata.feed",
    "app.marketdata.ibkr_feed",
    "app.broker.ibkr.bar_models",
    "app.broker.ibkr.bars",
    "app.broker.ibkr.client",
    "app.broker.ibkr.config",
    "app.broker.ibkr.event_codes",
    "app.broker.ibkr.health",
    "app.broker.ibkr.keepalive",
    "app.broker.ibkr.models",
    "app.broker.ibkr.recovery_state_machine",
    "app.broker.ibkr.auto_reconnect_monitor",
    "app.broker.ibkr.contracts",
    "app.broker.ibkr.market_data",
    "app.broker.ibkr.surface",
    "app.broker.ibkr.symbol_search",
    "app.services.market_data_capability_service",
]

# Dotted-path prefixes considered "account/order/session bucket" — a
# retained module importing anything under these prefixes is a real
# regression unless explicitly allow-listed below.
BANNED_PREFIXES = (
    "app.broker.ibkr.account",
    "app.broker.ibkr.account_recovery",
    "app.broker.ibkr.account_truth",
    "app.broker.ibkr.account_truth_freshness",
    "app.broker.ibkr.order_history",
    "app.broker.ibkr.order_previews",
    "app.broker.ibkr.orders",
    "app.broker.ibkr.pnl",
    "app.broker.ibkr.order_error_stream",
    "app.broker.ibkr.order_evidence",
    "app.broker.ibkr.order_projection",
    "app.broker.safety_verdict",
    "app.services.account_truth_refresh",
    "app.services.account_reconciliation",
    "app.services.account_safety_access",
    "app.services.account_safety_snapshot",
    "app.services.account_truth_snapshot",
    "app.services.account_event_journal",
    "app.services.broker_activity_publisher",
    "app.services.broker_activity_reconciler",
    "app.services.broker_activity_reconstruction",
    "app.services.broker_session_history",
    "app.services.broker_session_mirror",
    "app.services.broker_session_reconciler",
    "app.services.journal_recovery",
    "app.services.host_capability",
    "app.services.activity_evidence_matching",
    "app.services.bot_event_rejection_bridge",
)

# (importing_module, banned_import) pairs allowed to remain, each with
# the slice that closes it. Remove the tuple when that slice lands.
_ALLOWED_EXCEPTIONS = {
    ("app.broker.ibkr.client", "app.broker.ibkr.order_error_stream"),  # closes in Slice 4
    ("app.broker.ibkr.models", "app.broker.safety_verdict"),  # closes in Slice 3
}


def _module_path(dotted: str) -> Path:
    return APP_ROOT.joinpath(*dotted.split(".")[1:]).with_suffix(".py")


def _imported_modules(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _walk_app_import_graph(seeds: Iterable[str]) -> tuple[set[str], list[tuple[str, str]]]:
    """Breadth-first walk of the ``app.*`` import graph starting at ``seeds``.

    Returns ``(visited, banned_edges)``:

    - ``visited`` is every module reached by the walk (seeds included),
      resolved to a real file under ``app/``.
    - ``banned_edges`` is every ``(importing_module, imported_module)`` pair
      found anywhere in the walk where ``imported_module`` matches
      ``BANNED_PREFIXES`` — allowed (tracked-exception) hits and violations
      alike. A banned edge is a dead end for the walk: it is recorded but
      not itself recursed into, since the bucket's internals are out of
      scope once one of its modules is reached at all.
    """
    visited: set[str] = set()
    banned_edges: list[tuple[str, str]] = []
    queue: deque[str] = deque(seeds)
    while queue:
        module = queue.popleft()
        if module in visited:
            continue
        visited.add(module)
        source_path = _module_path(module)
        if not source_path.exists():
            continue
        for imported_module in sorted(_imported_modules(source_path)):
            if not imported_module.startswith("app."):
                continue
            if imported_module.startswith(BANNED_PREFIXES):
                banned_edges.append((module, imported_module))
                continue
            if imported_module in visited:
                continue
            if not _module_path(imported_module).exists():
                continue
            queue.append(imported_module)
    return visited, banned_edges


def test_retained_feed_modules_transitive_import_graph_has_no_unlisted_account_order_session_import() -> None:
    for dotted_module in RETAINED_FEED_MODULES:
        assert _module_path(dotted_module).exists(), (
            f"{dotted_module} does not resolve to {_module_path(dotted_module)}"
        )

    _visited, banned_edges = _walk_app_import_graph(RETAINED_FEED_MODULES)
    violations = [
        (importing_module, imported_module)
        for importing_module, imported_module in banned_edges
        if (importing_module, imported_module) not in _ALLOWED_EXCEPTIONS
    ]
    assert not violations, (
        "Walking the transitive app.* import graph from RETAINED_FEED_MODULES reaches "
        f"account/order/session-bucket import(s) with no tracked exception: {violations} "
        "— see _ALLOWED_EXCEPTIONS in this test."
    )


def test_no_stale_allowed_exceptions() -> None:
    """Every entry in _ALLOWED_EXCEPTIONS must reflect a real, current, reachable import.

    Prevents the exception list from silently outliving the code it was
    written for — if Slice 3 or 4 removes the import another way, or the
    importing module itself falls out of the retained-feed walk, this
    fails loudly instead of leaving a permissive dead entry.
    """
    visited, banned_edges = _walk_app_import_graph(RETAINED_FEED_MODULES)
    banned_edge_set = set(banned_edges)
    for dotted_module, banned_import in _ALLOWED_EXCEPTIONS:
        assert dotted_module in visited, (
            f"_ALLOWED_EXCEPTIONS names importer {dotted_module!r} but it is no longer "
            "reachable from RETAINED_FEED_MODULES — delete this stale exception."
        )
        assert (dotted_module, banned_import) in banned_edge_set, (
            f"_ALLOWED_EXCEPTIONS names ({dotted_module!r}, {banned_import!r}) but {dotted_module} "
            "no longer imports it — delete this stale exception."
        )
