"""Terminal evidence recording for supervised in-process bot runs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from app.engine.live.bot_lifecycle_state import BotDutyOutcome
from app.engine.live.desired_state import DesiredState, DesiredStateRepo
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_carryover import prove_stop_outcome
from app.services.bot_clerk_lifecycle import commit_stop_before_task_cancel
from app.services.bot_run_evidence import PROVISIONAL_STOP_REASON_CODE, BotRunEvidenceService
from app.services.bot_runtime import ManagedBot

_UPDATED_BY = "bot_runner"
logger = logging.getLogger(__name__)


class BotRunTerminalRecorder:
    """Own idempotent terminal evidence and run-id-fenced task reaping."""

    def __init__(
        self,
        *,
        managed_bots: dict[str, ManagedBot],
        desired_repo_for: Callable[[str], DesiredStateRepo],
        run_evidence: BotRunEvidenceService,
        now_ms: Callable[[], int],
    ) -> None:
        self._managed_bots = managed_bots
        self._desired_repo_for = desired_repo_for
        self._run_evidence = run_evidence
        self._now_ms = now_ms

    def finalize(
        self,
        binding: BrokerBotBinding,
        *,
        kind: Literal["STOPPED", "CRASHED", "EXITED_UNVERIFIED"],
        reason_code: str,
    ) -> None:
        """Record the terminal duty fact once for the binding's exact run."""
        managed = self._managed_bots.get(binding.strategy_instance_id)
        if managed is not None and managed.binding.run_id == binding.run_id:
            if managed.finalized:
                return
            managed.finalized = True
        recorded_at_ms = self._now_ms()
        if kind in ("CRASHED", "EXITED_UNVERIFIED"):
            self._desired_repo_for(binding.strategy_instance_id).set(
                DesiredState.STOPPED,
                updated_by=_UPDATED_BY,
                now_ms=recorded_at_ms,
                reason=f"terminal_outcome:{reason_code}",
            )
        outcome = BotDutyOutcome(
            kind=kind,
            reason_code=reason_code,
            recorded_at_ms=recorded_at_ms,
            run_id=binding.run_id,
        )
        self._run_evidence.record_terminal(
            binding.strategy_instance_id,
            outcome,
            updated_by=_UPDATED_BY,
            reason=reason_code,
            expected_active_run_id=binding.run_id,
            persist_receipt=reason_code != PROVISIONAL_STOP_REASON_CODE,
        )

    async def finalize_after_authority_stop(
        self,
        binding: BrokerBotBinding,
        *,
        kind: Literal["CRASHED", "EXITED_UNVERIFIED"],
        reason_code: str,
    ) -> None:
        """Commit SQLite STOP before publishing an unexpected terminal fact."""

        managed = self._managed_bots.get(binding.strategy_instance_id)
        if (
            managed is not None
            and managed.binding.run_id == binding.run_id
            and managed.finalized
        ):
            return
        try:
            await commit_stop_before_task_cancel(binding, reason=reason_code)
        except Exception:
            logger.exception(
                "Bot terminal evidence could not commit SQLite STOP",
                extra={
                    "action": "bot_terminal_authority_stop_failed",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": binding.run_id,
                    "reason_code": reason_code,
                },
            )
        self.finalize(binding, kind=kind, reason_code=reason_code)

    def reap(self, strategy_instance_id: str, run_id: str) -> None:
        """Remove only the task that still owns this exact run identity."""
        managed = self._managed_bots.get(strategy_instance_id)
        if managed is not None and managed.binding.run_id == run_id:
            self._managed_bots.pop(strategy_instance_id, None)

    def replace_provisional_stop(
        self,
        binding: BrokerBotBinding,
        *,
        reason_code: str,
    ) -> None:
        """Replace provisional stop evidence with the Clerk-proven outcome."""
        outcome = BotDutyOutcome(
            kind="STOPPED",
            reason_code=reason_code,
            recorded_at_ms=self._now_ms(),
            run_id=binding.run_id,
        )
        self._run_evidence.record_terminal(
            binding.strategy_instance_id,
            outcome,
            updated_by=_UPDATED_BY,
            reason=reason_code,
        )


async def prove_terminal_stop_outcome(
    binding: BrokerBotBinding,
    *,
    checkpoint_path: Path,
    now_ms: Callable[[], int],
) -> str:
    """Obtain fresh Clerk custody and persist the stop checkpoint."""
    from app.broker.alpaca.clerk import get_alpaca_clerk

    clerk = get_alpaca_clerk()
    if clerk is None:
        logger.error(
            "Trade bot stopped without an available Alpaca Clerk proof",
            extra={
                "action": "stop_custody_unprovable",
                "strategy_instance_id": binding.strategy_instance_id,
            },
        )
        return "STOPPED_CUSTODY_UNPROVABLE"
    return await prove_stop_outcome(
        binding,
        clerk=clerk,
        checkpoint_path=checkpoint_path,
        now_ms=now_ms,
    )
