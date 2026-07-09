"""Router tests for account reconciliation read endpoints."""

from __future__ import annotations

import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrConnectionHealth,
    IbkrPositionsSnapshot,
)
from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    account_artifacts_root,
    read_account_freeze,
    write_account_freeze,
)
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    latest_account_instance_binding,
    read_account_instance_registry,
    write_account_instance_binding,
)
from app.routers import account_reconciliation
from app.services.account_reconciliation import AccountReconciliationService


def _health() -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=True,
        account_id="DU1234567",
        is_paper=True,
        fetched_at_ms=1_780_000_000_000,
        connection_state="connected",
        last_transition_ms=1_780_000_000_000,
    )


def _account_summary() -> IbkrAccountSummary:
    return IbkrAccountSummary(
        account_id="DU1234567",
        is_paper=True,
        base_currency="USD",
        net_liquidation=100_000.0,
        buying_power=50_000.0,
        fetched_at_ms=1_780_000_000_000,
    )


def _positions_snapshot() -> IbkrPositionsSnapshot:
    return IbkrPositionsSnapshot(
        account_id="DU1234567",
        is_paper=True,
        positions=[],
        fetched_at_ms=1_780_000_000_400,
    )


def _truth():
    return compose_account_truth(
        health=_health(),
        account_instance_bindings=[],
        account_recovery_state=AccountRecoveryState.clear("DU1234567"),
        account=_account_summary(),
        positions_snapshot=_positions_snapshot(),
        open_orders=[],
        completed_orders=[],
        executions=[],
        generated_at_ms=1_780_000_001_000,
    )


async def test_latest_reconciliation_returns_404_when_missing(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/reconciliation/latest")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


async def test_triage_returns_latest_receipt(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/triage")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "DU1234567"
    assert body["account_reconciliation_receipt"]["receipt_id"] == receipt.receipt_id
    assert body["overall_gate_result"]["status"] == "pass"
    assert body["conditions"] == []


async def test_triage_returns_unknown_when_registry_is_corrupt(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    account_root = account_artifacts_root(tmp_path, "DU1234567")
    account_root.mkdir(parents=True, exist_ok=True)
    (account_root / "instance_registry.jsonl").write_text("{not-json\n", encoding="utf-8")
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/triage")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["overall_gate_result"]["status"] == "unknown"
    assert {row["gate_id"] for row in body["gate_rows"]} == {
        "account.reconciliation",
        "account.instance_registry",
    }


async def test_latest_reconciliation_normalizes_account_id(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/du1234567/reconciliation/latest")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "DU1234567"
    assert body["receipt_id"] == receipt.receipt_id


async def test_triage_contract_returns_condition_rows_for_active_freeze(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="CHECK_IBKR",
        ),
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/triage")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["overall_gate_result"]["status"] == "freeze"
    assert body["clear_freeze_actionable"] is False
    assert body["conditions"] == [
        {
            "condition_type": "exposure_freeze",
            "scope": "account",
            "owner": {
                "owner_type": "account",
                "owner_id": "DU1234567",
                "label": "Account DU1234567",
                "strategy_instance_id": None,
                "run_id": None,
                "lifecycle_state": None,
            },
            "severity": "critical",
            "title": "Account freeze active",
            "detail": "watchdog.flatten_timed_out",
            "operator_next_step": "CHECK_IBKR",
            "source": "watchdog_halt_executor",
            "evidence_at_ms": 1_780_000_002_500,
            "evidence_refs": [],
            "affected_strategy_instance_ids": [],
            "cure_action": "resolve_exposure",
        }
    ]


async def test_clear_freeze_endpoint_refuses_stale_pre_freeze_receipt(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_002_500,
            operator_next_step="CHECK_IBKR",
        ),
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/freeze/clear",
                json={"requested_by": "operator", "receipt_id": receipt.receipt_id},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "newer than the active freeze" in response.json()["detail"]
    assert read_account_freeze(tmp_path, "DU1234567") is not None


async def test_clear_freeze_endpoint_returns_refreshed_triage_after_success(tmp_path: Path) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_001_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    receipt = service.write_receipt(
        requested_account_id="DU1234567",
        account_truth=_truth(),
        now_ms=1_780_000_002_000,
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/freeze/clear",
                json={"requested_by": "operator", "receipt_id": receipt.receipt_id},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] is True
    assert body["receipt_id"] == receipt.receipt_id
    assert body["triage"]["overall_gate_result"]["status"] == "pass"
    assert body["triage"]["conditions"] == []
    assert read_account_freeze(tmp_path, "DU1234567") is None


async def test_accept_exposure_override_endpoint_returns_refreshed_triage(
    tmp_path: Path,
) -> None:
    from app.main import app

    service = AccountReconciliationService(artifacts_root=tmp_path, ttl_ms=10_000_000_000)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            freeze_kind="exposure",
            reason="watchdog.flatten_timed_out",
            source="watchdog_halt_executor",
            recorded_at_ms=1_780_000_001_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    app.dependency_overrides[
        account_reconciliation.get_account_reconciliation_service
    ] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/freeze/accept-exposure-override",
                json={
                    "requested_by": "operator",
                    "reason": "Operator accepts the current account exposure.",
                    "strategy_instance_id": "bot-a",
                    "run_id": "run-a",
                    "bot_order_namespace": "learn-ai/bot-a/v1",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] is True
    assert body["cleared_source"] == "account_audited_override"
    assert body["triage"]["overall_gate_result"]["status"] == "unknown"
    assert read_account_freeze(tmp_path, "DU1234567") is None


async def test_false_crash_backfill_endpoint_repairs_disproven_registry_row(
    tmp_path: Path,
) -> None:
    from app.main import app

    active = AccountInstanceBinding(
        account_id="DU1234567",
        strategy_instance_id="bot-a",
        run_id="run-halt",
        bot_order_namespace="learn-ai/bot-a/v1",
        lifecycle_state="ACTIVE",
        recorded_at_ms=1_780_000_001_000,
        source="host_daemon.start",
    )
    retired = active.model_copy(
        update={
            "lifecycle_state": "RETIRED",
            "recorded_at_ms": 1_780_000_002_000,
            "source": "host_daemon.process_crashed",
        }
    )
    write_account_instance_binding(tmp_path, active)
    write_account_instance_binding(tmp_path, retired)
    run_dir = tmp_path / "live_runs" / "run-halt"
    run_dir.mkdir(parents=True)
    (run_dir / "run_status.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": "run-halt",
                "started_at_ms": 1_780_000_001_000,
                "last_update_ms": 1_780_000_002_000,
                "ended_at_ms": 1_780_000_002_000,
                "exit_code": 1,
                "exit_reason": "fatal_halt",
            }
        ),
        encoding="utf-8",
    )
    app.dependency_overrides[
        account_reconciliation.get_account_artifacts_root
    ] = lambda: tmp_path
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/accounts/DU1234567/registry/backfill-false-crashes"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["rows_repaired"] == 1
    assert body["repaired_run_ids"] == ["run-halt"]
    repaired = latest_account_instance_binding(
        read_account_instance_registry(tmp_path, "DU1234567"),
        account_id="DU1234567",
        strategy_instance_id="bot-a",
    )
    assert repaired is not None
    assert repaired.source == "host_daemon.process_halted"
