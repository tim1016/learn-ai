"""In-container bot runner: supervised asyncio tasks + durable lifecycle artifacts.

Alpaca Bot Control v2, S2 (#1260) — principles P1, P4, P9, P10; decision L1.

One :class:`BotTaskRegistry` lives in the polygon-data-service process and
owns spawn / liveness / reap for every strategy-instance bot task.  There is
**no host daemon, host socket, or host process anywhere in this path** — the
guard test asserts this module never imports daemon-client or subprocess
machinery.

The registry writes the SAME durable lifecycle artifacts the existing
operator plane already reads, so artifact-derived surfaces stay truthful
without modification:

- ``live_state/<sid>/lifecycle_state.json`` — :class:`BotLifecycleStateRepo`
  (ON_DUTY on start; OFF_DUTY + typed ``duty_outcome`` on any exit).
- ``live_state/<sid>/desired_state.json`` — :class:`DesiredStateRepo`
  (durable operator intent; STOPPED is written BEFORE the task is cancelled
  so the Button-Rule exit survives a crash mid-stop).
- ``live_state/<sid>/broker_binding.json`` — the broker-tagged run binding
  (P9: bindings carry their broker tag from day one).

Exit taxonomy (typed, durable, artifact-derived — never liveness-inferred):

- operator stop / service shutdown → ``duty_outcome.kind = "STOPPED"``
  (``reason_code`` ``OPERATOR_STOP`` / ``SERVICE_SHUTDOWN``).
- unhandled exception in the bot → ``"CRASHED"`` with the exception class as
  ``reason_code`` (``FEED_DEATH`` for a dead market-data feed).
- task cancelled without stop intent (a kill) → ``"EXITED_UNVERIFIED"``
  with ``CANCELLED_WITHOUT_STOP_INTENT``.
- bar stream ended on its own → ``"EXITED_UNVERIFIED"`` with
  ``BAR_STREAM_ENDED``.

Restart intensity reuses the canonical :class:`RestartIntensityPolicy`
parameters and the ``project_restart_intensity_gate`` comparison (refuse when
``prior_starts_in_window + 1 >= threshold``), scoped per bot.  The account
journal is not written by this walking skeleton — durable cross-restart
intensity arrives with the S3 account binding.

The log-only mode consumes closed 1-minute bars and records decisions without
order effects. Trade mode delegates typed ENTER/EXIT decisions to the Alpaca
Clerk; the runner never authors broker sides or execution truth.

All temporal fields are ``int64 ms UTC`` per ``.claude/rules/temporal-rigor.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from app.config import settings
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
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
from app.schemas.action_plan import ActionPlan
from app.schemas.broker_bots import BotDutyOutcomeView, BotProcessFact, BotStatusView
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.bot_boot_recovery import BootRecoveryReport, BotBootRecovery
from app.services.bot_carryover import (
    CarryoverResumeRefusedError,
    checkpoint_status,
    prove_stop_outcome,
    require_resume_custody,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_CARRYOVER_CHECKPOINT_FILENAME = "carryover_checkpoint.json"
_UPDATED_BY = "bot_runner"
_STOP_TIMEOUT_S = 5.0


class BotRunnerError(Exception):
    """Base typed bot-runner error; the router translates to HTTP."""

    http_status: int = 500

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class UnknownBotError(BotRunnerError):
    http_status = 404


class InvalidStrategyInstanceIdError(BotRunnerError):
    http_status = 422


class BotAlreadyRunningError(BotRunnerError):
    http_status = 409


class MarketDataFeedUnavailableError(BotRunnerError):
    http_status = 503


class RestartIntensityRefusedError(BotRunnerError):
    http_status = 429


class BootRecoveryIncompleteError(BotRunnerError):
    http_status = 503


class RecoveryUncertainError(BotRunnerError):
    http_status = 409


class CarryoverPolicyRefusedError(BotRunnerError):
    http_status = 409


@dataclass
class _ManagedBot:
    """One supervised bot task and its stop bookkeeping."""

    binding: BrokerBotBinding
    task: asyncio.Task[None] = field(repr=False)
    # Set BEFORE cancellation by a deliberate stop; a cancel that arrives
    # without this set is a kill and is finalized as EXITED_UNVERIFIED.
    stop_reason_code: str | None = None
    # Exactly one terminal duty outcome per run: set by the first finalize.
    finalized: bool = False


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
    ) -> None:
        self._artifacts_root = Path(artifacts_root)
        self._feed_resolver = feed_resolver
        self._restart_policy = restart_policy or RestartIntensityPolicy()
        self._now_ms = now_ms
        self._registry_generation = uuid4().hex
        self._bots: dict[str, _ManagedBot] = {}
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
            settings.ALPACA_PAPER_CARRYOVER_ENABLED
            if carryover_allowed is None
            else carryover_allowed
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

    # ── deploy / stop ─────────────────────────────────────────────────

    async def deploy(
        self,
        *,
        broker: str,
        strategy_instance_id: str,
        strategy_key: str = "deployment_validation",
        symbol: str,
        use_rth: bool = True,
        mode: Literal["log_only", "trade"] = "log_only",
        quantity: int = 1,
        carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID",
    ) -> BotStatusView:
        """Deploy and start a bot; durable evidence before liveness."""
        if carryover_policy == "ALLOW" and not self._carryover_allowed:
            raise CarryoverPolicyRefusedError(
                "Exposure carryover is disabled for this Alpaca paper account.",
                detail=(
                    "Both the account policy and this deployment must opt in; "
                    "the account policy is currently disabled."
                ),
            )
        async with self._operation_lock(strategy_instance_id):
            return await self._launch(
                broker=broker,
                strategy_instance_id=strategy_instance_id,
                strategy_key=strategy_key,
                symbol=symbol,
                use_rth=use_rth,
                mode=mode,
                quantity=quantity,
                carryover_policy=carryover_policy,
                action_plan=alpaca_v1_action_plan(symbol),
                reason="deploy",
            )

    async def resume_existing(self, broker: str, strategy_instance_id: str) -> BotStatusView:
        """Start a new run from one stopped bot's durable deployment binding.

        Resume never reconstructs strategy semantics in the panel. It reads the
        backend-owned immutable configuration, preserves its ``ActionPlan``, and
        launches a newly identified run through the same recovery and restart
        gates as a first deployment.
        """
        async with self._operation_lock(strategy_instance_id):
            binding = self.binding_for_control(broker, strategy_instance_id)
            await self._require_resume_custody(binding)
            return await self._launch(
                broker=binding.broker,
                strategy_instance_id=binding.strategy_instance_id,
                strategy_key=binding.strategy_key,
                symbol=binding.symbol,
                use_rth=binding.use_rth,
                mode=binding.mode,
                quantity=binding.quantity,
                carryover_policy=binding.carryover_policy,
                action_plan=binding.action_plan,
                reason="resume",
            )

    async def _launch(
        self,
        *,
        broker: str,
        strategy_instance_id: str,
        strategy_key: str,
        symbol: str,
        use_rth: bool,
        mode: Literal["log_only", "trade"],
        quantity: int,
        carryover_policy: Literal["FORBID", "ALLOW"],
        action_plan: ActionPlan,
        reason: Literal["deploy", "resume"],
    ) -> BotStatusView:
        """Launch one run after the shared recovery, intensity, and feed gates."""
        self._confined_instance_dir(strategy_instance_id)
        managed = self._bots.get(strategy_instance_id)
        if managed is not None and not managed.task.done():
            raise BotAlreadyRunningError(
                f"Bot '{strategy_instance_id}' is already running.",
                detail=f"Active run {managed.binding.run_id}; stop it before redeploying.",
            )
        await self._require_recovered()
        now = self._now_ms()
        self._enforce_restart_intensity(strategy_instance_id, now)
        feed = self._feed_resolver()
        if feed is None:
            raise MarketDataFeedUnavailableError(
                "Market-data feed is not available; bot cannot be deployed.",
                detail="The shared MarketDataFeed is not installed (broker disabled or not started).",
            )

        run_id = uuid4().hex
        binding = BrokerBotBinding(
            strategy_instance_id=strategy_instance_id,
            strategy_key=strategy_key,
            broker=broker,
            symbol=symbol,
            use_rth=use_rth,
            mode=mode,
            quantity=quantity,
            carryover_policy=carryover_policy,
            action_plan=action_plan,
            run_id=run_id,
            created_at_ms=now,
        )
        self._bindings.write(binding)
        self._desired_repo(strategy_instance_id).set(
            DesiredState.RUNNING, updated_by=_UPDATED_BY, now_ms=now, reason=reason
        )
        self._lifecycle_repo(strategy_instance_id).set_phase(
            BotLifecyclePhase.ON_DUTY,
            now_ms=now,
            updated_by=_UPDATED_BY,
            active_run_id=run_id,
            carryover_policy=carryover_policy,
            reason=f"{reason}_{mode}_bot",
        )
        task = asyncio.create_task(
            self._supervise(binding, feed), name=f"bot:{strategy_instance_id}"
        )
        self._bots[strategy_instance_id] = _ManagedBot(binding=binding, task=task)
        self._start_history.setdefault(strategy_instance_id, []).append(now)
        # Give the supervisor one scheduling slot so it enters its try block
        # before deploy returns — a cancel that lands on a never-started
        # coroutine would otherwise skip supervision entirely and leave the
        # bot ON_DUTY with no terminal outcome.
        await asyncio.sleep(0)
        logger.info(
            "Bot run launched",
            extra={
                "action": "bot_run_launched",
                "launch_reason": reason,
                "strategy_instance_id": strategy_instance_id,
                "broker": broker,
                "symbol": symbol,
                "run_id": run_id,
            },
        )
        return self.status(broker, strategy_instance_id)

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
        managed.stop_reason_code = "STOPPED_PENDING_CUSTODY_PROOF"
        managed.task.cancel()
        await asyncio.wait({managed.task}, timeout=_STOP_TIMEOUT_S)
        # Backstop for a coroutine that never entered supervision (cancelled
        # pre-start): _finalize is idempotent, so this is a no-op whenever the
        # supervisor already recorded the outcome.
        self._finalize(
            managed.binding,
            kind="STOPPED",
            reason_code="STOPPED_PENDING_CUSTODY_PROOF",
        )
        self._reap(strategy_instance_id, managed.binding.run_id)
        outcome = "OPERATOR_STOP"
        if broker == "alpaca" and managed.binding.mode == "trade":
            outcome = await self._prove_stop_outcome(managed.binding)
        self._lifecycle_repo(strategy_instance_id).record_terminal_outcome(
            BotDutyOutcome(
                kind="STOPPED",
                reason_code=outcome,
                recorded_at_ms=self._now_ms(),
                run_id=managed.binding.run_id,
            ),
            updated_by=_UPDATED_BY,
            reason=outcome,
        )
        return self.status(broker, strategy_instance_id)

    async def stop_all(self) -> None:
        """Service shutdown: stop every task without overwriting operator intent."""
        stopping: list[_ManagedBot] = []
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
            self._finalize(
                managed.binding, kind="STOPPED", reason_code="SERVICE_SHUTDOWN"
            )
            self._reap(managed.binding.strategy_instance_id, managed.binding.run_id)

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
                    f"Bot starts are refused: {unresolved} order intent(s) remain "
                    "unresolved after recovery.",
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

        lifecycle = self._lifecycle_repo(strategy_instance_id).read()
        managed = self._bots.get(strategy_instance_id)
        process_identity: str | None = None
        state: Literal["STARTING", "RUNNING", "STOPPING", "EXITED", "UNKNOWN"] = "UNKNOWN"
        if managed is not None and managed.binding.run_id == binding.run_id:
            process_identity = f"in-process-task:{binding.run_id}"
            lifecycle_matches = (
                lifecycle is not None
                and lifecycle.phase is BotLifecyclePhase.ON_DUTY
                and lifecycle.active_run_id == binding.run_id
            )
            if lifecycle_matches and not managed.task.done():
                state = "STOPPING" if managed.stop_reason_code is not None else "RUNNING"
        elif (
            lifecycle is not None
            and lifecycle.phase is BotLifecyclePhase.OFF_DUTY
            and lifecycle.active_run_id is None
            and lifecycle.duty_outcome is not None
            and lifecycle.duty_outcome.run_id == binding.run_id
        ):
            state = "EXITED"

        return BotProcessFact(
            strategy_instance_id=strategy_instance_id,
            run_id=binding.run_id,
            process_identity=process_identity,
            state=state,
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
        return [
            self._compose_status(binding)
            for binding in self._bindings.list_for_broker(broker)
        ]

    # ── supervision ───────────────────────────────────────────────────

    async def _supervise(self, binding: BrokerBotBinding, feed: MarketDataFeed) -> None:
        """Run the bot; on ANY exit record a typed durable duty outcome, then reap."""
        from app.services.bot_trade_strategy import run_trade_bot

        sid = binding.strategy_instance_id
        try:
            if binding.mode == "trade":
                await run_trade_bot(binding, feed)
            else:
                await self._run_log_only_bot(binding, feed)
        except asyncio.CancelledError:
            managed = self._bots.get(sid)
            stop_reason = managed.stop_reason_code if managed is not None else None
            if stop_reason is not None:
                self._finalize(binding, kind="STOPPED", reason_code=stop_reason)
            else:
                # A cancellation nobody asked for is a kill, not a clean stop.
                self._finalize(
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
            self._finalize(binding, kind="CRASHED", reason_code="FEED_DEATH")
        except Exception as exc:
            # Supervision boundary: every crash becomes typed durable evidence
            # plus a logged traceback — deliberately not re-raised, so the
            # orphaned task does not double-log via the event loop.
            logger.exception(
                "Bot crashed",
                extra={"action": "bot_crashed", "strategy_instance_id": sid},
            )
            self._finalize(binding, kind="CRASHED", reason_code=type(exc).__name__)
        else:
            self._finalize(binding, kind="EXITED_UNVERIFIED", reason_code="BAR_STREAM_ENDED")
        finally:
            self._reap(sid, binding.run_id)

    async def _run_log_only_bot(self, binding: BrokerBotBinding, feed: MarketDataFeed) -> None:
        """Walking skeleton: consume closed bars, log a decision per bar."""
        sid = binding.strategy_instance_id
        logger.info(
            "Log-only bot on duty",
            extra={
                "action": "bot_on_duty",
                "strategy_instance_id": sid,
                "broker": binding.broker,
                "symbol": binding.symbol,
                "run_id": binding.run_id,
            },
        )
        async for bar in feed.stream_bars(binding.symbol, use_rth=binding.use_rth):
            logger.info(
                "Log-only bot decision",
                extra={
                    "action": "bot_decision",
                    "strategy_instance_id": sid,
                    "broker": binding.broker,
                    "decision": "HOLD",
                    "symbol": bar.symbol,
                    "bar_start_ms": bar.start_ms,
                    "bar_end_ms": bar.end_ms,
                    "close": str(bar.close),
                },
            )

    def _finalize(
        self,
        binding: BrokerBotBinding,
        *,
        kind: Literal["STOPPED", "CRASHED", "EXITED_UNVERIFIED"],
        reason_code: str,
    ) -> None:
        """Record the terminal duty fact; OFF_DUTY, run-id fenced, idempotent."""
        managed = self._bots.get(binding.strategy_instance_id)
        if managed is not None and managed.binding.run_id == binding.run_id:
            if managed.finalized:
                return
            managed.finalized = True
        now_ms = self._now_ms()
        if kind in ("CRASHED", "EXITED_UNVERIFIED"):
            # Unexpected terminal exits are fail-closed.  A RUNNING intent with
            # no live task strands the bot because proof-gated Start is legal
            # only from STOPPED; persist the legal recovery state before the
            # lifecycle terminal so a crash between writes remains conservative.
            self._desired_repo(binding.strategy_instance_id).set(
                DesiredState.STOPPED,
                updated_by=_UPDATED_BY,
                now_ms=now_ms,
                reason=f"terminal_outcome:{reason_code}",
            )
        outcome = BotDutyOutcome(
            kind=kind,
            reason_code=reason_code,
            recorded_at_ms=now_ms,
            run_id=binding.run_id,
        )
        self._lifecycle_repo(binding.strategy_instance_id).record_terminal_outcome(
            outcome,
            updated_by=_UPDATED_BY,
            reason=reason_code,
            expected_active_run_id=binding.run_id,
        )

    def _reap(self, strategy_instance_id: str, run_id: str) -> None:
        managed = self._bots.get(strategy_instance_id)
        if managed is not None and managed.binding.run_id == run_id:
            self._bots.pop(strategy_instance_id, None)

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

    async def _prove_stop_outcome(self, binding: BrokerBotBinding) -> str:
        """Cancel entries, obtain fresh custody, and persist the STOP checkpoint."""
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
            checkpoint_path=self._carryover_checkpoint_path(
                binding.strategy_instance_id
            ),
            now_ms=self._now_ms,
        )

    async def _require_resume_custody(self, binding: BrokerBotBinding) -> None:
        """Admit Resume only after a fresh exact Clerk proof."""
        if binding.carryover_policy == "ALLOW" and not self._carryover_allowed:
            raise CarryoverPolicyRefusedError(
                "Exposure carryover is disabled for this Alpaca paper account.",
                detail=(
                    "The deployment opted in, but the account policy is no "
                    "longer enabled. Flatten the attributed exposure before Resume."
                ),
            )
        status = self.status(binding.broker, binding.strategy_instance_id)
        clerk = None
        if binding.broker == "alpaca" and binding.mode == "trade":
            from app.broker.alpaca.clerk import get_alpaca_clerk

            clerk = get_alpaca_clerk()
        try:
            await require_resume_custody(
                binding,
                clerk=clerk,
                checkpoint_path=self._carryover_checkpoint_path(
                    binding.strategy_instance_id
                ),
                desired_state=status.desired_state,
                phase=status.phase,
            )
        except CarryoverResumeRefusedError as exc:
            raise RecoveryUncertainError(
                str(exc), detail=exc.detail
            ) from exc

    def _carryover_checkpoint_path(self, strategy_instance_id: str) -> Path:
        return (
            self._confined_instance_dir(strategy_instance_id)
            / _CARRYOVER_CHECKPOINT_FILENAME
        )

    def _enforce_restart_intensity(self, strategy_instance_id: str, now_ms: int) -> None:
        """Per-bot projection mirror of ``project_restart_intensity_gate``:
        refuse when ``prior_starts_in_window + 1 >= threshold``."""
        window_start_ms = now_ms - self._restart_policy.window_ms
        history = [
            start_ms
            for start_ms in self._start_history.get(strategy_instance_id, [])
            if window_start_ms <= start_ms <= now_ms
        ]
        self._start_history[strategy_instance_id] = history
        projected_count = len(history) + 1
        if projected_count >= self._restart_policy.threshold:
            raise RestartIntensityRefusedError(
                f"Restart intensity for bot '{strategy_instance_id}': "
                f"{projected_count} activation(s) within {self._restart_policy.window_ms} ms "
                f"meets the threshold of {self._restart_policy.threshold}.",
                detail="WAIT_OR_RECOVER_ACCOUNT_BEFORE_STARTING_ANOTHER_BOT",
            )

    def _confined_instance_dir(self, strategy_instance_id: str) -> Path:
        try:
            return strategy_instance_artifact_dir(
                self._artifacts_root, "live_state", strategy_instance_id
            )
        except ValueError as exc:
            raise InvalidStrategyInstanceIdError(str(exc)) from exc

    def _lifecycle_repo(self, strategy_instance_id: str) -> BotLifecycleStateRepo:
        return BotLifecycleStateRepo(
            stable_bot_lifecycle_state_path(self._artifacts_root, strategy_instance_id)
        )

    def _desired_repo(self, strategy_instance_id: str) -> DesiredStateRepo:
        return DesiredStateRepo(
            stable_desired_state_path(self._artifacts_root, strategy_instance_id)
        )

    def _read_binding(self, strategy_instance_id: str) -> BrokerBotBinding | None:
        return self._bindings.read(strategy_instance_id)

    def _compose_status(self, binding: BrokerBotBinding) -> BotStatusView:
        sid = binding.strategy_instance_id
        lifecycle = self._lifecycle_repo(sid).read()
        desired = self._desired_repo(sid).read_state()
        managed = self._bots.get(sid)
        running = managed is not None and not managed.task.done()
        duty_outcome = None
        if lifecycle is not None and lifecycle.duty_outcome is not None:
            duty_outcome = BotDutyOutcomeView(
                kind=lifecycle.duty_outcome.kind,
                reason_code=lifecycle.duty_outcome.reason_code,
                recorded_at_ms=lifecycle.duty_outcome.recorded_at_ms,
                run_id=lifecycle.duty_outcome.run_id,
            )
        checkpoint_exposure, checkpoint_matches = checkpoint_status(
            binding,
            self._carryover_checkpoint_path(sid),
        )
        return BotStatusView(
            strategy_instance_id=sid,
            strategy_key=binding.strategy_key,
            broker=binding.broker,
            symbol=binding.symbol,
            mode=binding.mode,
            quantity=binding.quantity,
            carryover_policy=binding.carryover_policy,
            carryover_account_policy_enabled=self._carryover_allowed,
            carryover_checkpoint_exposure=checkpoint_exposure,
            carryover_checkpoint_config_matches=checkpoint_matches,
            running=running,
            phase=(lifecycle.phase.value if lifecycle is not None else "OFF_DUTY"),
            desired_state=desired.value,
            active_run_id=(lifecycle.active_run_id if lifecycle is not None else None),
            duty_outcome=duty_outcome,
            binding_created_at_ms=binding.created_at_ms,
            last_transition_at_ms=(
                lifecycle.last_transition_at_ms if lifecycle is not None else None
            ),
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
