"""Boot-time repair of durable bot lifecycle artifacts.

This collaborator never owns asyncio tasks. The task registry supplies its
current liveness and broker-ownership predicates; the sweep only repairs
durable lifecycle intent and records interrupted runs after a container boot.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecyclePhase,
    BotLifecycleStateCorruptError,
    BotLifecycleStateRepo,
)
from app.engine.live.desired_state import DesiredState, DesiredStateRepo
from app.services.bot_lifecycle_projection import (
    AlpacaLifecycleProjectionResult,
    AlpacaLifecycleProjector,
    ProjectionStatus,
)

logger = logging.getLogger(__name__)


class BootRecoveryReport(BaseModel):
    """What the boot sweep found and did (S5, #1263)."""

    model_config = ConfigDict(frozen=True)

    interrupted_instances: tuple[str, ...]
    unresolved_intents: int
    completed_at_ms: int


class BootAuthorityPreparationError(RuntimeError):
    """SQLite recovery or reconciliation failed before projection repair."""


@dataclass(frozen=True, slots=True)
class BotRecoveryCandidate:
    """One binding- or SQLite-derived run that boot must reconcile."""

    strategy_instance_id: str
    run_id: str
    sqlite_active: bool


class BotBootRecovery:
    """Repair interrupted durable state and run the Clerk recovery sequence."""

    def __init__(
        self,
        artifacts_root: Path,
        *,
        lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo],
        lifecycle_projector: AlpacaLifecycleProjector,
        lifecycle_projector_for: Callable[[str], AlpacaLifecycleProjector] | None = None,
        desired_repo_for: Callable[[str], DesiredStateRepo],
        recovery_candidates: Callable[[], Iterable[BotRecoveryCandidate]],
        stop_authority_run: Callable[[str, str], Awaitable[None]],
        manages_instance: Callable[[str], bool],
        is_running: Callable[[str], bool],
        now_ms: Callable[[], int],
    ) -> None:
        del artifacts_root
        self._lifecycle_repo_for = lifecycle_repo_for
        self._lifecycle_projector = lifecycle_projector
        self._lifecycle_projector_for = lifecycle_projector_for or (
            lambda _strategy_instance_id: lifecycle_projector
        )
        self._desired_repo_for = desired_repo_for
        self._recovery_candidates = recovery_candidates
        self._stop_authority_run = stop_authority_run
        self._manages_instance = manages_instance
        self._is_running = is_running
        self._now_ms = now_ms

    async def run(
        self,
        *,
        recover: Callable[[], Awaitable[None]] | None = None,
        reconcile: Callable[[], Awaitable[object]] | None = None,
        unresolved_intents_probe: Callable[[str | None], Awaitable[int]] | None = None,
    ) -> BootRecoveryReport:
        """Recover SQLite authority first, then repair derived file projections."""
        for step_name, step in (("recover", recover), ("reconcile", reconcile)):
            if step is None:
                continue
            try:
                await step()
            except Exception as exc:
                logger.exception(
                    "Boot recovery step failed",
                    extra={"action": "boot_recovery_step_failed", "step": step_name},
                )
                raise BootAuthorityPreparationError(
                    f"SQLite boot authority step {step_name!r} failed"
                ) from exc
        interrupted = await self._repair_lifecycle_artifacts()
        # Account-wide on purpose: this is a boot summary of the whole
        # authority, not an admission decision about one bot (#1793).
        unresolved = (
            await unresolved_intents_probe(None) if unresolved_intents_probe is not None else 0
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

    async def _repair_lifecycle_artifacts(self) -> list[str]:
        interrupted: list[str] = []
        try:
            candidates = sorted(
                set(self._recovery_candidates()),
                key=lambda candidate: (
                    candidate.strategy_instance_id,
                    candidate.run_id,
                ),
            )
        except Exception as exc:
            raise BootAuthorityPreparationError(
                "SQLite boot recovery candidate enumeration failed"
            ) from exc
        for candidate in candidates:
            strategy_instance_id = candidate.strategy_instance_id
            run_id = candidate.run_id
            if not self._manages_instance(strategy_instance_id):
                continue
            projector = self._lifecycle_projector_for(strategy_instance_id)
            if self._is_running(strategy_instance_id):
                projection = projector.refresh(
                    strategy_instance_id=strategy_instance_id,
                    now_ms=self._now_ms(),
                    updated_by="bot_runner_boot_sweep",
                    reason="refresh_running_projection",
                )
                self._require_settled_projection(
                    projection,
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                )
                continue
            if candidate.sqlite_active:
                try:
                    await self._stop_authority_run(strategy_instance_id, run_id)
                except Exception as exc:
                    raise BootAuthorityPreparationError(
                        f"SQLite boot stop failed for {strategy_instance_id!r}"
                    ) from exc
            repo = self._lifecycle_repo_for(strategy_instance_id)
            try:
                record = repo.read()
            except BotLifecycleStateCorruptError as exc:
                logger.warning(
                    "Boot sweep skipping corrupt lifecycle state",
                    extra={"action": "boot_sweep_corrupt_lifecycle", "path": str(exc.path)},
                )
                continue

            desired_repo = self._desired_repo_for(strategy_instance_id)
            desired_state = desired_repo.read_state()
            if (
                record is not None
                and record.phase is BotLifecyclePhase.OFF_DUTY
                and record.duty_outcome is not None
                and desired_state in {DesiredState.RUNNING, DesiredState.PAUSED}
            ):
                projection = projector.refresh(
                    strategy_instance_id=strategy_instance_id,
                    now_ms=self._now_ms(),
                    updated_by="bot_runner_boot_sweep",
                    reason="repair_terminal_projection",
                )
                if not self._require_settled_projection(
                    projection,
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                ):
                    continue
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
            needs_interrupted_evidence = candidate.sqlite_active or (
                record is not None and record.phase is BotLifecyclePhase.ON_DUTY
            ) or (
                desired_state in {DesiredState.RUNNING, DesiredState.PAUSED}
                and (
                    record is None
                    or (
                        record.phase is BotLifecyclePhase.OFF_DUTY
                        and record.duty_outcome is None
                    )
                )
            )
            if not needs_interrupted_evidence:
                projection = projector.refresh(
                    strategy_instance_id=strategy_instance_id,
                    now_ms=self._now_ms(),
                    updated_by="bot_runner_boot_sweep",
                    reason="refresh_boot_projection",
                )
                self._require_settled_projection(
                    projection,
                    strategy_instance_id=strategy_instance_id,
                    run_id=run_id,
                )
                continue

            now_ms = self._now_ms()
            outcome = BotDutyOutcome(
                kind="EXITED_UNVERIFIED",
                reason_code="INTERRUPTED_BY_RESTART",
                recorded_at_ms=now_ms,
                run_id=run_id,
            )
            update_result = projector.project_terminal(
                strategy_instance_id=strategy_instance_id,
                outcome=outcome,
                now_ms=now_ms,
                updated_by="bot_runner_boot_sweep",
                reason="container_restart",
            )
            if not self._require_settled_projection(
                update_result,
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
            ):
                continue
            desired_repo.set(
                DesiredState.STOPPED,
                updated_by="bot_runner_boot_sweep",
                now_ms=now_ms,
                reason="interrupted_by_restart",
            )
            interrupted.append(strategy_instance_id)
            logger.warning(
                "Boot sweep recorded interrupted bot",
                extra={
                    "action": "boot_sweep_interrupted",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": run_id,
                },
            )
        return interrupted

    @staticmethod
    def _require_settled_projection(
        result: AlpacaLifecycleProjectionResult,
        *,
        strategy_instance_id: str,
        run_id: str,
    ) -> bool:
        if result.status is ProjectionStatus.RECORDED:
            return True
        if result.status is ProjectionStatus.AUTHORITY_EXPECTATION_SUPERSEDED:
            logger.info(
                "Boot sweep skipped authority-superseded lifecycle expectation",
                extra={
                    "action": "boot_sweep_superseded",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": run_id,
                },
            )
            return False
        detail = result.status.value
        if result.refusal_reason is not None:
            detail = f"{detail}:{result.refusal_reason}"
        raise BootAuthorityPreparationError(
            f"Lifecycle projection repair remained unresolved for "
            f"{strategy_instance_id!r}: {detail}"
        )
