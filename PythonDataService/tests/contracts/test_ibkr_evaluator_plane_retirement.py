"""Structural contract for ADR 0038's evaluator-plane retirement."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
APPLICATION_ROOT = REPOSITORY_ROOT / "PythonDataService" / "app"
OPENAPI_CONTRACT = REPOSITORY_ROOT / "contracts/openapi/python-data-service.openapi.json"

RETIRED_RUNTIME_PATHS = (
    "engine/live/bot_lifecycle_evaluator.py",
    "engine/live/bot_lifecycle_fence.py",
    "engine/live/lifecycle_exit_finalizer.py",
    "engine/live/deploy.py",
    "engine/live/pre_flight.py",
    "engine/live/run.py",
    "engine/live/account_recovery_cli.py",
    "engine/live/action_plan_deploy_readiness.snapshot.json",
    "engine/live/daemon_connectivity_monitor.py",
    "engine/live/child_watchdog.py",
    "engine/live/orphan_classifier.py",
    "engine/live/process_registry.py",
    "engine/live/host_daemon_bot_events.py",
    "engine/live/watchdog_controller.py",
    "engine/live/engine_runtime_publisher.py",
    "engine/live/post_halt_gate.py",
    "engine/live/run_logging.py",
    "engine/live/signal_tone.py",
    "operator/incidents/watchdog_notices.py",
    "operator/notices/runtime_freshness.py",
    "schemas/account_recovery.py",
    "services/bot_control_plane.py",
    "services/bot_deletion.py",
    "services/deploy_admission.py",
    "services/deploy_preflight.py",
    "services/ibkr_lifecycle_guard.py",
    "services/risk_reducing_lifecycle_intent.py",
    "services/resume_guard_state.py",
    "services/account_crash_recovery.py",
    "services/bot_lifecycle_receipt_copy.py",
    "services/bot_roll_call.py",
    "services/runtime_freshness.py",
    "services/account_fleet_read_context.py",
    "services/activity_lifecycle_consistency.py",
)

RETIRED_APPLICATION_MODULES = (
    "app.engine.live.bot_lifecycle_evaluator",
    "app.engine.live.bot_lifecycle_fence",
    "app.engine.live.lifecycle_exit_finalizer",
    "app.engine.live.deploy",
    "app.engine.live.pre_flight",
    "app.engine.live.run",
    "app.engine.live.account_recovery_cli",
    "app.engine.live.daemon_connectivity_monitor",
    "app.engine.live.child_watchdog",
    "app.engine.live.orphan_classifier",
    "app.engine.live.process_registry",
    "app.engine.live.host_daemon_bot_events",
    "app.engine.live.watchdog_controller",
    "app.engine.live.engine_runtime_publisher",
    "app.engine.live.post_halt_gate",
    "app.engine.live.run_logging",
    "app.engine.live.signal_tone",
    "app.operator.incidents.watchdog_notices",
    "app.operator.notices.runtime_freshness",
    "app.schemas.account_recovery",
    "app.services.bot_control_plane",
    "app.services.bot_deletion",
    "app.services.deploy_admission",
    "app.services.deploy_preflight",
    "app.services.ibkr_lifecycle_guard",
    "app.services.risk_reducing_lifecycle_intent",
    "app.services.resume_guard_state",
    "app.services.account_crash_recovery",
    "app.services.bot_lifecycle_receipt_copy",
    "app.services.bot_roll_call",
    "app.services.runtime_freshness",
    "app.services.account_fleet_read_context",
    "app.services.activity_lifecycle_consistency",
)

RETIRED_DATA_PLANE_ROUTES = {
    ("POST", "/api/live-instances"),
    ("POST", "/api/live-instances/preview-action-plan"),
    ("POST", "/api/live-instances/runs/{run_id}/start"),
    ("POST", "/api/live-instances/runs/{run_id}/stop"),
    ("GET", "/api/live-instances/daemon-diagnose"),
    ("GET", "/api/live-instances/{strategy_instance_id}/daemon-diagnose"),
    # PR-B of #1813 (2026-08-27) retired the last four survivors of the
    # ``/api/live-instances`` prefix along with the host bridge and the
    # broker-activity publisher. The prefix is now empty; the assertion
    # below pins that rather than pinning a shrinking allow-list.
    ("GET", "/api/live-instances/daemon-health"),
    ("POST", "/api/live-instances/daemon-health/renew-lease"),
    ("GET", "/api/live-instances/{strategy_instance_id}/broker-activity"),
    ("GET", "/api/live-instances/{strategy_instance_id}/broker-activity/stream"),
    ("GET", "/api/accounts/{account_id}/presented-lifecycle-actions/{action_id}"),
    ("POST", "/api/accounts/{account_id}/bindings/retire"),
    ("POST", "/api/accounts/{account_id}/legacy-stale-claims/candidates"),
    ("POST", "/api/accounts/{account_id}/legacy-stale-claims/retire"),
}

PRESERVED_ROUTES = {
    # Alpaca V2 remains the sole bot-control product.
    ("POST", "/api/brokers/{broker}/accounts/{account_id}/bots"),
    ("POST", "/api/brokers/{broker}/accounts/{account_id}/bots/{sid}/actions"),
    ("GET", "/api/brokers/{broker}/accounts/{account_id}/bots/{sid}/panel"),
    # IBKR remains a read-only market-data/capability source.
    # `/api/broker/account`, `/api/broker/positions`, and
    # `/api/broker/orders/completed` (account authority / Account Truth
    # evidence) were retired by PR-A of #1813 (2026-08-26);
    # `/api/broker/orders/open` (the open-order projection) was retired by
    # PR-B of #1813 (2026-08-27) with `app/broker/ibkr/orders.py`.
    ("GET", "/api/broker/capability"),
    ("GET", "/api/broker/bars/snapshot"),
}


def _registered_methods_and_paths() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def _application_imports() -> set[str]:
    imports: set[str] = set()
    for path in APPLICATION_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.update(f"{node.module}.{alias.name}" for alias in node.names)
    return imports


def test_evaluator_authority_modules_and_imports_are_absent() -> None:
    for relative_path in RETIRED_RUNTIME_PATHS:
        assert not (APPLICATION_ROOT / relative_path).exists(), relative_path

    application_imports = _application_imports()
    for retired_module in RETIRED_APPLICATION_MODULES:
        assert retired_module not in application_imports


def test_retired_eight_bot_launcher_is_absent() -> None:
    assert not (
        REPOSITORY_ROOT / "PythonDataService/scripts/launch_eight_bot_paper_run.py"
    ).exists()


def test_ibkr_bot_control_routes_are_unregistered() -> None:
    registered = _registered_methods_and_paths()

    assert registered.isdisjoint(RETIRED_DATA_PLANE_ROUTES)
    assert not {
        item for item in registered if item[1].startswith("/api/live-instances")
    }


def test_legacy_ledger_parser_is_replaced_by_a_read_only_identity_reader() -> None:
    assert not (APPLICATION_ROOT / "engine/live/run_ledger.py").exists()

    source = (APPLICATION_ROOT / "engine/live/historical_run_identity.py").read_text(
        encoding="utf-8"
    )

    assert "def read_historical_strategy_instance_id" in source
    assert "def build_ledger" not in source
    assert "def write_ledger" not in source
    assert "def compute_run_id" not in source


def test_account_capability_host_and_its_runtime_are_absent() -> None:
    """Successor to the source-scan that asserted the host bridge could not
    reach the bot runner. PR-B of #1813 (2026-08-27) deleted the bridge
    itself, so the guarantee is now the strictly stronger "no host bridge
    exists at all" — there is no source left to scan for a `/runs/` or
    `/deploy` route literal."""
    for relative_path in (
        "engine/live/host_daemon.py",
        "engine/live/host_daemon_client.py",
        "engine/live/daemon_auth.py",
        "engine/live/daemon_transport.py",
        "engine/live/host_runner_policy.py",
        "engine/live/control_plane.py",
        "engine/live/command_channel.py",
        "engine/live/live_engine.py",
        "engine/live/live_portfolio.py",
        "services/host_capability.py",
    ):
        assert not (APPLICATION_ROOT / relative_path).exists(), relative_path


def test_alpaca_control_and_ibkr_read_evidence_routes_remain_registered() -> None:
    registered = _registered_methods_and_paths()

    assert registered >= PRESERVED_ROUTES


def test_committed_openapi_contract_excludes_evaluator_control_plane() -> None:
    contract = json.loads(OPENAPI_CONTRACT.read_text(encoding="utf-8"))
    live_instance_operations = {
        (method.upper(), path)
        for path, path_item in contract["paths"].items()
        if path.startswith("/api/live-instances")
        for method in path_item
    }

    assert live_instance_operations == set()
    schemas = contract["components"]["schemas"]
    assert schemas.keys().isdisjoint(
        {
            "DaemonDiagnosticsSnapshot",
            "ActivityReconciliationWarning",
            "EndDayIntentResponse",
            "FleetRosterSnapshot",
            "HostRunnerActionResponse",
            "HostRunnerDeployRequest",
            "HostRunnerDeployResponse",
            "HostRunnerStartRequest",
            "HostRunnerStopRequest",
            "LiveInstanceSummary",
            "SetInstanceDesiredStateResponse",
        }
    )
