"""Two structural guards over ``app/``, sharing one AST module resolver.

**The feed boundary.** Issue #1813 (IBKR control-plane decommission),
Slice 0. Walks the retained feed-side modules' import statements and
asserts none of them reach into the account/order/session bucket. Slice
0 tracked three named, temporary exceptions, each with a second live
consumer outside its scope; PR-B of #1813 (2026-08-27) retired all
three consumers (``order_error_stream.py``, ``broker_session_events``
emission, and ``app/broker/safety_verdict.py``), so the exception list
is now empty. See
``docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md``.

**The module-retirement contract.** The single home for "retirement X
deleted module M — prove M is still gone and that nothing names it",
across all three retirement programmes (#1583 order actuation, ADR 0038
evaluator plane, #1813 control plane). It lives here rather than in
``tests/contracts/`` because it is the same AST walk over the same tree
as the boundary guard and shares ``_module_path`` / ``_is_package_dir``
/ ``_resolve_relative_base`` with it; splitting them would duplicate
those helpers. PR-C of #1813 consolidated it: ``tests/contracts/``
previously asserted 21 of these modules absent a second time in a
second notation, through a scanner that could not see
``from app.x import y`` at all. Those files now assert only what they
alone can — route registration, OpenAPI shape, frontend literals, and
symbol-level absences.

Both guards are deliberately source-level import scans (``ast``), not
runtime ``sys.modules`` inspection — they catch an import statement
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
    "app.services.market_data_capability_service",
]

# Dotted-path prefixes considered "account/order/session bucket" — a
# retained module importing anything under these prefixes is a real
# regression unless explicitly allow-listed below.
#
# As of PR-C of #1813 (2026-08-27), **every entry below names a module that no
# longer exists**: PR-A and PR-B deleted the whole bucket. So the list is now
# entirely forward-looking — it does not describe code that is here and must
# stay out of the feed's reach, it describes code that is gone and must not
# come back through the feed. That is deliberate and the entries stay: a
# prefix guard costs nothing and is the cheapest way to make a resurrection
# fail loudly. (An earlier review described this list as mixing live and
# deleted modules; that was measured before the bucket was fully emptied.
# `test_retired_modules_no_longer_resolve` below pins the "gone" half as a
# fact rather than leaving it implied.)
BANNED_PREFIXES = (
    "app.broker.ibkr.account",
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
    "app.services.broker_session_events",
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
#
# Empty as of PR-B of #1813 (2026-08-27) — all three Slice-0 exceptions
# closed by retiring their blocking consumer, per this repo's rule that
# exceptions close only by retirement, never by widening the allow-list.
_ALLOWED_EXCEPTIONS: set[tuple[str, str]] = set()


# Every ``app.*`` module retired by the #1813 IBKR control-plane decommission
# (PR-A, PR-B, and PR-C), pinned by dotted path. Derived from
# ``git diff --diff-filter=D --name-only 03ce52b6..`` over
# ``PythonDataService/app/`` at PR-C, then frozen here so the list is a
# contract rather than a re-derivation that would quietly agree with whatever
# the tree happens to contain.
#
# BANNED_PREFIXES stops the *feed* from reaching into this bucket.
# RETIRED_MODULES is the stronger, repo-wide statement: these modules are gone
# and nothing anywhere may name them.
RETIRED_MODULES = (
    "app.broker.ibkr.account",
    "app.broker.ibkr.account_recovery",
    "app.broker.ibkr.account_truth",
    "app.broker.ibkr.account_truth_freshness",
    "app.broker.ibkr.diagnostics",
    "app.broker.ibkr.order_error_stream",
    "app.broker.ibkr.order_evidence",
    "app.broker.ibkr.order_history",
    "app.broker.ibkr.order_previews",
    "app.broker.ibkr.order_projection",
    "app.broker.ibkr.orders",
    "app.broker.ibkr.persistence",
    "app.broker.ibkr.pnl",
    "app.broker.ibkr.symbol_search",
    "app.broker.runtime_snapshot",
    "app.broker.safety_verdict",
    "app.engine.live.account_identity",
    "app.engine.live.account_observation_lease",
    "app.engine.live.account_safety",
    "app.engine.live.account_session_policy",
    "app.engine.live.broker_callbacks",
    "app.engine.live.broker_socket_probe",
    "app.engine.live.command_channel",
    "app.engine.live.control_plane",
    "app.engine.live.daemon_auth",
    "app.engine.live.daemon_transport",
    "app.engine.live.fleet",
    "app.engine.live.host_daemon",
    "app.engine.live.host_daemon_client",
    "app.engine.live.host_runner_policy",
    "app.engine.live.intent_wal",
    "app.engine.live.journal_exposure",
    "app.engine.live.journal_recovery_state",
    "app.engine.live.run_lookup",
    "app.operator.notices.broker_activity_health",
    "app.operator.notices.broker_session",
    "app.routers.account_reconciliation",
    "app.routers.bot_events",
    "app.routers.broker_account_truth",
    "app.routers.broker_activity",
    "app.routers.broker_session",
    "app.routers.live_instances",
    "app.routers.live_runs",
    "app.schemas.account_cockpit",
    "app.schemas.account_directory",
    "app.schemas.account_events",
    "app.schemas.account_reconciliation",
    "app.schemas.account_safety_snapshot",
    "app.schemas.account_truth",
    "app.schemas.bot_events",
    "app.schemas.broker_activity",
    "app.schemas.broker_session",
    "app.schemas.journal_recovery",
    "app.schemas.presented_operator_action",
    "app.services.account_cockpit",
    "app.services.account_desk_guidance",
    "app.services.account_directory",
    "app.services.account_event_journal",
    "app.services.account_gate_policy",
    "app.services.account_gate_promotion",
    "app.services.account_journal_authority",
    "app.services.account_reconciliation",
    "app.services.account_safety_access",
    "app.services.account_safety_snapshot",
    "app.services.account_truth_refresh",
    "app.services.account_truth_snapshot",
    "app.services.activity_evidence_matching",
    "app.services.activity_projection_contract",
    "app.services.activity_repair_projection",
    "app.services.bot_event_incidents",
    "app.services.bot_event_projection",
    "app.services.bot_event_rejection_bridge",
    "app.services.bot_event_replacement_map",
    "app.services.bot_event_stream_service",
    "app.services.bot_event_wal",
    "app.services.broker_activity_publisher",
    "app.services.broker_activity_publisher_registry",
    "app.services.broker_activity_reconciler",
    "app.services.broker_activity_reconstruction",
    "app.services.broker_activity_templates",
    "app.services.broker_activity_wal",
    "app.services.broker_session_events",
    "app.services.broker_session_history",
    "app.services.broker_session_mirror",
    "app.services.broker_session_reconciler",
    "app.services.clerk_custody_timeline",
    "app.services.clerk_transaction_projection_ibkr",
    "app.services.clerk_transaction_projection_store",
    "app.services.durable_event_channel",
    "app.services.durable_event_stream",
    "app.services.fleet_contamination",
    "app.services.host_capability",
    "app.services.journal_recovery",
    "app.services.legacy_stale_claim_retirement",
    "app.services.live_log_failures",
    "app.services.live_log_parser",
    "app.services.live_run_state",
    "app.services.observation_lease_parity",
    "app.services.presented_account_actions",
    "app.services.sse_keepalive",
)

# Modules retired *before* #1813, by #1583's order-actuation retirement and ADR
# 0038's evaluator-plane retirement. Until PR-C of #1813 these were asserted
# absent by path in ``tests/contracts/test_ibkr_order_actuation_retirement.py``
# and ``test_ibkr_evaluator_plane_retirement.py``, the latter pairing the path
# list with a second dotted list scanned by a helper that recorded only
# ``f"{node.module}.{alias.name}"`` — so a plain ``from app.services.foo import
# bar`` walked straight through it. Folded in here so one mechanism covers
# every retired module and every one of them gets the stronger scan. The
# provenance split is kept rather than merged into ``RETIRED_MODULES`` above,
# which stays exactly the set derivable from
# ``git diff --diff-filter=D --name-only 03ce52b6..`` and nothing else.
EARLIER_RETIRED_MODULES = (
    "app.broker.ibkr.order_cancel_capability",
    "app.broker.ibkr.order_cancel_decision",
    "app.engine.live.account_classifier",
    "app.engine.live.account_clerk",
    "app.engine.live.account_clerk_cursor",
    "app.engine.live.account_clerk_emergency_sequence",
    "app.engine.live.account_clerk_lease",
    "app.engine.live.account_clerk_operations",
    "app.engine.live.account_clerk_reconciler",
    "app.engine.live.account_clerk_rpc",
    "app.engine.live.account_clerk_rpc_protocol",
    "app.engine.live.account_clerk_supervisor",
    "app.engine.live.account_custody_projection",
    "app.engine.live.account_custody_topology",
    "app.engine.live.account_effect",
    "app.engine.live.account_epoch_observer",
    "app.engine.live.account_epoch_reconciliation",
    "app.engine.live.account_owner_fence",
    "app.engine.live.account_recovery_cli",
    "app.engine.live.bar_adapter",
    "app.engine.live.bot_event_spine",
    "app.engine.live.bot_lifecycle_evaluator",
    "app.engine.live.bot_lifecycle_fence",
    "app.engine.live.child_watchdog",
    "app.engine.live.clock_out",
    "app.engine.live.daemon_command_idempotency",
    "app.engine.live.daemon_connectivity_monitor",
    "app.engine.live.deploy",
    "app.engine.live.engine_runtime",
    "app.engine.live.engine_runtime_publisher",
    "app.engine.live.fleet_reset_baseline",
    "app.engine.live.host_daemon_bot_events",
    "app.engine.live.ibkr_broker_ownership_query",
    "app.engine.live.lifecycle_exit_finalizer",
    "app.engine.live.live_context",
    "app.engine.live.live_engine",
    "app.engine.live.live_portfolio",
    "app.engine.live.no_submit_broker_adapter",
    "app.engine.live.orphan_classifier",
    "app.engine.live.post_halt_gate",
    "app.engine.live.pre_flight",
    "app.engine.live.process_registry",
    "app.engine.live.readiness",
    "app.engine.live.readiness_sidecar",
    "app.engine.live.reconciliation_orchestrator",
    "app.engine.live.replay_layer",
    "app.engine.live.run",
    "app.engine.live.run_ledger",
    "app.engine.live.run_logging",
    "app.engine.live.runtime_producer",
    "app.engine.live.session_metadata",
    "app.engine.live.shadow_fill_simulator",
    "app.engine.live.signal_tone",
    "app.engine.live.submit_state_machine",
    "app.engine.live.watchdog_controller",
    "app.operator.incidents.watchdog_notices",
    "app.operator.notices.runtime_freshness",
    "app.routers.offline_replay",
    "app.schemas.account_recovery",
    "app.schemas.offline_replay",
    "app.services.account_crash_recovery",
    "app.services.account_fleet_read_context",
    "app.services.account_start_gate",
    "app.services.activity_lifecycle_consistency",
    "app.services.bot_control_plane",
    "app.services.bot_deletion",
    "app.services.bot_lifecycle_receipt_copy",
    "app.services.bot_roll_call",
    "app.services.deploy_admission",
    "app.services.deploy_preflight",
    "app.services.ibkr_lifecycle_guard",
    "app.services.manual_order_submission",
    "app.services.mutation_attempt",
    "app.services.offline_replay_clock",
    "app.services.offline_replay_data",
    "app.services.offline_replay_service",
    "app.services.presented_recovery_action_dispatch",
    "app.services.presented_recovery_actions",
    "app.services.resume_guard_state",
    "app.services.risk_reducing_lifecycle_intent",
    "app.services.runtime_freshness",
)

ALL_RETIRED_MODULES = RETIRED_MODULES + EARLIER_RETIRED_MODULES


def _referenced_module_names(source_path: Path) -> set[str]:
    """Every dotted name this file's imports could denote, resolved or not.

    Deliberately different from ``_imported_modules``: that helper keeps only
    names that resolve to a real file, which is right for walking a live import
    graph but wrong for retirement checking — a reference to a *deleted* module
    is exactly what we want to catch, and it resolves to nothing by definition.
    ``from app.services import account_cockpit`` is recorded here as both
    ``app.services`` and ``app.services.account_cockpit``.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            base = node.module
        else:
            relative_base = _resolve_relative_base(source_path, node.level)
            base = f"{relative_base}.{node.module}" if relative_base and node.module else relative_base
        if base is None:
            continue
        found.add(base)
        found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def test_retired_modules_no_longer_resolve() -> None:
    """No module any of the three retirements deleted may resolve to a file again.

    This is the "structural-retirement" half of the contract: the boundary
    test proves the feed does not *import* the bucket, this one proves the
    bucket is not *there*. A module that came back would satisfy the
    import-graph test trivially (nothing imports it yet) while quietly
    reopening the surface the retirement closed.
    """
    assert RETIRED_MODULES, "RETIRED_MODULES is empty — this guard would pass vacuously."
    assert EARLIER_RETIRED_MODULES, "EARLIER_RETIRED_MODULES is empty — this guard would pass vacuously."
    assert not set(RETIRED_MODULES) & set(EARLIER_RETIRED_MODULES), (
        "A module is listed under both retirements — the provenance split is meant to be exclusive."
    )

    resurrected = [
        dotted
        for dotted in ALL_RETIRED_MODULES
        if _module_path(dotted).exists() or _is_package_dir(dotted)
    ]
    assert not resurrected, (
        "Module(s) retired by #1583, ADR 0038 or #1813 resolve again: "
        f"{resurrected}. Re-adding one is a deliberate decision that belongs in a PR of "
        "its own, with its row in the registry docs — not a silent resurrection."
    )


def test_no_surviving_module_references_a_retired_module() -> None:
    """No file under ``app/`` may name a retired module, at any import depth.

    Catches the dangling reference a module-existence check cannot: an import
    of a deleted module nested inside a function body, which never executes in
    the suite and so never raises, but is broken the moment that branch runs.
    """
    assert ALL_RETIRED_MODULES, "ALL_RETIRED_MODULES is empty — this guard would pass vacuously."

    retired = set(ALL_RETIRED_MODULES)
    scanned = 0
    offenders: list[tuple[str, str]] = []
    for source_path in sorted(APP_ROOT.rglob("*.py")):
        scanned += 1
        importer = ".".join(source_path.relative_to(APP_ROOT.parent).with_suffix("").parts)
        offenders.extend(
            (importer, referenced)
            for referenced in sorted(_referenced_module_names(source_path))
            if referenced in retired
        )
    assert scanned > 0, f"Scanned no files under {APP_ROOT} — the walk is broken, not clean."
    assert not offenders, (
        "Surviving module(s) reference a module retired by #1813: "
        f"{offenders}. The reference cannot work — the target no longer exists."
    )


def _module_path(dotted: str) -> Path:
    return APP_ROOT.joinpath(*dotted.split(".")[1:]).with_suffix(".py")


def _is_package_dir(dotted: str) -> bool:
    return APP_ROOT.joinpath(*dotted.split(".")[1:]).is_dir()


def _containing_package(source_path: Path) -> str:
    """Dotted package containing ``source_path`` — everything but the filename."""
    parts = source_path.relative_to(APP_ROOT.parent).with_suffix("").parts
    return ".".join(parts[:-1])


def _resolve_relative_base(source_path: Path, level: int) -> str | None:
    """Resolve a relative import's dot-count against the importing file's own package."""
    package_parts = _containing_package(source_path).split(".")
    climbed = len(package_parts) - (level - 1)
    if climbed < 0:
        return None
    trimmed = package_parts[:climbed]
    return ".".join(trimmed) if trimmed else None


def _imported_modules(source_path: Path) -> set[str]:
    """Every ``app.*`` module this file's imports could plausibly reference.

    Beyond plain ``import a.b`` and ``from a.b import c`` (absolute, level 0),
    this also resolves two forms a flat ``node.module`` read would miss:

    - ``from app.broker.ibkr import account`` — ``account`` here may be a
      *submodule* of the ``app.broker.ibkr`` package, not a symbol defined
      inside it. When ``node.module`` resolves to a real package directory,
      each imported name is also checked as ``{node.module}.{name}`` and
      recorded if that resolves to a real module or package.
    - ``from . import account`` / ``from .. import x`` — relative imports
      (``node.level > 0``) are resolved against the importing file's own
      containing package before the same submodule check is applied.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            base = node.module
        else:
            relative_base = _resolve_relative_base(source_path, node.level)
            base = f"{relative_base}.{node.module}" if relative_base and node.module else relative_base
        if base is None:
            continue
        found.add(base)
        if _is_package_dir(base):
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if _module_path(candidate).exists() or _is_package_dir(candidate):
                    found.add(candidate)
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

    **This test is vacuously green today and that is deliberate.**
    ``_ALLOWED_EXCEPTIONS`` has been empty since PR-B of #1813, so the loop
    below has nothing to iterate; it asserts nothing until someone adds an
    exception, which is precisely when it starts earning its keep. PR-C
    considered the alternatives and rejected both: deleting it would remove
    the guard that keeps a future exception from going stale, and re-adding
    an exception just to give it something to iterate would reopen a boundary
    the decommission closed in order to make a test look busy. Read the empty
    ``_ALLOWED_EXCEPTIONS`` as the real assertion — the boundary currently
    carries no exceptions at all.
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
