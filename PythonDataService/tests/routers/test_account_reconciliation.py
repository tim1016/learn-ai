"""Router tests for account reconciliation read endpoints."""

from __future__ import annotations

from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import IbkrConnectionHealth
from app.engine.live.account_artifacts import account_artifacts_root
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


def _truth():
    return compose_account_truth(
        health=_health(),
        account_instance_bindings=[],
        account_recovery_state=AccountRecoveryState.clear("DU1234567"),
        account=None,
        positions_snapshot=None,
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
