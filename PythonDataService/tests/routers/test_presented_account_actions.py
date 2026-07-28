"""HTTP boundary regressions for closed presented account actions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import IbkrAccountSummary, IbkrConnectionHealth, IbkrPositionsSnapshot
from app.routers import account_reconciliation
from app.schemas.account_safety_snapshot import PresentedOperatorActionResult
from app.services.account_reconciliation import AccountReconciliationService
from app.services.presented_account_actions import PresentedAccountActionService

_ACCOUNT_ID = "DU1234567"
_NOW_MS = 1_780_000_000_000


def _truth():
    return compose_account_truth(
        health=IbkrConnectionHealth(
            mode="paper",
            host="127.0.0.1",
            port=4002,
            client_id=7,
            connected=True,
            account_id=_ACCOUNT_ID,
            is_paper=True,
            fetched_at_ms=_NOW_MS,
            connection_state="connected",
            last_transition_ms=_NOW_MS,
        ),
        account_instance_bindings=[],
        account_recovery_state=AccountRecoveryState.clear(_ACCOUNT_ID),
        account=IbkrAccountSummary(
            account_id=_ACCOUNT_ID,
            is_paper=True,
            base_currency="USD",
            net_liquidation=100_000.0,
            buying_power=50_000.0,
            fetched_at_ms=_NOW_MS,
        ),
        positions_snapshot=IbkrPositionsSnapshot(
            account_id=_ACCOUNT_ID,
            is_paper=True,
            positions=[],
            fetched_at_ms=_NOW_MS,
        ),
        open_orders=[],
        completed_orders=[],
        executions=[],
        evidence_gaps=[],
        generated_at_ms=_NOW_MS,
    )


async def test_presented_reconcile_claims_once_and_returns_an_explicit_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.config import settings
    from app.main import app
    from app.security.data_plane_control import CONTROL_SECRET_HEADER

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    action = account_reconciliation.AccountSafetySnapshotService._presented_actions(
        account_id=_ACCOUNT_ID,
        snapshot_id="a" * 64,
        generated_at_ms=_NOW_MS,
        verdict="RECONCILING",
        reason_code="ACCOUNT_RECONCILIATION_REQUIRED",
        evidence_refs=(),
    )[0]
    reconciliation = AccountReconciliationService(artifacts_root=tmp_path)
    action_service = PresentedAccountActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    initial_snapshot = SimpleNamespace(
        actions=(action,), snapshot_id=action.snapshot_id, snapshot_version=action.snapshot_version
    )
    settled_snapshot = SimpleNamespace(actions=(), snapshot_id="c" * 64, snapshot_version="c" * 64)
    snapshot_reads = 0

    def snapshot(*, account_id: str):
        nonlocal snapshot_reads
        snapshot_reads += 1
        return initial_snapshot if snapshot_reads == 1 else settled_snapshot

    snapshot_service = SimpleNamespace(snapshot=snapshot)
    refresh_calls = 0

    async def ensure_account_clerk(*_args: object, **_kwargs: object) -> None:
        return None

    async def refresh_account_truth(*_args: object, **_kwargs: object):
        nonlocal refresh_calls
        refresh_calls += 1
        return _truth()

    monkeypatch.setattr(account_reconciliation.host_daemon_client, "ensure_account_clerk", ensure_account_clerk)
    monkeypatch.setattr(account_reconciliation, "refresh_account_truth_now", refresh_account_truth)
    monkeypatch.setattr(
        account_reconciliation,
        "get_settings",
        lambda: SimpleNamespace(live_runner_daemon_url="http://daemon"),
    )
    app.dependency_overrides[account_reconciliation.require_connected_client] = lambda: object()
    app.dependency_overrides[account_reconciliation.get_account_reconciliation_service] = lambda: reconciliation
    app.dependency_overrides[account_reconciliation.get_account_safety_snapshot_service] = lambda: snapshot_service
    app.dependency_overrides[account_reconciliation.get_presented_account_action_service] = lambda: action_service
    payload = {
        "action_id": action.action_id,
        "target": action.target.model_dump(),
        "snapshot_id": action.snapshot_id,
        "snapshot_version": action.snapshot_version,
        "idempotency_key": action.idempotency_key,
        "issued_at_ms": action.issued_at_ms,
        "expires_at_ms": action.expires_at_ms,
        "presentation_token": action.presentation_token,
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={CONTROL_SECRET_HEADER: "test-control-secret"},
        ) as client:
            first = await client.post(
                f"/api/accounts/{_ACCOUNT_ID}/presented-actions/reconcile-now", json=payload
            )
            replay = await client.post(
                f"/api/accounts/{_ACCOUNT_ID}/presented-actions/reconcile-now", json=payload
            )
            stale = await client.post(
                f"/api/accounts/{_ACCOUNT_ID}/presented-actions/reconcile-now",
                json={**payload, "snapshot_version": "d" * 64},
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert first.json()["state"] == "ACCEPTED"
    assert first.json()["replayed"] is False
    assert first.json()["refreshed_snapshot_id"] == settled_snapshot.snapshot_id
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "reason_code": "ACTION_ENVELOPE_MISMATCH",
        "message": "The action envelope does not match the server presentation.",
        "snapshot_id": settled_snapshot.snapshot_id,
        "snapshot_version": settled_snapshot.snapshot_version,
    }
    assert refresh_calls == 1
    operation = app.openapi()["paths"][
        "/api/accounts/{account_id}/presented-actions/reconcile-now"
    ]["post"]
    assert operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PresentedOperatorActionResult"
    }
    assert operation["responses"]["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PresentedOperatorActionRejectionResponse"
    }


async def test_replayed_unknown_attempt_returns_accepted_for_reconciliation(
    monkeypatch,
) -> None:
    from app.config import settings
    from app.main import app
    from app.security.data_plane_control import CONTROL_SECRET_HEADER

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    prior_result = PresentedOperatorActionResult(
        action_id="reconcile_now",
        action_attempt_id="b" * 64,
        state="OUTCOME_UNKNOWN",
        replayed=True,
        finished_copy="The action outcome is not proven.",
        reconciliation_receipt=None,
        refreshed_snapshot_id=None,
    )
    action_service = SimpleNamespace(replay_if_claimed=lambda _request: prior_result)
    app.dependency_overrides[account_reconciliation.require_connected_client] = lambda: object()
    app.dependency_overrides[account_reconciliation.get_account_reconciliation_service] = lambda: object()
    app.dependency_overrides[account_reconciliation.get_account_safety_snapshot_service] = lambda: object()
    app.dependency_overrides[account_reconciliation.get_presented_account_action_service] = lambda: action_service
    payload = {
        "action_id": "reconcile_now",
        "target": {"account_id": _ACCOUNT_ID},
        "snapshot_id": "a" * 64,
        "snapshot_version": "a" * 64,
        "idempotency_key": "b" * 64,
        "issued_at_ms": _NOW_MS,
        "expires_at_ms": _NOW_MS + 60_000,
        "presentation_token": "c" * 64,
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={CONTROL_SECRET_HEADER: "test-control-secret"},
        ) as client:
            response = await client.post(
                f"/api/accounts/{_ACCOUNT_ID}/presented-actions/reconcile-now",
                json=payload,
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["state"] == "OUTCOME_UNKNOWN"
    assert response.json()["replayed"] is True
