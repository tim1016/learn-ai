"""Regression matrix for the closed, snapshot-bound reconciliation action."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.broker.ibkr.account_recovery import AccountRecoveryState
from app.broker.ibkr.account_truth import compose_account_truth
from app.broker.ibkr.models import IbkrAccountSummary, IbkrConnectionHealth, IbkrPositionsSnapshot
from app.config import settings
from app.schemas.account_reconciliation import AccountReconciliationReceipt
from app.schemas.account_safety_snapshot import (
    PresentedOperatorAction,
    PresentedOperatorActionInvocation,
    PresentedOperatorActionPrecondition,
    PresentedOperatorActionTarget,
    has_valid_presented_operator_action_token,
    issue_presented_operator_action_token,
)
from app.schemas.operator_blocker import OperatorConfirmationCopy
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_safety_snapshot import AccountSafetySnapshotService
from app.services.presented_account_actions import (
    PresentedAccountActionService,
    PresentedActionOutcomeUnknownError,
    PresentedActionRejectedError,
)

_ACCOUNT_ID = "DU1234567"
_NOW_MS = 1_780_000_000_000
_SNAPSHOT_ID = "a" * 64
_ACTION_KEY = "b" * 64


@pytest.fixture(autouse=True)
def _configured_presented_action_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)


def _action(
    *,
    issued_at_ms: int = _NOW_MS,
    expires_at_ms: int = _NOW_MS + 60_000,
) -> PresentedOperatorAction:
    target = PresentedOperatorActionTarget(account_id=_ACCOUNT_ID)
    return PresentedOperatorAction(
        action_id="reconcile_now",
        target=target,
        snapshot_id=_SNAPSHOT_ID,
        snapshot_version=_SNAPSHOT_ID,
        effect_class="EVIDENCE_REFRESH",
        idempotency_key=_ACTION_KEY,
        issued_at_ms=issued_at_ms,
        expires_at_ms=expires_at_ms,
        presentation_token=issue_presented_operator_action_token(
            action_id="reconcile_now",
            target=target,
            snapshot_id=_SNAPSHOT_ID,
            snapshot_version=_SNAPSHOT_ID,
            idempotency_key=_ACTION_KEY,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
        ),
        preconditions=(
            PresentedOperatorActionPrecondition(
                code="account_safety.verdict", expected_value="RECONCILING"
            ),
        ),
        confirmation=OperatorConfirmationCopy(
            title="Run account reconciliation",
            body="Request a fresh account reconciliation for this account.",
            consequence="The server will revalidate this snapshot before it refreshes account evidence.",
            confirm_label="Run account reconcile",
        ),
        availability="AVAILABLE",
        disposition="fix_here",
        finished_copy="Reconciliation request accepted. Refresh account safety evidence for its observed result.",
    )


def _invocation(
    action: PresentedOperatorAction,
    *,
    account_id: str = _ACCOUNT_ID,
    snapshot_id: str | None = None,
    snapshot_version: str | None = None,
) -> PresentedOperatorActionInvocation:
    target = PresentedOperatorActionTarget(account_id=account_id)
    invocation_snapshot_id = action.snapshot_id if snapshot_id is None else snapshot_id
    invocation_snapshot_version = action.snapshot_version if snapshot_version is None else snapshot_version
    return PresentedOperatorActionInvocation(
        action_id=action.action_id,
        target=target,
        snapshot_id=invocation_snapshot_id,
        snapshot_version=invocation_snapshot_version,
        idempotency_key=action.idempotency_key,
        issued_at_ms=action.issued_at_ms,
        expires_at_ms=action.expires_at_ms,
        presentation_token=issue_presented_operator_action_token(
            action_id=action.action_id,
            target=target,
            snapshot_id=invocation_snapshot_id,
            snapshot_version=invocation_snapshot_version,
            idempotency_key=action.idempotency_key,
            issued_at_ms=action.issued_at_ms,
            expires_at_ms=action.expires_at_ms,
        ),
    )


def _receipt(root: Path) -> AccountReconciliationReceipt:
    truth = compose_account_truth(
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
    return AccountReconciliationService(artifacts_root=root).write_receipt(
        requested_account_id=_ACCOUNT_ID,
        account_truth=truth,
        now_ms=_NOW_MS,
    )


def test_presentation_token_uses_the_shared_configured_secret_across_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    invocation = _invocation(action)
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "shared-service-secret")
    token_from_first_process = issue_presented_operator_action_token(
        action_id=invocation.action_id,
        target=invocation.target,
        snapshot_id=invocation.snapshot_id,
        snapshot_version=invocation.snapshot_version,
        idempotency_key=invocation.idempotency_key,
        issued_at_ms=invocation.issued_at_ms,
        expires_at_ms=invocation.expires_at_ms,
    )
    second_process_invocation = invocation.model_copy(
        update={"presentation_token": token_from_first_process}
    )

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "different-service-secret")
    assert has_valid_presented_operator_action_token(second_process_invocation) is False
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "shared-service-secret")
    assert has_valid_presented_operator_action_token(second_process_invocation) is True


def test_missing_signing_configuration_rejects_an_envelope_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _action()
    invocation = _invocation(action)
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    assert has_valid_presented_operator_action_token(invocation) is False


async def test_valid_action_claims_once_and_replays_the_durable_receipt(tmp_path: Path) -> None:
    service = PresentedAccountActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    action = _action()
    calls = 0
    receipt = _receipt(tmp_path)

    async def invoke():
        nonlocal calls
        calls += 1
        return receipt

    first = await service.execute_reconcile(
        action=action,
        invocation=_invocation(action),
        invoke=invoke,
        refreshed_snapshot_id=lambda: "c" * 64,
    )
    replay = await service.execute_reconcile(
        action=action,
        invocation=_invocation(action),
        invoke=invoke,
        refreshed_snapshot_id=lambda: "d" * 64,
    )

    assert calls == 1
    assert first.state == "ACCEPTED"
    assert first.replayed is False
    assert first.reconciliation_receipt == receipt
    assert first.refreshed_snapshot_id == "c" * 64
    assert replay == first.model_copy(update={"replayed": True})


async def test_renewed_presentation_of_the_same_snapshot_replays_its_one_attempt(tmp_path: Path) -> None:
    now = [_NOW_MS]
    service = PresentedAccountActionService(artifacts_root=tmp_path, now_ms=lambda: now[0])
    action = _action()
    receipt = _receipt(tmp_path)
    calls = 0

    async def invoke():
        nonlocal calls
        calls += 1
        return receipt

    await service.execute_reconcile(
        action=action,
        invocation=_invocation(action),
        invoke=invoke,
        refreshed_snapshot_id=lambda: "c" * 64,
    )
    now[0] += 1_000
    renewed = action.model_copy(
        update={
            "issued_at_ms": now[0],
            "expires_at_ms": now[0] + 60_000,
            "presentation_token": issue_presented_operator_action_token(
                action_id=action.action_id,
                target=action.target,
                snapshot_id=action.snapshot_id,
                snapshot_version=action.snapshot_version,
                idempotency_key=action.idempotency_key,
                issued_at_ms=now[0],
                expires_at_ms=now[0] + 60_000,
            ),
        }
    )
    replay = await service.execute_reconcile(
        action=renewed,
        invocation=_invocation(renewed),
        invoke=invoke,
        refreshed_snapshot_id=lambda: "d" * 64,
    )

    assert calls == 1
    assert replay.replayed is True


async def test_first_execution_accepts_the_same_snapshot_presented_milliseconds_earlier(tmp_path: Path) -> None:
    final_millisecond = 1_780_000_019_999
    service = PresentedAccountActionService(artifacts_root=tmp_path, now_ms=lambda: final_millisecond + 1)
    first = AccountSafetySnapshotService._presented_actions(
        account_id=_ACCOUNT_ID,
        snapshot_id=_SNAPSHOT_ID,
        generated_at_ms=final_millisecond,
        verdict="RECONCILING",
        reason_code="ACCOUNT_RECONCILIATION_REQUIRED",
        evidence_refs=(),
    )[0]
    current = AccountSafetySnapshotService._presented_actions(
        account_id=_ACCOUNT_ID,
        snapshot_id=_SNAPSHOT_ID,
        generated_at_ms=final_millisecond + 1,
        verdict="RECONCILING",
        reason_code="ACCOUNT_RECONCILIATION_REQUIRED",
        evidence_refs=(),
    )[0]
    receipt = _receipt(tmp_path)

    async def invoke():
        return receipt

    result = await service.execute_reconcile(
        action=current,
        invocation=_invocation(first),
        invoke=invoke,
        refreshed_snapshot_id=lambda: "c" * 64,
    )

    assert first.issued_at_ms == final_millisecond
    assert first.expires_at_ms == final_millisecond + 60_000
    assert current.issued_at_ms == final_millisecond + 1
    assert current.expires_at_ms == final_millisecond + 60_001
    assert result.state == "ACCEPTED"


async def test_expired_stale_or_wrong_target_action_rejects_before_effect(tmp_path: Path) -> None:
    service = PresentedAccountActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    calls = 0

    async def invoke():
        nonlocal calls
        calls += 1
        raise AssertionError("reconciliation must not run")

    expired = _action(expires_at_ms=_NOW_MS)
    with pytest.raises(PresentedActionRejectedError, match="expired") as expiry:
        await service.execute_reconcile(
            action=expired,
            invocation=_invocation(expired),
            invoke=invoke,
            refreshed_snapshot_id=lambda: "c" * 64,
        )
    assert expiry.value.reason_code == "ACTION_EXPIRED"

    action = _action()
    stale = _invocation(action, snapshot_version="d" * 64)
    with pytest.raises(PresentedActionRejectedError) as stale_error:
        await service.execute_reconcile(
            action=action,
            invocation=stale,
            invoke=invoke,
            refreshed_snapshot_id=lambda: "c" * 64,
        )
    assert stale_error.value.reason_code == "ACTION_SNAPSHOT_STALE"

    with pytest.raises(PresentedActionRejectedError) as target_error:
        await service.execute_reconcile(
            action=action,
            invocation=_invocation(action, account_id="DU7654321"),
            invoke=invoke,
            refreshed_snapshot_id=lambda: "c" * 64,
        )
    assert target_error.value.reason_code == "ACTION_TARGET_MISMATCH"
    assert calls == 0


async def test_timeout_is_durable_outcome_unknown_and_never_replays_effect(tmp_path: Path) -> None:
    service = PresentedAccountActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    action = _action()
    calls = 0

    async def timeout():
        nonlocal calls
        calls += 1
        raise TimeoutError("broker connection dropped")

    with pytest.raises(PresentedActionOutcomeUnknownError) as unknown:
        await service.execute_reconcile(
            action=action,
            invocation=_invocation(action),
            invoke=timeout,
            refreshed_snapshot_id=lambda: "c" * 64,
        )

    replay = await service.execute_reconcile(
        action=action,
        invocation=_invocation(action),
        invoke=timeout,
        refreshed_snapshot_id=lambda: "c" * 64,
    )

    assert calls == 1
    assert unknown.value.result.state == "OUTCOME_UNKNOWN"
    assert unknown.value.result.replayed is False
    assert "outcome is not proven" in unknown.value.result.finished_copy
    assert replay.state == "OUTCOME_UNKNOWN"
    assert replay.replayed is True


async def test_replay_of_an_inflight_action_reports_in_progress(tmp_path: Path) -> None:
    service = PresentedAccountActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    action = _action()
    started = asyncio.Event()
    release = asyncio.Event()

    async def invoke() -> AccountReconciliationReceipt:
        started.set()
        await release.wait()
        return _receipt(tmp_path)

    first = asyncio.create_task(
        service.execute_reconcile(
            action=action,
            invocation=_invocation(action),
            invoke=invoke,
            refreshed_snapshot_id=lambda: "c" * 64,
        )
    )
    await started.wait()
    replay = await service.execute_reconcile(
        action=action,
        invocation=_invocation(action),
        invoke=invoke,
        refreshed_snapshot_id=lambda: "d" * 64,
    )
    release.set()

    assert replay.state == "IN_PROGRESS"
    assert replay.replayed is True
    assert (await first).state == "ACCEPTED"


async def test_snapshot_refresh_failure_preserves_accepted_reconciliation(tmp_path: Path) -> None:
    service = PresentedAccountActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    action = _action()

    async def invoke() -> AccountReconciliationReceipt:
        return _receipt(tmp_path)

    def refresh_fails() -> str:
        raise OSError("snapshot unavailable")

    result = await service.execute_reconcile(
        action=action,
        invocation=_invocation(action),
        invoke=invoke,
        refreshed_snapshot_id=refresh_fails,
    )

    assert result.state == "ACCEPTED"
    assert result.reconciliation_receipt is not None
    assert result.refreshed_snapshot_id is None
