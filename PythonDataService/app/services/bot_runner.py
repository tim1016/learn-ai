"""In-container bot runner: supervised asyncio tasks + durable lifecycle artifacts.

One :class:`BotTaskRegistry` lives in the polygon-data-service process and
owns spawn, liveness, and reap for every strategy-instance bot task. This path
has no host daemon or subprocess; a guard test enforces that boundary.

The registry keeps desired intent, lifecycle, immutable configuration,
append-only launch/terminal evidence, and the replaceable current-run pointer
in the existing ``live_state/<sid>/`` operator-plane artifact tree.

Exit taxonomy (typed, durable, artifact-derived — never liveness-inferred):

- operator stop / service shutdown → ``duty_outcome.kind = "STOPPED"``
  (``reason_code`` ``OPERATOR_STOP`` / ``SERVICE_SHUTDOWN``).
- unhandled exception in the bot → ``"CRASHED"`` with the exception class as
  ``reason_code`` (``FEED_DEATH`` for a dead market-data feed).
- task cancelled without stop intent (a kill) → ``"EXITED_UNVERIFIED"``
  with ``CANCELLED_WITHOUT_STOP_INTENT``.
- bar stream ended on its own → ``"EXITED_UNVERIFIED"`` with
  ``BAR_STREAM_ENDED``.

Restart intensity uses the canonical :class:`RestartIntensityPolicy`. Trade
mode delegates effects to the Alpaca Clerk; the runner never authors broker
execution truth.

All temporal fields are ``int64 ms UTC`` per ``.claude/rules/temporal-rigor.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.config import settings
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.engine.live.identity import strategy_instance_artifact_dir
from app.marketdata.feed import MarketDataFeed, MarketDataFeedError
from app.schemas.broker_bots import (
    BotProcessFact,
    BotRunHistoryPage,
    BotRunView,
    BotStatusView,
)
from app.schemas.run_admission import (
    ResumeCheckpointAdmissionFact,
    RunAdmissionDecision,
    RunProcessAdmissionFact,
    StartRuntimeAdmissionFact,
)
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.bot_boot_recovery import BootRecoveryReport, BotBootRecovery
from app.services.bot_carryover import (
    checkpoint_status,
    read_checkpoint,
)
from app.services.bot_dry_run import DryRunActivity
from app.services.bot_registry_projection import (
    project_bot_status,
    project_process_fact,
    read_dry_run_activity,
)
from app.services.bot_resume_admission import (
    AdmittedBotResume,
    BotResumeAdmission,
)
from app.services.bot_run_evidence import (
    PROVISIONAL_STOP_REASON_CODE,
    BotRunEvidenceService,
)
from app.services.bot_run_terminal import (
    BotRunTerminalRecorder,
    prove_terminal_stop_outcome,
)
from app.services.bot_runner_errors import (
    BootRecoveryIncompleteError,
    BotAlreadyRunningError,
    BotRunnerError,
    CarryoverPolicyRefusedError,
    InvalidStrategyInstanceIdError,
    MarketDataFeedUnavailableError,
    RecoveryUncertainError,
    RestartIntensityRefusedError,
    RunAdmissionRefusedError,
    UnknownBotError,
    raise_run_refusal,
    require_start_configuration,
)
from app.services.bot_runtime import (
    ManagedBot,
    execute_bot_run,
    require_live_managed_bot,
)
from app.services.bot_start_admission import (
    AdmittedBotStart,
    BotStartAdmission,
    StartAdmissionDenied,
    StartAdmissionEvidenceChanged,
    StartAdmissionUnavailable,
    default_start_custody_guard,
    log_run_launch,
    make_start_request,
    resolve_start_runtime_fact,
)
from app.utils.timestamps import now_ms_utc

__all__ = [
    "AdmittedBotResume",
    "AdmittedBotStart",
    "BootRecoveryIncompleteError",
    "BotAlreadyRunningError",
    "BotRunnerError",
    "BotTaskRegistry",
    "CarryoverPolicyRefusedError",
    "InvalidStrategyInstanceIdError",
    "MarketDataFeedUnavailableError",
    "RecoveryUncertainError",
    "RestartIntensityRefusedError",
    "RunAdmissionRefusedError",
    "UnknownBotError",
]

logger = logging.getLogger(__name__)

_CARRYOVER_CHECKPOINT_FILENAME = "carryover_checkpoint.json"
_UPDATED_BY = "bot_runner"
_STOP_TIMEOUT_S = 5.0


class BotTaskRegistry:
    """Spawn, track, and reap one supervised asyncio task per bot.

    ``feed_resolver`` returns the process-level shared :class:`MarketDataFeed`
    (or ``None`` when the feed is not installed) — resolved per deploy so the
    registry can be constructed before the feed exists.
    """

    def __init__(
        self,
        artifacts_root: Path,
        *,
        feed_resolver: Callable[[], MarketDataFeed | None],
        restart_policy: RestartIntensityPolicy | None = None,
        now_ms: Callable[[], int] = now_ms_utc,
        boot_recovery_required: bool = True,
        supported_broker_ids: frozenset[str] | None = None,
        carryover_allowed: bool | None = None,
        start_custody_guard: Callable[[str], AbstractAsyncContextManager[ClerkCustodySnapshot]] | None = None,
    ) -> None:
        self._artifacts_root = Path(artifacts_root)
        self._feed_resolver = feed_resolver
        self._restart_policy = restart_policy or RestartIntensityPolicy()
        self._now_ms = now_ms
        self._registry_generation = uuid4().hex
        self._bots: dict[str, ManagedBot] = {}
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._start_history: dict[str, list[int]] = {}
        # S5 (#1263) fail-closed start gate: no bot starts until the boot
        # recovery sweep has run, and none while recovery left an uncertain
        # outcome (the probe re-evaluates per deploy, so a later resolution
        # unblocks without a restart). Tests that do not exercise recovery
        # opt out explicitly with ``boot_recovery_required=False``.
        self._boot_recovery_required = boot_recovery_required
        self._boot_recovery_complete = False
        self._unresolved_intents_probe: Callable[[], Awaitable[int]] | None = None
        # When set, the boot sweep skips bots whose binding carries a broker
        # tag that is not in this set (e.g. IBKR bots share the same
        # artifacts_root but are managed by the host daemon, not the
        # in-container runner).
        self._supported_broker_ids = supported_broker_ids
        self._carryover_allowed = (
            settings.ALPACA_PAPER_CARRYOVER_ENABLED if carryover_allowed is None else carryover_allowed
        )
        self._bindings = BotBindingRepository(
            self._artifacts_root,
            instance_dir_for=self._confined_instance_dir,
        )
        self._boot_recovery = BotBootRecovery(
            self._artifacts_root,
            lifecycle_repo_for=self._lifecycle_repo,
            desired_repo_for=self._desired_repo,
            manages_instance=self._manages_boot_recovery,
            is_running=self._is_running,
            now_ms=self._now_ms,
        )
        self._start_admission = BotStartAdmission(
            now_ms=self._now_ms,
            feed_resolver=self._feed_resolver,
            custody_guard=start_custody_guard or default_start_custody_guard,
            process_fact=self._start_process_fact,
            runtime_fact=self._start_runtime_fact,
            activate=self._activate_start_binding,
        )
        self._resume_admission = BotResumeAdmission(
            now_ms=self._now_ms,
            feed_resolver=self._feed_resolver,
            custody_guard=start_custody_guard or default_start_custody_guard,
            process_fact=self._resume_process_fact,
            runtime_fact=self._start_runtime_fact,
            checkpoint=self._resume_checkpoint_fact,
            activate=self._activate_resume_binding,
            carryover_account_policy_enabled=self._carryover_allowed,
        )
        self._run_evidence = BotRunEvidenceService(
            self._bindings,
            lifecycle_repo_for=self._lifecycle_repo,
        )
        self._terminal = BotRunTerminalRecorder(
            managed_bots=self._bots,
            desired_repo_for=self._desired_repo,
            run_evidence=self._run_evidence,
            now_ms=self._now_ms,
        )

    # ── deploy / stop ─────────────────────────────────────────────────

    async def deploy(
        self,
        *,
        broker: str,
        strategy_instance_id: str,
        strategy_key: str = "deployment_validation",
        symbol: str,
        use_rth: bool = True,
        mode: Literal["log_only", "dry_run", "trade"] = "log_only",
        quantity: int = 1,
        carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID",
    ) -> BotStatusView:
        """Deploy and start a bot; durable evidence before liveness."""
        return (
            await self.deploy_with_admission(
                broker=broker,
                strategy_instance_id=strategy_instance_id,
                strategy_key=strategy_key,
                symbol=symbol,
                use_rth=use_rth,
                mode=mode,
                quantity=quantity,
                carryover_policy=carryover_policy,
            )
        ).bot

    async def deploy_with_admission(
        self,
        *,
        broker: str,
        strategy_instance_id: str,
        strategy_key: str = "deployment_validation",
        symbol: str,
        use_rth: bool = True,
        mode: Literal["log_only", "dry_run", "trade"] = "log_only",
        quantity: int = 1,
        carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID",
    ) -> AdmittedBotStart:
        """Start one bot and return the exact execution-time admission."""
        require_start_configuration(
            carryover_policy,
            carryover_allowed=self._carryover_allowed,
        )
        self._confined_instance_dir(strategy_instance_id)
        request = make_start_request(
            broker=broker,
            strategy_instance_id=strategy_instance_id,
            strategy_key=strategy_key,
            symbol=symbol,
            use_rth=use_rth,
            mode=mode,
            quantity=quantity,
            carryover_policy=carryover_policy,
            action_plan=alpaca_v1_action_plan(symbol),
        )
        async with self._operation_lock(strategy_instance_id):
            try:
                return await self._start_admission.start(request)
            except StartAdmissionDenied as exc:
                raise_run_refusal(exc.decision)
            except StartAdmissionUnavailable as exc:
                raise RunAdmissionRefusedError(str(exc), detail=exc.detail) from exc
            except StartAdmissionEvidenceChanged as exc:
                raise RunAdmissionRefusedError(
                    "Start admission could not obtain stable Clerk custody.",
                    detail="Refresh admission after Clerk reconciliation settles.",
                ) from exc

    async def preview_start_admission(
        self,
        *,
        broker: str,
        strategy_instance_id: str,
        strategy_key: str = "deployment_validation",
        symbol: str,
        use_rth: bool = True,
        mode: Literal["log_only", "dry_run", "trade"] = "log_only",
        quantity: int = 1,
        carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID",
    ) -> RunAdmissionDecision:
        """Project the same Start decision used immediately before mutation."""
        require_start_configuration(
            carryover_policy,
            carryover_allowed=self._carryover_allowed,
        )
        self._confined_instance_dir(strategy_instance_id)
        request = make_start_request(
            broker=broker,
            strategy_instance_id=strategy_instance_id,
            strategy_key=strategy_key,
            symbol=symbol,
            use_rth=use_rth,
            mode=mode,
            quantity=quantity,
            carryover_policy=carryover_policy,
            action_plan=alpaca_v1_action_plan(symbol),
        )
        async with self._operation_lock(strategy_instance_id):
            try:
                return await self._start_admission.preview(request)
            except StartAdmissionUnavailable as exc:
                raise RunAdmissionRefusedError(str(exc), detail=exc.detail) from exc
            except StartAdmissionEvidenceChanged as exc:
                raise RunAdmissionRefusedError(
                    "Start admission could not obtain stable Clerk custody.",
                    detail="Refresh admission after Clerk reconciliation settles.",
                ) from exc

    async def resume_existing(self, broker: str, strategy_instance_id: str) -> BotStatusView:
        """Start a new run from one stopped bot's durable deployment binding.

        Resume never reconstructs strategy semantics in the panel. It reads the
        backend-owned immutable configuration, preserves its ``ActionPlan``, and
        launches a newly identified run through the same recovery and restart
        gates as a first deployment.
        """
        return (await self.resume_existing_with_admission(broker, strategy_instance_id)).bot

    async def preview_resume_admission(
        self,
        broker: str,
        strategy_instance_id: str,
    ) -> RunAdmissionDecision:
        """Project the exact new-run Resume decision without mutation."""
        async with self._operation_lock(strategy_instance_id):
            binding = self.binding_for_control(broker, strategy_instance_id)
            try:
                return await self._resume_admission.preview(
                    binding,
                    self.status(broker, strategy_instance_id),
                )
            except StartAdmissionUnavailable as exc:
                raise RunAdmissionRefusedError(str(exc), detail=exc.detail) from exc
            except StartAdmissionEvidenceChanged as exc:
                raise RunAdmissionRefusedError(
                    "Resume admission could not obtain stable Clerk custody.",
                    detail="Refresh admission after Clerk reconciliation settles.",
                ) from exc

    async def resume_existing_with_admission(
        self,
        broker: str,
        strategy_instance_id: str,
    ) -> AdmittedBotResume:
        """Create a new run using the same policy exposed by preview."""
        async with self._operation_lock(strategy_instance_id):
            binding = self.binding_for_control(broker, strategy_instance_id)
            try:
                return await self._resume_admission.resume(
                    binding,
                    self.status(broker, strategy_instance_id),
                )
            except StartAdmissionDenied as exc:
                raise_run_refusal(exc.decision)
            except StartAdmissionUnavailable as exc:
                raise RunAdmissionRefusedError(str(exc), detail=exc.detail) from exc
            except StartAdmissionEvidenceChanged as exc:
                raise RunAdmissionRefusedError(
                    "Resume admission could not obtain stable Clerk custody.",
                    detail="Refresh admission after Clerk reconciliation settles.",
                ) from exc

    async def _activate_start_binding(
        self,
        binding: BrokerBotBinding,
        feed: MarketDataFeed,
        now_ms: int,
    ) -> BotStatusView:
        await self._activate_binding(binding, feed, now=now_ms, reason="deploy")
        log_run_launch(binding, reason="deploy")
        return self.status(binding.broker, binding.strategy_instance_id)

    async def _activate_resume_binding(
        self,
        binding: BrokerBotBinding,
        feed: MarketDataFeed,
        now_ms: int,
    ) -> BotStatusView:
        await self._activate_binding(binding, feed, now=now_ms, reason="resume")
        log_run_launch(binding, reason="resume")
        return self.status(binding.broker, binding.strategy_instance_id)

    async def _activate_binding(
        self,
        binding: BrokerBotBinding,
        feed: MarketDataFeed,
        *,
        now: int,
        reason: Literal["deploy", "resume"],
    ) -> None:
        """Write run evidence and install supervision while caller holds its gate."""
        lifecycle_repo = self._lifecycle_repo(binding.strategy_instance_id)
        # Preserve the prior immutable outcome before record_launch advances
        # current_run.json. A crash between these operations must not hide a
        # previous run's only terminal evidence from the history projection.
        self._run_evidence.preserve_terminal(
            binding.strategy_instance_id,
            lifecycle_repo.read(),
        )
        self._bindings.record_launch(binding, launch_reason=reason)
        self._desired_repo(binding.strategy_instance_id).set(
            DesiredState.RUNNING, updated_by=_UPDATED_BY, now_ms=now, reason=reason
        )
        lifecycle_repo.set_phase(
            BotLifecyclePhase.ON_DUTY,
            now_ms=now,
            updated_by=_UPDATED_BY,
            active_run_id=binding.run_id,
            carryover_policy=binding.carryover_policy,
            reason=f"{reason}_{binding.mode}_bot",
        )
        task = asyncio.create_task(self._supervise(binding, feed), name=f"bot:{binding.strategy_instance_id}")
        managed = ManagedBot(
            binding=binding,
            task=task,
        )
        managed.run_gate.set()
        self._bots[binding.strategy_instance_id] = managed
        self._start_history[binding.strategy_instance_id] = [
            *self._starts_in_window(binding.strategy_instance_id, now),
            now,
        ]
        # Let supervision enter its exception boundary before a Start releases
        # Clerk intake. A first effect waits on that same fence.
        await asyncio.sleep(0)

    def _start_process_fact(
        self,
        binding: BrokerBotBinding,
        observed_at_ms: int,
    ) -> RunProcessAdmissionFact:
        stored_binding = self._read_binding(binding.strategy_instance_id)
        lifecycle = self._lifecycle_repo(binding.strategy_instance_id).read()
        managed = self._bots.get(binding.strategy_instance_id)
        if stored_binding is None:
            state = "ABSENT" if lifecycle is None and managed is None else "UNKNOWN"
            return RunProcessAdmissionFact(
                state=state,
                registry_generation=self._registry_generation,
                observed_at_ms=observed_at_ms,
            )
        current = self.process_fact(binding.broker, binding.strategy_instance_id)
        return RunProcessAdmissionFact(
            state=current.state,
            run_id=current.run_id,
            process_identity=current.process_identity,
            registry_generation=current.registry_generation,
            observed_at_ms=current.observed_at_ms,
        )

    async def _start_runtime_fact(
        self,
        strategy_instance_id: str,
        observed_at_ms: int,
    ) -> StartRuntimeAdmissionFact:
        return await resolve_start_runtime_fact(
            strategy_instance_id=strategy_instance_id,
            observed_at_ms=observed_at_ms,
            boot_recovery_required=self._boot_recovery_required,
            boot_recovery_complete=self._boot_recovery_complete,
            unresolved_intents_probe=self._unresolved_intents_probe,
            projected_start_count=self._projected_start_count(strategy_instance_id, observed_at_ms),
            restart_threshold=self._restart_policy.threshold,
            restart_window_ms=self._restart_policy.window_ms,
        )

    def _resume_process_fact(
        self,
        binding: BrokerBotBinding,
        observed_at_ms: int,
    ) -> RunProcessAdmissionFact:
        process = self.process_fact(binding.broker, binding.strategy_instance_id)
        return RunProcessAdmissionFact(
            state=process.state,
            run_id=process.run_id,
            process_identity=process.process_identity,
            registry_generation=process.registry_generation,
            observed_at_ms=observed_at_ms,
        )

    def _resume_checkpoint_fact(
        self,
        binding: BrokerBotBinding,
    ) -> ResumeCheckpointAdmissionFact | None:
        path = self._carryover_checkpoint_path(binding.strategy_instance_id)
        checkpoint = read_checkpoint(path)
        if checkpoint is None:
            return None
        return ResumeCheckpointAdmissionFact(
            account_id=checkpoint.account_id,
            stopped_run_id=checkpoint.stopped_run_id,
            configuration_hash=checkpoint.configuration_hash,
            exposure=checkpoint.exposure,
            approved=checkpoint.approved,
            evidence_ref=f"carryover-checkpoint:{path.name}:{checkpoint.recorded_at_ms}",
        )

    async def stop(
        self,
        broker: str,
        strategy_instance_id: str,
        *,
        updated_by: str = "operator",
        reason: str | None = None,
    ) -> BotStatusView:
        """Button-Rule exit: durable STOPPED intent first, then cancel + reap."""
        async with self._operation_lock(strategy_instance_id):
            return await self._stop_locked(
                broker,
                strategy_instance_id,
                updated_by=updated_by,
                reason=reason,
            )

    async def pause(
        self,
        broker: str,
        strategy_instance_id: str,
        *,
        updated_by: str = "operator",
        reason: str | None = None,
    ) -> BotStatusView:
        """Pause bar evaluation without ending or replacing the current run."""
        async with self._operation_lock(strategy_instance_id):
            self._confined_instance_dir(strategy_instance_id)
            managed = require_live_managed_bot(self._bots, broker, strategy_instance_id)
            current = self._desired_repo(strategy_instance_id).read_state()
            if current is DesiredState.PAUSED:
                raise RunAdmissionRefusedError(
                    "The current run is already paused.",
                    detail="Use Continue to let this same run evaluate bars again.",
                )
            managed.run_gate.clear()
            try:
                self._desired_repo(strategy_instance_id).set(
                    DesiredState.PAUSED,
                    updated_by=updated_by,
                    now_ms=self._now_ms(),
                    reason=reason or "operator_pause",
                )
            except Exception:
                managed.run_gate.set()
                raise
            return self.status(broker, strategy_instance_id)

    async def continue_paused(
        self,
        broker: str,
        strategy_instance_id: str,
        *,
        updated_by: str = "operator",
        reason: str | None = None,
    ) -> BotStatusView:
        """Continue one paused live run without changing its run identity."""
        async with self._operation_lock(strategy_instance_id):
            self._confined_instance_dir(strategy_instance_id)
            managed = require_live_managed_bot(self._bots, broker, strategy_instance_id)
            current = self._desired_repo(strategy_instance_id).read_state()
            if current is not DesiredState.PAUSED:
                raise RunAdmissionRefusedError(
                    "Continue requires an authoritatively live paused run.",
                    detail="Use Resume only after a stopped run has terminal evidence.",
                )
            self._desired_repo(strategy_instance_id).set(
                DesiredState.RUNNING,
                updated_by=updated_by,
                now_ms=self._now_ms(),
                reason=reason or "operator_continue",
            )
            managed.run_gate.set()
            return self.status(broker, strategy_instance_id)

    async def _stop_locked(
        self,
        broker: str,
        strategy_instance_id: str,
        *,
        updated_by: str,
        reason: str | None,
    ) -> BotStatusView:
        """Serialized STOP implementation with terminal Clerk custody proof."""
        self._confined_instance_dir(strategy_instance_id)
        managed = self._bots.get(strategy_instance_id)
        if managed is None or managed.task.done():
            raise UnknownBotError(
                f"Bot '{strategy_instance_id}' is not running.",
                detail="Only a running bot can be stopped; see its status for the last outcome.",
            )
        if managed.binding.broker != broker:
            raise UnknownBotError(
                f"Bot '{strategy_instance_id}' is not bound to broker '{broker}'.",
                detail=f"The bot's binding carries broker '{managed.binding.broker}'.",
            )
        now = self._now_ms()
        # Durable intent BEFORE the in-process cancellation: if the container
        # dies between these two steps, the STOPPED intent survives.
        self._desired_repo(strategy_instance_id).set(
            DesiredState.STOPPED,
            updated_by=updated_by,
            now_ms=now,
            reason=reason or "operator_stop",
        )
        # Stop strategy evaluation before any network-bound custody work. The
        # provisional terminal is replaced under this instance's operation lock
        # once the Clerk returns a fresh proof.
        managed.stop_reason_code = PROVISIONAL_STOP_REASON_CODE
        managed.task.cancel()
        await asyncio.wait({managed.task}, timeout=_STOP_TIMEOUT_S)
        # Backstop for a coroutine that never entered supervision (cancelled
        # pre-start): _finalize is idempotent, so this is a no-op whenever the
        # supervisor already recorded the outcome.
        self._terminal.finalize(
            managed.binding,
            kind="STOPPED",
            reason_code=PROVISIONAL_STOP_REASON_CODE,
        )
        self._terminal.reap(strategy_instance_id, managed.binding.run_id)
        outcome = "OPERATOR_STOP"
        if broker == "alpaca" and managed.binding.mode == "trade":
            outcome = await prove_terminal_stop_outcome(
                managed.binding,
                checkpoint_path=self._carryover_checkpoint_path(strategy_instance_id),
                now_ms=self._now_ms,
            )
        self._terminal.replace_provisional_stop(
            managed.binding,
            reason_code=outcome,
        )
        return self.status(broker, strategy_instance_id)

    async def stop_all(self) -> None:
        """Service shutdown: stop every task without overwriting operator intent."""
        stopping: list[ManagedBot] = []
        for managed in self._bots.values():
            if managed.task.done():
                continue
            managed.stop_reason_code = "SERVICE_SHUTDOWN"
            managed.task.cancel()
            stopping.append(managed)
        if stopping:
            await asyncio.wait([m.task for m in stopping], timeout=_STOP_TIMEOUT_S)
        for managed in stopping:
            # Idempotent backstop, same as stop().
            self._terminal.finalize(
                managed.binding,
                kind="STOPPED",
                reason_code="SERVICE_SHUTDOWN",
            )
            self._terminal.reap(
                managed.binding.strategy_instance_id,
                managed.binding.run_id,
            )

    # ── S5 boot recovery (container restart is a drilled event) ───────

    async def run_boot_recovery(
        self,
        *,
        recover: Callable[[], Awaitable[None]] | None = None,
        reconcile: Callable[[], Awaitable[object]] | None = None,
        unresolved_intents_probe: Callable[[], Awaitable[int]] | None = None,
    ) -> BootRecoveryReport:
        """Reconcile durable ON_DUTY state against the (empty) task registry.

        Every instance recorded on-duty with no live task gets typed durable
        interrupted evidence (``EXITED_UNVERIFIED`` / ``INTERRUPTED_BY_RESTART``)
        and is NEVER auto-restarted. Then the clerk's ``recover`` (identity
        resolution — present/absent/uncertain, no blind retry) and a
        reconciliation pass run; a failure in either is surfaced and leaves the
        journal to speak for itself — the per-deploy uncertainty probe keeps
        starts refused until the intents actually resolve.
        """
        report = await self._boot_recovery.run(
            recover=recover,
            reconcile=reconcile,
            unresolved_intents_probe=unresolved_intents_probe,
        )
        self._unresolved_intents_probe = unresolved_intents_probe
        self._boot_recovery_complete = True
        return report

    async def _require_recovered(self) -> None:
        """Fail-closed start gate (S5 AC4)."""
        if self._boot_recovery_required and not self._boot_recovery_complete:
            raise BootRecoveryIncompleteError(
                "Bot starts are refused until the boot recovery sweep completes.",
                detail="The data plane restarted; durable state is being reconciled.",
            )
        if self._unresolved_intents_probe is not None:
            unresolved = await self._unresolved_intents_probe()
            if unresolved > 0:
                raise RecoveryUncertainError(
                    f"Bot starts are refused: {unresolved} order intent(s) remain unresolved after recovery.",
                    detail=(
                        "An unresolved intent may still represent live broker "
                        "exposure; resolve it (recovery replay / sweep) before "
                        "starting bots."
                    ),
                )

    # ── read surface ──────────────────────────────────────────────────

    def status(self, broker: str, strategy_instance_id: str) -> BotStatusView:
        """One bot's roster row, artifact-derived + registry liveness."""
        self._confined_instance_dir(strategy_instance_id)
        binding = self._read_binding(strategy_instance_id)
        if binding is None or binding.broker != broker:
            raise UnknownBotError(
                f"No bot '{strategy_instance_id}' is bound to broker '{broker}'.",
                detail="Deploy the bot first; bindings are broker-tagged.",
            )
        return self._compose_status(binding)

    def process_fact(self, broker: str, strategy_instance_id: str) -> BotProcessFact:
        """Return process-owner evidence without inferring broker custody."""
        self._confined_instance_dir(strategy_instance_id)
        binding = self._read_binding(strategy_instance_id)
        if binding is None or binding.broker != broker:
            raise UnknownBotError(
                f"No bot '{strategy_instance_id}' is bound to broker '{broker}'.",
                detail="Deploy the bot first; bindings are broker-tagged.",
            )

        return project_process_fact(
            binding,
            self._lifecycle_repo(strategy_instance_id).read(),
            self._bots.get(strategy_instance_id),
            registry_generation=self._registry_generation,
            observed_at_ms=self._now_ms(),
        )

    def panel_action_receipt_path(self, strategy_instance_id: str) -> Path:
        """Return this instance's durable panel-command receipt location.

        Panel commands are lifecycle custody, so their idempotency evidence is
        co-located with the binding and lifecycle artifacts rather than held in
        a process-local web handler.
        """
        return self._confined_instance_dir(strategy_instance_id) / "panel_action_receipts.json"

    def binding_for_control(self, broker: str, strategy_instance_id: str) -> BrokerBotBinding:
        """Return immutable deployed configuration for a Clerk control action."""
        binding = self._read_binding(strategy_instance_id)
        if binding is None or binding.broker != broker:
            raise UnknownBotError(
                f"No bot '{strategy_instance_id}' is bound to broker '{broker}'.",
                detail="Deploy the bot first; bindings are broker-tagged.",
            )
        return binding

    def list_bots(self, broker: str) -> list[BotStatusView]:
        """All bots whose durable binding carries ``broker``."""
        return [self._compose_status(binding) for binding in self._bindings.list_for_broker(broker)]

    def current_run(self, broker: str, strategy_instance_id: str) -> BotRunView:
        """Return the backend-owned current-run projection."""
        binding = self.binding_for_control(broker, strategy_instance_id)
        return self._run_evidence.current(
            binding,
            self.process_fact(broker, strategy_instance_id),
        )

    def run_history(
        self,
        broker: str,
        strategy_instance_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> BotRunHistoryPage:
        """Return a bounded page of previous-run projections."""
        binding = self.binding_for_control(broker, strategy_instance_id)
        return self._run_evidence.history(
            binding,
            cursor=cursor,
            limit=limit,
        )

    def dry_run_activity(
        self,
        broker: str,
        strategy_instance_id: str,
        *,
        limit: int = 8,
    ) -> list[DryRunActivity]:
        """Return bounded, explicitly simulated activity for a dry instance."""
        binding = self.binding_for_control(broker, strategy_instance_id)
        return read_dry_run_activity(
            binding,
            self._confined_instance_dir(strategy_instance_id),
            limit=limit,
        )

    # ── supervision ───────────────────────────────────────────────────

    async def _supervise(self, binding: BrokerBotBinding, feed: MarketDataFeed) -> None:
        """Run the bot; on ANY exit record a typed durable duty outcome, then reap."""
        sid = binding.strategy_instance_id
        managed = self._bots.get(sid)
        run_gate = (
            managed.run_gate
            if managed is not None and managed.binding.run_id == binding.run_id
            else None
        )
        try:
            await execute_bot_run(
                binding,
                feed,
                run_gate=run_gate,
                instance_dir=self._confined_instance_dir(sid),
            )
        except asyncio.CancelledError:
            managed = self._bots.get(sid)
            stop_reason = managed.stop_reason_code if managed is not None else None
            if stop_reason is not None:
                self._terminal.finalize(binding, kind="STOPPED", reason_code=stop_reason)
            else:
                # A cancellation nobody asked for is a kill, not a clean stop.
                self._terminal.finalize(
                    binding,
                    kind="EXITED_UNVERIFIED",
                    reason_code="CANCELLED_WITHOUT_STOP_INTENT",
                )
            raise
        except MarketDataFeedError as exc:
            logger.error(
                "Bot crashed: market-data feed died",
                extra={"action": "bot_crashed", "strategy_instance_id": sid, "error": str(exc)},
            )
            self._terminal.finalize(binding, kind="CRASHED", reason_code="FEED_DEATH")
        except Exception as exc:
            # Supervision boundary: every crash becomes typed durable evidence
            # plus a logged traceback — deliberately not re-raised, so the
            # orphaned task does not double-log via the event loop.
            logger.exception(
                "Bot crashed",
                extra={"action": "bot_crashed", "strategy_instance_id": sid},
            )
            self._terminal.finalize(
                binding,
                kind="CRASHED",
                reason_code=type(exc).__name__,
            )
        else:
            self._terminal.finalize(
                binding,
                kind="EXITED_UNVERIFIED",
                reason_code="BAR_STREAM_ENDED",
            )
        finally:
            self._terminal.reap(sid, binding.run_id)

    # ── guards and composition ────────────────────────────────────────

    def _operation_lock(self, strategy_instance_id: str) -> asyncio.Lock:
        """One lifecycle mutation at a time for a strategy instance."""
        return self._operation_locks.setdefault(strategy_instance_id, asyncio.Lock())

    def _is_running(self, strategy_instance_id: str) -> bool:
        managed = self._bots.get(strategy_instance_id)
        return managed is not None and not managed.task.done()

    def _manages_boot_recovery(self, strategy_instance_id: str) -> bool:
        """Keep daemon-owned broker artifacts out of this runner's sweep."""
        if self._supported_broker_ids is None:
            return True
        try:
            binding = self._read_binding(strategy_instance_id)
        except InvalidStrategyInstanceIdError:
            return False
        except (OSError, ValidationError, ValueError) as exc:
            logger.warning(
                "Boot sweep skipping undecodable broker binding",
                extra={
                    "action": "boot_sweep_undecodable_binding",
                    "strategy_instance_id": strategy_instance_id,
                    "error": str(exc),
                },
            )
            return False
        return binding is not None and binding.broker in self._supported_broker_ids

    def _carryover_checkpoint_path(self, strategy_instance_id: str) -> Path:
        return self._confined_instance_dir(strategy_instance_id) / _CARRYOVER_CHECKPOINT_FILENAME

    def _enforce_restart_intensity(self, strategy_instance_id: str, now_ms: int) -> None:
        """Per-bot projection mirror of ``project_restart_intensity_gate``:
        refuse when ``prior_starts_in_window + 1 >= threshold``."""
        projected_count = self._projected_start_count(strategy_instance_id, now_ms)
        if projected_count >= self._restart_policy.threshold:
            raise RestartIntensityRefusedError(
                f"Restart intensity for bot '{strategy_instance_id}': "
                f"{projected_count} activation(s) within {self._restart_policy.window_ms} ms "
                f"meets the threshold of {self._restart_policy.threshold}.",
                detail="WAIT_OR_RECOVER_ACCOUNT_BEFORE_STARTING_ANOTHER_BOT",
            )

    def _projected_start_count(self, strategy_instance_id: str, now_ms: int) -> int:
        """Return the next activation count without mutating preview state."""
        return len(self._starts_in_window(strategy_instance_id, now_ms)) + 1

    def _starts_in_window(self, strategy_instance_id: str, now_ms: int) -> list[int]:
        """Return bounded activation history for the current policy window."""
        window_start_ms = now_ms - self._restart_policy.window_ms
        return [
            start_ms
            for start_ms in self._start_history.get(strategy_instance_id, [])
            if window_start_ms <= start_ms <= now_ms
        ]

    def _confined_instance_dir(self, strategy_instance_id: str) -> Path:
        try:
            return strategy_instance_artifact_dir(self._artifacts_root, "live_state", strategy_instance_id)
        except ValueError as exc:
            raise InvalidStrategyInstanceIdError(str(exc)) from exc

    def _lifecycle_repo(self, strategy_instance_id: str) -> BotLifecycleStateRepo:
        return BotLifecycleStateRepo(stable_bot_lifecycle_state_path(self._artifacts_root, strategy_instance_id))

    def _desired_repo(self, strategy_instance_id: str) -> DesiredStateRepo:
        return DesiredStateRepo(stable_desired_state_path(self._artifacts_root, strategy_instance_id))

    def _read_binding(self, strategy_instance_id: str) -> BrokerBotBinding | None:
        return self._bindings.read(strategy_instance_id)

    def _compose_status(self, binding: BrokerBotBinding) -> BotStatusView:
        sid = binding.strategy_instance_id
        lifecycle = self._lifecycle_repo(sid).read()
        desired = self._desired_repo(sid).read_state()
        managed = self._bots.get(sid)
        checkpoint_exposure, checkpoint_matches = checkpoint_status(
            binding,
            self._carryover_checkpoint_path(sid),
        )
        return project_bot_status(
            binding,
            lifecycle,
            desired,
            running=managed is not None and not managed.task.done(),
            carryover_account_policy_enabled=self._carryover_allowed,
            checkpoint_exposure=checkpoint_exposure,
            checkpoint_matches=checkpoint_matches,
        )


# ---------------------------------------------------------------------------
# Process-level singleton — installed at startup in main.py.
# ---------------------------------------------------------------------------

_REGISTRY: BotTaskRegistry | None = None


def get_bot_task_registry() -> BotTaskRegistry | None:
    """Return the process-level bot task registry, or ``None`` when absent."""
    return _REGISTRY


def set_bot_task_registry(registry: BotTaskRegistry | None) -> None:
    """Install (or clear) the process-level bot task registry."""
    global _REGISTRY
    _REGISTRY = registry
