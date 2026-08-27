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

RETIRED_HOST_MUTATION_PATHS = {
    "/accounts/{account_id}/clerk/ensure",
    "/accounts/{account_id}/clerk/operator-adjustment",
    "/accounts/{account_id}/clerk/operator-recovery-flatten",
    "/accounts/{account_id}/clerk/operator-pending-cancel",
    "/accounts/{account_id}/clerk/operator-exact-cancel",
    "/accounts/{account_id}/clerk/authorize-emergency-flatten",
    "/accounts/{account_id}/clerk/release",
    "/accounts/{account_id}/emergency-flatten",
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
    ("GET", "/api/broker/orders/open"),
    ("GET", "/api/broker/orders/stream"),
    ("GET", "/api/broker/ibkr/evidence"),
    ("GET", "/api/broker/ibkr/evidence/stream"),
    ("GET", "/api/broker/bars/snapshot"),
    ("GET", "/api/broker/bars-5s/snapshot"),
    ("GET", "/api/broker/session-mirror"),
    ("GET", "/api/broker/session-mirror/history"),
    ("GET", "/api/accounts/{account_id}/transactions"),
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
    assert not any(path.startswith(RETIRED_OFFLINE_REPLAY_PREFIX) for _method, path in registered)
    assert registered >= PRESERVED_IBKR_READ_ROUTES
    for relative_path in RETIRED_DIRECT_MUTATION_MODULES:
        assert not (APPLICATION_ROOT / relative_path).exists(), relative_path


def test_ibkr_order_projection_has_no_place_or_cancel_primitive() -> None:
    defined_names = _defined_function_names(APPLICATION_ROOT / "broker/ibkr/orders.py")

    assert defined_names.isdisjoint({"place_paper_order", "cancel_paper_order"})


def test_production_python_has_no_ibkr_order_actuation_reference() -> None:
    assert _production_python_symbol_references() == {}


def test_account_clerk_order_actuation_runtime_is_absent() -> None:
    for relative_path in (
        *RETIRED_ACCOUNT_MUTATION_MODULES,
        *RETIRED_EXECUTION_RUNTIME_MODULES,
    ):
        assert not (APPLICATION_ROOT / relative_path).exists(), relative_path

    runtime_snapshot_functions = _defined_function_names(APPLICATION_ROOT / "broker/runtime_snapshot.py")
    assert "make_live_engine_verdict_provider" not in runtime_snapshot_functions

    host_source = (APPLICATION_ROOT / "engine/live/host_daemon.py").read_text(encoding="utf-8")
    for route_path in RETIRED_HOST_MUTATION_PATHS:
        assert route_path not in host_source, route_path


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
        "intent_wal.py": {"append"},
        "run_status.py": {"write_run_status"},
    }
    for filename, names in retired_writers.items():
        assert _defined_function_names(live_root / filename).isdisjoint(names), filename


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
    assert not any(path.startswith(RETIRED_OFFLINE_REPLAY_PREFIX) for _method, path in openapi_methods_and_paths)
    assert openapi_methods_and_paths >= PRESERVED_IBKR_READ_ROUTES
    for _method, path in RETIRED_DATA_PLANE_ROUTES:
        assert f'"{path}"' not in frontend_contract, path
    assert RETIRED_OFFLINE_REPLAY_PREFIX not in frontend_contract
    for _method, path in PRESERVED_IBKR_READ_ROUTES:
        assert f'"{path}"' in frontend_contract, path


def test_presented_action_contract_is_reconciliation_only() -> None:
    schemas = json.loads(OPENAPI_CONTRACT.read_text(encoding="utf-8"))["components"]["schemas"]
    invocation = schemas["PresentedOperatorActionInvocation"]
    target = schemas["PresentedOperatorActionTarget"]
    result = schemas["PresentedOperatorActionResult"]
    action = schemas["PresentedOperatorAction"]

    assert invocation["properties"]["action_id"]["const"] == "reconcile_now"
    assert set(target["properties"]) == {"account_id"}
    assert target["required"] == ["account_id"]
    assert "confirmation_token" not in invocation["properties"]
    assert "effect_receipt" not in result["properties"]
    assert action["properties"]["effect_class"]["const"] == "EVIDENCE_REFRESH"
    assert "PresentedOperatorActionEffectReceipt" not in schemas
