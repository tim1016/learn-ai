"""Operator-surface projection (PRD #607, extended by PRD #616).

Pure-function projection of run state into the cockpit-facing
``OperatorSurface`` model.  Single source of truth for operational
verdicts, risk posture, structured daily-cap usage, action-plan
consumption, broker safety verdict + connection, prior-run
classification, host-process state, trading-session phase, per-action
capability + reason codes, and the per-gate operator-facing
remediation metadata (``OperatorGate``).

Frontend renders these fields; it does not derive verdicts from raw
status fields.  See ``docs/runbooks/broker-instance-operator-surface.md``
and ``docs/architecture/adrs/0013-operator-surface-judgment-vs-evidence.md``
for the operator-surface inclusion boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time
from typing import Literal, assert_never
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.engine.live.bot_lifecycle_state import BotLifecyclePhase
from app.engine.live.daemon_connectivity_monitor import DaemonConnectivityState
from app.engine.live.daemon_transport import DaemonResultKind
from app.engine.live.exit_taxonomy import RunExitEvidence, classify_run_exit
from app.lean_sidecar.trading_calendar import next_trading_day, session_window_for_date
from app.operator.notices.broker_activity_health import compose_broker_activity_health
from app.operator.notices.runtime_freshness import compose_runtime_freshness_notices
from app.operator.notices.schema import OperatorNotice
from app.schemas.live_runs import (
    ActionCapability,
    BrokerActivityHealth,
    BrokerObservationConsistency,
    DesiredStateView,
    ExposureCoherenceFacts,
    FocusAction,
    GateResult,
    HostProcessStartCapability,
    HostProcessStartDisabledReasonCode,
    HostRunnerStartRequest,
    InstanceBrokerView,
    InstanceLastExit,
    InstanceProcessView,
    InstanceSizing,
    InstanceStartDefaults,
    InvokeCapabilityAction,
    LiveBinding,
    OpenRunbookAction,
    OperatorGate,
    OperatorSurface,
    OperatorSurfaceAccountOwner,
    OperatorSurfaceActionPlan,
    OperatorSurfaceActions,
    OperatorSurfaceBlockageLadder,
    OperatorSurfaceConfiguration,
    OperatorSurfaceControlPlane,
    OperatorSurfaceCurrentRisk,
    OperatorSurfaceDailyOrderCap,
    OperatorSurfaceDomainFreshness,
    OperatorSurfaceExecution,
    OperatorSurfaceHostProcess,
    OperatorSurfaceNoticePlacement,
    OperatorSurfacePriorRun,
    OperatorSurfaceReconciliation,
    OperatorSurfaceRunSignal,
    OperatorSurfaceRuntimeFreshness,
    OperatorSurfaceTradingSession,
    ReadinessGate,
    ReadinessVector,
    ReconciliationReceipt,
    ReconciliationState,
    RedeployAction,
)
from app.schemas.operator_blocker import (
    OperatorBlocker,
    OperatorMove,
    RemoveAction,
    RetireReplaceAction,
)
from app.services.account_truth_snapshot import AccountTruthReadinessEvidence, assess_account_truth
from app.services.broker_activity_publisher import BrokerActivityPublisher
from app.services.mutation_attempt import MutationAttempt
from app.services.operator_blockage_ladder import author_blockage_ladder
from app.services.operator_broker_projection import BrokerConnectionStateInput, project_broker
from app.services.operator_capability import evaluate_all_actions
from app.services.operator_trader_guidance import (
    IBKR_CLIENT_ID_IN_USE,
    author_submit_readiness,
    author_trader_guidance,
)
from app.services.resume_guard_state import ResumeGuardState, empty_guard_state
from app.services.runtime_freshness import (
    DomainFreshness,
    EngineEffectivePosture,
    RuntimeFreshness,
    runtime_freshness_reason_codes,
)

TraderExecutionPosture = Literal["PAPER_EXECUTION", "READ_ONLY", "UNSAFE", "UNKNOWN"]

_NOTICE_TIER_ORDER = {"critical": 0, "warning": 1, "info": 2}
_NOTICE_STAGE_ORDER = {
    "control_plane": 0,
    "host_process": 1,
    "broker": 2,
    "account_safety": 3,
    "account_owner": 4,
    "reconciliation": 5,
    "preflight": 6,
    "trading_session": 7,
    "runtime_freshness": 8,
}


# ---------------------------------------------------------------------------
# host_process
# ---------------------------------------------------------------------------

# Daemon ``process.state`` (``running | stopping | exited | idle |
# unreachable``) -> operator-facing ``host_process.state`` enum.  ``idle``
# means the daemon answered but is tracking no subprocess for this
# instance; it stays ``IDLE`` unless the operator has indicated
# durable intent ``RUNNING``, in which case the projection upgrades it
# to ``WAITING_FOR_HOST`` (computed below).
_DAEMON_STATE_TO_HOST_PROCESS_STATE: dict[str, str] = {
    "running": "RUNNING",
    "stopping": "STOPPING",
    "exited": "EXITED",
    "idle": "IDLE",
    "unreachable": "UNREACHABLE",
}

_HOST_PROCESS_NOTICE_BY_STATE: dict[str, str] = {
    "STOPPING": "The bot is shutting down.",
    "EXITED": "The previous bot process ended. Run roll call for a fresh start offer.",
    "IDLE": "The host is reachable but this bot has no active process. Run roll call for a fresh start offer.",
    "WAITING_FOR_HOST": (
        "Trading was requested, but this bot's process has not started yet. Start it to begin trading."
    ),
    "UNREACHABLE": ("The bot service is offline. The cockpit cannot confirm any process state until it is reachable."),
}


_START_CAPABLE_STATES = frozenset({"IDLE", "WAITING_FOR_HOST", "EXITED"})


def _replace_move() -> OperatorMove:
    return OperatorMove(
        label="Replace",
        action=RetireReplaceAction(kind="retire_replace"),
    )


def _remove_move() -> OperatorMove:
    return OperatorMove(
        label="Remove",
        action=RemoveAction(kind="remove"),
    )


def _author_operator_blockers(
    *,
    poisoned: bool,
    bot_lifecycle_phase: BotLifecyclePhase | None,
) -> list[OperatorBlocker]:
    if bot_lifecycle_phase == BotLifecyclePhase.RETIRED:
        return [
            OperatorBlocker(
                id="retired",
                severity="blocking",
                disposition="terminal",
                headline="Can't recover",
                detail="This bot has been retired. Remove it from the catalog or replace it with a fresh deploy.",
                primary_move=_remove_move(),
                secondary_moves=[_replace_move()],
                applies_to="run",
            )
        ]
    if poisoned:
        return [
            OperatorBlocker(
                id="run_poisoned",
                severity="blocking",
                disposition="terminal",
                headline="Can't recover",
                detail="This run is poisoned and cannot be restarted safely. Replace it or remove the bot.",
                primary_move=_replace_move(),
                secondary_moves=[_remove_move()],
                applies_to="run",
            )
        ]
    return []


def _project_host_start_capability(
    state: str,
    desired_state: DesiredStateView | None,
    start_run_id: str | None,
    start_defaults: InstanceStartDefaults | None,
    poisoned: bool,
    now_ms: int,
    account_freeze: AccountFreezeEvidence | None = None,
    crash_recovery_gate: GateResult | None = None,
) -> HostProcessStartCapability:
    """Project the per-instance Start-bot-process affordance.

    The data-plane proxy MUST re-evaluate the same enable rule before
    forwarding the POST to the daemon — this projection is the cockpit's
    presentation hint, not the gate. Reason codes are closed; the priority
    order below is exhaustive for the 5 host-process states.
    """

    reason: HostProcessStartDisabledReasonCode | None = None
    if account_freeze is not None:
        return HostProcessStartCapability(
            enabled=False,
            disabled_reason_code="ACCOUNT_FROZEN",
            gate_results=[account_freeze.to_gate_result()],
        )
    if crash_recovery_gate is not None:
        return HostProcessStartCapability(
            enabled=False,
            disabled_reason_code="CRASH_RECOVERY_REQUIRED",
            gate_results=[crash_recovery_gate],
        )
    # Poisoned runs are dead and still require redeploy. Durable STOPPED no
    # longer blocks starts; the duty roster is the operator-owned "stay down"
    # control in the daily lifecycle model.
    if poisoned:
        reason = "STOPPED_REQUIRES_REDEPLOY"
    elif state == "RUNNING":
        reason = "ALREADY_RUNNING"
    elif state == "STOPPING":
        reason = "STOPPING"
    elif state == "UNREACHABLE":
        reason = "HOST_SERVICE_OFFLINE"
    elif state not in _START_CAPABLE_STATES:
        # Defensive: any future state we don't classify is off by default.
        reason = "START_SETTINGS_INCOMPLETE"
    elif not start_run_id or start_defaults is None or not start_defaults.strategy:
        reason = "START_SETTINGS_INCOMPLETE"

    def _start_gate(
        *,
        status: str,
        operator_reason: str,
        operator_next_step: str | None,
    ) -> GateResult:
        return GateResult(
            gate_id="start.daemon_state",
            status=status,  # type: ignore[arg-type]
            source="operator_surface",
            operator_reason=operator_reason,
            operator_next_step=operator_next_step,
            evidence_at_ms=now_ms,
        )

    if reason is not None:
        return HostProcessStartCapability(
            enabled=False,
            disabled_reason_code=reason,
            gate_results=[
                _start_gate(
                    status="block",
                    operator_reason=reason,
                    operator_next_step=reason,
                )
            ],
        )

    assert start_run_id is not None and start_defaults is not None  # narrowed above
    # Fail closed if the saved start settings cannot build a valid request
    # body (strategy pattern, max_orders_per_day range, ibkr_host length).
    # Otherwise a constraint violation would raise ValidationError and 500
    # the operator-surface projection — same outcome the operator sees from
    # the "settings incomplete" branch above, so route it the same way.
    try:
        request = HostRunnerStartRequest(
            readonly=start_defaults.readonly,
            hydrate_policy=start_defaults.hydrate_policy,
            strategy=start_defaults.strategy,
            max_orders_per_day=start_defaults.max_orders_per_day,
            ibkr_host=start_defaults.ibkr_host,
        )
    except ValidationError:
        return HostProcessStartCapability(
            enabled=False,
            disabled_reason_code="START_SETTINGS_INCOMPLETE",
            gate_results=[
                _start_gate(
                    status="block",
                    operator_reason="START_SETTINGS_INCOMPLETE",
                    operator_next_step="START_SETTINGS_INCOMPLETE",
                )
            ],
        )
    return HostProcessStartCapability(
        enabled=True,
        run_id=start_run_id,
        request=request,
        gate_results=[
            _start_gate(
                status="pass",
                operator_reason="Start settings complete and daemon state is startable.",
                operator_next_step=None,
            )
        ],
    )


def _project_host_process(
    process: InstanceProcessView,
    desired_state: DesiredStateView | None,
    last_exit: InstanceLastExit | None = None,
    start_run_id: str | None = None,
    start_defaults: InstanceStartDefaults | None = None,
    poisoned: bool = False,
    host_start_command: str | None = None,
    now_ms: int = 0,
    account_freeze: AccountFreezeEvidence | None = None,
    crash_recovery_gate: GateResult | None = None,
) -> OperatorSurfaceHostProcess:
    state = _DAEMON_STATE_TO_HOST_PROCESS_STATE.get(process.state, "UNREACHABLE")
    # WAITING_FOR_HOST: daemon reachable + no tracked subprocess + the
    # operator has set durable intent to RUNNING.
    if state == "IDLE" and desired_state is not None and desired_state.state == "RUNNING":
        state = "WAITING_FOR_HOST"
    last_exit_error_code = last_exit.exit_error_code if last_exit is not None else None
    last_exit_error_message = last_exit.exit_error_message if last_exit is not None else None
    last_exit_error_detail = last_exit.exit_error_detail if last_exit is not None else {}
    if state != "RUNNING" and last_exit_error_code == IBKR_CLIENT_ID_IN_USE:
        notice = last_exit_error_message or "IBKR rejected startup because the requested client ID is already in use."
    else:
        notice = None if state == "RUNNING" else _HOST_PROCESS_NOTICE_BY_STATE.get(state)
    # ``copyable_command`` is authored ONLY for UNREACHABLE, and only when
    # trusted deployment configuration supplies a non-empty command. The
    # EXITED / IDLE / WAITING_FOR_HOST cases use ``start_capability``
    # below — restarting the host daemon does NOT restart an exited
    # per-bot subprocess.
    copyable_command = host_start_command if state == "UNREACHABLE" and host_start_command else None
    start_capability = _project_host_start_capability(
        state,
        desired_state,
        start_run_id,
        start_defaults,
        poisoned,
        now_ms,
        account_freeze,
        crash_recovery_gate,
    )
    return OperatorSurfaceHostProcess(
        state=state,
        notice=notice,
        copyable_command=copyable_command,
        last_exit_error_code=last_exit_error_code,
        last_exit_error_message=last_exit_error_message,
        last_exit_error_detail=last_exit_error_detail,
        start_capability=start_capability,
    )


def _project_run_signal(
    host_process: OperatorSurfaceHostProcess,
    blockage_ladder: OperatorSurfaceBlockageLadder,
) -> OperatorSurfaceRunSignal:
    host_stage = next(
        (stage for stage in blockage_ladder.stages if stage.id == "host_process"),
        None,
    )
    title = host_stage.title if host_stage is not None else "Host process"
    detail = (
        host_stage.summary if host_stage is not None else host_process.notice or "Host-process status is unavailable."
    )
    match host_process.state:
        case "RUNNING":
            state_label = "On"
            tone = "on"
        case "STOPPING":
            state_label = "Stopping"
            tone = "transition"
        case "UNREACHABLE":
            state_label = "Needs proof"
            tone = "attention"
        case "EXITED" | "IDLE" | "WAITING_FOR_HOST":
            state_label = "Off"
            tone = "off"
        case _:
            assert_never(host_process.state)
    return OperatorSurfaceRunSignal(
        state_label=state_label,
        tone=tone,
        title=title,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# prior_run
# ---------------------------------------------------------------------------


def _project_prior_run(last_exit: InstanceLastExit | None) -> OperatorSurfacePriorRun:
    if last_exit is None:
        return OperatorSurfacePriorRun(classification="UNKNOWN")
    verdict = classify_run_exit(
        RunExitEvidence(
            status_present=True,
            exit_code=last_exit.exit_code,
            exit_reason=last_exit.exit_reason,
        ),
        returncode=last_exit.exit_code,
        stopping=False,
    )
    if last_exit.halt_trigger is not None or verdict.category == "halted":
        return OperatorSurfacePriorRun(classification="HALT_TRIGGERED")
    if last_exit.exit_code == 0 or last_exit.exit_reason == "normal":
        return OperatorSurfacePriorRun(classification="CLEAN")
    if last_exit.exit_code is not None and last_exit.exit_code != 0:
        return OperatorSurfacePriorRun(classification="EXITED_WITH_ERROR")
    return OperatorSurfacePriorRun(classification="UNKNOWN")


# ---------------------------------------------------------------------------
# execution — authored translation of engine effective_posture
# ---------------------------------------------------------------------------


def _project_execution(runtime_freshness: RuntimeFreshness | None) -> OperatorSurfaceExecution | None:
    """Translate engine posture into the trader-facing execution chip.

    Slice 2 decision: engine ``UNSAFE`` stays visible as trader
    ``UNSAFE`` instead of being collapsed into ``UNKNOWN``. Known danger
    should not look like missing evidence.
    """
    if runtime_freshness is None:
        return None
    return OperatorSurfaceExecution(
        posture=_trader_execution_posture(runtime_freshness.effective_posture),
    )


def _trader_execution_posture(posture: EngineEffectivePosture) -> TraderExecutionPosture:
    match posture:
        case "PAPER_EXECUTION":
            return "PAPER_EXECUTION"
        case "PAPER_OBSERVATION":
            return "READ_ONLY"
        case "UNSAFE":
            return "UNSAFE"
        case "UNKNOWN":
            return "UNKNOWN"
        case _:
            raise AssertionError(f"Unhandled engine effective posture: {posture}")


# ---------------------------------------------------------------------------
# current_risk
# ---------------------------------------------------------------------------


def normalize_exposure_positions(owned_positions: Mapping[str, object]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for symbol, quantity in owned_positions.items():
        name = str(symbol).strip().upper()
        if not name:
            continue
        qty = int(quantity)
        if qty != 0:
            normalized[name] = normalized.get(name, 0) + qty
    return {symbol: qty for symbol, qty in sorted(normalized.items()) if qty != 0}


def _derive_posture(owned_positions: Mapping[str, int]) -> str:
    non_zero = {sym: qty for sym, qty in owned_positions.items() if qty != 0}
    if not non_zero:
        return "FLAT"
    sides = {("LONG" if qty > 0 else "SHORT") for qty in non_zero.values()}
    if len(sides) > 1:
        return "MIXED"
    return next(iter(sides))


def compose_exposure_coherence_facts(
    broker: InstanceBrokerView | None,
    *,
    source: str,
    strategy_instance_id: str | None = None,
    run_id: str | None = None,
) -> ExposureCoherenceFacts:
    if broker is None:
        return ExposureCoherenceFacts(
            posture="UNKNOWN",
            pending_order_count=None,
            owned_positions={},
            source=source,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
        )
    owned_positions = normalize_exposure_positions(broker.owned_positions)
    return ExposureCoherenceFacts(
        posture=_derive_posture(owned_positions),  # type: ignore[arg-type]
        pending_order_count=broker.pending_order_count,
        owned_positions=owned_positions,
        source=source,
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
    )


def _project_current_risk(broker: InstanceBrokerView | None) -> OperatorSurfaceCurrentRisk:
    facts = compose_exposure_coherence_facts(
        broker,
        source="live_state.expected_position_by_symbol",
    )
    posture = facts.posture
    pending = facts.pending_order_count
    verdict = "UNKNOWN" if broker is None else ("READY" if posture == "FLAT" and pending == 0 else "ATTENTION")
    return OperatorSurfaceCurrentRisk(
        posture=posture,
        owned_positions=facts.owned_positions,
        pending_order_count=pending,
        verdict=verdict,
        unrealized_pnl=broker.unrealized_pnl if broker is not None else None,
    )


# ---------------------------------------------------------------------------
# daily_order_cap
# ---------------------------------------------------------------------------


def _project_daily_order_cap(readiness: ReadinessVector | None) -> OperatorSurfaceDailyOrderCap:
    if readiness is None:
        return OperatorSurfaceDailyOrderCap(used=None, limit=None)
    return OperatorSurfaceDailyOrderCap(used=readiness.orders_used, limit=readiness.orders_cap)


# ---------------------------------------------------------------------------
# action_plan
# ---------------------------------------------------------------------------


def _project_action_plan(
    action_plan: dict | None,
    start_defaults: InstanceStartDefaults | None,
) -> OperatorSurfaceActionPlan:
    if action_plan is None:
        return OperatorSurfaceActionPlan(consumption="UNKNOWN", anomaly_verdict="UNKNOWN")
    from app.engine.live.config import stock_symbol_from_action_plan

    consuming_strategy = start_defaults is not None and start_defaults.strategy == "deployment_validation"
    consumption = (
        "ACTIVE"
        if consuming_strategy and stock_symbol_from_action_plan(action_plan) is not None
        else "DECLARATIVE_ONLY"
    )
    return OperatorSurfaceActionPlan(consumption=consumption, anomaly_verdict="READY")


def _action_gate_result(
    action_name: str,
    capability: ActionCapability,
    *,
    now_ms: int,
) -> GateResult:
    reason = capability.disabled_reason_code or "ACTION_ENABLED"
    return GateResult(
        gate_id=f"action.{action_name}",
        status="pass" if capability.enabled else "block",
        source="operator_surface",
        operator_reason=reason,
        operator_next_step=None if capability.enabled else reason,
        evidence_at_ms=now_ms,
    )


def _attach_action_gate_results(
    actions: OperatorSurfaceActions,
    *,
    now_ms: int,
    account_freeze: AccountFreezeEvidence | None = None,
) -> OperatorSurfaceActions:
    resume = actions.resume
    if account_freeze is not None:
        resume = resume.model_copy(
            update={
                "enabled": False,
                "disabled_reason_code": "ACCOUNT_FROZEN",
                "disabled_reasons": ["ACCOUNT_FROZEN"],
                "gate_results": [account_freeze.to_gate_result()],
            }
        )
    else:
        resume = resume.model_copy(
            update={"gate_results": [_action_gate_result("resume", actions.resume, now_ms=now_ms)]}
        )
    return OperatorSurfaceActions(
        resume=resume,
        pause=actions.pause.model_copy(
            update={"gate_results": [_action_gate_result("pause", actions.pause, now_ms=now_ms)]}
        ),
        stop=actions.stop.model_copy(
            update={"gate_results": [_action_gate_result("stop", actions.stop, now_ms=now_ms)]}
        ),
        flatten_and_pause=actions.flatten_and_pause.model_copy(
            update={
                "gate_results": [
                    _action_gate_result(
                        "flatten_and_pause",
                        actions.flatten_and_pause,
                        now_ms=now_ms,
                    )
                ]
            }
        ),
        mark_poisoned=actions.mark_poisoned.model_copy(
            update={
                "gate_results": [
                    _action_gate_result(
                        "mark_poisoned",
                        actions.mark_poisoned,
                        now_ms=now_ms,
                    )
                ]
            }
        ),
    )


# ---------------------------------------------------------------------------
# configuration rules
# ---------------------------------------------------------------------------


def _project_configuration(
    start_defaults: InstanceStartDefaults | None,
    sizing: InstanceSizing | None,
    instance_broker_self_consistent: bool | None,
) -> OperatorSurfaceConfiguration:
    if start_defaults is None and sizing is None and instance_broker_self_consistent is None:
        return OperatorSurfaceConfiguration(verdict="UNKNOWN", reason_codes=[])

    reasons: list[str] = []

    if start_defaults is None or not (start_defaults.strategy or "").strip():
        reasons.append("STRATEGY_KEY_MISSING")

    if start_defaults is None or start_defaults.max_orders_per_day is None or start_defaults.max_orders_per_day <= 0:
        reasons.append("MAX_ORDERS_CAP_UNSET")

    if sizing is None or sizing.policy is None:
        reasons.append("SIZING_PRESET_MISSING")

    if sizing is None or not getattr(sizing, "sizing_provenance", None):
        reasons.append("SIZING_PROVENANCE_MISSING")

    if instance_broker_self_consistent is False:
        reasons.append("INSTANCE_BROKER_SELF_INCONSISTENT")

    verdict = "ATTENTION" if reasons else "READY"
    return OperatorSurfaceConfiguration(verdict=verdict, reason_codes=reasons)


# ---------------------------------------------------------------------------
# trading_session — server-authored phase + permission + next-transition
# ---------------------------------------------------------------------------

_NY = ZoneInfo("America/New_York")

_PRE_OPEN = time(4, 0)
_POST_CLOSE = time(20, 0)


def _ny_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).astimezone(_NY)


def _at(ny_day: datetime, t: time) -> datetime:
    return datetime.combine(ny_day.date(), t, tzinfo=_NY)


def _ms_utc(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp() * 1000)


def _next_session_pre_open(now_ny: datetime) -> int:
    """Return the next NYSE session's 04:00 NY pre-open as int64 ms UTC."""
    candidate = next_trading_day(now_ny.date())
    target = datetime.combine(candidate, _PRE_OPEN, tzinfo=_NY)
    return _ms_utc(target)


def _project_trading_session(
    *,
    now_ms: int,
    strategy_session_policy: Literal["rth_only"] | None = None,
) -> OperatorSurfaceTradingSession:
    """Compute the trading-session phase + the *next* boundary transition.

    PRD #616 — ``next_transition_ms`` was hard-coded ``None``.  We now
    compute the next boundary in America/New_York accounting for
    weekday/weekend and the default RTH-only policy.  Per-strategy
    session policies (extended hours, RTH+POST, ...) are a future
    field on the wire; until then the policy defaults to RTH-only.
    """
    now_ny = _ny_dt(now_ms)

    phase: str
    permits: bool | None
    next_transition_ms: int | None

    try:
        session_window = session_window_for_date(now_ny.date())
    except LookupError:
        phase = "CLOSED"
        permits = False
        next_transition_ms = _next_session_pre_open(now_ny)
    else:
        session_open_ny = _ny_dt(session_window.open_ms_utc)
        session_close_ny = _ny_dt(session_window.close_ms_utc)
        pre_open_ny = _at(now_ny, _PRE_OPEN)
        post_close_ny = _at(now_ny, _POST_CLOSE)

        if now_ny < pre_open_ny:
            phase = "CLOSED"
            permits = False
            next_transition_ms = _ms_utc(pre_open_ny)
        elif now_ny < session_open_ny:
            phase = "PRE"
            permits = False
            next_transition_ms = session_window.open_ms_utc
        elif now_ny < session_close_ny:
            phase = "RTH"
            permits = True
            next_transition_ms = session_window.close_ms_utc
        elif now_ny < post_close_ny:
            phase = "POST"
            permits = False
            next_transition_ms = _ms_utc(post_close_ny)
        else:
            # After 20:00 ET on a session day → CLOSED until the next session's 04:00 ET.
            phase = "CLOSED"
            permits = False
            next_transition_ms = _next_session_pre_open(now_ny)

    if strategy_session_policy is None:
        # Default policy = RTH-only.
        permits = phase == "RTH"

    return OperatorSurfaceTradingSession(
        phase=phase,  # type: ignore[arg-type]
        permits_strategy_activity=permits,
        next_transition_ms=next_transition_ms,
        timezone="America/New_York",
        as_of_ms=now_ms,
    )


# ---------------------------------------------------------------------------
# readiness_gates — server-authored remediation metadata (PRD #616)
# ---------------------------------------------------------------------------

# Per-gate suggested-action authoring rules.  Authoring is data-driven
# (a small table here) rather than scattered "if gate.name == X" branches
# so that adding a new gate-fix mapping is a one-line edit.  Each entry
# is a callable that receives the gate and returns either a
# ``GateSuggestedAction`` or ``(None, unavailable_reason)``.

_INVOKE_RESUME = InvokeCapabilityAction(kind="invoke_capability", capability="resume")
_FOCUS_FLATTEN = FocusAction(kind="focus_action", tab="status", action="flatten_and_pause")
_FOCUS_MARK_POISONED = FocusAction(kind="focus_action", tab="audit", action="mark_poisoned")
_REDEPLOY = RedeployAction(kind="redeploy")
_OPEN_BROKER_RECONNECT_RUNBOOK = OpenRunbookAction(kind="open_runbook", slug="broker-reconnect")


# PRD #619-A §A6 — replace the inlined if/elif chain in ``_action_for_gate``
# with one backend-authored table mapping ``gate.name`` →
# (suggested_action, unavailable_reason). Exactly one of the two
# components is non-None per row. The cockpit consumes the table's
# output verbatim; gate-routing decisions are not derived in two
# places.
#
# ``broker_connection`` routes to a runbook (``broker-reconnect``)
# instead of Redeploy: reconnecting the IBKR session is an
# out-of-band task that Redeploy does not perform. The previous
# Redeploy entry was annotated as a placeholder; PRD #619-A makes it
# correct.
_GATE_ACTION_TABLE: dict[str, tuple[object | None, str | None]] = {
    "broker_connection": (_OPEN_BROKER_RECONNECT_RUNBOOK, None),
    "poison_sentinel": (_REDEPLOY, None),
    "fleet_contamination": (None, "REQUIRES_OUT_OF_BAND_RESOLUTION"),
    "daily_order_cap": (None, "NO_INLINE_REMEDIATION"),
    "warmup": (None, "WAIT_FOR_CONDITION"),
    "calendar": (None, "WAIT_FOR_CONDITION"),
    "session": (None, "WAIT_FOR_CONDITION"),
    "instrument_surface": (None, "WAIT_FOR_CONDITION"),
    "indicator_state_hydration": (_REDEPLOY, None),
    "spec_signature": (_REDEPLOY, None),
    "intent_wal_clean": (_FOCUS_MARK_POISONED, None),
    "positions_self_consistent": (_FOCUS_FLATTEN, None),
    "halt_clear": (_INVOKE_RESUME, None),
}


def _action_for_gate(
    gate: ReadinessGate,
) -> tuple[object | None, str | None]:
    """Return ``(suggested_action, unavailable_reason)`` from the
    backend-authored ``_GATE_ACTION_TABLE``.

    Either ``suggested_action`` is a ``GateSuggestedAction`` instance
    and ``unavailable_reason`` is ``None``, or ``suggested_action`` is
    ``None`` and ``unavailable_reason`` is a stable ALL_CAPS_SNAKE
    code documenting *why* no action is authored.

    Passing gates short-circuit to ``GATE_PASSING``; unknown gate
    names surface ``UNKNOWN_GATE_NAME`` rather than silently dropping
    the suggestion.
    """
    if gate.status == "pass":
        return None, "GATE_PASSING"
    return _GATE_ACTION_TABLE.get(gate.name, (None, "UNKNOWN_GATE_NAME"))


def _gate_result_status(status: str) -> str:
    if status == "pass":
        return "pass"
    if status == "fail":
        return "block"
    if status == "unknown":
        return "unknown"
    return "unknown"


def project_readiness_gates(readiness: ReadinessVector | None) -> list[OperatorGate]:
    """Project engine readiness gates into operator-facing gates with
    structured remediation metadata.

    PRD #616 — the engine's ``ReadinessGate`` carries ``name`` /
    ``status`` / ``severity`` / ``detail``.  ``OperatorGate`` adds
    ``suggested_action`` / ``suggested_action_unavailable_reason`` so
    the cockpit never infers a "fix" from the gate name.

    The ordering preserves the engine's gate order.  An ``UNKNOWN``
    status surfaces as a non-passing gate (still gets a suggested
    action or an unavailable-reason).
    """
    if readiness is None:
        return []
    out: list[OperatorGate] = []
    for gate in readiness.gates:
        action, unavailable = _action_for_gate(gate)
        next_step = getattr(action, "kind", None) if action is not None else unavailable
        gate_result = gate.gate_result or GateResult(
            gate_id=gate.name,
            status=_gate_result_status(gate.status),  # type: ignore[arg-type]
            source=readiness.source,
            operator_reason=gate.detail,
            operator_next_step=next_step,
            evidence_at_ms=readiness.as_of_ms,
        )
        out.append(
            OperatorGate(
                name=gate.name,
                status=gate.status,
                severity=gate.severity,
                detail=gate.detail,
                gate_result=gate_result,
                suggested_action=action,  # type: ignore[arg-type]
                suggested_action_unavailable_reason=unavailable,
            )
        )
    return out


def _project_domain_freshness(
    domain: DomainFreshness,
) -> OperatorSurfaceDomainFreshness:
    return OperatorSurfaceDomainFreshness(
        state=domain.state,
        age_ms=domain.age_ms,
        stale_reason_codes=domain.stale_reason_codes,
    )


def _project_runtime_freshness(
    freshness: RuntimeFreshness | None,
) -> OperatorSurfaceRuntimeFreshness | None:
    if freshness is None:
        return None
    headline, additional_reasons = compose_runtime_freshness_notices(freshness)
    return OperatorSurfaceRuntimeFreshness(
        posture_demoted=freshness.posture_demoted,
        stale_reason_codes=runtime_freshness_reason_codes(freshness),
        command_loop=_project_domain_freshness(freshness.command_loop),
        broker=_project_domain_freshness(freshness.broker),
        bar_loop=_project_domain_freshness(freshness.bar_loop),
        control_plane=_project_domain_freshness(freshness.control_plane),
        headline=headline,
        additional_reasons=additional_reasons,
    )


# Operator-language notices keyed by ``DaemonResultKind``. The table is
# backend-authored; Angular renders the strings verbatim and MUST NOT
# compose them from the enum.
_CONTROL_PLANE_NOTICE_TABLE: dict[DaemonResultKind, tuple[str | None, str | None]] = {
    "CONNECTED": (None, None),
    "RETRYING": (
        "Host daemon connectivity is degraded; the data plane is retrying. "
        "Cockpit values may lag until the daemon responds.",
        "daemon-retrying",
    ),
    "UNREACHABLE": (
        "Host daemon is unreachable. Verify the launcher process is running and that the daemon URL is correct.",
        "daemon-unreachable",
    ),
    "AUTH_FAILED": (
        "Host daemon rejected the data plane's credentials. Rotate or refresh "
        "the daemon token and restart the data plane.",
        "daemon-auth-failed",
    ),
    "PROTOCOL_ERROR": (
        "Host daemon returned a malformed or error response. It may be mid-restart — check the daemon logs.",
        "daemon-protocol-error",
    ),
    "INCOMPATIBLE_CONTRACT": (
        "Host daemon API is incompatible with this data-plane build. Rebuild "
        "and redeploy the daemon to match the data-plane release.",
        "daemon-incompatible-contract",
    ),
}


def _project_control_plane(
    state: DaemonConnectivityState | None,
) -> OperatorSurfaceControlPlane | None:
    """Project the connectivity monitor's folded state into the operator
    surface.

    ``None`` ↦ ``None`` (the data plane booted without a daemon URL — the
    cockpit hides the control-plane card). When the monitor exists but
    has not yet observed any probe, ``state.kind`` is ``RETRYING`` with
    ``attempt=0`` — the operator surface shows that exactly.
    """
    if state is None:
        return None
    notice, runbook_slug = _CONTROL_PLANE_NOTICE_TABLE.get(state.kind, (None, None))
    return OperatorSurfaceControlPlane(
        state=state.kind,
        last_transition_ms=state.last_transition_ms,
        last_success_ms=state.last_success_ms,
        attempt=state.attempt,
        daemon_boot_id=state.observed_daemon_boot_id,
        notice=notice,
        runbook_slug=runbook_slug,
    )


# ---------------------------------------------------------------------------
# cold-start reconciliation (ADR-0008 §5 / PR 1)
# ---------------------------------------------------------------------------


def _project_reconciliation(
    receipt: ReconciliationReceipt | None,
    *,
    current_wal_seq: int | None,
    current_run_id: str | None,
    current_namespace: str | None,
    latest_broker_event_ms: int | None,
    latest_mutation_ms: int | None,
    ttl_ms: int | None,
    now_ms: int,
) -> OperatorSurfaceReconciliation:
    """Compose the cockpit-facing reconciliation state from the receipt +
    freshness inputs (ADR-0008 §5 / PR 1).

    Decision order (first match wins):
      * receipt missing                                 → NOT_AVAILABLE
      * receipt.status == in_progress                   → IN_PROGRESS
      * receipt.status == failed                        → FAILED + reason
      * receipt.status == passed AND any of the
        following — WAL advanced past receipt.sidecar_wal_seq,
        run_id changed, namespace changed, broker event or
        operator mutation after broker_observed_at_ms, or
        ``ttl_ms`` exceeded — → STALE
      * receipt.status == passed otherwise              → CLEAN | ADOPTED
        (per receipt.outcome; ``adopted_intent_ids`` surfaced)
    """
    if receipt is None:
        return OperatorSurfaceReconciliation(state="NOT_AVAILABLE")
    if receipt.status == "in_progress":
        return _reconciliation_from_receipt(receipt, state="IN_PROGRESS")
    if receipt.status == "failed":
        return _reconciliation_from_receipt(receipt, state="FAILED", failure_reason=receipt.failure_reason)
    # status == "passed" from here on. Apply staleness rules.
    if current_wal_seq is not None and current_wal_seq > receipt.sidecar_wal_seq:
        return _reconciliation_from_receipt(receipt, state="STALE")
    if current_run_id is not None and current_run_id != receipt.run_id:
        return _reconciliation_from_receipt(receipt, state="STALE")
    if current_namespace is not None and current_namespace != receipt.namespace:
        return _reconciliation_from_receipt(receipt, state="STALE")
    observed = receipt.broker_observed_at_ms
    if observed is not None and latest_broker_event_ms is not None and latest_broker_event_ms > observed:
        return _reconciliation_from_receipt(receipt, state="STALE")
    if observed is not None and latest_mutation_ms is not None and latest_mutation_ms > observed:
        return _reconciliation_from_receipt(receipt, state="STALE")
    if ttl_ms is not None and receipt.last_reconcile_ms is not None and (now_ms - receipt.last_reconcile_ms) > ttl_ms:
        return _reconciliation_from_receipt(receipt, state="STALE")
    # Fresh passed receipt — distinguish clean vs adopted.
    state = "ADOPTED" if receipt.outcome == "adopted" else "CLEAN"
    return _reconciliation_from_receipt(
        receipt,
        state=state,
        adopted_intent_ids=receipt.adopted_intent_ids,
    )


def _reconciliation_from_receipt(
    receipt: ReconciliationReceipt,
    *,
    state: ReconciliationState,
    failure_reason: str | None = None,
    adopted_intent_ids: tuple[str, ...] | None = None,
) -> OperatorSurfaceReconciliation:
    return OperatorSurfaceReconciliation(
        state=state,
        failure_reason=failure_reason,
        adopted_intent_ids=adopted_intent_ids if adopted_intent_ids is not None else receipt.adopted_intent_ids,
        last_reconcile_ms=receipt.last_reconcile_ms,
        sidecar_wal_seq=receipt.sidecar_wal_seq,
        broker_observed_at_ms=receipt.broker_observed_at_ms,
    )


def _notice_stage_id(notice: OperatorNotice) -> str:
    code = str(notice.code)
    if code.startswith("runtime."):
        if code.startswith("runtime.control_plane_"):
            return "control_plane"
        if code in {"runtime.broker_probe_stale", "runtime.broker_probe_missing"}:
            return "broker"
        if code.startswith("runtime.market_"):
            return "runtime_freshness"
        return "host_process"
    if code.startswith(("broker_session.", "activity.")):
        return "broker"
    if code.startswith(("watchdog.", "reconciliation.")):
        return "reconciliation"
    if code.startswith(("order.", "submit.", "safety_halt.")):
        return "preflight"
    if code.startswith("fleet."):
        return "account_safety"
    return "runtime_freshness"


def _notice_sort_key(notice: OperatorNotice) -> tuple[int, int, str]:
    return (
        _NOTICE_TIER_ORDER[notice.tier],
        _NOTICE_STAGE_ORDER[_notice_stage_id(notice)],
        str(notice.code),
    )


def _dedupe_notices(notices: list[OperatorNotice]) -> list[OperatorNotice]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[OperatorNotice] = []
    for notice in notices:
        key = (str(notice.code), notice.title, notice.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(notice)
    return deduped


def _compose_notice_placement(
    *,
    runtime_freshness: OperatorSurfaceRuntimeFreshness | None,
    incident_headline: OperatorNotice | None,
    broker_activity_health: BrokerActivityHealth | None,
) -> OperatorSurfaceNoticePlacement:
    notices: list[OperatorNotice] = []
    if incident_headline is not None:
        notices.append(incident_headline)
    if runtime_freshness is not None:
        if runtime_freshness.headline is not None:
            notices.append(runtime_freshness.headline)
        notices.extend(runtime_freshness.additional_reasons)
    if broker_activity_health is not None:
        # headline (when set) is always the first element of notices, so extending
        # from notices alone is sufficient — no separate headline append needed.
        notices.extend(broker_activity_health.notices)

    ordered = sorted(_dedupe_notices(notices), key=_notice_sort_key)
    criticals = [notice for notice in ordered if notice.tier == "critical"]
    warnings = [notice for notice in ordered if notice.tier == "warning"]
    infos = [notice for notice in ordered if notice.tier == "info"]
    banner = criticals[0] if criticals else None
    folded = criticals[1:] if banner is not None else []
    return OperatorSurfaceNoticePlacement(
        banner=banner,
        banner_fold_count=len(folded),
        banner_folded=folded,
        attention=warnings,
        quiet_status=infos,
    )


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------


def compute_operator_surface(
    *,
    process: InstanceProcessView,
    last_exit: InstanceLastExit | None = None,
    safety_verdict_final: Literal["paper-only", "unsafe", "unknown"] | None = None,
    broker_connection_state: BrokerConnectionStateInput | None = None,
    broker: InstanceBrokerView | None = None,
    readiness: ReadinessVector | None = None,
    action_plan: dict | None = None,
    start_defaults: InstanceStartDefaults | None = None,
    sizing: InstanceSizing | None = None,
    instance_broker_self_consistent: bool | None = None,
    live_binding: LiveBinding | None = None,
    poisoned: bool = False,
    bot_lifecycle_phase: BotLifecyclePhase | None = None,
    desired_state: DesiredStateView | None = None,
    guard_state: ResumeGuardState | None = None,
    runtime_freshness: RuntimeFreshness | None = None,
    control_plane_state: DaemonConnectivityState | None = None,
    latest_mutation: MutationAttempt | None = None,
    broker_observation_consistency: BrokerObservationConsistency | None = None,
    account_truth_snapshot: AccountTruthReadinessEvidence | None = None,
    host_start_command: str | None = None,
    start_run_id: str | None = None,
    account_freeze: AccountFreezeEvidence | None = None,
    crash_recovery_gate: GateResult | None = None,
    account_owner: OperatorSurfaceAccountOwner | None = None,
    # ADR-0008 §5 / PR 1 — cold-start reconciliation projection inputs.
    # All optional: when no live binding is resolved, the router passes
    # ``reconciliation_receipt=None`` and the projection turns into
    # ``NOT_AVAILABLE`` (the only legitimate "we have nothing" state).
    reconciliation_receipt: ReconciliationReceipt | None = None,
    current_wal_seq: int | None = None,
    current_run_id: str | None = None,
    current_namespace: str | None = None,
    latest_broker_event_ms: int | None = None,
    latest_mutation_ms: int | None = None,
    reconciliation_ttl_ms: int | None = None,
    # PR 5 — broker-activity publisher health inputs.
    # ``activity_publisher`` is the registered publisher for the current
    # instance (``None`` when nothing is registered).
    # ``activity_publisher_registered_at_ms`` is the wall-clock ms when
    # the publisher was first registered (from the registry).
    activity_publisher: BrokerActivityPublisher | None = None,
    activity_publisher_registered_at_ms: int | None = None,
    # PR 2 — post-halt watchdog incident headline forwarded verbatim.
    incident_headline_notice: OperatorNotice | None = None,
    now_ms: int,
) -> OperatorSurface:
    """Build the operator-surface projection for one instance.

    The function is intentionally pure: every input is a primitive value
    or an already-resolved view model.  The router does the source
    blending (settings, readiness gate, broker safety verdict, sidecars,
    desired-state sidecar, resume-guard resolution, now_ms) and hands
    the result in.

    PRD #616 replaced ``configured_mode`` with ``safety_verdict_final``
    (ADR-0011's reactive final verdict) and added the ``guard_state``
    parameter; both keyword-only and additive on the call site.
    """

    owned_positions_empty = broker is None or not any(qty != 0 for qty in broker.owned_positions.values())
    resolved_guards = guard_state if guard_state is not None else empty_guard_state()

    # PR 5 — broker-activity health. Only composed when there is a live
    # binding (an active strategy instance context); without one there
    # is no publisher to query.
    broker_activity_health: BrokerActivityHealth | None = None
    if live_binding is not None:
        broker_activity_health = compose_broker_activity_health(
            publisher=activity_publisher,
            registered_at_ms=activity_publisher_registered_at_ms,
            last_row_ms=activity_publisher.latest_row_ms if activity_publisher is not None else None,
            now_ms=now_ms,
        )

    host_process = _project_host_process(
        process,
        desired_state,
        last_exit=last_exit,
        start_run_id=start_run_id,
        start_defaults=start_defaults,
        poisoned=poisoned,
        host_start_command=host_start_command,
        now_ms=now_ms,
        account_freeze=account_freeze,
        crash_recovery_gate=crash_recovery_gate,
    )
    broker_projection = project_broker(
        safety_verdict_final,
        broker_connection_state,
        runtime_bound=live_binding is not None or host_process.state == "RUNNING",
    )
    daily_order_cap = _project_daily_order_cap(readiness)
    actions = _attach_action_gate_results(
        evaluate_all_actions(
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
            desired_state=desired_state,
            guard_state=resolved_guards,
            runtime_freshness=runtime_freshness,
            latest_mutation=latest_mutation,
        ),
        now_ms=now_ms,
        account_freeze=account_freeze,
    )
    trading_session = _project_trading_session(now_ms=now_ms)
    readiness_gates = project_readiness_gates(readiness)
    reconciliation_projection = _project_reconciliation(
        reconciliation_receipt,
        current_wal_seq=current_wal_seq,
        current_run_id=current_run_id,
        current_namespace=current_namespace,
        latest_broker_event_ms=latest_broker_event_ms,
        latest_mutation_ms=latest_mutation_ms,
        ttl_ms=reconciliation_ttl_ms,
        now_ms=now_ms,
    )
    runtime_freshness_projection = _project_runtime_freshness(runtime_freshness)
    control_plane_projection = _project_control_plane(control_plane_state)
    account_truth_assessment = assess_account_truth(account_truth_snapshot, now_ms=now_ms)
    submit_readiness = author_submit_readiness(
        host_process=host_process,
        broker=broker_projection,
        trading_session=trading_session,
        account_owner=account_owner,
        account_freeze=account_freeze,
        guard_state=resolved_guards,
        reconciliation=reconciliation_projection,
        runtime_freshness=runtime_freshness_projection,
        readiness_gates=readiness_gates,
        account_truth=account_truth_assessment,
    )
    trader_guidance = author_trader_guidance(
        submit_readiness=submit_readiness,
        host_process=host_process,
        broker=broker_projection,
        trading_session=trading_session,
        account_owner=account_owner,
        account_freeze=account_freeze,
        guard_state=resolved_guards,
        reconciliation=reconciliation_projection,
        runtime_freshness=runtime_freshness_projection,
        readiness_gates=readiness_gates,
        daily_order_cap=daily_order_cap,
        account_truth=account_truth_assessment,
    )
    blockage_ladder = author_blockage_ladder(
        host_process=host_process,
        broker=broker_projection,
        trading_session=trading_session,
        account_owner=account_owner,
        account_freeze=account_freeze,
        guard_state=resolved_guards,
        reconciliation=reconciliation_projection,
        runtime_freshness=runtime_freshness_projection,
        readiness_gates=readiness_gates,
        account_truth=account_truth_assessment,
        control_plane=control_plane_projection,
    )
    notice_placement = _compose_notice_placement(
        runtime_freshness=runtime_freshness_projection,
        incident_headline=incident_headline_notice,
        broker_activity_health=broker_activity_health,
    )
    blockers = _author_operator_blockers(
        poisoned=poisoned,
        bot_lifecycle_phase=bot_lifecycle_phase,
    )

    return OperatorSurface(
        host_process=host_process,
        prior_run=_project_prior_run(last_exit),
        broker=broker_projection,
        execution=_project_execution(runtime_freshness),
        configuration=_project_configuration(start_defaults, sizing, instance_broker_self_consistent),
        current_risk=_project_current_risk(broker),
        daily_order_cap=daily_order_cap,
        action_plan=_project_action_plan(action_plan, start_defaults),
        account_owner=account_owner,
        submit_readiness=submit_readiness,
        trader_guidance=trader_guidance,
        blockage_ladder=blockage_ladder,
        run_signal=_project_run_signal(host_process, blockage_ladder),
        actions=actions,
        trading_session=trading_session,
        readiness_gates=readiness_gates,
        blockers=blockers,
        runtime_freshness=runtime_freshness_projection,
        control_plane=control_plane_projection,
        broker_observation_consistency=broker_observation_consistency,
        reconciliation=reconciliation_projection,
        incident_headline=incident_headline_notice,
        broker_activity_health=broker_activity_health,
        notice_placement=notice_placement,
    )
