"""HTTP boundary regressions for snapshot-bound recovery actions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.host_daemon_client import HostDaemonError
from app.routers import account_reconciliation
from app.schemas.presented_operator_action import PresentedOperatorActionTarget
from app.services.presented_recovery_action_dispatch import (
    _post_effect_proven,
    known_host_recovery_rejection,
)
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


async def test_presented_flatten_intention_does_not_require_a_connected_data_plane_broker(
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

    def no_data_plane_broker() -> object:
        raise AssertionError("an intention-only recovery action must not request the data-plane broker")

    monkeypatch.setattr(account_reconciliation, "require_connected_client", no_data_plane_broker)
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
            response = await client.post(
                f"/api/accounts/{_ACCOUNT_ID}/presented-actions/recovery", json=_invocation(action)
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["state"] == "PENDING_PROOF"


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


async def test_abandoned_claim_is_returned_as_unknown_before_a_missing_presentation_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.main import app
    from app.security.data_plane_control import CONTROL_SECRET_HEADER

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    action = _flatten_intention_action()
    action_service = PresentedRecoveryActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)

    async def crash_after_claim() -> object:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await action_service.execute(
            action=action,
            invocation=account_reconciliation.PresentedOperatorActionInvocation.model_validate(_invocation(action)),
            invoke=crash_after_claim,
        )

    def current_presentation_must_not_be_read_for_an_abandoned_claim(**_kwargs: object) -> object:
        raise AssertionError("the abandoned claim must settle before a current-presentation lookup")

    app.dependency_overrides[account_reconciliation.get_account_reconciliation_service] = lambda: object()
    app.dependency_overrides[account_reconciliation.get_account_safety_snapshot_service] = lambda: SimpleNamespace(
        snapshot=current_presentation_must_not_be_read_for_an_abandoned_claim
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

    assert response.status_code == 202
    assert response.json()["state"] == "OUTCOME_UNKNOWN"
    assert response.json()["replayed"] is False


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
    ambiguous_clerk_rejection = known_host_recovery_rejection(
        HostDaemonError(
            409,
            {
                "reason_code": "CLERK_RECOVERY_BATCH_PARTIALLY_ACKNOWLEDGED",
                "message": "Some broker effects may already have completed.",
            },
        )
    )

    assert rejection is not None
    assert rejection.reason_code == "CLERK_A0_CANCEL_TARGET_NOT_PENDING"
    assert uncertain is None
    assert ambiguous_clerk_rejection is None

    for reason_code in ("ACCOUNT_CLERK_PROTOCOL_ERROR:MALFORMED_RESPONSE", "ACCOUNT_CLERK_INTERNAL_ERROR"):
        assert known_host_recovery_rejection(
            HostDaemonError(409, {"reason_code": reason_code, "message": "Response was not provable."})
        ) is None


def test_account_flat_proof_ignores_zero_quantity_broker_rows() -> None:
    receipt = SimpleNamespace(
        account_truth=SimpleNamespace(
            positions=[SimpleNamespace(quantity=0)],
            orders=[],
        )
    )

    assert _post_effect_proven(
        receipt=receipt,
        target=PresentedOperatorActionTarget(account_id=_ACCOUNT_ID, kind="ACCOUNT_EMERGENCY"),
        expected="ACCOUNT_FLAT",
    )
