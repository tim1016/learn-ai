"""HTTP boundary regressions for snapshot-bound recovery actions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.host_daemon_client import HostDaemonError
from app.routers import account_reconciliation
from app.services.presented_recovery_action_dispatch import known_host_recovery_rejection
from app.services.presented_recovery_action_presentation import present_recovery_actions
from app.services.presented_recovery_actions import PresentedRecoveryActionService

_ACCOUNT_ID = "DU1234567"
_NOW_MS = 1_780_000_000_000
_SNAPSHOT_ID = "a" * 64


def _flatten_intention_action():
    [action] = present_recovery_actions(
        account_id=_ACCOUNT_ID,
        snapshot_id=_SNAPSHOT_ID,
        generated_at_ms=_NOW_MS,
        verdict="RECONCILING",
        evidence_refs=(),
        reconciliation_receipt=None,
        custody_statuses=(),
        recovery_candidates=(),
        emergency_confirmation=None,
    )
    return action


def _invocation(action):
    return {
        "action_id": action.action_id,
        "target": action.target.model_dump(mode="json"),
        "snapshot_id": action.snapshot_id,
        "snapshot_version": action.snapshot_version,
        "idempotency_key": action.idempotency_key,
        "issued_at_ms": action.issued_at_ms,
        "expires_at_ms": action.expires_at_ms,
        "presentation_token": action.presentation_token,
        "confirmation_token": "FLATTEN",
    }


async def test_presented_flatten_without_current_proof_records_one_intention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.main import app
    from app.security.data_plane_control import CONTROL_SECRET_HEADER

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    action = _flatten_intention_action()
    snapshot = SimpleNamespace(
        actions=(action,), snapshot_id=action.snapshot_id, snapshot_version=action.snapshot_version
    )
    action_service = PresentedRecoveryActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    app.dependency_overrides[account_reconciliation.require_connected_client] = lambda: object()
    app.dependency_overrides[account_reconciliation.get_account_reconciliation_service] = lambda: object()
    app.dependency_overrides[account_reconciliation.get_account_safety_snapshot_service] = lambda: SimpleNamespace(
        snapshot=lambda **_kwargs: snapshot
    )
    app.dependency_overrides[account_reconciliation.get_presented_recovery_action_service] = (
        lambda: action_service
    )
    app.dependency_overrides[account_reconciliation.get_account_artifacts_root] = lambda: tmp_path
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={CONTROL_SECRET_HEADER: "test-control-secret"},
        ) as client:
            first = await client.post(
                f"/api/accounts/{_ACCOUNT_ID}/presented-actions/recovery", json=_invocation(action)
            )
            replay = await client.post(
                f"/api/accounts/{_ACCOUNT_ID}/presented-actions/recovery", json=_invocation(action)
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202
    assert first.json()["state"] == "PENDING_PROOF"
    assert first.json()["effect_receipt"]["kind"] == "FLATTEN_INTENTION_RECORDED"
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    assert len(list(tmp_path.rglob("presented_recovery_actions/*.json"))) == 1


async def test_missing_current_presentation_refuses_before_claiming_or_dispatching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.main import app
    from app.security.data_plane_control import CONTROL_SECRET_HEADER

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    action = _flatten_intention_action()
    current_snapshot = SimpleNamespace(actions=(), snapshot_id="d" * 64, snapshot_version="d" * 64)
    action_service = PresentedRecoveryActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    app.dependency_overrides[account_reconciliation.require_connected_client] = lambda: object()
    app.dependency_overrides[account_reconciliation.get_account_reconciliation_service] = lambda: object()
    app.dependency_overrides[account_reconciliation.get_account_safety_snapshot_service] = lambda: SimpleNamespace(
        snapshot=lambda **_kwargs: current_snapshot
    )
    app.dependency_overrides[account_reconciliation.get_presented_recovery_action_service] = (
        lambda: action_service
    )
    app.dependency_overrides[account_reconciliation.get_account_artifacts_root] = lambda: tmp_path
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={CONTROL_SECRET_HEADER: "test-control-secret"},
        ) as client:
            response = await client.post(
                f"/api/accounts/{_ACCOUNT_ID}/presented-actions/recovery", json=_invocation(action)
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "ACTION_NOT_PRESENTED"
    assert not list(tmp_path.rglob("presented_recovery_actions/*.json"))


def test_known_host_clerk_rejection_is_not_reclassified_as_an_unknown_outcome() -> None:
    rejection = known_host_recovery_rejection(
        HostDaemonError(
            409,
            {
                "reason_code": "CLERK_A0_CANCEL_TARGET_NOT_PENDING",
                "message": "Clerk rejected or could not complete the request.",
            },
        )
    )
    uncertain = known_host_recovery_rejection(
        HostDaemonError(
            409,
            {
                "reason_code": "ACCOUNT_CLERK_CANCEL_NAMESPACE_UNCERTAIN",
                "message": "Clerk outcome requires reconciliation.",
            },
        )
    )

    assert rejection is not None
    assert rejection.reason_code == "CLERK_A0_CANCEL_TARGET_NOT_PENDING"
    assert uncertain is None
