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
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("dotted_module", RETAINED_FEED_MODULES)
def test_retained_feed_module_has_no_unlisted_account_order_session_import(dotted_module: str) -> None:
    source_path = _module_path(dotted_module)
    assert source_path.exists(), f"{dotted_module} does not resolve to {source_path}"
    imported = _imported_modules(source_path)
    violations = []
    for imported_module in imported:
        if not imported_module.startswith(BANNED_PREFIXES):
            continue
        if (dotted_module, imported_module) in _ALLOWED_EXCEPTIONS:
            continue
        violations.append(imported_module)
    assert not violations, (
        f"{dotted_module} imports account/order/session-bucket module(s) {violations} "
        "with no tracked exception — see _ALLOWED_EXCEPTIONS in this test."
    )


def test_no_stale_allowed_exceptions() -> None:
    """Every entry in _ALLOWED_EXCEPTIONS must reflect a real, current import.

    Prevents the exception list from silently outliving the code it was
    written for — if Slice 3 or 4 removes the import another way, this
    fails loudly instead of leaving a permissive dead entry.
    """
    for dotted_module, banned_import in _ALLOWED_EXCEPTIONS:
        source_path = _module_path(dotted_module)
        imported = _imported_modules(source_path)
        assert banned_import in imported, (
            f"_ALLOWED_EXCEPTIONS names ({dotted_module!r}, {banned_import!r}) but {dotted_module} "
            "no longer imports it — delete this stale exception."
        )
