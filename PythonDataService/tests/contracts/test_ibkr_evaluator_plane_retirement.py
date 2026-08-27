"""Structural contract for ADR 0038's evaluator-plane retirement.

Module *absence* for this retirement is asserted by
``tests/structural/test_ibkr_feed_boundary.py::EARLIER_RETIRED_MODULES``,
which is the single home for "retirement X deleted module M" across all
three retirement programmes. PR-C of #1813 moved the 33 dotted paths and
their by-path twins there: this file's own scanner recorded only
``f"{node.module}.{alias.name}"`` for an ``ImportFrom`` and ignored
relative imports, so ``from app.services.bot_control_plane import x``
was invisible to it. What stays here is what only this file can assert —
route registration, the committed OpenAPI shape, and the non-module
artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
APPLICATION_ROOT = REPOSITORY_ROOT / "PythonDataService" / "app"
OPENAPI_CONTRACT = REPOSITORY_ROOT / "contracts/openapi/python-data-service.openapi.json"

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


def test_retired_evaluator_plane_non_module_artifacts_are_absent() -> None:
    """The two retired artifacts the module guard cannot express.

    ``EARLIER_RETIRED_MODULES`` pins dotted ``app.*`` modules; neither of
    these is one — a JSON snapshot inside ``app/`` and a script outside it.
    """
    assert not (APPLICATION_ROOT / "engine/live/action_plan_deploy_readiness.snapshot.json").exists()
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
    # ``app.engine.live.run_ledger``'s absence is pinned by
    # ``EARLIER_RETIRED_MODULES``; what only this test can assert is the shape
    # of the read-only reader that replaced it.
    source = (APPLICATION_ROOT / "engine/live/historical_run_identity.py").read_text(
        encoding="utf-8"
    )

    assert "def read_historical_strategy_instance_id" in source
    assert "def build_ledger" not in source
    assert "def write_ledger" not in source
    assert "def compute_run_id" not in source


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
