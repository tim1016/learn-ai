"""Boot-time repair of durable bot lifecycle artifacts.

This collaborator never owns asyncio tasks. The task registry supplies its
current liveness and broker-ownership predicates; the sweep only repairs
durable lifecycle intent and records interrupted runs after a container boot.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecyclePhase,
    BotLifecycleStateCorruptError,
    BotLifecycleStateRepo,
)
from app.engine.live.desired_state import DesiredState, DesiredStateRepo

logger = logging.getLogger(__name__)


class BootRecoveryReport(BaseModel):
    """What the boot sweep found and did (S5, #1263)."""

    model_config = ConfigDict(frozen=True)

    interrupted_instances: tuple[str, ...]
    unresolved_intents: int
    completed_at_ms: int


class BotBootRecovery:
    """Repair interrupted durable state and run the Clerk recovery sequence."""

    def __init__(
        self,
        artifacts_root: Path,
        *,
        lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo],
        desired_repo_for: Callable[[str], DesiredStateRepo],
        manages_instance: Callable[[str], bool],
        is_running: Callable[[str], bool],
        now_ms: Callable[[], int],
    ) -> None:
        self._live_state_root = Path(artifacts_root) / "live_state"
        self._lifecycle_repo_for = lifecycle_repo_for
        self._desired_repo_for = desired_repo_for
        self._manages_instance = manages_instance
        self._is_running = is_running
        self._now_ms = now_ms

    async def run(
        self,
        *,
        recover: Callable[[], Awaitable[None]] | None = None,
        reconcile: Callable[[], Awaitable[object]] | None = None,
        unresolved_intents_probe: Callable[[], Awaitable[int]] | None = None,
    ) -> BootRecoveryReport:
        """Record interrupted runs, then complete non-task recovery steps."""
        interrupted = self._repair_lifecycle_artifacts()
        for step_name, step in (("recover", recover), ("reconcile", reconcile)):
            if step is None:
                continue
            try:
                await step()
            except Exception:
                # Surfaced, not silenced; the uncertainty probe still refuses
                # starts while intents remain unresolved in the journal.
                logger.exception(
                    "Boot recovery step failed",
                    extra={"action": "boot_recovery_step_failed", "step": step_name},
                )
        unresolved = (
            await unresolved_intents_probe() if unresolved_intents_probe is not None else 0
        )
        report = BootRecoveryReport(
            interrupted_instances=tuple(interrupted),
            unresolved_intents=unresolved,
            completed_at_ms=self._now_ms(),
        )
        logger.info(
            "Boot recovery sweep complete",
            extra={
                "action": "boot_recovery_complete",
                "interrupted": list(report.interrupted_instances),
                "unresolved_intents": report.unresolved_intents,
            },
        )
        return report

    def _repair_lifecycle_artifacts(self) -> list[str]:
        interrupted: list[str] = []
        if not self._live_state_root.is_dir():
            return interrupted

        for child in sorted(self._live_state_root.iterdir()):
            if not (child / "lifecycle_state.json").is_file():
                continue
            strategy_instance_id = child.name
            if not self._manages_instance(strategy_instance_id):
                continue
            repo = self._lifecycle_repo_for(strategy_instance_id)
            try:
                record = repo.read()
            except BotLifecycleStateCorruptError as exc:
                logger.warning(
                    "Boot sweep skipping corrupt lifecycle state",
                    extra={"action": "boot_sweep_corrupt_lifecycle", "path": str(exc.path)},
                )
                continue
            if record is None:
                continue

            desired_repo = self._desired_repo_for(strategy_instance_id)
            if (
                record.phase is BotLifecyclePhase.OFF_DUTY
                and record.duty_outcome is not None
                and desired_repo.read_state() in {DesiredState.RUNNING, DesiredState.PAUSED}
            ):
                desired_repo.set(
                    DesiredState.STOPPED,
                    updated_by="bot_runner_boot_sweep",
                    now_ms=self._now_ms(),
                    reason="repair_terminal_nonstopped_intent",
                )
                logger.warning(
                    "Boot sweep repaired terminal bot desired state",
                    extra={
                        "action": "boot_sweep_repaired_terminal_intent",
                        "strategy_instance_id": strategy_instance_id,
                        "run_id": record.duty_outcome.run_id,
                        "reason_code": record.duty_outcome.reason_code,
                    },
                )
                continue
            if record.phase is not BotLifecyclePhase.ON_DUTY or self._is_running(
                strategy_instance_id
            ):
                continue

            now_ms = self._now_ms()
            desired_repo.set(
                DesiredState.STOPPED,
                updated_by="bot_runner_boot_sweep",
                now_ms=now_ms,
                reason="interrupted_by_restart",
            )
            repo.record_terminal_outcome(
                BotDutyOutcome(
                    kind="EXITED_UNVERIFIED",
                    reason_code="INTERRUPTED_BY_RESTART",
                    recorded_at_ms=now_ms,
                    run_id=record.active_run_id,
                ),
                updated_by="bot_runner_boot_sweep",
                reason="container_restart",
                expected_active_run_id=record.active_run_id,
            )
            interrupted.append(strategy_instance_id)
            logger.warning(
                "Boot sweep recorded interrupted bot",
                extra={
                    "action": "boot_sweep_interrupted",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": record.active_run_id,
                },
            )
        return interrupted
