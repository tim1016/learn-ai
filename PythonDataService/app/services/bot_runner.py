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

Trade mode delegates effects to the Alpaca Clerk; the runner never authors
broker execution truth.

All temporal fields are ``int64 ms UTC`` per ``.claude/rules/temporal-rigor.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.account_authority import SIM_ACCOUNT_PREFIX
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    SyntheticActivationStore,
    get_active_clerk_runtime,
)
from app.broker.alpaca.clerk.active_runtime import SQLITE_FACADE_AUTHORITIES
from app.broker.alpaca.clerk.models import ClerkCustodySnapshot, ReconciliationCut
from app.broker.alpaca.clerk.sqlite.repository import ExecutionLeaseHeld
from app.broker.alpaca.clerk.sqlite.scheduled_end import SCHEDULED_END_REASON, ScheduledEnd
from app.broker.v2panel.action_policy import evaluate_archive
from app.engine.live.bot_lifecycle_state import (
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRecord,
    DesiredStateRepo,
    instances_with_recorded_desired_state,
    stable_desired_state_path,
)
from app.engine.live.identity import strategy_instance_artifact_dir
from app.marketdata.feed import (
    FEED_REFUSAL_REASON_CODES,
    MarketDataFeed,
    MarketDataFeedError,
)
from app.schemas.bot_end import BotEnd, BotEndInput, BotEndView
from app.schemas.broker_bots import (
    AlpacaPaperEvidenceOverride,
    BotProcessFact,
    BotRunView,
    BotStatusView,
)
from app.schemas.canary_admission import CanaryRollbackDecision
from app.schemas.deployment_budget import DeployBudgetConsent
from app.schemas.exit_terms import ExitTerms
from app.schemas.run_admission import (
    RunAdmissionDecision,
    RunProcessAdmissionFact,
    StartRuntimeAdmissionFact,
)
from app.schemas.run_replay import RunReplayReceipt
from app.schemas.signal_program_seal import ParameterOrigin
from app.services.alpaca_bot_identity import AlpacaBotIdentityGuard
from app.services.alpaca_live_graduation_gate import graduation_mutation_fence
from app.services.bot_binding_authority import (
    BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S,
    BindingAuthority,
    BindingAuthoritySelector,
    UnboundDryRunAuthority,
)
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.bot_boot_recovery import (
    LEASE_REVIVAL_PROVENANCE,
    BootRecoveryReport,
    BotBootRecovery,
    BotRecoveryCandidate,
)
from app.services.bot_clerk_lifecycle import (
    ActiveClerkUnavailableError,
    ClerkAdmissionTokenStaleError,
    commit_deploy_launch,
    commit_stop_before_task_cancel,
    register_alpaca_duty_run,
    stop_interrupted_alpaca_duty_run,
)
from app.services.bot_dry_run import DryRunActivity
from app.services.bot_end import (
    SCHEDULED_END_REASON_CODE,
    bot_end_view,
    end_edit_refusal,
    resolve_bot_end,
)
from app.services.bot_lifecycle_projection import (
    ActiveSqliteAlpacaLifecycleAuthority,
    AlpacaLifecycleAuthorityUnavailableError,
    AlpacaLifecycleProjector,
    ProjectionStatus,
)
from app.services.bot_registry_projection import (
    project_bot_status,
    project_process_fact,
    read_dry_run_activity,
)
from app.services.bot_run_evidence import (
    ACTIVATION_FAILED_STOP_REASON_CODE,
    PROVISIONAL_STOP_REASON_CODE,
    BotRunEvidenceService,
)
from app.services.bot_run_terminal import (
    BotRunTerminalRecorder,
    StopProver,
    prove_end_stop_outcome,
    prove_terminal_stop_outcome,
)
from app.services.bot_runner_errors import (
    LANE_GO_LIVE_HOLD_UNREADABLE,
    LANE_GO_LIVE_PENDING,
    ActivationFailedCleanupProvenError,
    BootRecoveryIncompleteError,
    BotAlreadyRunningError,
    BotRunnerError,
    CarryoverPolicyRefusedError,
    InvalidStrategyInstanceIdError,
    MarketDataFeedUnavailableError,
    RecoveryUncertainError,
    RunAdmissionRefusedError,
    UnknownBotError,
    raise_run_refusal,
    require_start_configuration,
)
from app.services.bot_runtime import (
    ManagedBot,
    execute_bot_run,
)
from app.services.bot_start_admission import (
    AdmissionCustodyCut,
    AdmittedBotStart,
    BotStartAdmission,
    DryRunRestorationState,
    MarketLivenessFactResolver,
    RecoveryEvaluationProbe,
    StartAdmissionDenied,
    StartAdmissionEvidenceChanged,
    StartAdmissionUnavailable,
    UnresolvedIntentsProbe,
    log_run_launch,
    make_start_request,
    refuse_unrestored_dry_run,
    resolve_start_runtime_fact,
)
from app.services.bot_trade_strategy import supported_alpaca_paper_strategy_keys
from app.services.canary_admission import canary_gate_applies, evaluate_canary_rollback
from app.services.go_live_hold import GoLiveHoldState
from app.services.market_data_capability_service import get_market_data_capability_service
from app.services.market_liveness import market_liveness_fact
from app.services.run_replay_proof import RunReplayProofService, RunReplayUnavailableError
from app.services.strategy_validation_admission import (
    ValidationFactResolver,
    current_strategy_validation_fact,
)
from app.utils.timestamps import now_ms_utc

__all__ = [
    "AdmittedBotStart",
    "BootRecoveryIncompleteError",
    "BotAlreadyRunningError",
    "BotRunnerError",
    "BotTaskRegistry",
    "CarryoverPolicyRefusedError",
    "InvalidStrategyInstanceIdError",
    "LaneIntentStoppedBot",
    "LaneStartGate",
    "LaneStopOutcome",
    "LaneStopRefusal",
    "LaneStoppedBot",
    "MarketDataFeedUnavailableError",
    "RecoveryUncertainError",
    "RunAdmissionRefusedError",
    "UnknownBotError",
    "fleet_lane_start_gate",
    "go_live_start_gate",
]

logger = logging.getLogger(__name__)

_CARRYOVER_CHECKPOINT_FILENAME = "carryover_checkpoint.json"
_UPDATED_BY = "bot_runner"
_STOP_TIMEOUT_S = 5.0
#: Who records a bot's end carried out, and stops the bot at it (#2607).
_END_UPDATED_BY = "account_clerk"
#: How often the end watch looks for a running bot whose end has come.
_END_WATCH_INTERVAL_S = 5.0


@dataclass(frozen=True, slots=True)
class LaneStoppedBot:
    """One bot the lane-wide stop ended, with the run it ended."""

    strategy_instance_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class LaneIntentStoppedBot:
    """A bot with no live task whose durable intent the lane-wide stop set to STOPPED.

    Its desired state still said it should run (a crash, or a restart that
    never resumed it), so the next boot or a migrated copy would have read it
    as a bot that wants to start.
    """

    strategy_instance_id: str
    previous_desired_state: str


@dataclass(frozen=True, slots=True)
class LaneStopRefusal:
    """One bot whose Stop refused; its task may still be running.

    ``run_id`` is ``None`` for a bot with no live task whose recorded intent
    could not be read or rewritten.
    """

    strategy_instance_id: str
    run_id: str | None
    message: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class LaneStopOutcome:
    """What one lane-wide stop did, and whether any task still runs after it.

    ``still_running`` is read from the task registry after every Stop has
    returned, not derived from ``refused``: a Stop whose cancellation timed
    out returns normally while its task is still alive.
    """

    stopped: tuple[LaneStoppedBot, ...]
    intent_stopped: tuple[LaneIntentStoppedBot, ...]
    refused: tuple[LaneStopRefusal, ...]
    still_running: bool


@dataclass(frozen=True, slots=True)
class _DryRunRestoration:
    """One Dry Run boot restores: its own account, then the repair its runs get (#2582)."""

    strategy_instance_id: str
    #: Built inside the bot's own restoration boundary (#2668): constructing
    #: an authority can fail for one bot, and that must refuse that bot's
    #: Start alone -- never abort the restoration of every later Dry Run.
    authority_for: Callable[[], BindingAuthority | UnboundDryRunAuthority]
    #: Runs once the account is open. A bound Dry Run's runs get the sweep's
    #: own repair, its binding's run the fallback candidate; an unbound
    #: orphan's get none, because its opening's full recovery already retired
    #: the run and failed its command (#2559).
    repair: Callable[[], Awaitable[tuple[str, ...]]]


async def _nothing_to_repair() -> tuple[str, ...]:
    return ()


def _release_run_owner(run_owner: asyncio.Future[None]) -> None:
    """Tell the Clerk this process no longer holds the run (#2369)."""
    if not run_owner.done():
        run_owner.set_result(None)


def _lane_stop_refusal(
    strategy_instance_id: str, run_id: str | None, message: str, detail: str | None
) -> LaneStopRefusal:
    """Record one bot whose lane-wide Stop did not complete, loudly."""
    logger.warning(
        "Lane-wide stop refused for one bot",
        extra={
            "action": "lane_stop_all_bot_refused",
            "strategy_instance_id": strategy_instance_id,
            "run_id": run_id,
            "error": message,
        },
    )
    return LaneStopRefusal(
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        message=message,
        detail=detail,
    )


def _say_stop_cannot_cancel_end(strategy_instance_id: str, exc: DesiredStateCorruptError) -> None:
    """An operator's Stop found the bot's desired state unreadable, so it could not cancel the bot's end.

    The Stop goes on: its STOP is what fences the bot, and no end is carried
    out while the file cannot be read. But a repaired file would carry its end
    out after all, so whoever repairs it must clear the end (#2607).
    """
    logger.error(
        "A Stop could not cancel a bot's end: its desired state cannot be read, and no end is carried out while "
        "it cannot. The Stop went on; clear its end when repairing the file, or the repaired end is carried out",
        extra={"action": "bot_end_cancel_unreadable", "strategy_instance_id": strategy_instance_id, "error": str(exc)},
    )


# Commit-time refusals, keyed by the archive rule's cause (ADR 0052). The
# panel renders its own operator copy from the same causes; this is what an
# operator sees when the world changed between presentation and click.
_ARCHIVE_REFUSAL: dict[str | None, tuple[str, str]] = {
    "BOT_STILL_RUNNING": (
        "The bot is still running.",
        "Stop the bot before clearing it.",
    ),
    "BOT_DUTY_NOT_SETTLED": (
        "This bot's last run has not finished settling.",
        "Wait for recovery to record how that run ended, then clear the bot.",
    ),
    "ARCHIVE_CUSTODY_UNPROVABLE": (
        "This account cannot prove the bot is flat.",
        "A bot is cleared only on proof that it holds nothing. Choose Reconcile "
        "now once Alpaca can be read, then clear it.",
    ),
    "ARCHIVE_WOULD_STRAND_CUSTODY": (
        "This bot still holds shares or has a working order.",
        "Flatten it and let its working orders finish, then clear it.",
    ),
    None: (
        "This bot cannot be cleared.",
        "Its clearing conditions are no longer met.",
    ),
}


#: One lane-level start gate: raises :class:`RunAdmissionRefusedError` when
#: this lane starts no new bot, and returns otherwise. ``BotTaskRegistry``
#: probes its gates in order on every deployment, before admission.
LaneStartGate = Callable[[str], None]


#: What the operator reads when the fleet lane starts no new bot, keyed by
#: the fleet refusal code ``FleetLaneBoot.start_refusal`` answers with — a
#: closed map, so no coordinator-authored code reaches operator prose.
_FLEET_LANE_START_REFUSAL: dict[str | None, tuple[str, str]] = {
    "clerk_lane_draining": (
        "This lane is drained; it starts no new bots.",
        "The fleet coordinator marked this lane draining and its binding is "
        "being handed over. Existing bots settle; new starts refuse for the "
        "rest of this lane's life.",
    ),
    "clerk_lane_retired": (
        "This lane is retired; it starts no new bots.",
        "The fleet coordinator retired this lane's clerk. It never returns to "
        "service; its bots are stopped.",
    ),
    "fleet_presence_refused": (
        "The fleet coordinator refused this lane; it starts no new bots.",
        "The coordinator answered this lane's registration with a refusal. "
        "Running bots keep running; new starts refuse until the coordinator "
        "admits the lane again.",
    ),
    None: (
        "This lane starts no new bots.",
        "The fleet lane reported a reason this runner does not recognise, so "
        "it refuses the start rather than guess.",
    ),
}


def fleet_lane_start_gate(start_refusal: Callable[[], str | None]) -> LaneStartGate:
    """#2155/#2351/#2320: a drained, retired or refused fleet lane starts no new runs.

    ``start_refusal`` is ``FleetLaneBoot.start_refusal``: the fleet refusal
    code while the lane may start nothing, ``None`` otherwise. A lane the
    coordinator admits again starts bots again; a drained or retired one
    never does.
    """

    def refuse_if_fleet_lane_refuses(_strategy_instance_id: str) -> None:
        reason = start_refusal()
        if reason is None:
            return
        message, detail = _FLEET_LANE_START_REFUSAL.get(reason, _FLEET_LANE_START_REFUSAL[None])
        raise RunAdmissionRefusedError(message, detail=detail, reason_code=reason)

    return refuse_if_fleet_lane_refuses


def go_live_start_gate(read_hold: Callable[[], GoLiveHoldState]) -> LaneStartGate:
    """#2269: a lane restored by installation migration starts no bot until
    go-live releases its hold; fails closed when the hold cannot be read."""

    def refuse_if_go_live_pending(strategy_instance_id: str) -> None:
        hold = read_hold()
        if not hold.held:
            return
        reason_code = LANE_GO_LIVE_PENDING if hold.problem is None else LANE_GO_LIVE_HOLD_UNREADABLE
        logger.warning(
            "Bot start refused: lane awaits go-live",
            extra={
                "action": "bot_start_refused_go_live_pending",
                "reason_code": reason_code,
                "strategy_instance_id": strategy_instance_id,
                "problem": hold.problem,
            },
        )
        if hold.problem is not None:
            raise RunAdmissionRefusedError(
                "This lane cannot read its go-live hold, so it starts no bots.",
                detail=f"{hold.problem}. Repair the lane volume, then run go-live "
                "(migrate_installation go-live) to release the hold.",
                reason_code=reason_code,
            )
        raise RunAdmissionRefusedError(
            "This lane was restored by an installation migration and awaits go-live; "
            "it starts no bots until then.",
            detail="Run `python -m scripts.migrate_installation go-live` on this machine: "
            "it proves IB Gateway delivers bars to every lane, takes your confirmation "
            "that the old machine is off, then releases every lane at once. Bots stay "
            "stopped until you start them.",
            reason_code=reason_code,
        )

    return refuse_if_go_live_pending


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
        now_ms: Callable[[], int] = now_ms_utc,
        boot_recovery_required: bool = True,
        supported_broker_ids: frozenset[str] | None = None,
        start_custody_guard: Callable[[str], AbstractAsyncContextManager[AdmissionCustodyCut]] | None = None,
        lifecycle_projector: AlpacaLifecycleProjector | None = None,
        market_liveness: MarketLivenessFactResolver | None = None,
        validation_fact: ValidationFactResolver | None = None,
        lane_start_gates: tuple[LaneStartGate, ...] = (),
    ) -> None:
        self._artifacts_root = Path(artifacts_root)
        self._feed_resolver = feed_resolver
        self._now_ms = now_ms
        self._market_liveness = market_liveness or market_liveness_fact
        self._registry_generation = uuid4().hex
        self._bots: dict[str, ManagedBot] = {}
        self._operation_locks: dict[str, asyncio.Lock] = {}
        # S5 (#1263) fail-closed start gate: no bot starts until the boot
        # recovery sweep has run, and none while recovery left an uncertain
        # outcome (the probe re-evaluates per deploy, so a later resolution
        # unblocks without a restart). The sweep's report is the gate fact:
        # absent means pending -- or failed, when the last sweep raised; present
        # means complete, or degraded when it names bots no lifecycle
        # authority could project. Tests that do not exercise recovery opt out
        # explicitly with ``boot_recovery_required=False``.
        self._boot_recovery_required = boot_recovery_required
        self._boot_recovery_report: BootRecoveryReport | None = None
        self._boot_recovery_failed = False
        # Each Dry Run boot restores off the serving path (#2582), by bot,
        # until its own restoration settles: absent once restored, and for
        # every bot boot never had to restore. Start refuses the rest.
        self._dry_run_restorations: dict[str, DryRunRestorationState] = {}
        self._dry_run_restoration_task: asyncio.Task[None] | None = None
        self._unresolved_intents_probe: UnresolvedIntentsProbe | None = None
        self._recovery_evaluation: RecoveryEvaluationProbe | None = None
        # When set, the boot sweep skips bots whose binding carries a broker
        # tag that is not in this set (e.g. IBKR bots share the same
        # artifacts_root but are managed by the host daemon, not the
        # in-container runner).
        self._supported_broker_ids = supported_broker_ids
        self._bindings = BotBindingRepository(
            self._artifacts_root,
            instance_dir_for=self._confined_instance_dir,
        )
        self._alpaca_identity = AlpacaBotIdentityGuard(self._artifacts_root)
        self._lifecycle_authority = ActiveSqliteAlpacaLifecycleAuthority()
        self._lifecycle_projector = lifecycle_projector or AlpacaLifecycleProjector(
            authority=self._lifecycle_authority,
            lifecycle_repo_for=self._lifecycle_repo,
            require_alpaca_identity=self._require_alpaca_identity,
        )
        self._authorities = BindingAuthoritySelector(
            artifacts_root=self._artifacts_root,
            lifecycle_repo_for=self._lifecycle_repo,
            real_projector=self._lifecycle_projector,
            external_start_guard=start_custody_guard,
            runtime_in_use=self._synthetic_runtime_in_use,
            clock=self._now_ms,
        )
        self._boot_recovery = BotBootRecovery(
            self._artifacts_root,
            lifecycle_repo_for=self._lifecycle_repo,
            lifecycle_projector=self._lifecycle_projector,
            lifecycle_projector_for=self._lifecycle_projector_for_instance,
            desired_repo_for=self._desired_repo,
            recovery_candidates=self._recovery_candidates,
            stop_authority_run=self._stop_interrupted_authority_run,
            manages_instance=self._manages_boot_recovery,
            is_running=self._is_running,
            now_ms=self._now_ms,
            binding_for=self._read_binding,
            installed_custody_account_id=lambda: None if (c := get_alpaca_clerk()) is None else c.account_id,
        )
        active_validation_fact = validation_fact or current_strategy_validation_fact
        self._start_admission = BotStartAdmission(
            now_ms=self._now_ms,
            feed_resolver=self._feed_resolver,
            custody_guard=self._start_custody_guard,
            process_fact=self._start_process_fact,
            runtime_fact=self._start_runtime_fact,
            validation_fact=active_validation_fact,
            activate=self._activate_start_binding,
            session_capability=get_market_data_capability_service().read_latest_for,
            market_liveness=self._market_liveness,
        )
        self._run_evidence = BotRunEvidenceService(
            self._bindings,
            lifecycle_repo_for=self._lifecycle_repo,
            lifecycle_projector=self._lifecycle_projector,
            lifecycle_projector_for=self._lifecycle_projector_for_instance,
        )
        self._terminal = BotRunTerminalRecorder(
            managed_bots=self._bots,
            desired_repo_for=self._desired_repo,
            run_evidence=self._run_evidence,
            now_ms=self._now_ms,
        )
        # Direction 2 (run-scoped replay proof): a completed Paper/Dry Run
        # proves itself against the backtest engine on the way out. This never
        # gates admission -- the permanent evidence-only Paper override is
        # untouched. ``run_record_for`` is the canonical ``read_run`` reader.
        self._replay_proof = RunReplayProofService(
            artifacts_root=self._artifacts_root,
            instance_dir_for=self._confined_instance_dir,
            binding_for=self.binding_for_control,
            run_record_for=self._bindings.read_run,
            is_running=self._is_running,
            run_outcome_for=self._bindings.read_outcome,
            authority_for=self._authorities.for_binding,
        )
        self._replay_receipt_tasks: set[asyncio.Task[None]] = set()
        # #2607: the stops the Clerk asked for at bots' ends, and the watch
        # that asks each running bot's Clerk for a pass when its end comes.
        # One stop at a time per bot; the bots whose end could not be read and
        # the failure each due end last met, each said once (#2607).
        self._end_stop_tasks: dict[str, asyncio.Task[None]] = {}
        self._end_watch_task: asyncio.Task[None] | None = None
        self._unreadable_end_sids: set[str] = set()
        # The stopped bots whose end may still come (``_record_ends_of_stopped_dry_runs``).
        self._stopped_end_candidates: set[str] = set()
        self._stopped_end_candidates_seeded = False
        self._end_watch_failures: dict[str, str] = {}
        # #2155 / #2269: lane-level refusals (drained, awaiting go-live),
        # probed in order per deployment so a flag the lane learns lands on
        # the next operator action without a process restart. Empty (tests,
        # non-lane deployments) refuses nothing.
        self._lane_start_gates = lane_start_gates

    # ── deploy / stop ─────────────────────────────────────────────────

    async def deploy(
        self,
        *,
        broker: str,
        strategy_instance_id: str,
        strategy_key: str = "deployment_validation",
        symbol: str,
        exit_terms: ExitTerms,
        use_rth: bool = True,
        mode: Literal["log_only", "dry_run", "trade"] = "log_only",
        quantity: int = 1,
        carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID",
        evidence_override: AlpacaPaperEvidenceOverride | None = None,
        end: BotEnd | None = None,
    ) -> BotStatusView:
        """Deploy and start a bot; durable evidence before liveness."""
        return (
            await self.deploy_with_admission(
                broker=broker,
                strategy_instance_id=strategy_instance_id,
                strategy_key=strategy_key,
                symbol=symbol,
                exit_terms=exit_terms,
                use_rth=use_rth,
                mode=mode,
                quantity=quantity,
                carryover_policy=carryover_policy,
                evidence_override=evidence_override,
                end=end,
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
        evidence_override: AlpacaPaperEvidenceOverride | None = None,
        exit_terms: ExitTerms,
        strategy_params: dict[str, Any] | None = None,
        # Widened to the canonical 3-member ParameterOrigin: this is threaded
        # straight through to `make_start_request` (bot_start_admission.py),
        # whose one real caller (`panel_data_source.py`) supplies
        # `resolve_deploy_strategy_params`'s `origins`, which legitimately
        # produces "deployment_symbol" once a caller supplies a
        # `symbol_profile`. A narrower type here was already silently out of
        # sync with the value actually flowing through it.
        strategy_param_origins: dict[str, ParameterOrigin] | None = None,
        budget_consent: DeployBudgetConsent | None = None,
        end: BotEnd | None = None,
    ) -> AdmittedBotStart:
        """Start one bot and return the exact execution-time admission.

        ``end`` is the owner's end for this deployment, already validated
        (``bot_end.resolve_bot_end``); ``None`` is no end (#2607).
        """
        for refuse_if_gated in self._lane_start_gates:
            refuse_if_gated(strategy_instance_id)
        require_start_configuration(
            carryover_policy,
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
            evidence_override=evidence_override,
            action_plan=alpaca_v1_action_plan(symbol),
            strategy_params=strategy_params,
            exit_terms=exit_terms,
            strategy_param_origins=strategy_param_origins,
            budget_consent=budget_consent,
            end=end,
        )
        # #2668: answered before the bot's operation lock, which a boot
        # restoration of this Dry Run may hold while it waits out a lease --
        # Start never waits on that restoration, it refuses it at once.
        self._refuse_unrestored_dry_run_at_once(strategy_instance_id)
        # Graduation re-observes the complete stopped roster and appends the
        # boot-selection fence. No deploy may cross that exact interval.
        async with graduation_mutation_fence(), self._operation_lock(strategy_instance_id):
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
        evidence_override: AlpacaPaperEvidenceOverride | None = None,
        exit_terms: ExitTerms | None = None,
        strategy_params: dict[str, Any] | None = None,
        # See the widening note on the matching parameter in
        # `deploy_with_admission` above.
        strategy_param_origins: dict[str, ParameterOrigin] | None = None,
        budget_consent: DeployBudgetConsent | None = None,
    ) -> RunAdmissionDecision:
        """Project the same Start decision used immediately before mutation."""
        require_start_configuration(
            carryover_policy,
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
            evidence_override=evidence_override,
            action_plan=alpaca_v1_action_plan(symbol),
            strategy_params=strategy_params,
            exit_terms=exit_terms,
            strategy_param_origins=strategy_param_origins,
            budget_consent=budget_consent,
        )
        # #2668: answered before the bot's operation lock, which a boot
        # restoration of this Dry Run may hold while it waits out a lease --
        # Start never waits on that restoration, it refuses it at once.
        self._refuse_unrestored_dry_run_at_once(strategy_instance_id)
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

    async def _activate_start_binding(
        self,
        binding: BrokerBotBinding,
        feed: MarketDataFeed,
        now_ms: int,
        custody: ClerkCustodySnapshot,
        end: BotEnd | None,
    ) -> BotStatusView:
        await self._activate_binding(
            binding,
            feed,
            now=now_ms,
            reason="deploy",
            admission_snapshot=custody,
            end=end,
        )
        log_run_launch(binding, reason="deploy")
        return self.status(binding.broker, binding.strategy_instance_id)

    async def _activate_binding(
        self,
        binding: BrokerBotBinding,
        feed: MarketDataFeed,
        *,
        now: int,
        reason: Literal["deploy"],
        admission_snapshot: ClerkCustodySnapshot | None = None,
        end: BotEnd | None = None,
    ) -> None:
        """Write run evidence and install supervision while caller holds its gate."""
        if binding.broker != "alpaca":
            raise RunAdmissionRefusedError(
                "The in-container runner accepts only Alpaca bot bindings.",
                detail="Use the legacy host boundary only for an existing IBKR run.",
            )
        lifecycle_repo = self._lifecycle_repo(binding.strategy_instance_id)
        # Preserve the prior run's terminal evidence before registering the
        # proposed run with the Clerk or advancing current_run.json. A
        # preservation failure must leave zero Clerk runs and zero process
        # activity behind, so it runs unguarded, ahead of both.
        self._run_evidence.preserve_terminal(
            binding.strategy_instance_id,
            lifecycle_repo.read(),
        )
        # What holds this run in the process until its supervise task has
        # ended; the Clerk's sweep retires the run once it is done (#2369).
        run_owner: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        try:
            await register_alpaca_duty_run(
                binding, admission_snapshot=admission_snapshot, run_owner=run_owner
            )
        except (ActiveClerkUnavailableError, ClerkAdmissionTokenStaleError) as exc:
            raise RunAdmissionRefusedError(
                str(exc),
                detail="Refresh Clerk custody before starting an Alpaca bot.",
            ) from exc
        task: asyncio.Task[None] | None = None
        try:
            self._bindings.record_launch(binding, launch_reason=reason)
            # The owner's end rides the same write that makes the bot RUNNING:
            # a deployment never runs with the end of an earlier one (#2607).
            self._desired_repo(binding.strategy_instance_id).set(
                DesiredState.RUNNING, updated_by=_UPDATED_BY, now_ms=now, reason=reason, end=end
            )
            projection = self._authority_for(binding).lifecycle_projector().project_active(
                strategy_instance_id=binding.strategy_instance_id,
                run_id=binding.run_id,
                now_ms=now,
                updated_by=_UPDATED_BY,
                carryover_policy=binding.carryover_policy,
                reason=f"{reason}_{binding.mode}_bot",
            )
            if projection.status is not ProjectionStatus.RECORDED:
                raise RunAdmissionRefusedError(
                    "SQLite lifecycle authority superseded bot activation.",
                    detail="Refresh the bot after Clerk reconciliation settles.",
                )
            run_gate = asyncio.Event()
            run_gate.set()
            task = asyncio.create_task(
                self._supervise(binding, feed, run_gate),
                name=f"bot:{binding.strategy_instance_id}",
            )
            task.add_done_callback(lambda _task: _release_run_owner(run_owner))
            managed = ManagedBot(
                binding=binding,
                task=task,
                run_gate=run_gate,
            )
            self._bots[binding.strategy_instance_id] = managed
            # Let supervision enter its exception boundary before a Start releases
            # Clerk intake. A first effect waits on that same fence.
            await asyncio.sleep(0)
            await commit_deploy_launch(binding)
        except BaseException as exc:
            if task is None:
                # No supervise task ever held the run, so nothing else will
                # let the owner go.
                _release_run_owner(run_owner)
            cleanup_proven = False
            try:
                await commit_stop_before_task_cancel(binding, reason=ACTIVATION_FAILED_STOP_REASON_CODE)
                cleanup_proven = True
            except Exception:
                logger.error(
                    "SQLite Clerk could not durably stop a failed activation",
                    extra={
                        "action": "sqlite_clerk_activation_stop_failed",
                        "strategy_instance_id": binding.strategy_instance_id,
                        "run_id": binding.run_id,
                    },
                    exc_info=True,
                )
            if cleanup_proven and task is not None and not task.done():
                # The Clerk recorded STOP and released the budget, so the task
                # this activation started must not keep supervising a stopped
                # run. Compensation is the normal Stop past its durable commit;
                # without that proof the task keeps holding its active run.
                try:
                    await self._stop_locked(
                        binding.broker,
                        binding.strategy_instance_id,
                        updated_by=_UPDATED_BY,
                        reason=ACTIVATION_FAILED_STOP_REASON_CODE,
                        clerk_stop_already_committed=True,
                        outcome_reason_code=ACTIVATION_FAILED_STOP_REASON_CODE,
                    )
                except Exception:
                    logger.error(
                        "A failed activation's task could not be stopped",
                        extra={
                            "action": "activation_failed_task_stop_failed",
                            "strategy_instance_id": binding.strategy_instance_id,
                            "run_id": binding.run_id,
                        },
                        exc_info=True,
                    )
            # A resolved failure is reported as known only for a genuine
            # Exception with cleanup proven. asyncio.CancelledError and other
            # BaseException-only paths are not Exception instances, so they
            # keep today's raw propagation and are never converted into an
            # operator-facing resolved failure.
            if cleanup_proven and isinstance(exc, Exception):
                raise ActivationFailedCleanupProvenError(
                    f"Activation failed after Clerk registration for run "
                    f"'{binding.run_id}'; the Clerk stop committed.",
                    attempted_run_id=binding.run_id,
                    detail=str(exc),
                ) from exc
            raise

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
        primary = get_active_clerk_runtime()
        return await resolve_start_runtime_fact(
            strategy_instance_id=strategy_instance_id,
            observed_at_ms=observed_at_ms,
            boot_recovery_required=self._boot_recovery_required,
            boot_recovery_report=self._boot_recovery_report,
            boot_recovery_failed=self._boot_recovery_failed,
            unresolved_intents_probe=self._unresolved_intents_probe,
            recovery_evaluation=self._recovery_evaluation,
            account_reconnecting=primary is not None and primary.reconnecting,
        )

    async def archive(
        self,
        broker: str,
        strategy_instance_id: str,
        *,
        updated_by: str = "operator",
        reason: str | None = None,
        reconciled: ReconciliationCut | None = None,
    ) -> BotStatusView:
        """Take a finished bot off the roster (ADR 0052).

        The one exit for a registration the operator is *done with* -- Clear
        on Home's Finished fold (#2567, #2578). It lands in the terminal
        phase, so ``run_admission`` refuses the registration ``BOT_RETIRED``
        and it leaves the catalog's per-row cost curve (#1911).

        Archive's enabling proof *is* custody, so it re-answers the shared
        rule against a freshly reconciled snapshot rather than the projected
        one the operator clicked on: a fill that landed in between must
        refuse the command, not be stranded by it.
        ``reconciled`` is a batch's one reconciliation pass
        (:meth:`_archive_custody` says when it stands in for this one's).
        """
        async with self._operation_lock(strategy_instance_id):
            binding = self._bindings.read(strategy_instance_id)
            if binding is None:
                raise BotRunnerError(
                    f"Bot '{strategy_instance_id}' has no registration to archive.",
                    detail="The roster has no binding for this instance.",
                )
            status = self.status(broker, strategy_instance_id)
            try:
                async with self._archive_custody(binding, reconciled) as (custody, _policy, _terms):
                    # A count the Clerk could not take carries no number and is
                    # no proof of zero: with Alpaca unreadable, "nothing
                    # working" is the Clerk's ignorance, not the bot's state.
                    counts = (custody.working_orders, custody.unresolved_effects, custody.pending_orders)
                    verdict = evaluate_archive(
                        running=status.running,
                        phase=status.phase,
                        has_exposure=custody.exposure.state != "zero",
                        working_order_count=custody.working_orders.count or 0,
                        # Bot-scoped, and only the commit can see it: an effect
                        # accepted before its broker order becomes working would
                        # otherwise create custody for a terminal registration.
                        outstanding_effect_count=(
                            (custody.unresolved_effects.count or 0) + (custody.pending_orders.count or 0)
                        ),
                        custody_provable=(
                            not custody.freeze.active and all(fact.count is not None for fact in counts)
                        ),
                    )
                if verdict.already_retired:
                    return status
                if not verdict.eligible:
                    headline, detail = _ARCHIVE_REFUSAL[verdict.cause]
                    raise BotRunnerError(headline, detail=detail, reason_code=verdict.cause)
                self._lifecycle_projector_for_instance(strategy_instance_id).retire(
                    strategy_instance_id=strategy_instance_id,
                    now_ms=self._now_ms(),
                    updated_by=updated_by,
                    reason=reason or f"Panel archive by {updated_by}",
                )
                return self.status(broker, strategy_instance_id)
            finally:
                # A stopped Dry Run has no managed task to release its synthetic
                # Clerk, and the custody guard above registers one to read from.
                # Every other read path releases it when the read ends; a
                # mutation must too, or each archived Dry Run leaks an open
                # SQLite authority for the rest of the process lifetime.
                await self._authority_for(binding).release_if_unused()

    @asynccontextmanager
    async def _archive_custody(
        self,
        binding: BrokerBotBinding,
        reconciled: ReconciliationCut | None,
    ) -> AsyncIterator[AdmissionCustodyCut]:
        """The custody archive's guard is re-answered against, under the bot's lock.

        Alone, archive reconciles the whole account for its one bot. Clearing
        many finished bots at once did that per bot: four Alpaca reads each,
        600 for 150 bots against Alpaca's 200 a minute, and a throttled read
        is a stale pass that puts the account on hold for every running bot
        (#2567). So a batch reconciles once and hands each leg its ``cut``.

        The leg's guard is unchanged -- not running, duty settled, provably
        flat, no working order, no outstanding effect (ADR 0052 §1) -- and is
        answered against the projection of the latest pass, which is the
        batch's or a later one, with this bot's own orders, effects and fills
        read now. That stands in for a fresh pass only while
        ``reconciliation_covers`` holds: no custody transition of this bot
        since the pass began. Then the pass saw every order the bot has, and
        a bot the guard admits has none working and nothing outstanding, so
        no fill can land for it: an order reaches the broker only after its
        effect's transition is written, and starting the bot again takes
        this lock. The check follows the read, so a transition landing
        between them fails it. Anything else -- no cut, a bot that moved, a
        Dry Run in its own ledger -- reconciles fresh, exactly as alone.
        """
        authority = self._authority_for(binding)
        if reconciled is not None:
            async with authority.start_custody_projection() as cut:
                if authority.reconciliation_covers(reconciled):
                    yield cut
                    return
        async with authority.start_custody_guard() as cut:
            yield cut

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
                clerk_stop_already_committed=False,
            )

    async def stop_after_durable_clerk_stop(
        self,
        broker: str,
        strategy_instance_id: str,
        *,
        lifecycle_run_id: str,
        updated_by: str,
        reason: str | None = None,
    ) -> BotStatusView:
        """Cancel and reap after the SQLite authority already committed run ``lifecycle_run_id``'s STOP.

        Recovery actions and the raw ``runs/stop`` route (#2664) commit the
        lifecycle transition before entering the process registry. Reusing
        :meth:`stop` would author the same natural command key again with
        registry-owned prose, turning a successful durable stop into a
        payload conflict before task cancellation.

        The Stop is its run's alone: a process running another run of the bot
        is left running, its end and intent untouched, and the Stop raises
        ``UnknownBotError`` as for a bot with no process here
        (:meth:`_runs_another_run_locked`).
        """
        async with self._operation_lock(strategy_instance_id):
            if self._runs_another_run_locked(strategy_instance_id, lifecycle_run_id):
                raise UnknownBotError(
                    f"Run '{lifecycle_run_id}' of bot '{strategy_instance_id}' is not running.",
                    detail="The bot runs a later run; a Stop of an earlier one leaves it running.",
                )
            return await self._stop_locked(
                broker,
                strategy_instance_id,
                updated_by=updated_by,
                reason=reason,
                clerk_stop_already_committed=True,
            )

    async def _stop_locked(
        self,
        broker: str,
        strategy_instance_id: str,
        *,
        updated_by: str,
        reason: str | None,
        clerk_stop_already_committed: bool,
        outcome_reason_code: str = "OPERATOR_STOP",
    ) -> BotStatusView:
        """Serialized STOP implementation with terminal Clerk custody proof.

        An operator's Stop, so its record -- the bot's end cancelled, its
        intent STOPPED (:meth:`_record_operator_stop_locked`) -- lands first,
        whether or not this runner still has the bot's process. The stop at
        the end itself is :meth:`_stop_at_its_end`'s.
        """
        self._confined_instance_dir(strategy_instance_id)
        managed = self._bots.get(strategy_instance_id)
        if managed is not None and managed.task.done():
            managed = None  # its task ended: no process left to stop
        if managed is not None and managed.binding.broker != broker:
            raise UnknownBotError(
                f"Bot '{strategy_instance_id}' is not bound to broker '{broker}'.",
                detail=f"The bot's binding carries broker '{managed.binding.broker}'.",
            )
        reason = reason or "operator_stop"
        try:
            # Durable intent BEFORE the in-process cancellation: if the
            # container dies between these two steps, the STOPPED intent survives.
            self._record_operator_stop_locked(strategy_instance_id, updated_by=updated_by, reason=reason)
        except DesiredStateCorruptError as exc:
            # The Stop goes on: its STOP fences the bot, and no end is carried
            # out while the file cannot be read.
            _say_stop_cannot_cancel_end(strategy_instance_id, exc)
        if managed is None:
            raise UnknownBotError(
                f"Bot '{strategy_instance_id}' is not running.",
                detail="Only a running bot can be stopped; see its status for the last outcome.",
            )
        if await self._stop_process_locked(
            managed, reason=reason, clerk_stop_already_committed=clerk_stop_already_committed
        ):
            # ``outcome_reason_code`` names who ended the run (#2559): the
            # failed-launch compensation passes the activation-failure code, an
            # operator's stop keeps OPERATOR_STOP. It is an internal flag, never
            # derived from operator-typed prose. In trade mode the Clerk's custody
            # proof replaces it: that proof (flat, carryover kept, flatten
            # required) drives the panel's next step, and one reason slot cannot
            # carry both, so a trade-mode failed launch still reads as a stop
            # (#2667).
            outcome, canary_rollback = await self._prove_stop(
                managed.binding, prove_terminal_stop_outcome, untraded_outcome=outcome_reason_code
            )
            await self._record_stop(managed.binding, reason_code=outcome, canary_rollback=canary_rollback)
        return self.status(broker, strategy_instance_id)

    async def _stop_process_locked(
        self,
        managed: ManagedBot,
        *,
        reason: str,
        clerk_stop_already_committed: bool,
    ) -> bool:
        """Stop's fence and cancel + reap, under the bot's operation lock, after its durable intent.

        False when the bot's task did not end within the timeout.
        """
        strategy_instance_id = managed.binding.strategy_instance_id
        managed.run_gate.clear()
        if not clerk_stop_already_committed:
            try:
                await commit_stop_before_task_cancel(
                    managed.binding,
                    reason=reason,
                )
            except ActiveClerkUnavailableError as exc:
                raise RunAdmissionRefusedError(
                    str(exc),
                    detail=(
                        "The durable process STOP is recorded, but broker custody "
                        "must be restored before the task can be terminated safely."
                    ),
                ) from exc
        # Stop strategy evaluation before any network-bound custody work. The
        # provisional terminal is replaced once the Clerk returns a fresh proof.
        managed.stop_reason_code = PROVISIONAL_STOP_REASON_CODE
        managed.task.cancel()
        _done, pending = await asyncio.wait({managed.task}, timeout=_STOP_TIMEOUT_S)
        if pending:
            logger.warning(
                "Stop cancellation did not terminate within the timeout",
                extra={
                    "action": "stop_cancellation_timeout",
                    "strategy_instance_id": strategy_instance_id,
                    "run_id": managed.binding.run_id,
                    "timeout_s": _STOP_TIMEOUT_S,
                },
            )
            return False
        # Backstop for a coroutine that never entered supervision (cancelled
        # pre-start): _finalize is idempotent, so this is a no-op whenever the
        # supervisor already recorded the outcome.
        self._terminal.finalize(
            managed.binding,
            kind="STOPPED",
            reason_code=PROVISIONAL_STOP_REASON_CODE,
        )
        self._terminal.reap(strategy_instance_id, managed.binding.run_id)
        return True

    async def _prove_stop(
        self, binding: BrokerBotBinding, prove: StopProver, *, untraded_outcome: str = "OPERATOR_STOP"
    ) -> tuple[str, CanaryRollbackDecision | None]:
        """A stopped run's custody outcome, and a canary's rollback verdict; ``untraded_outcome`` unless it traded.

        ``prove`` is the stop's own proof: a fresh Clerk proof for an operator's
        Stop, the Clerk's own pass for the stop at the bot's end (#2607).
        """
        if binding.broker != "alpaca" or binding.mode != "trade":
            return untraded_outcome, None
        outcome = await prove(
            binding,
            checkpoint_path=self._carryover_checkpoint_path(binding.strategy_instance_id),
            now_ms=self._now_ms,
        )
        # #1729 AC10: the rollback verdict is keyed off this run having
        # been admitted as a Signal-Program-backed trade-mode instance
        # (`program_build.state == "PROVEN"`, the same live-reproof
        # `canary_gate_applies` checks at Deploy) -- never off
        # current canary admission membership. A rollback plausibly
        # *means* revoking the pairing in the activation ledger, so
        # keying the verdict off present membership would
        # read "not a canary" at exactly the moment it matters. This is
        # an evidence record, not a gate: it never influences whether
        # Stop proceeds, only what gets recorded once it has.
        program_build_state = (
            binding.program_build.state if binding.program_build is not None else "NOT_APPLICABLE"
        )
        if not canary_gate_applies(mode=binding.mode, program_build_state=program_build_state):
            return outcome, None
        return outcome, evaluate_canary_rollback(
            strategy_instance_id=binding.strategy_instance_id,
            stop_outcome=outcome,
            evaluated_at_ms=self._now_ms(),
        )

    async def _record_stop(
        self,
        binding: BrokerBotBinding,
        *,
        reason_code: str,
        canary_rollback: CanaryRollbackDecision | None,
    ) -> None:
        """Replace the provisional stop with its proven outcome; then the run's receipt is owed and its authority released."""
        self._terminal.replace_provisional_stop(
            binding,
            reason_code=reason_code,
            canary_rollback=canary_rollback,
        )
        await self._settle_stopped_run(binding)

    async def _settle_stopped_run(self, binding: BrokerBotBinding) -> None:
        """A stopped run whose outcome is recorded owes its replay receipt and releases its authority."""
        self._schedule_run_replay_receipt(binding)
        await self._authority_for(binding).release_after_run_end()

    async def stop_every_running_bot(self, *, updated_by: str, reason: str) -> LaneStopOutcome:
        """The operator's Stop, applied to every live task on this lane (#2268).

        Not ``stop_all``: service shutdown preserves operator intent so the
        bots want to run again after the restart, whereas this is exactly
        ``stop`` per bot — durable ``STOPPED`` intent first, then the Clerk
        STOP and the reap — so every bot stays stopped wherever the lane next
        boots. A bot whose Stop refuses or fails is reported with the
        refusal's own words (or the failure's type) and left to the operator;
        the other bots are still stopped, and nothing here retries or
        escalates.
        A task that ended on its own between the snapshot and its Stop had
        nothing left to stop and is not reported.

        Then every bot on the lane **without** a live task whose recorded
        intent is not STOPPED gets the same durable STOPPED intent (#2269):
        otherwise the lane is idle yet its volume still says those bots want
        to run, and export — which refuses such a copy — would have no way
        out.
        """
        stopped: list[LaneStoppedBot] = []
        refused: list[LaneStopRefusal] = []
        running = [
            (sid, managed.binding.broker, managed.binding.run_id)
            for sid, managed in self._bots.items()
            if not managed.task.done()
        ]
        for sid, broker, run_id in running:
            try:
                await self.stop(broker, sid, updated_by=updated_by, reason=reason)
            except UnknownBotError:
                continue
            except BotRunnerError as exc:
                refused.append(_lane_stop_refusal(sid, run_id, str(exc), exc.detail))
                continue
            except Exception as exc:
                # Deliberately broad: a Clerk or custody failure has no common
                # base, and one bot's failure must neither hide the others'
                # Stops nor cost the lane its receipt. It is logged with its
                # traceback, recorded by type, and the lane-wide stop still
                # fails closed on it (``all_stopped`` is false).
                logger.exception(
                    "Lane-wide stop failed for one bot",
                    extra={
                        "action": "lane_stop_all_bot_failed",
                        "strategy_instance_id": sid,
                        "run_id": run_id,
                    },
                )
                refused.append(
                    _lane_stop_refusal(sid, run_id, f"{type(exc).__name__}: {exc}", None)
                )
                continue
            stopped.append(LaneStoppedBot(strategy_instance_id=sid, run_id=run_id))
        intent_stopped = await self._stop_recorded_intent_of_idle_bots(
            updated_by=updated_by, reason=reason, refused=refused
        )
        return LaneStopOutcome(
            stopped=tuple(stopped),
            intent_stopped=tuple(intent_stopped),
            refused=tuple(refused),
            still_running=self.any_running(),
        )

    async def _stop_recorded_intent_of_idle_bots(
        self, *, updated_by: str, reason: str, refused: list[LaneStopRefusal]
    ) -> list[LaneIntentStoppedBot]:
        """The operator's Stop for every idle bot: its end cancelled, its intent STOPPED.

        Enumerated exactly as export's copy check reads the volume, so a
        lane-wide stop leaves nothing that check would refuse -- and no end
        behind it, even of a bot whose intent already says STOPPED: a crash
        keeps its end for the Clerk, and the end moves with the volume
        (#2607). Each record runs under the bot's operation lock and
        re-checks that no task has started meanwhile; a bot whose intent
        cannot be read or written is a refusal, never skipped. Only a changed
        intent is reported.
        """
        intent_stopped: list[LaneIntentStoppedBot] = []
        for sid in instances_with_recorded_desired_state(self._artifacts_root):
            try:
                async with self._operation_lock(sid):
                    if self._is_running(sid):
                        continue
                    previous = self._record_operator_stop_locked(sid, updated_by=updated_by, reason=reason)
            except (ValueError, OSError, DesiredStateCorruptError) as exc:
                refused.append(
                    _lane_stop_refusal(sid, None, f"{type(exc).__name__}: {exc}", None)
                )
                continue
            if previous is None:
                continue
            logger.warning(
                "Lane-wide stop recorded STOPPED intent for an idle bot",
                extra={
                    "action": "lane_stop_all_intent_stopped",
                    "strategy_instance_id": sid,
                    "previous_desired_state": previous.value,
                },
            )
            intent_stopped.append(
                LaneIntentStoppedBot(
                    strategy_instance_id=sid, previous_desired_state=previous.value
                )
            )
        return intent_stopped

    async def stop_dry_run_restoration(self) -> None:
        """Cancel and await boot's Dry Run restoration, on every path out of the process (#2668).

        An opening a cancellation interrupts releases its account's lease
        (#2582). Stopping is safe when no restoration ever started.
        """
        restoring = self._dry_run_restoration_task
        if restoring is not None and not restoring.done():
            restoring.cancel()
            with suppress(asyncio.CancelledError):
                await restoring

    async def stop_all(self) -> None:
        """Service shutdown: stop every task without overwriting operator intent."""
        await self.stop_dry_run_restoration()
        # No new end is asked for; a stop already asked for finishes first,
        # so its outcome is the end's, not the shutdown's (#2607).
        watch = self._end_watch_task
        if watch is not None and not watch.done():
            watch.cancel()
            with suppress(asyncio.CancelledError):
                await watch
        if self._end_stop_tasks:
            await asyncio.wait(set(self._end_stop_tasks.values()), timeout=_STOP_TIMEOUT_S)
        stopping: list[ManagedBot] = []
        for managed in self._bots.values():
            if managed.task.done():
                continue
            managed.run_gate.clear()
            try:
                await commit_stop_before_task_cancel(
                    managed.binding,
                    reason="service_shutdown",
                )
            except Exception:
                logger.error(
                    "Service shutdown could not commit the Clerk run STOP",
                    extra={
                        "action": "service_shutdown_clerk_stop_failed",
                        "strategy_instance_id": managed.binding.strategy_instance_id,
                        "run_id": managed.binding.run_id,
                    },
                    exc_info=True,
                )
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
            await self._authority_for(managed.binding).release_after_run_end()

    # ── the owner-set end (#2607) ─────────────────────────────────────
    #
    # A bot's end is its desired state: Deploy records it, the owner edits it
    # here, and the Clerk carries it out on its reconciliation pass, reading
    # it through this registry -- the process's installed end schedule
    # (``clerk.sqlite.scheduled_end.BotEndSchedule``).

    def bot_end(self, broker: str, strategy_instance_id: str) -> BotEndView:
        """One bot's end in the owner's words."""
        binding = self.binding_for_control(broker, strategy_instance_id)
        return self._bot_end_view(binding, self._desired_repo(strategy_instance_id).read(), now_ms=self._now_ms())

    async def edit_bot_end(
        self, broker: str, strategy_instance_id: str, choice: BotEndInput, *, updated_by: str
    ) -> BotEndView:
        """Change a bot's end now: no restart, no seal, no binding touched.

        Whether it may change now is ``bot_end.end_edit_refusal``'s answer --
        the one the view's ``editable`` gives too. Raises
        :class:`BotEndRefused` in plain words.
        """
        async with self._operation_lock(strategy_instance_id):
            binding = self.binding_for_control(broker, strategy_instance_id)
            repo = self._desired_repo(strategy_instance_id)
            now = self._now_ms()
            record = repo.read()
            refusal = end_edit_refusal(
                None if record is None else record.pending_end(),
                running=self._is_running(strategy_instance_id),
                now_ms=now,
            )
            if refusal is not None:
                raise refusal
            resolved = resolve_bot_end(
                choice, now_ms=now, dry_run=binding.mode == "dry_run", use_rth=binding.use_rth,
            )
            record = repo.set_end(resolved.end, updated_by=updated_by, now_ms=now)
            logger.info(
                "The owner changed a bot's end",
                extra={
                    "action": "bot_end_edited",
                    "strategy_instance_id": strategy_instance_id,
                    "end_at_ms": None if resolved.end is None else resolved.end.end_at_ms,
                    "end_action": None if resolved.end is None else resolved.end.end_action,
                },
            )
            return self._bot_end_view(binding, record, now_ms=now, notice=resolved.notice)

    def pending_ends(self, strategy_instance_ids: Sequence[str]) -> list[ScheduledEnd]:
        """The end schedule's read: each named bot's end the Clerk has not carried out."""
        return [
            ScheduledEnd(strategy_instance_id=sid, end=end)
            for sid in strategy_instance_ids
            if (end := self._pending_end(sid)) is not None
        ]

    def _pending_end(self, strategy_instance_id: str) -> BotEnd | None:
        """The bot's end still to be carried out (``_desired_record_for_end``)."""
        record = self._desired_record_for_end(strategy_instance_id)
        return None if record is None else record.pending_end()

    def _desired_record_for_end(self, strategy_instance_id: str) -> DesiredStateRecord | None:
        """The bot's desired state as its end reads it; ``None`` when absent or unreadable.

        An unreadable file's end cannot be carried out until the file is
        repaired. That is said once, loudly, when it becomes unreadable -- not
        on every pass and every look of the watch -- and once more when it
        can be read again.
        """
        try:
            record = self._desired_repo(strategy_instance_id).read()
        except (ValueError, OSError, DesiredStateCorruptError) as exc:
            if strategy_instance_id not in self._unreadable_end_sids:
                self._unreadable_end_sids.add(strategy_instance_id)
                logger.error(
                    "A bot's end could not be read, so the Clerk cannot carry it out",
                    extra={
                        "action": "bot_end_unreadable",
                        "strategy_instance_id": strategy_instance_id,
                        "error": str(exc),
                    },
                )
            return None
        if strategy_instance_id in self._unreadable_end_sids:
            self._unreadable_end_sids.discard(strategy_instance_id)
            logger.info(
                "A bot's end can be read again",
                extra={"action": "bot_end_readable_again", "strategy_instance_id": strategy_instance_id},
            )
        return record

    async def cancel_end(self, strategy_instance_id: str, *, lifecycle_run_id: str, updated_by: str) -> None:
        """The owner's Stop of run ``lifecycle_run_id`` cancels the bot's scheduled end: nothing is sold at the end time.

        The panel's Stop and the raw ``runs/stop`` route (#2664) call this
        before they commit the run's STOP, through one sequence
        (``recovery_execution.operator_stop_run``), as :meth:`stop` records its
        intent first: between that STOP and the process stop, neither the end watch
        nor a Clerk pass may read the end as still to be carried out. The rest
        of either Stop's record -- its STOPPED intent -- lands with the
        process stop (:meth:`stop_after_durable_clerk_stop`). Durable in the bot's desired
        state, so no restart revives it, and whether or not this runner has
        the bot's process. A process running another run of the bot keeps its
        end (:meth:`_runs_another_run_locked`).
        """
        async with self._operation_lock(strategy_instance_id):
            if self._runs_another_run_locked(strategy_instance_id, lifecycle_run_id):
                return
            try:
                self._cancel_end_locked(strategy_instance_id, updated_by=updated_by)
            except DesiredStateCorruptError as exc:
                # The Stop goes on: its STOP fences the bot, and no end is
                # carried out while the file cannot be read.
                _say_stop_cannot_cancel_end(strategy_instance_id, exc)

    def _runs_another_run_locked(self, strategy_instance_id: str, lifecycle_run_id: str) -> bool:
        """Whether this runner's live process of the bot runs a run other than ``lifecycle_run_id``.

        An operator's Stop acts for its run alone, as the Clerk's stop at the
        end does (:meth:`stop_bot_at_its_end`). The Clerk answers whether the
        run is the bot's current one before the Stop reaches this runner, and
        a Deploy may start a later run in between -- a retry of the Stop
        landing while the bot is redeployed -- whose process, end and intent
        that Stop must not touch. Read under the bot's operation lock, which a
        Deploy holds until its process runs. With no live process here nothing
        names another run, and the Stop is the bot's.
        """
        managed = self._bots.get(strategy_instance_id)
        if managed is None or managed.task.done() or managed.binding.run_id == lifecycle_run_id:
            return False
        logger.info(
            "An operator's Stop of an earlier run left the bot's later run as it was",
            extra={
                "action": "operator_stop_of_an_earlier_run",
                "strategy_instance_id": strategy_instance_id,
                "run_id": lifecycle_run_id,
                "running_run_id": managed.binding.run_id,
            },
        )
        return True

    def _record_operator_stop_locked(
        self, strategy_instance_id: str, *, updated_by: str, reason: str
    ) -> DesiredState | None:
        """An operator's Stop, as the bot's desired state records it: the one write every operator Stop makes.

        The panel's, the raw ``runs/stop`` route's (#2664), the lane-wide one,
        with or without the bot's process in this runner (#2607). First the
        bot's end is cancelled, whatever its
        intent already says: Stop does not sell, so no sale is left scheduled
        behind it -- a crash's STOPPED keeps its end for the Clerk, and the
        owner's Stop ends it. Then the intent is STOPPED, the end left as the
        cancel left it. An intent already STOPPED keeps its record, and a bot
        with neither a desired state nor a process here is given none.

        Returns the intent it replaced, ``None`` when it replaced none. Raises
        ``DesiredStateCorruptError`` when the desired state cannot be read.
        Every other STOPPED write keeps the end, since none is an operator's
        Stop: a run's crash or unverified exit (``BotRunTerminalRecorder``),
        boot recovery's repair of an interrupted or terminal run
        (``BotBootRecovery``), and the Clerk's stop at the end, which keeps it
        for the Clerk to record carried out (:meth:`_stop_at_its_end`).
        """
        self._cancel_end_locked(strategy_instance_id, updated_by=updated_by)
        repo = self._desired_repo(strategy_instance_id)
        record = repo.read()
        if record is None and not self._is_running(strategy_instance_id):
            return None
        if record is not None and record.desired_state is DesiredState.STOPPED:
            return None
        repo.set(DesiredState.STOPPED, updated_by=updated_by, now_ms=self._now_ms(), reason=reason)
        return DesiredState.RUNNING if record is None else record.desired_state

    def _cancel_end_locked(self, strategy_instance_id: str, *, updated_by: str) -> None:
        """Cancel the bot's pending end, durably; raises ``DesiredStateCorruptError`` when it cannot be read."""
        cancelled = self._desired_repo(strategy_instance_id).cancel_end(
            updated_by=updated_by, now_ms=self._now_ms()
        )
        if cancelled is not None:
            logger.info(
                "The owner's Stop cancelled a bot's scheduled end",
                extra={
                    "action": "bot_end_cancelled",
                    "strategy_instance_id": strategy_instance_id,
                    "end_at_ms": cancelled.end_at_ms,
                    "end_action": cancelled.end_action,
                },
            )

    def stop_bot_at_its_end(self, strategy_instance_id: str, lifecycle_run_id: str) -> None:
        """The Clerk committed the run's STOP at its end: fence the bot now, stop its task next."""
        managed = self._bots.get(strategy_instance_id)
        if managed is None or managed.task.done() or managed.binding.run_id != lifecycle_run_id:
            return
        self._start_stop_at_its_end(managed)

    def _start_stop_at_its_end(self, managed: ManagedBot) -> None:
        """Raise Stop's fence (``run_gate``) at once, then stop the process in a task of its own.

        The fence means the bot makes no further decision. The rest of Stop --
        cancel, terminal evidence, release -- runs as its own task: the
        Clerk's pass that asks for it is still running, and the stop's proof
        is that pass once published. One such task per bot at a time.
        """
        sid = managed.binding.strategy_instance_id
        managed.run_gate.clear()
        if sid in self._end_stop_tasks:
            return
        task = asyncio.get_running_loop().create_task(self._stop_at_its_end(managed.binding), name=f"bot-end:{sid}")
        self._end_stop_tasks[sid] = task
        task.add_done_callback(lambda _done: self._end_stop_tasks.pop(sid, None))

    def record_end_carried_out(self, end: ScheduledEnd, *, at_ms: int) -> None:
        """The end schedule's report: the Clerk carried ``end`` out."""
        self._desired_repo(end.strategy_instance_id).mark_end_carried_out(
            end.end, updated_by=_END_UPDATED_BY, now_ms=at_ms
        )

    async def _stop_at_its_end(self, binding: BrokerBotBinding) -> None:
        """Stop the process under the bot's lock, then wait for the proof with the lock released.

        The proof waits up to ``END_STOP_PROOF_WAIT_S`` for the Clerk's pass.
        Holding the bot's operation lock that long would hold a Deploy of the
        bot -- which takes the process-wide graduation fence first -- and with
        it every Deploy and the live cutover. The outcome is recorded under
        the lock again (:meth:`_record_stop_at_its_end`).
        """
        sid = binding.strategy_instance_id
        try:
            async with self._operation_lock(sid):
                managed = self._bots.get(sid)
                if managed is None or managed.task.done() or managed.binding.run_id != binding.run_id:
                    return
                # Durable intent first, as every Stop's. The end is the
                # Clerk's to record carried out, so this write keeps it.
                self._desired_repo(sid).set(
                    DesiredState.STOPPED, updated_by=_END_UPDATED_BY, now_ms=self._now_ms(), reason=SCHEDULED_END_REASON
                )
                if not await self._stop_process_locked(
                    managed, reason=SCHEDULED_END_REASON, clerk_stop_already_committed=True
                ):
                    return
            # At its end the Clerk's own pass is the proof: every bot on the
            # default end stops in the same minute, and a reconcile each would
            # be one whole account pass per bot.
            _outcome, canary_rollback = await self._prove_stop(binding, prove_end_stop_outcome)
            async with self._operation_lock(sid):
                await self._record_stop_at_its_end(binding, canary_rollback=canary_rollback)
        except Exception:
            # Its own task, so nothing above it can report the failure. The
            # run is already stopped at the Clerk and fenced here; what failed
            # is the process stop and its evidence, which the end watch
            # retries while the process still runs.
            logger.exception(
                "A bot could not be stopped at its end; the Clerk already stopped its run",
                extra={"action": "bot_end_process_stop_failed", "strategy_instance_id": sid, "run_id": binding.run_id},
            )

    async def _record_stop_at_its_end(
        self, binding: BrokerBotBinding, *, canary_rollback: CanaryRollbackDecision | None
    ) -> None:
        """Record a stop at the bot's end, proven with the bot's lock released; under that lock again.

        While the proof was awaited the bot may have moved on. A later run of
        it began: the outcome is the stopped run's alone, recorded under its
        run id -- its receipt, and the replay receipt it owes -- and never
        projected over the later run's. Its registration is gone: there is
        nothing to record it in, and the run's outcome stays provisional.
        """
        sid = binding.strategy_instance_id
        current = self._read_binding(sid)
        if current is None:
            logger.warning(
                "A bot's registration was gone once its stop at its end was proven; the stopped run's outcome "
                "stays provisional",
                extra={"action": "bot_end_proof_unrecorded", "strategy_instance_id": sid, "run_id": binding.run_id},
            )
            return
        if current.run_id == binding.run_id:
            await self._record_stop(binding, reason_code=SCHEDULED_END_REASON_CODE, canary_rollback=canary_rollback)
            return
        logger.info(
            "A later run of the bot began before its stop at its end was proven; the proof is recorded as the "
            "stopped run's alone",
            extra={
                "action": "bot_end_proof_superseded",
                "strategy_instance_id": sid,
                "run_id": binding.run_id,
                "later_run_id": current.run_id,
            },
        )
        self._terminal.record_replaced_run_stop(
            binding, reason_code=SCHEDULED_END_REASON_CODE, canary_rollback=canary_rollback
        )
        await self._settle_stopped_run(binding)

    def start_end_watch(self) -> asyncio.Task[None]:
        """Start carrying each due end forward from the runner's side (:meth:`carry_out_due_ends`)."""
        if self._end_watch_task is None or self._end_watch_task.done():
            self._end_watch_task = asyncio.get_running_loop().create_task(self._watch_ends(), name="bot-end-watch")
        return self._end_watch_task

    async def _watch_ends(self) -> None:
        while True:
            try:
                await self.carry_out_due_ends()
            except Exception:
                logger.exception("The end watch failed one look; it looks again", extra={"action": "bot_end_watch_failed"})
            await asyncio.sleep(_END_WATCH_INTERVAL_S)

    async def carry_out_due_ends(self) -> None:
        """Carry each end that has come forward from the runner's side; the Clerk's pass does the rest.

        For each running bot whose end has come:

        * its run still ACTIVE at the Clerk: ask the bot's Clerk for a pass
          now, once per end and again after a failed pass. The account's
          periodic sweep would reach the end within its interval; a Dry Run's
          own Clerk has no periodic sweep.
        * its run already stopped at the Clerk -- a pass fenced it, but the
          process stop it asked for failed or never came: stop the process
          now. Meanwhile the bot decides nothing: a fenced bot only observes,
          and the Clerk refuses the next decision of one whose fence never
          came.

        And a stopped Dry Run whose end has come has it recorded carried out
        (:meth:`_record_ends_of_stopped_dry_runs`).
        """
        now = self._now_ms()
        for sid, managed in list(self._bots.items()):
            if managed.task.done() or sid in self._end_stop_tasks:
                continue
            try:
                pending = self._pending_end(sid)
                if pending is None or pending.end_at_ms > now:
                    continue
                await self._carry_out_due_end(managed, pending)
            except Exception as exc:
                # Deliberately broad (#2363): one bot's failure -- its Clerk
                # absent, its database locked -- is that bot's, said and
                # retried on the next look; the watch still reaches the rest.
                self._end_watch_failed(sid, exc)
        self._record_ends_of_stopped_dry_runs(now)

    async def _carry_out_due_end(self, managed: ManagedBot, pending: BotEnd) -> None:
        sid = managed.binding.strategy_instance_id
        run_id = managed.binding.run_id
        authority = self._authority_for(managed.binding)
        projector = authority.lifecycle_projector()
        if not projector.run_is_active(strategy_instance_id=sid, run_id=run_id):
            # Only the Clerk's STOP at the end is the end's to finish here. Any
            # other Stop stops the process itself and never keeps or carries
            # out the end -- the owner's cancels it before its STOP commits.
            if projector.run_stop_reason(strategy_instance_id=sid, run_id=run_id) != SCHEDULED_END_REASON:
                return
            logger.warning(
                "A bot's run was stopped at its end while its process still ran; stopping the process now",
                extra={"action": "bot_end_process_restopped", "strategy_instance_id": sid, "run_id": run_id},
            )
            self._start_stop_at_its_end(managed)
            return
        if managed.end_pass_asked_for_ms == pending.end_at_ms:
            return
        await authority.reconcile_for_end()
        self._end_watch_failures.pop(sid, None)
        managed.end_pass_asked_for_ms = pending.end_at_ms

    def _end_watch_failed(self, strategy_instance_id: str, exc: Exception) -> None:
        """Say why a due end could not be carried forward: once per kind of failure, then quietly.

        The watch looks again every few seconds, so the same failure -- no
        account Clerk installed, a Dry Run's account closed -- is logged when
        it starts, with its stack only when it is not one of those expected
        refusals, and at debug while it lasts.
        """
        kind = type(exc).__name__
        extra = {"action": "bot_end_pass_failed", "strategy_instance_id": strategy_instance_id, "error": str(exc)}
        if self._end_watch_failures.get(strategy_instance_id) == kind:
            logger.debug("A bot's end still cannot be carried forward", extra=extra)
            return
        self._end_watch_failures[strategy_instance_id] = kind
        message = "A bot's end has come but could not be carried forward; the watch looks again"
        if isinstance(exc, (StartAdmissionUnavailable, AlpacaLifecycleAuthorityUnavailableError)):
            logger.warning(message, extra=extra)
        else:
            logger.error(message, exc_info=exc, extra=extra)

    def _record_ends_of_stopped_dry_runs(self, now_ms: int) -> None:
        """Record carried out the end of every stopped Dry Run once it comes.

        A stopped Dry Run has nothing left for its end to do: its run is over,
        and its simulation closed what it held at the last price it saw when
        the run ended (#2641). Its account may be closed, so no pass of its
        Clerk would ever come to record the end -- it would read as due
        forever. A bot whose owner wants it running (a restart's restoration
        is pending) is left to its run.

        Only candidates are read, never every bot on every look: every bot
        with a desired state on the first look (what the last process left),
        then each Dry Run whose run ends here (``_supervise``). A candidate is
        dropped once it runs, has no end left, or is no Dry Run.
        """
        if not self._stopped_end_candidates_seeded:
            self._stopped_end_candidates_seeded = True
            self._stopped_end_candidates.update(instances_with_recorded_desired_state(self._artifacts_root))
        for sid in sorted(self._stopped_end_candidates):
            record = None if self._is_running(sid) else self._desired_record_for_end(sid)
            pending = None if record is None else record.pending_end()
            if record is None or pending is None:
                self._stopped_end_candidates.discard(sid)
                continue
            if pending.end_at_ms > now_ms or record.desired_state is not DesiredState.STOPPED:
                continue
            self._stopped_end_candidates.discard(sid)
            binding = self._read_binding(sid)
            if binding is None or binding.mode != "dry_run":
                continue  # a trading bot's end is its account Clerk's to carry out
            self.record_end_carried_out(ScheduledEnd(strategy_instance_id=sid, end=pending), at_ms=now_ms)
            logger.info(
                "Recorded a stopped Dry Run's end carried out: its simulation closed what it held when its run ended",
                extra={"action": "dry_run_end_recorded", "strategy_instance_id": sid, "end_at_ms": pending.end_at_ms},
            )

    def _bot_end_view(
        self,
        binding: BrokerBotBinding,
        record: DesiredStateRecord | None,
        *,
        now_ms: int,
        notice: str | None = None,
    ) -> BotEndView:
        return bot_end_view(
            None if record is None else record.end,
            now_ms=now_ms,
            dry_run=binding.mode == "dry_run",
            running=self._is_running(binding.strategy_instance_id),
            use_rth=binding.use_rth,
            notice=notice,
        )

    # ── S5 boot recovery (container restart is a drilled event) ───────

    async def run_boot_recovery(
        self,
        *,
        recover: Callable[[], Awaitable[None]] | None = None,
        reconcile: Callable[[], Awaitable[object]] | None = None,
        unresolved_intents_probe: UnresolvedIntentsProbe | None = None,
        recovery_evaluation: RecoveryEvaluationProbe | None = None,
    ) -> BootRecoveryReport:
        """Reconcile durable ON_DUTY state against the (empty) task registry.

        The Clerk first recovers and reconciles SQLite authority. Each runner
        restoration candidate with no live task is then projected from that
        authority and, when interrupted, receives typed durable evidence
        (``EXITED_UNVERIFIED`` / ``INTERRUPTED_BY_RESTART``). Nothing is
        auto-restarted. A failed authority step leaves the boot gate closed,
        and so does a sweep that finished without a lifecycle authority to
        project against: the service still boots and serves its read
        surface, but Start stays refused with the sweep's reason.

        Dry Runs are not in this sweep: each is restored by its own authority
        (``start_dry_run_restoration``), off the serving path (#2582).
        """
        # A sweep in progress -- or one that raised -- is pending, never the
        # answer an earlier sweep gave: a reconnected account authority runs
        # this again (#2582), and Start must not read the Clerk-less report.
        self._boot_recovery_report = None
        self._boot_recovery_failed = False
        try:
            report = await self._boot_recovery.run(
                recover=recover,
                reconcile=reconcile,
                unresolved_intents_probe=unresolved_intents_probe,
            )
        except Exception:
            # Nothing reruns a sweep that raised but a reconnect, so Start
            # stops saying "wait" for one (#2620).
            self._boot_recovery_failed = True
            raise
        self._unresolved_intents_probe = unresolved_intents_probe
        self._recovery_evaluation = recovery_evaluation
        self._boot_recovery_report = report
        # Direction 2: heal replay receipts a dead process owed (orphaned
        # `pending` or a terminal run that never scheduled). After the sweep so
        # `_is_running` reflects the recovered fleet.
        self._resume_pending_replay_receipts()
        return report

    async def run_lease_recovery(
        self,
        *,
        reconcile: Callable[[], Awaitable[object]] | None = None,
    ) -> BootRecoveryReport:
        """Close the terminal-evidence hole after a lease revival (ADR 0050).

        A narrow in-process re-run of the boot scan's repair pass: one
        reconcile pass, then the lifecycle repair that commits the SQLite
        STOPs for runs whose tasks died on the dead handle. Deliberately
        skips the boot-only steps (Dry Run restoration, replay receipts) —
        those belong to a fresh process, not a revived lease.
        Its report is diagnostic only: the start gate keeps the report from
        ``run_boot_recovery`` (a revived lease implies the authority is
        installed, so this pass cannot leave a bot unprojected).
        """
        return await self._boot_recovery.run(
            reconcile=reconcile,
            unresolved_intents_probe=self._unresolved_intents_probe,
            provenance=LEASE_REVIVAL_PROVENANCE,
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

    def bindings_for_broker(self, broker: str) -> list[BrokerBotBinding]:
        """Return durable bindings without projecting a currently live authority.

        Broker V2 uses this only to select an account authority before it
        reads that authority's roster. In particular, a stopped Dry Run may
        have released its in-memory Clerk runtime while its sealed evidence
        remains available for a read-only projection.
        """
        return self._bindings.list_for_broker(broker)

    def current_run(self, broker: str, strategy_instance_id: str) -> BotRunView:
        """Return the backend-owned current-run projection."""
        binding = self.binding_for_control(broker, strategy_instance_id)
        return self._run_evidence.current(
            binding,
            self.process_fact(broker, strategy_instance_id),
        )

    def run_replay_receipt(
        self, broker: str, strategy_instance_id: str, run_id: str
    ) -> RunReplayReceipt | None:
        """Return the durable replay receipt for one run, or an honest None."""
        del broker  # the receipt file is instance-scoped; the router validated the segment
        return self._replay_proof.read(strategy_instance_id, run_id)

    async def generate_run_replay_receipt(
        self, broker: str, strategy_instance_id: str, run_id: str
    ) -> RunReplayReceipt:
        """Recompute one completed run's replay receipt on demand."""
        return await self._replay_proof.generate(broker, strategy_instance_id, run_id)

    def _schedule_run_replay_receipt(self, binding: BrokerBotBinding) -> None:
        """Direction 2: a stopping run owes a parity receipt. Never blocks Stop."""
        if binding.mode not in ("trade", "dry_run"):
            return
        if binding.strategy_key not in supported_alpaca_paper_strategy_keys():
            logger.info(
                "Run replay receipt skipped: no Signal Program",
                extra={
                    "action": "run_replay_receipt_skipped",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": binding.run_id,
                    "strategy_key": binding.strategy_key,
                },
            )
            return
        try:
            self._replay_proof.write_pending(binding, binding.run_id)
        except OSError as error:
            # An unwritable receipt directory (disk full, path conflict) must
            # not fail Stop -- and must not abort boot repair for the whole
            # fleet when scheduled from _resume_pending_replay_receipts (Codex
            # PR #1769). Skip scheduling; the run's terminal outcome persists,
            # so the next boot scan re-attempts.
            logger.warning(
                "Run replay pending receipt could not be written; skipping generation",
                extra={
                    "action": "run_replay_pending_write_failed",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": binding.run_id,
                    "reason": str(error),
                },
            )
            return
        task = asyncio.get_running_loop().create_task(
            self._generate_replay_receipt_in_background(binding)
        )
        self._replay_receipt_tasks.add(task)
        task.add_done_callback(self._replay_receipt_tasks.discard)

    async def _generate_replay_receipt_in_background(self, binding: BrokerBotBinding) -> None:
        # When scheduled from a terminal branch of the run's own task
        # (_supervise), that task has not finished yet, so `is_running` would
        # briefly refuse generation. Wait for the supervised task to settle
        # first -- bounded by the same timeout Stop uses for cancellation.
        managed = self._bots.get(binding.strategy_instance_id)
        if managed is not None and not managed.task.done():
            await asyncio.wait({managed.task}, timeout=_STOP_TIMEOUT_S)
        try:
            await self._replay_proof.generate(
                binding.broker, binding.strategy_instance_id, binding.run_id
            )
        except RunReplayUnavailableError as error:
            logger.warning(
                "Run replay receipt unavailable",
                extra={
                    "action": "run_replay_receipt_unavailable",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": binding.run_id,
                    "reason": str(error),
                },
            )
        except (BotRunnerError, ValueError, OSError):
            # generate() converts compute failures into a durable replay_failed
            # receipt itself; the failures that can still escape it -- a reaped
            # binding (BotRunnerError), a corrupt runs/<run_id>.json read
            # (ValueError), or a failed final receipt write (OSError) -- would
            # otherwise be lost as an unretrieved-task warning, leaving the
            # receipt stuck `pending`. Log them structured so they stay
            # observable and the boot scan can retry (Codex PR #1769).
            logger.exception(
                "Run replay background generation failed",
                extra={
                    "action": "run_replay_background_failed",
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": binding.run_id,
                },
            )

    def _resume_pending_replay_receipts(self) -> None:
        """Boot repair (Direction 2): re-schedule receipts a dead process owed.

        Covers two crash shapes: a `pending` receipt whose in-memory task died
        with the process, and a terminal run (crashed / stream-ended /
        service-shutdown) that never reached scheduling at all. Scope is each
        instance's *current* run -- older runs stay on-demand via POST.
        Alpaca is the only in-container runner broker (IBKR bots are
        host-daemon-managed), so the sweep is alpaca-scoped like _supervise.
        """
        for binding in self._bindings.list_for_broker("alpaca"):
            if binding.mode not in ("trade", "dry_run"):
                continue
            if binding.strategy_key not in supported_alpaca_paper_strategy_keys():
                continue
            if self._is_running(binding.strategy_instance_id):
                continue
            try:
                receipt = self._replay_proof.read(binding.strategy_instance_id, binding.run_id)
                if receipt is not None and receipt.status != "pending":
                    continue
                outcome = self._bindings.read_outcome(binding.strategy_instance_id, binding.run_id)
            except (ValueError, OSError) as error:
                logger.warning(
                    "Boot replay-receipt scan skipped one instance",
                    extra={
                        "action": "run_replay_boot_scan_skipped",
                        "strategy_instance_id": binding.strategy_instance_id,
                        "run_id": binding.run_id,
                        "reason": str(error),
                    },
                )
                continue
            if outcome is None:
                continue  # not terminal; its own Stop/terminal path will schedule
            self._schedule_run_replay_receipt(binding)

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

    async def _supervise(
        self,
        binding: BrokerBotBinding,
        feed: MarketDataFeed,
        run_gate: asyncio.Event,
    ) -> None:
        """Run the bot; on ANY exit record a typed durable duty outcome, then reap."""
        sid = binding.strategy_instance_id
        try:
            source_bars = self._authority_for(binding).source_bars()
            await execute_bot_run(
                binding,
                feed,
                run_gate=run_gate,
                instance_dir=self._confined_instance_dir(sid),
                source_bars=source_bars,
            )
        except asyncio.CancelledError:
            managed = self._bots.get(sid)
            stop_reason = managed.stop_reason_code if managed is not None else None
            if stop_reason is not None:
                self._terminal.finalize(binding, kind="STOPPED", reason_code=stop_reason)
            else:
                # A cancellation nobody asked for is a kill, not a clean stop.
                await self._terminal.finalize_after_authority_stop(
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
            # A refusal where no decision was ever made on the refused data is
            # recorded under its own reason (``FEED_REFUSAL_REASON_CODES``):
            # "the feed died" would send the operator looking at a running
            # bot's stream for a refused warmup (#2365, #2314), or at
            # connectivity when the data itself is corrupt (#2444). Every
            # other feed failure keeps the long-standing FEED_DEATH code the
            # manual and panel describe.
            await self._terminal.finalize_crash(
                binding,
                exc,
                reason_code=(
                    exc.reason
                    if exc.reason in FEED_REFUSAL_REASON_CODES
                    else "FEED_DEATH"
                ),
            )
            self._schedule_run_replay_receipt(binding)
        except Exception as exc:
            # Supervision boundary: every crash becomes typed durable evidence
            # plus a logged traceback — deliberately not re-raised, so the
            # orphaned task does not double-log via the event loop.
            logger.exception(
                "Bot crashed",
                extra={"action": "bot_crashed", "strategy_instance_id": sid},
            )
            await self._terminal.finalize_crash(
                binding,
                exc,
                reason_code=type(exc).__name__,
            )
            self._schedule_run_replay_receipt(binding)
        else:
            await self._terminal.finalize_after_authority_stop(
                binding,
                kind="EXITED_UNVERIFIED",
                reason_code="BAR_STREAM_ENDED",
            )
            self._schedule_run_replay_receipt(binding)
        finally:
            # Preserve the record long enough to distinguish an
            # operator/service STOP from an unexpected task exit. ``reap``
            # removes it from ``_bots``.
            managed = self._bots.get(sid)
            self._terminal.reap(sid, binding.run_id)
            if binding.mode == "dry_run":
                # Its end, if one is still to come, is recorded when it comes (#2607).
                self._stopped_end_candidates.add(sid)
            # An operator/service STOP performs a second, authoritative
            # terminal projection after this task unwinds. Keep its exact
            # synthetic authority alive until that projection completes;
            # otherwise a fast cancellation can unregister custody between
            # the task's provisional terminal record and the final proof.
            if managed is None or managed.stop_reason_code is None:
                await self._authority_for(binding).release_after_run_end()

    # ── guards and composition ────────────────────────────────────────

    def _operation_lock(self, strategy_instance_id: str) -> asyncio.Lock:
        """One lifecycle mutation at a time for a strategy instance."""
        return self._operation_locks.setdefault(strategy_instance_id, asyncio.Lock())

    def _refuse_unrestored_dry_run_at_once(self, strategy_instance_id: str) -> None:
        """Refuse Start for an unrestored Dry Run before the bot's operation lock (#2668).

        The restoration now holds that lock while it waits out a lease, and a
        Start that waited for it would wait out the lease too -- #2582's
        contract is the opposite: the refusal is immediate, translated the
        same way the admission flow translates it. It is the one place Start
        refuses one: both Start entries answer it before the lock, and a
        restoration that has left ``restoring`` never returns to it.
        """
        try:
            refuse_unrestored_dry_run(self.dry_run_restoration_state(strategy_instance_id))
        except StartAdmissionUnavailable as exc:
            raise RunAdmissionRefusedError(str(exc), detail=exc.detail) from exc

    def _authority_for(self, binding: BrokerBotBinding):
        """Return the one typed custody/evidence authority for a binding."""
        return self._authorities.for_binding(binding)

    def _start_custody_guard(
        self,
        binding: BrokerBotBinding,
    ) -> AbstractAsyncContextManager[AdmissionCustodyCut]:
        return self._authority_for(binding).start_custody_guard()

    def _start_custody_projection(
        self,
        binding: BrokerBotBinding,
    ) -> AbstractAsyncContextManager[AdmissionCustodyCut]:
        return self._authority_for(binding).start_custody_projection()

    def _lifecycle_projector_for_instance(self, strategy_instance_id: str) -> AlpacaLifecycleProjector:
        binding = self._bindings.read(strategy_instance_id)
        if binding is None:
            return self._lifecycle_projector
        return self._authority_for(binding).lifecycle_projector()

    @asynccontextmanager
    async def synthetic_runtime_for_projection(
        self,
        binding: BrokerBotBinding,
    ) -> AsyncIterator[ActiveClerkRuntime]:
        """Temporarily compose an inactive Dry Run authority for one request.

        A stopped synthetic run still has durable custody evidence that must
        remain visible in Broker V2 and recoverable inside its simulator. A
        panel read, or a recovery action it presents, may therefore reopen its
        sealed authority, but a runtime composed solely for that request is
        released once the request has finished.
        """
        if binding.mode != "dry_run":
            raise ValueError("Only a Dry Run binding has a synthetic authority.")
        async with self._authority_for(binding).runtime_for_projection() as runtime:
            if runtime is None:
                raise RunAdmissionRefusedError(
                    "Only a Dry Run binding has a synthetic projection runtime."
                )
            yield runtime

    def unbound_dry_run(self, strategy_instance_id: str) -> UnboundDryRunAuthority | None:
        """The Dry Run authority a Deploy committed before recording its binding, if any.

        Deploy commits the private ``sim:`` authority's budget and run before
        the launch writes the binding, so a crash in between leaves only that
        authority's own activation to find it by. ``None`` when no private
        authority was ever activated for this identity. The authority it
        answers can only be read, never admitted or launched (#2559).
        """
        return self._authorities.for_unbound_dry_run(strategy_instance_id)

    def dry_run_restoration_state(self, strategy_instance_id: str) -> DryRunRestorationState | None:
        """Where boot's restoration of this Dry Run stands; ``None`` once restored (#2582).

        The one read the panel uses to answer at once for a bot still being
        restored (#2668) -- its account's runtime lock is held by the
        restoration's lease wait -- and Start uses to refuse until the bot's
        own restoration settles.
        """
        return self._dry_run_restorations.get(strategy_instance_id)

    def start_dry_run_restoration(self) -> asyncio.Task[None]:
        """Restore every Dry Run's own simulated account after boot, off the serving path (#2582).

        A quick restart meets its dead predecessor's execution lease on each
        Dry Run's account, and waiting those out inside the lifespan held the
        real-money lane off the network. So each Dry Run is marked restoring
        now -- Start refuses it until its own restoration settles -- and a
        background task restores them one at a time: open its account,
        waiting out a held lease, then give its runs the sweep's own repair.

        A Deploy that crashed between its budget commit and its binding
        record left a private ``sim:`` authority no binding indexes; those
        orphans join the same restoration (#2559), so a restart releases
        them through the recovery path instead of the next read doing it.
        """
        dry_runs = [binding for binding in self._bindings.list_for_broker("alpaca") if binding.mode == "dry_run"]
        restorations = [
            _DryRunRestoration(
                binding.strategy_instance_id,
                partial(self._authority_for, binding),
                partial(self._repair_restored_dry_run, binding),
            )
            for binding in dry_runs
        ]
        restorations += [
            _DryRunRestoration(
                orphan.strategy_instance_id,
                (lambda authority=orphan: authority),
                _nothing_to_repair,
            )
            for orphan in self._unbound_dry_run_authorities(bound={binding.strategy_instance_id for binding in dry_runs})
        ]
        self._dry_run_restorations = dict.fromkeys(
            (restoration.strategy_instance_id for restoration in restorations), "restoring"
        )
        self._dry_run_restoration_task = asyncio.create_task(
            self._restore_dry_runs(restorations), name="dry-run-boot-restoration"
        )
        return self._dry_run_restoration_task

    def _unbound_dry_run_authorities(self, *, bound: set[str]) -> list[UnboundDryRunAuthority]:
        """Private ``sim:`` authorities a crash left with no binding (#2559).

        Found by their own activations, skipped when a binding indexes them:
        those take the bound path above, which reads the freshest binding
        rather than this boot-time snapshot. Runs synchronously inside the
        lifespan, before the restoration task, so -- like that task -- it is
        an isolation boundary for every exception: an unreadable ledger or one
        orphan that cannot be matched is a logged skip, never a lane that does
        not start (#2582). Each orphan's authority is built from this one
        listing; the ledger's own consistency is proven where the orphan is
        opened, inside the restoration's per-bot boundary.
        """
        orphans: list[UnboundDryRunAuthority] = []
        try:
            account_ids = SyntheticActivationStore(self._artifacts_root).account_ids()
        except Exception as exc:
            logger.error(
                "Dry Run activations could not be listed at boot; unbound orphans wait for the next restart",
                extra={"action": "boot_dry_run_activations_unreadable", "error": str(exc)},
                exc_info=True,
            )
            return orphans
        for account_id in account_ids:
            # The store admits only ``sim:`` accounts.
            sid = account_id.removeprefix(SIM_ACCOUNT_PREFIX)
            if sid in bound:
                continue
            try:
                if self._read_binding(sid) is not None:
                    continue
                orphans.append(self._authorities.unbound_dry_run(sid))
            except Exception as exc:
                logger.error(
                    "A Dry Run activation could not be matched to a binding at boot; it is skipped",
                    extra={
                        "action": "boot_dry_run_activation_unmatched",
                        "account_id": account_id,
                        "error": str(exc),
                    },
                    exc_info=True,
                )
        return orphans

    async def _repair_restored_dry_run(self, binding: BrokerBotBinding) -> tuple[str, ...]:
        """The sweep's own repair for a restored Dry Run's runs; its binding's run is the fallback candidate."""
        return await self._boot_recovery.repair_restored_dry_run(self._binding_recovery_candidates(binding).values())

    async def _restore_dry_runs(self, restorations: list[_DryRunRestoration]) -> None:
        """Restore each Dry Run under one lease deadline; one bot's failure is only its own.

        The dead process's leases all lapse within one lease lifetime of its
        death, so one deadline covers every Dry Run: a lease still held when
        it passes is another live process's. Whatever stops one Dry Run --
        this is the isolation boundary, so it is every exception, including
        building its authority -- is logged with its cause and refuses that
        bot's Start alone.

        Each restoration runs under its bot's own operation lock (#2668), so
        an Archive or Stop of that bot waits for the restoration to settle
        instead of interleaving with it over one account. The lock is
        per-bot: the lane's other bots and the serving path never wait on it.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S
        for restoration in restorations:
            sid = restoration.strategy_instance_id
            try:
                async with self._operation_lock(sid):
                    authority = restoration.authority_for()
                    await authority.ensure_recoverable(lease_wait_s=max(0.0, deadline - loop.time()))
                    interrupted = await restoration.repair()
            except Exception as exc:
                self._dry_run_restorations[sid] = (
                    "account_held" if isinstance(exc, ExecutionLeaseHeld) else "not_restored"
                )
                detail = exc.detail if isinstance(exc, StartAdmissionUnavailable) else None
                logger.error(
                    "A Dry Run's simulated account could not be restored at boot: %s%s",
                    exc,
                    "" if detail is None else f" ({detail})",
                    extra={
                        "action": "boot_dry_run_restoration_failed",
                        "strategy_instance_id": sid,
                        "account_id": f"{SIM_ACCOUNT_PREFIX}{sid}",
                        "error": str(exc),
                        "error_detail": detail,
                        "restoration": self._dry_run_restorations[sid],
                    },
                    exc_info=True,
                )
                continue
            del self._dry_run_restorations[sid]
            logger.info(
                "A Dry Run's simulated account was restored at boot",
                extra={
                    "action": "boot_dry_run_restored",
                    "strategy_instance_id": sid,
                    "account_id": f"{SIM_ACCOUNT_PREFIX}{sid}",
                    "interrupted": list(interrupted),
                },
            )

    async def _stop_interrupted_authority_run(
        self,
        strategy_instance_id: str,
        run_id: str,
    ) -> None:
        """Stop an orphaned run through its binding's exact custody authority."""
        binding = self._read_binding(strategy_instance_id)
        await stop_interrupted_alpaca_duty_run(
            self._artifacts_root,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            binding=binding,
        )

    def _synthetic_runtime_in_use(self, strategy_instance_id: str) -> bool:
        """Keep a deterministic per-instance runtime only while its task owns it."""
        return any(
            managed.binding.strategy_instance_id == strategy_instance_id
            and not managed.task.done()
            for managed in self._bots.values()
        )

    def _is_running(self, strategy_instance_id: str) -> bool:
        managed = self._bots.get(strategy_instance_id)
        return managed is not None and not managed.task.done()

    @property
    def artifacts_root(self) -> Path:
        """The lane's artifact root, under which every bot's evidence lives."""
        return self._artifacts_root

    def any_running(self) -> bool:
        """Whether this registry currently owns any live bot task."""
        return any(not managed.task.done() for managed in self._bots.values())

    def running_mode_counts(self, broker: str) -> Counter[str]:
        """How many live bot tasks ``broker`` has, by binding mode.

        The task table alone -- no binding, lifecycle or desired-state file is
        read -- so the lane can count on every heartbeat. ``running`` here is
        exactly ``list_bots``'s ``running``: the owned task has not exited.
        """
        return Counter(
            managed.binding.mode for managed in self._bots.values()
            if managed.binding.broker == broker and not managed.task.done()
        )

    def _manages_boot_recovery(self, strategy_instance_id: str) -> bool:
        """Admit Alpaca bindings and SQLite-positive pre-binding candidates."""
        if self._supported_broker_ids is None:
            return True
        if "alpaca" not in self._supported_broker_ids:
            return False
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
        return binding is None or binding.broker in self._supported_broker_ids

    def _binding_recovery_candidates(
        self, binding: BrokerBotBinding
    ) -> dict[str, BotRecoveryCandidate]:
        """One binding's run, superseded by any run its own authority still holds active."""
        candidates = {
            binding.strategy_instance_id: BotRecoveryCandidate(
                strategy_instance_id=binding.strategy_instance_id,
                run_id=binding.run_id,
                sqlite_active=False,
            )
        }
        for strategy_instance_id, run_id in self._authority_for(binding).lifecycle_recovery_candidates():
            candidates[binding.strategy_instance_id] = BotRecoveryCandidate(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                sqlite_active=True,
            )
        return candidates

    def _recovery_candidates(self) -> tuple[BotRecoveryCandidate, ...]:
        """The account's own sweep: its bindings and runs, never a Dry Run's (#2582).

        A Dry Run's custody is its own ``sim:`` account, restored and repaired
        by ``start_dry_run_restoration``; the account's sweep -- at boot, after
        a reconnect, after a lease revival -- never waits on or repairs one.
        """
        candidates: dict[str, BotRecoveryCandidate] = {}
        for binding in self._bindings.list_for_broker("alpaca"):
            if binding.mode != "dry_run":
                candidates.update(self._binding_recovery_candidates(binding))
        clerk = get_alpaca_clerk()
        has_sqlite_candidate_capability = callable(
            getattr(clerk, "lifecycle_recovery_candidates", None)
        )
        if (
            getattr(clerk, "authority_kind", None) not in SQLITE_FACADE_AUTHORITIES
            and not has_sqlite_candidate_capability
        ):
            return tuple(candidates.values())
        for strategy_instance_id, run_id in self._lifecycle_authority.recovery_candidates():
            self._alpaca_identity.require(
                strategy_instance_id,
                sqlite_claim=True,
            )
            candidates[strategy_instance_id] = BotRecoveryCandidate(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                sqlite_active=True,
            )
        return tuple(candidates.values())

    def _require_alpaca_identity(
        self,
        strategy_instance_id: str,
        sqlite_claim: bool,
    ) -> None:
        self._alpaca_identity.require(
            strategy_instance_id,
            sqlite_claim=sqlite_claim,
        )

    def _carryover_checkpoint_path(self, strategy_instance_id: str) -> Path:
        return self._confined_instance_dir(strategy_instance_id) / _CARRYOVER_CHECKPOINT_FILENAME

    def _confined_instance_dir(self, strategy_instance_id: str) -> Path:
        try:
            return strategy_instance_artifact_dir(self._artifacts_root, "live_state", strategy_instance_id)
        except ValueError as exc:
            raise InvalidStrategyInstanceIdError(str(exc)) from exc

    def _lifecycle_repo(self, strategy_instance_id: str) -> BotLifecycleStateRepo:
        return BotLifecycleStateRepo(stable_bot_lifecycle_state_path(self._artifacts_root, strategy_instance_id))

    def _desired_repo(self, strategy_instance_id: str) -> DesiredStateRepo:
        return DesiredStateRepo(stable_desired_state_path(self._artifacts_root, strategy_instance_id))

    def desired_state(self, strategy_instance_id: str) -> DesiredState:
        """This instance's durable operator intent (defaults to RUNNING)."""
        return self._desired_repo(strategy_instance_id).read_state()

    def _read_binding(self, strategy_instance_id: str) -> BrokerBotBinding | None:
        return self._bindings.read(strategy_instance_id)

    def _compose_status(self, binding: BrokerBotBinding) -> BotStatusView:
        sid = binding.strategy_instance_id
        lifecycle = self._lifecycle_repo(sid).read()
        desired = self._desired_repo(sid).read_state()
        managed = self._bots.get(sid)
        return project_bot_status(
            binding,
            lifecycle,
            desired,
            running=managed is not None and not managed.task.done(),
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
