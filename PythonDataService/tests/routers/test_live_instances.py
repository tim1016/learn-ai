"""Focused contracts for the retained live-run compatibility router."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routers import live_instances
from app.schemas.broker_bots import BotStatusView
from app.services.mutation_rung_receipts import accepted_mutation_receipts


def _route_paths() -> set[str]:
    return {route.path for route in live_instances.router.routes}


def test_deprecated_ibkr_control_routes_are_not_registered() -> None:
    paths = _route_paths()
    retired = {
        "/catalog",
        "/catalog/page",
        "/roll-call",
        "/{strategy_instance_id}",
        "/{strategy_instance_id}/active-dates",
        "/{strategy_instance_id}/activity",
        "/{strategy_instance_id}/chart-snapshot",
        "/{strategy_instance_id}/commands",
        "/{strategy_instance_id}/crash-recovery-override",
        "/{strategy_instance_id}/desired-state",
        "/{strategy_instance_id}/end-day-now",
        "/{strategy_instance_id}/flatten-and-pause",
        "/{strategy_instance_id}/lifecycle/roster",
        "/{strategy_instance_id}/operator-surface/stream",
        "/{strategy_instance_id}/reconcile",
        "/{strategy_instance_id}/reconcile-mutation",
        "/{strategy_instance_id}/retire-and-replace",
        "/{strategy_instance_id}/status",
    }

    assert paths.isdisjoint(retired)


def test_runner_and_diagnostic_boundaries_remain_registered() -> None:
    paths = _route_paths()

    assert {
        "",
        "/account",
        "/account-summary",
        "/audit-copy-sizing-lookup",
        "/daemon-diagnose",
        "/daemon-health",
        "/daemon-health/renew-lease",
        "/deploy-preflight",
        "/fleet/stream",
        "/preview-action-plan",
        "/qc-audit-copies",
        "/runs/{run_id}/start",
        "/runs/{run_id}/stop",
        "/{strategy_instance_id}/daemon-diagnose",
    } <= paths


@pytest.mark.asyncio
async def test_activated_sqlite_roster_skips_legacy_runner_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = BotStatusView(
        strategy_instance_id="bot-a",
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        running=True,
        phase="ON_DUTY",
        desired_state="RUNNING",
        active_run_id="run-a",
        duty_outcome=None,
        binding_created_at_ms=1_700_000_000_000,
        last_transition_at_ms=1_700_000_001_000,
    )
    monkeypatch.setattr(
        live_instances,
        "read_sqlite_roster_statuses",
        lambda broker: [status] if broker == "alpaca" else None,
    )

    def forbidden_scan(_root: Path):
        raise AssertionError("legacy runner artifacts must not be scanned after activation")

    monkeypatch.setattr(live_instances, "_visible_runs_by_instance", forbidden_scan)

    rows = await live_instances._build_live_instance_summaries(
        SimpleNamespace(live_runner_daemon_url="http://unused"),
        tmp_path,
        fleet_observation=SimpleNamespace(
            is_current=True,
            payload={
                "instances": [
                    {
                        "strategy_instance_id": "bot-a",
                        "run_id": "run-a",
                        "process": {"state": "running"},
                    }
                ]
            },
        ),
    )

    assert [row.strategy_instance_id for row in rows] == ["bot-a"]
    assert rows[0].process_state == "running"
    assert rows[0].bound_run_id == "run-a"


@pytest.mark.asyncio
async def test_activated_sqlite_roster_never_consults_registry_derived_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S5.2: the SQLite-active roster reports ephemeral process liveness only.

    ``_resolve_readiness`` derives its verdict from the legacy JSONL
    account-registry/readiness-sidecar tree — durable-identity reconstruction
    this slice retires from the active-SQLite path. The roster must report
    ``UNKNOWN`` from ``BotStatusView`` alone, never call it.
    """
    status = BotStatusView(
        strategy_instance_id="bot-a",
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        running=True,
        phase="ON_DUTY",
        desired_state="RUNNING",
        active_run_id="run-a",
        duty_outcome=None,
        binding_created_at_ms=1_700_000_000_000,
        last_transition_at_ms=1_700_000_001_000,
    )
    monkeypatch.setattr(
        live_instances,
        "read_sqlite_roster_statuses",
        lambda broker: [status] if broker == "alpaca" else None,
    )

    def forbidden_readiness(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("SQLite-active roster must not consult registry-derived readiness")

    monkeypatch.setattr(live_instances, "_resolve_readiness", forbidden_readiness)

    rows = await live_instances._build_live_instance_summaries(
        SimpleNamespace(live_runner_daemon_url="http://unused"),
        tmp_path,
        fleet_observation=SimpleNamespace(is_current=False, payload=None),
    )

    assert rows[0].readiness_verdict == "UNKNOWN"
    assert rows[0].readiness_as_of_ms == status.last_transition_at_ms


@pytest.mark.asyncio
async def test_sqlite_lifecycle_run_does_not_invent_a_live_process() -> None:
    status = BotStatusView(
        strategy_instance_id="bot-a",
        strategy_key="unknown",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=None,
        running=True,
        phase="ON_DUTY",
        desired_state="RUNNING",
        active_run_id="run-a",
        duty_outcome=None,
        binding_created_at_ms=1_700_000_000_000,
        last_transition_at_ms=1_700_000_001_000,
    )

    rows = await live_instances._sqlite_live_instance_summaries(
        SimpleNamespace(live_runner_daemon_url="http://unused"),
        [status],
        fleet_observation=SimpleNamespace(is_current=False, payload=None),
    )

    assert rows[0].process_state == "unreachable"
    assert rows[0].bound_run_id is None
    assert rows[0].latest_run_id == "run-a"


def test_retained_runner_receipt_does_not_rebuild_operator_surface() -> None:
    receipt, warnings = accepted_mutation_receipts(
        mutation_key="stop",
        occurred_at_ms=1_700_000_000_000,
    )

    assert receipt.code == "mutation.scoped_all_clear"
    assert receipt.title == "Stop accepted."
    assert warnings == []
