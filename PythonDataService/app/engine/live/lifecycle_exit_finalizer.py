"""Durable terminal lifecycle projection for host-managed bot processes."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.engine.live.bot_lifecycle_evaluator import BotLifecycleEvaluator
from app.engine.live.bot_lifecycle_state import BotDutyOutcome
from app.engine.live.clock_out import (
    ClockOutReceiptCorruptError,
    clock_out_completion_is_durable,
    read_clock_out_receipt,
)
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.engine.live.exit_taxonomy import classify_run_exit, read_run_exit_evidence


class _ProcessWithReturnCode(Protocol):
    returncode: int | None


class ExitedManagedProcess(Protocol):
    """The narrow host-process view required for terminal projection."""

    strategy_instance_id: str
    run_id: str
    run_dir: Path
    process: _ProcessWithReturnCode
    stopping: bool


def record_failed_launch_outcome(
    artifacts_root: Path,
    *,
    now_ms: int,
    run_id: str,
    strategy_instance_id: str,
    source: str,
    operation_fence_held: bool = False,
) -> None:
    """Record an identifiable pre-spawn failure as a durable duty outcome."""

    if not strategy_instance_id:
        return
    BotLifecycleEvaluator(artifacts_root, strategy_instance_id).record_terminal_outcome(
        BotDutyOutcome(
            kind="FAILED_LAUNCH",
            reason_code="FAILED_LAUNCH",
            recorded_at_ms=now_ms,
            run_id=run_id,
        ),
        updated_by="host_daemon",
        reason=source,
        operation_fence_held=operation_fence_held,
    )


def record_terminal_lifecycle_outcome(
    artifacts_root: Path,
    managed: ExitedManagedProcess,
    *,
    now_ms: int,
    operation_fence_held: bool = False,
) -> None:
    """Project a reaped process into lifecycle state without inferring success."""

    if not managed.strategy_instance_id:
        return
    try:
        receipt = read_clock_out_receipt(managed.run_dir)
    except ClockOutReceiptCorruptError:
        receipt = None
        reason_code = "CLOCK_OUT_RECEIPT_CORRUPT"
    else:
        reason_code = ""
    if (
        receipt is not None
        and receipt.run_id == managed.run_id
        and clock_out_completion_is_durable(managed.run_dir, receipt)
        and clock_out_stop_latch_is_durable(artifacts_root, managed.strategy_instance_id)
    ):
        outcome = BotDutyOutcome(
            kind="CLOCKED_OUT_FLAT",
            reason_code=receipt.reason_code,
            recorded_at_ms=receipt.completed_at_ms,
            run_id=managed.run_id,
        )
        reason = "clock_out.flat_broker_evidence"
    else:
        verdict = classify_run_exit(
            read_run_exit_evidence(managed.run_dir),
            returncode=managed.process.returncode,
            stopping=managed.stopping,
        )
        kind = {
            "controlled_stop": "STOPPED",
            "interrupted": "STOPPED",
            "halted": "HALTED",
            "poisoned": "HALTED",
            "crashed": "CRASHED",
        }.get(verdict.category, "EXITED_UNVERIFIED")
        outcome = BotDutyOutcome(
            kind=kind,
            reason_code=reason_code or verdict.registry_source.upper().replace(".", "_"),
            recorded_at_ms=now_ms,
            run_id=managed.run_id,
        )
        reason = verdict.registry_source
    BotLifecycleEvaluator(artifacts_root, managed.strategy_instance_id).record_terminal_outcome(
        outcome,
        updated_by="host_daemon",
        reason=reason,
        expected_active_run_id=managed.run_id,
        operation_fence_held=operation_fence_held,
    )


def clock_out_stop_latch_is_durable(artifacts_root: Path, strategy_instance_id: str) -> bool:
    """Require the completed Clock Out command's durable STOPPED latch."""

    try:
        return (
            DesiredStateRepo(
                stable_desired_state_path(artifacts_root, strategy_instance_id),
                trusted_root=artifacts_root / "live_state",
            ).read_state()
            is DesiredState.STOPPED
        )
    except (DesiredStateCorruptError, OSError, ValueError):
        return False


__all__ = [
    "ExitedManagedProcess",
    "clock_out_stop_latch_is_durable",
    "record_failed_launch_outcome",
    "record_terminal_lifecycle_outcome",
]
