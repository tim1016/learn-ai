"""Outage-policy regressions for signed ordinary lifecycle actions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.engine.live.bot_lifecycle_evaluator import BotLifecycleEvaluator
from app.engine.live.desired_state import DesiredState
from app.schemas.presented_operator_action import PresentedOperatorActionInvocation, PresentedOperatorActionTarget
from app.services import presented_lifecycle_http
from app.services.account_directory import AccountDirectoryError
from app.services.presented_lifecycle_actions import (
    PresentedLifecycleActionRejectedError,
    PresentedLifecycleActionService,
)

_ACCOUNT_ID = "DU1234567"
_SID = "amd-outage-bot"
_NOW_MS = 1_780_000_000_000


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)


def _snapshot(verdict: str = "CLEAN") -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_id="a" * 64,
        snapshot_version="a" * 64,
        verdict=verdict,
        evidence_refs=(),
    )


def _invocation(action) -> PresentedOperatorActionInvocation:
    return PresentedOperatorActionInvocation(
        action_id=action.action_id,
        target=action.target,
        snapshot_id=action.snapshot_id,
        snapshot_version=action.snapshot_version,
        idempotency_key=action.idempotency_key,
        issued_at_ms=action.issued_at_ms,
        expires_at_ms=action.expires_at_ms,
        presentation_token=action.presentation_token,
    )


@pytest.mark.parametrize("action_id", ["pause", "stop", "end_day"])
def test_risk_reducing_actions_remain_available_when_the_next_safety_read_is_unavailable(
    action_id: str,
) -> None:
    service = PresentedLifecycleActionService(now_ms=lambda: _NOW_MS)
    target = PresentedOperatorActionTarget(account_id=_ACCOUNT_ID, strategy_instance_id=_SID)
    action = service.present(action_id=action_id, snapshot=_snapshot("UNAVAILABLE"), target=target)  # type: ignore[arg-type]

    validated = service.validate(
        action_id=action_id,  # type: ignore[arg-type]
        target=target,
        invocation=action.model_copy(update={"presentation_token": action.presentation_token}),
        snapshot=None,
    )

    assert action.availability == "AVAILABLE"
    assert validated.action_id == action_id
    assert validated.target == target


@pytest.mark.parametrize("action_id", ["resume", "start", "deploy"])
def test_risk_increasing_action_requires_current_clean_proof(action_id: str) -> None:
    service = PresentedLifecycleActionService(now_ms=lambda: _NOW_MS)
    target = PresentedOperatorActionTarget(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        run_id="run-1" if action_id == "start" else None,
    )
    action = service.present(action_id=action_id, snapshot=_snapshot(), target=target)  # type: ignore[arg-type]

    with pytest.raises(PresentedLifecycleActionRejectedError) as rejected:
        service.validate(
            action_id=action_id,  # type: ignore[arg-type]
            target=target,
            invocation=action.model_copy(update={"presentation_token": action.presentation_token}),
            snapshot=None,
        )

    assert rejected.value.reason_code == "ACTION_PROOF_UNAVAILABLE"


@pytest.mark.parametrize("action_id", ["resume", "start", "deploy"])
def test_risk_increasing_action_accepts_its_unchanged_clean_snapshot(action_id: str) -> None:
    service = PresentedLifecycleActionService(now_ms=lambda: _NOW_MS)
    target = PresentedOperatorActionTarget(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        run_id="run-1" if action_id == "start" else None,
    )
    snapshot = _snapshot()
    action = service.present(action_id=action_id, snapshot=snapshot, target=target)  # type: ignore[arg-type]

    validated = service.validate(
        action_id=action_id,  # type: ignore[arg-type]
        target=target,
        invocation=_invocation(action),
        snapshot=snapshot,
    )

    assert validated.idempotency_key == action.idempotency_key
    assert validated.snapshot_version == action.snapshot_version


def test_changed_snapshot_rejects_resume_before_side_effect() -> None:
    service = PresentedLifecycleActionService(now_ms=lambda: _NOW_MS)
    target = PresentedOperatorActionTarget(account_id=_ACCOUNT_ID, strategy_instance_id=_SID)
    action = service.present(action_id="resume", snapshot=_snapshot(), target=target)
    changed = SimpleNamespace(
        snapshot_id="b" * 64,
        snapshot_version="b" * 64,
        verdict="CLEAN",
        evidence_refs=(),
    )

    with pytest.raises(PresentedLifecycleActionRejectedError) as rejected:
        service.validate(
            action_id="resume",
            target=target,
            invocation=action.model_copy(update={"presentation_token": action.presentation_token}),
            snapshot=changed,
        )

    assert rejected.value.reason_code == "ACTION_SNAPSHOT_STALE"


def test_new_pause_presentation_is_not_blocked_after_resume_with_unchanged_evidence(tmp_path) -> None:
    service = PresentedLifecycleActionService(now_ms=lambda: _NOW_MS)
    target = PresentedOperatorActionTarget(account_id=_ACCOUNT_ID, strategy_instance_id=_SID)
    first_pause = service.present(action_id="pause", snapshot=_snapshot(), target=target)
    resume = service.present(action_id="resume", snapshot=_snapshot(), target=target)
    second_pause = service.present(action_id="pause", snapshot=_snapshot(), target=target)
    evaluator = BotLifecycleEvaluator(tmp_path, _SID)

    first = evaluator.set_desired_state(
        DesiredState.PAUSED,
        updated_by="operator",
        reason="Pause",
        now_ms=_NOW_MS,
        idempotency_key=first_pause.idempotency_key,
    )
    replay = evaluator.set_desired_state(
        DesiredState.PAUSED,
        updated_by="operator",
        reason="Pause",
        now_ms=_NOW_MS,
        idempotency_key=first_pause.idempotency_key,
    )
    evaluator.set_desired_state(
        DesiredState.RUNNING,
        updated_by="operator",
        reason="Resume",
        now_ms=_NOW_MS,
        idempotency_key=resume.idempotency_key,
    )
    second = evaluator.set_desired_state(
        DesiredState.PAUSED,
        updated_by="operator",
        reason="Pause",
        now_ms=_NOW_MS,
        idempotency_key=second_pause.idempotency_key,
    )

    assert first_pause.idempotency_key != second_pause.idempotency_key
    assert replay.replayed is True
    assert first.desired_state is not None and first.desired_state.desired_state is DesiredState.PAUSED
    assert second.desired_state is not None and second.desired_state.desired_state is DesiredState.PAUSED


def test_production_boundary_requires_a_presentation_before_any_lifecycle_write(tmp_path) -> None:
    with pytest.raises(HTTPException) as rejected:
        presented_lifecycle_http.require_presented_lifecycle_action(
            artifacts_root=tmp_path,
            account_id=_ACCOUNT_ID,
            strategy_instance_id=_SID,
            run_id=None,
            action_id="pause",
            invocation=None,
        )

    assert rejected.value.status_code == 409
    assert rejected.value.detail["reason_code"] == "ACTION_PRESENTATION_REQUIRED"


@pytest.mark.parametrize("action_id", ["pause", "stop", "end_day"])
def test_signed_risk_reducing_action_survives_an_account_snapshot_outage(
    action_id: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PresentedLifecycleActionService(now_ms=lambda: _NOW_MS)
    target = PresentedOperatorActionTarget(account_id=_ACCOUNT_ID, strategy_instance_id=_SID)
    action = service.present(action_id=action_id, snapshot=_snapshot("UNAVAILABLE"), target=target)  # type: ignore[arg-type]

    def unavailable_snapshot_service(*, artifacts_root):
        return SimpleNamespace(snapshot=lambda *, account_id: (_ for _ in ()).throw(AccountDirectoryError("outage")))

    monkeypatch.setattr(
        presented_lifecycle_http,
        "account_safety_snapshot_service",
        unavailable_snapshot_service,
    )
    monkeypatch.setattr(
        presented_lifecycle_http,
        "PresentedLifecycleActionService",
        lambda: service,
    )

    validated = presented_lifecycle_http.require_presented_lifecycle_action(
        artifacts_root=tmp_path,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_SID,
        run_id=None,
        action_id=action_id,  # type: ignore[arg-type]
        invocation=_invocation(action),
    )

    assert validated is not None
    assert validated.action_id == action_id
