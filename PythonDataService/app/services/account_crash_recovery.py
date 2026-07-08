"""Crash-retired account recovery policy and audit writes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.engine.live.account_artifacts import AccountArtifactError, AccountAuditedOverride, append_account_event
from app.engine.live.account_registry import AccountInstanceBinding, crash_retired_restart_blocking_binding
from app.schemas.account_recovery import CrashRecoveryOverrideRequest, CrashRecoveryOverrideResponse
from app.schemas.live_runs import GateResult

logger = logging.getLogger(__name__)

CRASH_RECOVERY_GATE_ID = "account.crash_recovery"
CRASH_RECOVERY_OVERRIDE_VALID_FOR_MS = 15 * 60 * 1000


class CrashRecoveryNotRequiredError(ValueError):
    """Raised when no crash-retired binding currently blocks restart."""


def crash_recovery_gate(binding: AccountInstanceBinding) -> GateResult:
    return GateResult(
        gate_id=CRASH_RECOVERY_GATE_ID,
        status="block",
        source="account_instance_registry",
        operator_reason="CRASH_RECOVERY_REQUIRED",
        operator_next_step=(
            "Verify the broker account is flat with no open orders, then record an audited recovery override."
        ),
        evidence_at_ms=binding.recorded_at_ms,
    )


def crash_recovery_gate_for_instance(
    artifacts_root: Path,
    *,
    account_id: str | None,
    strategy_instance_id: str,
) -> GateResult | None:
    """Project the crash-recovery gate for the read-only operator surface.

    Observability reads degrade on corrupt account artifacts instead of
    failing the whole status projection; the start-mutation path stays
    fail-closed by letting the same errors propagate.
    """
    if account_id is None:
        return None
    try:
        binding = crash_retired_restart_blocking_binding(
            artifacts_root,
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
        )
    except (OSError, json.JSONDecodeError, AccountArtifactError) as exc:
        logger.warning(
            "failed to read account artifacts while projecting crash-recovery gate",
            extra={
                "account_id": account_id,
                "strategy_instance_id": strategy_instance_id,
                "exception": repr(exc),
            },
        )
        return None
    if binding is None:
        return None
    return crash_recovery_gate(binding)


def crash_recovery_blocking_binding(
    artifacts_root: Path,
    *,
    account_id: str,
    strategy_instance_id: str,
) -> AccountInstanceBinding | None:
    return crash_retired_restart_blocking_binding(
        artifacts_root,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
    )


def crash_recovery_block_detail(
    strategy_instance_id: str,
    binding: AccountInstanceBinding,
) -> dict:
    return {
        "reason_code": "CRASH_RECOVERY_REQUIRED",
        "message": (
            f"Previous host runner for {strategy_instance_id!r} crashed without later account recovery proof."
        ),
        "remediation": (
            "Verify the broker account is flat with no open orders, then record an audited recovery override "
            "before restarting this bot."
        ),
        "gate_id": CRASH_RECOVERY_GATE_ID,
        "strategy_instance_id": strategy_instance_id,
        "run_id": binding.run_id,
        "account_id": binding.account_id,
        "bot_order_namespace": binding.bot_order_namespace,
        "blocking_recorded_at_ms": binding.recorded_at_ms,
        "gate_result": crash_recovery_gate(binding).model_dump(mode="json"),
    }


def record_crash_recovery_override_evidence(
    artifacts_root: Path,
    *,
    account_id: str,
    strategy_instance_id: str,
    request: CrashRecoveryOverrideRequest,
    now_ms: int,
) -> CrashRecoveryOverrideResponse:
    binding = crash_recovery_blocking_binding(
        artifacts_root,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
    )
    if binding is None:
        raise CrashRecoveryNotRequiredError("crash recovery is not required")
    recorded_at_ms = max(now_ms, binding.recorded_at_ms + 1)
    reason = (
        request.reason.strip()
        if request.reason is not None and request.reason.strip()
        else "Operator verified the broker account is flat and has no open orders after a crash-retired host runner."
    )
    override = AccountAuditedOverride(
        account_id=account_id,
        override_id=f"crash-recovery-{recorded_at_ms}",
        approved_decision="continue",
        reason=reason,
        approved_by=request.approved_by,
        approved_at_ms=recorded_at_ms,
        valid_until_ms=recorded_at_ms + CRASH_RECOVERY_OVERRIDE_VALID_FOR_MS,
        prior_evidence={
            "strategy_instance_id": strategy_instance_id,
            "run_id": binding.run_id,
            "bot_order_namespace": binding.bot_order_namespace,
            "crash_recorded_at_ms": binding.recorded_at_ms,
            "crash_source": binding.source,
            "operator_confirmation": "account_flat_no_open_orders",
        },
        next_reconciliation_step="Run account reconciliation on the next broker reconnect before submitting orders.",
        strategy_instance_id=strategy_instance_id,
        run_id=binding.run_id,
        bot_order_namespace=binding.bot_order_namespace,
    )
    append_account_event(
        artifacts_root,
        account_id,
        {
            "event_type": "account_audited_override_recorded",
            **override.model_dump(mode="json"),
        },
    )
    return CrashRecoveryOverrideResponse(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        run_id=binding.run_id,
        bot_order_namespace=binding.bot_order_namespace,
        override_id=override.override_id,
        recorded_at_ms=recorded_at_ms,
        blocking_recorded_at_ms=binding.recorded_at_ms,
    )
