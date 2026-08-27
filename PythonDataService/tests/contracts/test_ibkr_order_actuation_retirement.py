"""Structural contract for #1583's IBKR order-actuation retirement."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
APPLICATION_ROOT = REPOSITORY_ROOT / "PythonDataService" / "app"
FRONTEND_APPLICATION_ROOT = REPOSITORY_ROOT / "Frontend" / "src" / "app"
OPENAPI_CONTRACT = REPOSITORY_ROOT / "contracts/openapi/python-data-service.openapi.json"
FRONTEND_CONTRACT = FRONTEND_APPLICATION_ROOT / "api/broker.types.ts"

RETIRED_DIRECT_MUTATION_MODULES = (
    "broker/ibkr/order_cancel_capability.py",
    "broker/ibkr/order_cancel_decision.py",
    "engine/live/bot_event_spine.py",
    "engine/live/live_context.py",
    "engine/live/live_engine.py",
    "engine/live/live_portfolio.py",
    "engine/live/no_submit_broker_adapter.py",
    "engine/live/replay_layer.py",
    "routers/offline_replay.py",
    "schemas/offline_replay.py",
    "services/manual_order_submission.py",
    "services/offline_replay_clock.py",
    "services/offline_replay_data.py",
    "services/offline_replay_service.py",
)

RETIRED_ACCOUNT_MUTATION_MODULES = (
    "engine/live/account_clerk.py",
    "engine/live/account_clerk_cursor.py",
    "engine/live/account_clerk_emergency_sequence.py",
    "engine/live/account_clerk_lease.py",
    "engine/live/account_clerk_operations.py",
    "engine/live/account_clerk_reconciler.py",
    "engine/live/account_clerk_rpc.py",
    "engine/live/account_clerk_rpc_protocol.py",
    "engine/live/account_clerk_supervisor.py",
    "engine/live/account_effect.py",
    "engine/live/account_owner_fence.py",
    "engine/live/daemon_command_idempotency.py",
    "services/account_start_gate.py",
    "services/presented_recovery_action_dispatch.py",
    "services/presented_recovery_actions.py",
)

RETIRED_EXECUTION_RUNTIME_MODULES = (
    "engine/live/account_classifier.py",
    "engine/live/account_custody_projection.py",
    "engine/live/account_custody_topology.py",
    "engine/live/account_epoch_observer.py",
    "engine/live/account_epoch_reconciliation.py",
    "engine/live/bar_adapter.py",
    "engine/live/clock_out.py",
    "engine/live/engine_runtime.py",
    "engine/live/fleet_reset_baseline.py",
    "engine/live/ibkr_broker_ownership_query.py",
    "engine/live/readiness.py",
    "engine/live/readiness_sidecar.py",
    "engine/live/reconciliation_orchestrator.py",
    "engine/live/runtime_producer.py",
    "engine/live/session_metadata.py",
    "engine/live/shadow_fill_simulator.py",
    "engine/live/submit_state_machine.py",
    "services/mutation_attempt.py",
)

RETIRED_DATA_PLANE_ROUTES = {
    ("POST", "/api/broker/orders"),
    ("DELETE", "/api/broker/orders/{order_id}"),
    ("POST", "/api/accounts/{account_id}/clerk/restore"),
    ("POST", "/api/accounts/{account_id}/gate-promotion/restart-smoke"),
    ("POST", "/api/accounts/{account_id}/presented-actions/recovery"),
    ("POST", "/api/accounts/{account_id}/journal-cures"),
    ("GET", "/api/accounts/{account_id}/journal-cures/preview"),
    ("POST", "/api/accounts/{account_id}/operator-recovery-flatten"),
    ("POST", "/api/accounts/{account_id}/emergency-flatten"),
    ("POST", "/api/accounts/{account_id}/binding-ledger/baseline"),
    ("POST", "/api/accounts/{account_id}/registry/backfill-false-crashes"),
}

RETIRED_OFFLINE_REPLAY_PREFIX = "/api/offline-replay"

# `/api/broker/account`, `/api/broker/positions`, `/api/broker/account-truth`,
# `/api/broker/orders/what-if`, `/api/broker/orders/completed`,
# `/api/accounts/{account_id}/reconciliation/latest`, and
# `/api/accounts/{account_id}/events` (IBKR account authority / Account
# Truth / reconciliation evidence) were retired by PR-A of #1813
# (2026-08-26) — see PRD #1817. They are intentionally absent from this set;
# do not re-add them.
PRESERVED_IBKR_READ_ROUTES = {
    ("GET", "/api/broker/health"),
    ("GET", "/api/broker/capability"),
    ("POST", "/api/broker/capability/probe"),
    ("GET", "/api/broker/ibkr/evidence"),
    ("GET", "/api/broker/ibkr/evidence/stream"),
    ("GET", "/api/broker/bars/snapshot"),
    ("GET", "/api/broker/bars-5s/snapshot"),
    ("GET", "/api/accounts/{account_id}/transactions"),
}

# Read surfaces PR-B of #1813 (2026-08-27) retired outright: the open-order
# projection and order-event stream (with `broker/ibkr/orders.py`) and the
# broker session mirror (with `services/broker_session_mirror.py`). They
# moved out of PRESERVED_IBKR_READ_ROUTES into an absence assertion — a
# route that cannot be served cannot leak evidence.
RETIRED_IBKR_READ_ROUTES = {
    ("GET", "/api/broker/orders/open"),
    ("GET", "/api/broker/orders/stream"),
    ("GET", "/api/broker/session-mirror"),
    ("GET", "/api/broker/session-mirror/history"),
    ("GET", "/api/broker/diagnose"),
    ("GET", "/api/broker/symbols/search"),
    ("GET", "/api/broker/pnl/stream"),
    ("GET", "/api/broker/pnl/positions/stream"),
}

RETIRED_FRONTEND_MODULES = (
    "api/offline-replay.types.ts",
    "components/broker/broker-orders/broker-orders.component.html",
    "components/broker/broker-orders/broker-orders.component.scss",
    "components/broker/broker-orders/broker-orders.component.spec.ts",
    "components/broker/broker-orders/broker-orders.component.ts",
    "services/offline-replay.service.ts",
    "shared/directives/paper-only.directive.ts",
)

RETIRED_FRONTEND_ROUTE_LITERALS = (
    "/api/accounts/${accountId}/clerk/restore",
    "/api/accounts/${accountId}/journal-cures",
    "/api/accounts/${accountId}/journal-cures/preview",
    "/api/accounts/${accountId}/presented-actions/recovery",
    "/api/accounts/${accountId}/operator-recovery-flatten",
    "/api/accounts/${accountId}/emergency-flatten",
)

RETIRED_PYTHON_CALL_NAMES = {
    "place_paper_order",
    "cancel_paper_order",
    "placeOrder",
    "cancelOrder",
}


def _registered_methods_and_paths() -> set[tuple[str, str]]:
    return {(method, route.path) for route in app.routes if isinstance(route, APIRoute) for method in route.methods}


def _defined_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _defined_class_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _production_python_symbol_references() -> dict[str, set[str]]:
    references: dict[str, set[str]] = {}
    for path in APPLICATION_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id in RETIRED_PYTHON_CALL_NAMES
        }
        names.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in RETIRED_PYTHON_CALL_NAMES
        )
        if names:
            references[str(path.relative_to(APPLICATION_ROOT))] = names
    return references


def _openapi_methods_and_paths() -> set[tuple[str, str]]:
    contract = json.loads(OPENAPI_CONTRACT.read_text(encoding="utf-8"))
    return {
        (method.upper(), path)
        for path, path_item in contract["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }


def test_direct_ibkr_order_mutation_routes_and_modules_are_absent() -> None:
    registered = _registered_methods_and_paths()

    assert registered.isdisjoint(RETIRED_DATA_PLANE_ROUTES)
    assert registered.isdisjoint(RETIRED_IBKR_READ_ROUTES)
    assert not any(path.startswith(RETIRED_OFFLINE_REPLAY_PREFIX) for _method, path in registered)
    assert registered >= PRESERVED_IBKR_READ_ROUTES
    for relative_path in RETIRED_DIRECT_MUTATION_MODULES:
        assert not (APPLICATION_ROOT / relative_path).exists(), relative_path


def test_ibkr_order_projection_and_execution_modules_are_absent() -> None:
    """Successor to the scan that asserted `broker/ibkr/orders.py` defined
    no place/cancel primitive. PR-B of #1813 (2026-08-27) deleted the module
    and its whole order/P&L/persistence family, so the guarantee is now the
    stronger "there is no order module to define a primitive in"."""
    for relative_path in (
        "broker/ibkr/orders.py",
        "broker/ibkr/order_error_stream.py",
        "broker/ibkr/order_evidence.py",
        "broker/ibkr/order_history.py",
        "broker/ibkr/order_previews.py",
        "broker/ibkr/order_projection.py",
        "broker/ibkr/pnl.py",
        "broker/ibkr/persistence.py",
        "broker/ibkr/diagnostics.py",
        "broker/ibkr/symbol_search.py",
        "broker/safety_verdict.py",
    ):
        assert not (APPLICATION_ROOT / relative_path).exists(), relative_path


def test_production_python_has_no_ibkr_order_actuation_reference() -> None:
    assert _production_python_symbol_references() == {}


def test_account_clerk_order_actuation_runtime_is_absent() -> None:
    for relative_path in (
        *RETIRED_ACCOUNT_MUTATION_MODULES,
        *RETIRED_EXECUTION_RUNTIME_MODULES,
    ):
        assert not (APPLICATION_ROOT / relative_path).exists(), relative_path

    # This used to assert that `make_live_engine_verdict_provider` was not
    # among broker/runtime_snapshot.py's functions. PR-C of #1813 (2026-08-27)
    # deleted that module outright: it was built as the typed boundary for
    # live_instances.py's safety-verdict and connected-account reads, PR-B
    # retired both, and its own test was left as its only caller. A file that
    # does not exist cannot define the function, so this absence assertion
    # replaces the symbol assertion and is strictly stronger.
    assert not (APPLICATION_ROOT / "broker/runtime_snapshot.py").exists()

    # The scan for the host bridge's clerk-mutation route literals inside
    # host_daemon.py is superseded by the bridge's own absence — PR-B of
    # #1813 (2026-08-27) deleted the file, so no host process can serve
    # `/accounts/{account_id}/clerk/*` or `/emergency-flatten` at all.
    assert not (APPLICATION_ROOT / "engine/live/host_daemon.py").exists()


def test_historical_ibkr_evidence_modules_expose_no_writer_api() -> None:
    live_root = APPLICATION_ROOT / "engine/live"
    assert "AccountClerkJournal" not in _defined_class_names(live_root / "account_clerk_journal.py")
    retired_writers = {
        "account_artifacts.py": {
            "advance_account_clerk_generation",
            "write_account_clerk_lease",
            "write_account_owner_generation",
        },
        "account_binding_ledger.py": {
            "append_binding_decision",
            "append_binding_retirement_proposal",
            "baseline_binding_ledger_from_registry",
            "fold_binding_retirement_proposals",
        },
        "account_registry.py": {
            "backfill_false_crash_registry_rows",
            "baseline_account_binding_ledger",
            "fold_account_binding_retirements",
            "retire_soft_deleted_instance_bindings",
            "retire_unmanaged_active_bindings_on_daemon_boot",
            "write_account_instance_binding",
        },
        "run_status.py": {"write_run_status"},
    }
    for filename, names in retired_writers.items():
        assert _defined_function_names(live_root / filename).isdisjoint(names), filename

    # `intent_wal.py` carried the last of these writers (`append`). PR-B of
    # #1813 (2026-08-27) retired the module with the host bridge that fed
    # it, so its writer is absent by the file's absence.
    assert not (live_root / "intent_wal.py").exists()


def test_frontend_has_no_orphaned_ibkr_order_or_recovery_control() -> None:
    for relative_path in RETIRED_FRONTEND_MODULES:
        assert not (FRONTEND_APPLICATION_ROOT / relative_path).exists(), relative_path

    production_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in FRONTEND_APPLICATION_ROOT.rglob("*")
        if path.suffix in {".ts", ".html"} and not path.name.endswith(".spec.ts") and path != FRONTEND_CONTRACT
    )
    assert re.search(r"['\"]/api/broker/orders['\"]", production_sources) is None
    assert re.search(r"/api/broker/orders/\$\{", production_sources) is None
    for route_literal in RETIRED_FRONTEND_ROUTE_LITERALS:
        assert route_literal not in production_sources, route_literal
    for retired_wrapper in (
        "accountTruth(",
        "openOrders(",
        "completedOrders(",
        "orderWhatIf(",
    ):
        assert retired_wrapper not in production_sources, retired_wrapper


def test_generated_contracts_retire_mutations_and_preserve_ibkr_reads() -> None:
    openapi_methods_and_paths = _openapi_methods_and_paths()
    frontend_contract = FRONTEND_CONTRACT.read_text(encoding="utf-8")

    assert openapi_methods_and_paths.isdisjoint(RETIRED_DATA_PLANE_ROUTES)
    assert openapi_methods_and_paths.isdisjoint(RETIRED_IBKR_READ_ROUTES)
    assert not any(path.startswith(RETIRED_OFFLINE_REPLAY_PREFIX) for _method, path in openapi_methods_and_paths)
    assert openapi_methods_and_paths >= PRESERVED_IBKR_READ_ROUTES
    for _method, path in RETIRED_DATA_PLANE_ROUTES:
        assert f'"{path}"' not in frontend_contract, path
    assert RETIRED_OFFLINE_REPLAY_PREFIX not in frontend_contract
    for _method, path in PRESERVED_IBKR_READ_ROUTES:
        assert f'"{path}"' in frontend_contract, path


def test_presented_action_contract_is_retired() -> None:
    """The presented-action contract itself retired with account_safety_snapshot.py
    and app/schemas/presented_operator_action.py (PR-A of #1813, 2026-08-26) —
    there is no presented-action surface left that could carry an order
    effect, so the invariant this test names now holds by the schemas'
    absence rather than by inspecting their shape.
    """
    schemas = json.loads(OPENAPI_CONTRACT.read_text(encoding="utf-8"))["components"]["schemas"]

    for retired_schema in (
        "PresentedOperatorActionInvocation",
        "PresentedOperatorActionTarget",
        "PresentedOperatorActionResult",
        "PresentedOperatorAction",
        "PresentedOperatorActionEffectReceipt",
    ):
        assert retired_schema not in schemas
