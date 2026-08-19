"""Contain legacy IBKR duty mutations behind positive plane evidence."""

from __future__ import annotations

from pathlib import Path

from app.engine.live.bot_lifecycle_evaluator import (
    BotLifecycleEvaluator,
    LifecycleDisposition,
    LifecycleStartAdmissionEvidence,
)
from app.engine.live.bot_lifecycle_state import BotDutyOutcome
from app.services.bot_control_plane import BotControlPlane, BotControlPlaneDiscriminator


class IbkrLifecycleMutationCapability:
    """Revalidate positive IBKR identity immediately before each mutation."""

    def __init__(self, artifacts_root: Path, strategy_instance_id: str) -> None:
        self._artifacts_root = Path(artifacts_root)
        self._strategy_instance_id = strategy_instance_id
        self._plane = BotControlPlaneDiscriminator(self._artifacts_root)

    def prepare_start_actuation(
        self,
        *,
        run_id: str,
        now_ms: int,
        updated_by: str,
        admission: LifecycleStartAdmissionEvidence,
        reason: str = "start_actuation_prepared",
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition:
        return self._evaluator().prepare_start(
            run_id=run_id,
            now_ms=now_ms,
            updated_by=updated_by,
            admission=admission,
            reason=reason,
            operation_fence_held=operation_fence_held,
        )

    def abort_start_actuation(
        self,
        *,
        run_id: str,
        failure: str,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition | None:
        return self._evaluator().abort_prepared_start(
            run_id=run_id,
            failure=failure,
            operation_fence_held=operation_fence_held,
        )

    def recover_prepared_start(
        self,
        *,
        run_id: str,
        daemon_state: str,
        observed_at_ms: int,
        updated_by: str = "daemon_observation_recovery",
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition | None:
        return self._evaluator().recover_prepared_start_from_daemon_observation(
            run_id=run_id,
            daemon_state=daemon_state,
            observed_at_ms=observed_at_ms,
            updated_by=updated_by,
            operation_fence_held=operation_fence_held,
        )

    def record_start(
        self,
        *,
        run_id: str,
        now_ms: int,
        updated_by: str,
        admission: LifecycleStartAdmissionEvidence,
        reason: str = "start_accepted",
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition:
        return self._evaluator().record_start_accepted(
            run_id=run_id,
            now_ms=now_ms,
            updated_by=updated_by,
            admission=admission,
            reason=reason,
            operation_fence_held=operation_fence_held,
        )

    def record_terminal(
        self,
        outcome: BotDutyOutcome,
        *,
        updated_by: str,
        reason: str,
        expected_active_run_id: str | None = None,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition | None:
        return self._evaluator().record_terminal_outcome(
            outcome,
            updated_by=updated_by,
            reason=reason,
            expected_active_run_id=expected_active_run_id,
            operation_fence_held=operation_fence_held,
        )

    def retire_bot(
        self,
        *,
        now_ms: int,
        updated_by: str,
        reason: str,
        replacement_strategy_instance_id: str | None = None,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition:
        return self._evaluator().retire(
            now_ms=now_ms,
            updated_by=updated_by,
            reason=reason,
            replacement_strategy_instance_id=replacement_strategy_instance_id,
            operation_fence_held=operation_fence_held,
        )

    def reopen_retired_for_deploy(
        self,
        *,
        now_ms: int,
        updated_by: str,
        reason: str,
        operation_fence_held: bool = False,
    ) -> LifecycleDisposition | None:
        return self._evaluator().reopen_for_deploy_if_retired(
            now_ms=now_ms,
            updated_by=updated_by,
            reason=reason,
            operation_fence_held=operation_fence_held,
        )

    def _evaluator(self) -> BotLifecycleEvaluator:
        self._plane.require(
            self._strategy_instance_id,
            BotControlPlane.IBKR_EVALUATOR,
        )
        return BotLifecycleEvaluator(self._artifacts_root, self._strategy_instance_id)


def ibkr_lifecycle_capability(
    artifacts_root: Path,
    strategy_instance_id: str,
) -> IbkrLifecycleMutationCapability:
    """Return the deletion-ready capability for legacy IBKR duty mutations."""

    return IbkrLifecycleMutationCapability(artifacts_root, strategy_instance_id)


__all__ = ["IbkrLifecycleMutationCapability", "ibkr_lifecycle_capability"]
