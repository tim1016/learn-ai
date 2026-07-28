"""HTTP contract tests for the AccountSafetySnapshot read endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.routers import account_reconciliation
from app.services.account_directory import AccountDirectoryService, CurrentBrokerAccount
from app.services.account_safety_snapshot import AccountSafetySnapshotService
from app.services.account_truth_snapshot import get_account_truth_snapshot_provider


async def test_account_safety_snapshot_endpoint_is_broker_free_and_uses_int64_ms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import app

    get_account_truth_snapshot_provider().clear()
    now_ms = 1_780_000_000_000
    directory = AccountDirectoryService(
        artifacts_root=tmp_path,
        current_account=CurrentBrokerAccount(account_id="DU1234567", is_paper=True),
        now_ms=lambda: now_ms,
    )
    service = AccountSafetySnapshotService(
        artifacts_root=tmp_path,
        directory=directory,
        now_ms=lambda: now_ms,
    )
    async def broker_call_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the safety-snapshot read must not call the broker")

    monkeypatch.setattr(account_reconciliation.ibkr_account, "fetch_positions", broker_call_forbidden)
    monkeypatch.setattr(account_reconciliation, "refresh_account_truth_now", broker_call_forbidden)
    app.dependency_overrides[account_reconciliation.get_account_safety_snapshot_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/accounts/DU1234567/safety-snapshot")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert len(body["snapshot_version"]) == 64
    assert body["generated_at_ms"] == now_ms
    assert body["posture"] == "PAPER_EXECUTION"
    assert body["verdict"] == "UNAVAILABLE"
    assert body["verdict_reason"]
    assert body["actions"][0]["action_id"] == "reconcile_now"
    assert body["actions"][0]["availability"] == "UNAVAILABLE"
    assert body["actions"][0]["disposition"] == "wait"
    assert all("critical" in source for source in body["sources"])
