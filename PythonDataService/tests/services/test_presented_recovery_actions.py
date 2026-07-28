"""Regression matrix for snapshot-bound Cancel and Flatten action attempts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import settings
from app.schemas.account_safety_snapshot import PresentedOperatorAction
from app.schemas.operator_blocker import OperatorConfirmationCopy
from app.schemas.presented_operator_action import (
    PresentedOperatorActionEffectReceipt,
    PresentedOperatorActionInvocation,
    PresentedOperatorActionTarget,
    has_valid_presented_operator_action_token,
    issue_presented_operator_action_token,
)
from app.services.presented_account_actions import PresentedActionRejectedError
from app.services.presented_recovery_actions import (
    PresentedRecoveryActionOutcome,
    PresentedRecoveryActionService,
)

_ACCOUNT_ID = "DU1234567"
_NOW_MS = 1_780_000_000_000
_SNAPSHOT_ID = "a" * 64


@pytest.fixture(autouse=True)
def _configured_presented_action_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)


def _action(
    *,
    action_id: str = "flatten",
    target: PresentedOperatorActionTarget | None = None,
    required_token: str = "FLATTEN",
    expires_at_ms: int = _NOW_MS + 60_000,
) -> PresentedOperatorAction:
    target = target or PresentedOperatorActionTarget(account_id=_ACCOUNT_ID)
    key = ("b" if action_id == "flatten" else "c") * 64
    return PresentedOperatorAction(
        action_id=action_id,
        target=target,
        snapshot_id=_SNAPSHOT_ID,
        snapshot_version=_SNAPSHOT_ID,
        effect_class="RISK_REDUCING_BROKER",
        idempotency_key=key,
        issued_at_ms=_NOW_MS,
        expires_at_ms=expires_at_ms,
        presentation_token=issue_presented_operator_action_token(
            action_id=action_id,
            target=target,
            snapshot_id=_SNAPSHOT_ID,
            snapshot_version=_SNAPSHOT_ID,
            idempotency_key=key,
            issued_at_ms=_NOW_MS,
            expires_at_ms=expires_at_ms,
        ),
        confirmation=OperatorConfirmationCopy(
            title="Flatten account exposure",
            body="Record or submit the snapshot-bound flatten action.",
            consequence="No liquidation is submitted without fresh current proof.",
            confirm_label="Confirm flatten",
            required_token=required_token,
        ),
        availability="AVAILABLE",
        disposition="fix_here",
        finished_copy="Flatten action accepted.",
    )


def _invocation(
    action: PresentedOperatorAction,
    *,
    target: PresentedOperatorActionTarget | None = None,
    confirmation_token: str | None = "FLATTEN",
) -> PresentedOperatorActionInvocation:
    target = action.target if target is None else target
    return PresentedOperatorActionInvocation(
        action_id=action.action_id,
        target=target,
        snapshot_id=action.snapshot_id,
        snapshot_version=action.snapshot_version,
        idempotency_key=action.idempotency_key,
        issued_at_ms=action.issued_at_ms,
        expires_at_ms=action.expires_at_ms,
        presentation_token=issue_presented_operator_action_token(
            action_id=action.action_id,
            target=target,
            snapshot_id=action.snapshot_id,
            snapshot_version=action.snapshot_version,
            idempotency_key=action.idempotency_key,
            issued_at_ms=action.issued_at_ms,
            expires_at_ms=action.expires_at_ms,
        ),
        confirmation_token=confirmation_token,
    )


def test_exact_order_token_cannot_be_replayed_against_a_sibling_or_reused_order_id() -> None:
    action = _action(
        action_id="cancel_exact",
        target=PresentedOperatorActionTarget(
            account_id=_ACCOUNT_ID,
            kind="EXACT_ORDER",
            target_order_id=101,
            target_order_ref="learn-ai/one:open-order",
        ),
        required_token="",
    )
    sibling = PresentedOperatorActionTarget(
        account_id=_ACCOUNT_ID,
        kind="EXACT_ORDER",
        target_order_id=101,
        target_order_ref="learn-ai/two:reused-order-id",
    )

    invocation = _invocation(action, confirmation_token=None)
    assert has_valid_presented_operator_action_token(invocation)
    assert not has_valid_presented_operator_action_token(
        invocation.model_copy(update={"target": sibling})
    )


async def test_flatten_without_current_proof_records_one_pending_proof_intention(
    tmp_path: Path,
) -> None:
    action = _action()
    service = PresentedRecoveryActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    calls = 0

    async def record_intention() -> PresentedRecoveryActionOutcome:
        nonlocal calls
        calls += 1
        return PresentedRecoveryActionOutcome(
            state="PENDING_PROOF",
            finished_copy="Flatten intention recorded. Reconcile and confirm fresh evidence before liquidation.",
            effect_receipt=PresentedOperatorActionEffectReceipt(
                kind="FLATTEN_INTENTION_RECORDED",
                account_id=_ACCOUNT_ID,
                recorded_at_ms=_NOW_MS,
            ),
        )

    first = await service.execute(
        action=action,
        invocation=_invocation(action),
        invoke=record_intention,
    )
    replay = await service.execute(
        action=action,
        invocation=_invocation(action),
        invoke=record_intention,
    )

    assert calls == 1
    assert first.state == "PENDING_PROOF"
    assert first.effect_receipt is not None
    assert first.effect_receipt.kind == "FLATTEN_INTENTION_RECORDED"
    assert replay == first.model_copy(update={"replayed": True})


async def test_flatten_requires_the_typed_confirmation_before_any_durable_claim(tmp_path: Path) -> None:
    action = _action()
    service = PresentedRecoveryActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    calls = 0

    async def should_not_run() -> PresentedRecoveryActionOutcome:
        nonlocal calls
        calls += 1
        raise AssertionError("confirmation must be validated before the action attempt is claimed")

    with pytest.raises(PresentedActionRejectedError) as exc_info:
        await service.execute(
            action=action,
            invocation=_invocation(action, confirmation_token="NO"),
            invoke=should_not_run,
        )

    assert exc_info.value.reason_code == "ACTION_CONFIRMATION_REQUIRED"
    assert calls == 0


@pytest.mark.parametrize(
    ("action_id", "target", "required_token", "confirmation_token", "reason_code"),
    [
        (
            "cancel_pending",
            PresentedOperatorActionTarget(
                account_id=_ACCOUNT_ID,
                strategy_instance_id="bot-a",
                run_id="run-a",
                kind="A0_INTENT",
                target_intent_id="intent-a",
                target_intent_order_ref="learn-ai/a:intent-a",
            ),
            "",
            None,
            "A0_CANCEL_TARGET_NOT_CURRENT",
        ),
        (
            "cancel_exact",
            PresentedOperatorActionTarget(
                account_id=_ACCOUNT_ID,
                kind="EXACT_ORDER",
                target_order_id=101,
                target_order_ref="learn-ai/a:open-order",
            ),
            "",
            None,
            "EXACT_CANCEL_TARGET_NOT_CURRENT",
        ),
        (
            "flatten",
            PresentedOperatorActionTarget(
                account_id=_ACCOUNT_ID,
                strategy_instance_id="retired-a",
                run_id="run-a",
                kind="RECOVERY_NAMESPACE",
                recovery_intent_id="recovery-a",
                recovery_order_ref="learn-ai/a:recovery-a",
                target_con_id=756733,
                expected_signed_quantity=2.0,
            ),
            "FLATTEN",
            "FLATTEN",
            "RECOVERY_FLATTEN_TARGET_NOT_CURRENT",
        ),
        (
            "flatten",
            PresentedOperatorActionTarget(account_id=_ACCOUNT_ID, kind="ACCOUNT_EMERGENCY"),
            "FLATTEN",
            "FLATTEN",
            "ACCOUNT_EMERGENCY_FLATTEN_NOT_CURRENT",
        ),
    ],
)
async def test_known_target_rejection_is_durable_and_replayably_refused(
    tmp_path: Path,
    action_id: str,
    target: PresentedOperatorActionTarget,
    required_token: str,
    confirmation_token: str | None,
    reason_code: str,
) -> None:
    action = _action(action_id=action_id, target=target, required_token=required_token)
    service = PresentedRecoveryActionService(artifacts_root=tmp_path, now_ms=lambda: _NOW_MS)
    calls = 0

    async def reject_stale_target() -> PresentedRecoveryActionOutcome:
        nonlocal calls
        calls += 1
        raise PresentedActionRejectedError(
            reason_code,
            "Fresh reconciliation no longer proves this exact recovery target safe.",
        )

    with pytest.raises(PresentedActionRejectedError) as first:
        await service.execute(
            action=action,
            invocation=_invocation(action, confirmation_token=confirmation_token),
            invoke=reject_stale_target,
        )
    with pytest.raises(PresentedActionRejectedError) as replay:
        await service.execute(
            action=action,
            invocation=_invocation(action, confirmation_token=confirmation_token),
            invoke=reject_stale_target,
        )

    assert first.value.reason_code == reason_code
    assert replay.value.reason_code == reason_code
    assert calls == 1


async def test_crashed_pending_attempt_retries_through_the_same_clerk_identity(
    tmp_path: Path,
) -> None:
    action = _action()
    now_ms = _NOW_MS
    service = PresentedRecoveryActionService(artifacts_root=tmp_path, now_ms=lambda: now_ms)

    async def crash_after_claim() -> PresentedRecoveryActionOutcome:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await service.execute(action=action, invocation=_invocation(action), invoke=crash_after_claim)

    assert service.replay_if_claimed(_invocation(action)) is None

    resumed_calls = 0

    async def resume_through_clerk() -> PresentedRecoveryActionOutcome:
        nonlocal resumed_calls
        resumed_calls += 1
        return PresentedRecoveryActionOutcome(
            state="PENDING_PROOF",
            finished_copy="The Clerk replay is durable; fresh broker proof remains pending.",
            effect_receipt=PresentedOperatorActionEffectReceipt(
                kind="FLATTEN_INTENTION_RECORDED",
                account_id=_ACCOUNT_ID,
                recorded_at_ms=now_ms,
            ),
        )

    resumed = await service.execute(action=action, invocation=_invocation(action), invoke=resume_through_clerk)

    assert resumed_calls == 1
    assert resumed.state == "PENDING_PROOF"
    assert resumed.replayed is False


async def test_live_dispatch_cannot_be_reclaimed_after_thirty_seconds(
    tmp_path: Path,
) -> None:
    """A slow Clerk call owns the action until it returns, not for a time lease."""

    action = _action(expires_at_ms=_NOW_MS + 300_000)
    now_ms = _NOW_MS
    first_service = PresentedRecoveryActionService(artifacts_root=tmp_path, now_ms=lambda: now_ms)
    retry_service = PresentedRecoveryActionService(artifacts_root=tmp_path, now_ms=lambda: now_ms)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_clerk_dispatch() -> PresentedRecoveryActionOutcome:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return PresentedRecoveryActionOutcome(
            state="PENDING_PROOF",
            finished_copy="The Clerk replay is durable; fresh broker proof remains pending.",
        )

    first_task = asyncio.create_task(
        first_service.execute(
            action=action,
            invocation=_invocation(action),
            invoke=slow_clerk_dispatch,
        )
    )
    await started.wait()
    now_ms += 131_000

    replay = retry_service.replay_if_claimed(_invocation(action))
    assert replay is not None
    assert replay.state == "IN_PROGRESS"

    retry = await retry_service.execute(
        action=action,
        invocation=_invocation(action),
        invoke=slow_clerk_dispatch,
    )
    assert retry.state == "IN_PROGRESS"
    assert calls == 1

    release.set()
    first = await first_task
    assert first.state == "PENDING_PROOF"
    assert calls == 1
