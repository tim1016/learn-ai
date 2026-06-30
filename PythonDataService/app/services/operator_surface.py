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

from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.engine.live.daemon_connectivity_monitor import DaemonConnectivityState
from app.engine.live.daemon_transport import DaemonResultKind
from app.operator.notices.broker_activity_health import compose_broker_activity_health
from app.operator.notices.runtime_freshness import compose_runtime_freshness_notices
from app.operator.notices.schema import OperatorNotice
from app.schemas.live_runs import (
    ActionCapability,
    BrokerActivityHealth,
    BrokerObservationConsistency,
    DesiredStateView,
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
    InvokeEndpointAction,
    LiveBinding,
    NoPrimaryRemediationAction,
    OpenRunbookAction,
    OperatorGate,
    OperatorSurface,
    OperatorSurfaceAccountOwner,
    OperatorSurfaceActionPlan,
    OperatorSurfaceActions,
    OperatorSurfaceAttentionGroup,
    OperatorSurfaceBroker,
    OperatorSurfaceConfiguration,
    OperatorSurfaceControlPlane,
    OperatorSurfaceCurrentRisk,
    OperatorSurfaceDailyOrderCap,
    OperatorSurfaceDomainFreshness,
    OperatorSurfaceEvidenceFact,
    OperatorSurfaceHostProcess,
    OperatorSurfacePriorRun,
    OperatorSurfaceReconciliation,
    OperatorSurfaceRuntimeFreshness,
    OperatorSurfaceSubmitReadiness,
    OperatorSurfaceTraderGuidance,
    OperatorSurfaceTradingSession,
    ReadinessGate,
    ReadinessVector,
    ReconciliationReceipt,
    ReconciliationState,
    RedeployAction,
)
from app.services.broker_activity_publisher import BrokerActivityPublisher
from app.services.mutation_attempt import MutationAttempt
from app.services.operator_capability import evaluate_all_actions
from app.services.resume_guard_state import ResumeGuardState, empty_guard_state
from app.services.runtime_freshness import (
    DomainFreshness,
    RuntimeFreshness,
    runtime_freshness_reason_codes,
)

# Server-side canonical input for the broker connection layer.  The
# router collapses the readiness ``broker_connection`` gate plus (when
# available) the broker monitor's recovery overlay into one of these
# tokens; the projection maps them to the wire-facing enums.
BrokerConnectionStateInput = Literal["connected", "disconnected", "degraded", "unknown"]


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
    "EXITED": "The previous bot process ended. Start this bot's process to resume trading.",
    "IDLE": "The host is reachable but this bot has no active process. Start it to resume trading.",
    "WAITING_FOR_HOST": (
        "Trading was requested, but this bot's process has not started yet. Start it to begin trading."
    ),
    "UNREACHABLE": ("The bot service is offline. The cockpit cannot confirm any process state until it is reachable."),
}


_START_CAPABLE_STATES = frozenset({"IDLE", "WAITING_FOR_HOST", "EXITED"})


def _project_host_start_capability(
    state: str,
    desired_state: DesiredStateView | None,
    start_run_id: str | None,
    start_defaults: InstanceStartDefaults | None,
    poisoned: bool,
    now_ms: int,
    account_freeze: AccountFreezeEvidence | None = None,
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
    # Permanent-retirement gates outrank every per-state guard.
    if poisoned or (desired_state is not None and desired_state.state == "STOPPED"):
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
    start_run_id: str | None = None,
    start_defaults: InstanceStartDefaults | None = None,
    poisoned: bool = False,
    host_start_command: str | None = None,
    now_ms: int = 0,
    account_freeze: AccountFreezeEvidence | None = None,
) -> OperatorSurfaceHostProcess:
    state = _DAEMON_STATE_TO_HOST_PROCESS_STATE.get(process.state, "UNREACHABLE")
    # WAITING_FOR_HOST: daemon reachable + no tracked subprocess + the
    # operator has set durable intent to RUNNING.
    if state == "IDLE" and desired_state is not None and desired_state.state == "RUNNING":
        state = "WAITING_FOR_HOST"
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
    )
    return OperatorSurfaceHostProcess(
        state=state,
        notice=notice,
        copyable_command=copyable_command,
        start_capability=start_capability,
    )


# ---------------------------------------------------------------------------
# prior_run
# ---------------------------------------------------------------------------


def _project_prior_run(last_exit: InstanceLastExit | None) -> OperatorSurfacePriorRun:
    if last_exit is None:
        return OperatorSurfacePriorRun(classification="UNKNOWN")
    if last_exit.halt_trigger is not None:
        return OperatorSurfacePriorRun(classification="HALT_TRIGGERED")
    if last_exit.exit_code == 0 or last_exit.exit_reason == "normal":
        return OperatorSurfacePriorRun(classification="CLEAN")
    if last_exit.exit_code is not None and last_exit.exit_code != 0:
        return OperatorSurfacePriorRun(classification="EXITED_WITH_ERROR")
    return OperatorSurfacePriorRun(classification="UNKNOWN")


# ---------------------------------------------------------------------------
# broker — connection + safety_verdict (two independent enums)
# ---------------------------------------------------------------------------

# PRD #616 — map ADR-0011's reactive ``final_verdict`` directly.  The
# previous implementation derived safety from ``configured_mode``
# alone, which ignored every runtime gate ADR-0011 introduced (port,
# account prefix, readonly flag).
_BROKER_FINAL_VERDICT_TO_SAFETY: dict[str, str] = {
    "paper-only": "PAPER_ONLY",
    "unsafe": "UNSAFE",
    "unknown": "UNKNOWN",
}


def _project_broker(
    safety_verdict_final: Literal["paper-only", "unsafe", "unknown"] | None,
    connection_state: BrokerConnectionStateInput | None,
) -> OperatorSurfaceBroker:
    # ``connection`` is whether the broker session is up; ``safety_verdict``
    # is whether we're allowed to trade.  They are independent and must
    # not be composed.  ``safety_verdict_final`` is ADR-0011's reactive
    # ``BrokerSafetyVerdict.final_verdict``; the cockpit's SAFETY pill
    # changes when runtime conditions change, not only on configured
    # mode reconnect.
    if connection_state == "connected":
        connection = "CONNECTED"
    elif connection_state == "disconnected":
        connection = "DISCONNECTED"
    elif connection_state == "degraded":
        # Currently unreachable from the live readiness gate (pass/fail
        # only).  When a richer health channel lands on the wire we
        # surface this honestly as DISCONNECTED-with-recovery rather
        # than inventing a third enum value for the cockpit.
        connection = "DISCONNECTED"
    else:
        connection = "UNKNOWN"

    if safety_verdict_final is None:
        safety_verdict = "UNKNOWN"
    else:
        safety_verdict = _BROKER_FINAL_VERDICT_TO_SAFETY.get(safety_verdict_final, "UNKNOWN")

    return OperatorSurfaceBroker(
        safety_verdict=safety_verdict,  # type: ignore[arg-type]
        connection=connection,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# current_risk
# ---------------------------------------------------------------------------


def _derive_posture(owned_positions: dict[str, int]) -> str:
    non_zero = {sym: qty for sym, qty in owned_positions.items() if qty != 0}
    if not non_zero:
        return "FLAT"
    sides = {("LONG" if qty > 0 else "SHORT") for qty in non_zero.values()}
    if len(sides) > 1:
        return "MIXED"
    return next(iter(sides))


def _project_current_risk(broker: InstanceBrokerView | None) -> OperatorSurfaceCurrentRisk:
    if broker is None:
        return OperatorSurfaceCurrentRisk(
            posture="UNKNOWN",
            pending_order_count=None,
            verdict="UNKNOWN",
            unrealized_pnl=None,
        )
    posture = _derive_posture(broker.owned_positions)
    pending = broker.pending_order_count
    verdict = "READY" if posture == "FLAT" and pending == 0 else "ATTENTION"
    return OperatorSurfaceCurrentRisk(
        posture=posture,  # type: ignore[arg-type]
        pending_order_count=pending,
        verdict=verdict,
        unrealized_pnl=broker.unrealized_pnl,
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


def _project_action_plan(action_plan: dict | None) -> OperatorSurfaceActionPlan:
    if action_plan is None:
        return OperatorSurfaceActionPlan(consumption="UNKNOWN", anomaly_verdict="UNKNOWN")
    return OperatorSurfaceActionPlan(consumption="DECLARATIVE_ONLY", anomaly_verdict="READY")


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

# Default RTH window when a strategy has not declared its own session
# policy.  Per the cockpit-revision contract, this default lives
# server-side; Angular MUST NOT hard-code it.  When per-strategy
# session policies ship, this helper grows to read them.
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
_PRE_OPEN = time(4, 0)
_POST_CLOSE = time(20, 0)


def _ny_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).astimezone(_NY)


def _at(ny_day: datetime, t: time) -> datetime:
    return datetime.combine(ny_day.date(), t, tzinfo=_NY)


def _ms_utc(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp() * 1000)


def _next_weekday_open(now_ny: datetime) -> int:
    """Return the next weekday's 04:00 NY pre-open as int64 ms UTC."""
    day = now_ny.date()
    delta = 1
    while True:
        candidate = day + timedelta(days=delta)
        # weekday: 0=Mon ... 6=Sun
        if candidate.weekday() < 5:
            target = datetime.combine(candidate, _PRE_OPEN, tzinfo=_NY)
            return _ms_utc(target)
        delta += 1


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
    wall = now_ny.time()
    weekday = now_ny.weekday()  # 0=Mon ... 6=Sun

    phase: str
    permits: bool | None
    next_transition_ms: int | None

    if weekday >= 5:
        # Weekend → CLOSED until Monday 04:00 ET.
        phase = "CLOSED"
        permits = False
        next_transition_ms = _next_weekday_open(now_ny)
    elif wall < _PRE_OPEN:
        phase = "CLOSED"
        permits = False
        next_transition_ms = _ms_utc(_at(now_ny, _PRE_OPEN))
    elif wall < _RTH_OPEN:
        phase = "PRE"
        permits = False
        next_transition_ms = _ms_utc(_at(now_ny, _RTH_OPEN))
    elif wall < _RTH_CLOSE:
        phase = "RTH"
        permits = True
        next_transition_ms = _ms_utc(_at(now_ny, _RTH_CLOSE))
    elif wall < _POST_CLOSE:
        phase = "POST"
        permits = False
        next_transition_ms = _ms_utc(_at(now_ny, _POST_CLOSE))
    else:
        # After 20:00 ET on a weekday → CLOSED until tomorrow's 04:00 ET.
        phase = "CLOSED"
        permits = False
        next_transition_ms = _next_weekday_open(now_ny)

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


# ---------------------------------------------------------------------------
# trader_guidance + submit_readiness (PRD #718)
# ---------------------------------------------------------------------------

_READY_RECONCILIATION_STATES = frozenset({"CLEAN", "ADOPTED"})

_SUBMIT_READINESS_COPY: dict[str, tuple[str, str]] = {
    "safe_to_submit": (
        "Safe to submit",
        "Broker safety, submit capability, AccountOwner generation, reconciliation, and runtime proofs are all satisfied.",
    ),
    "safe_to_monitor": (
        "Safe to monitor",
        "The cockpit can observe this bot, but order submission is not currently active or appropriate.",
    ),
    "blocked_before_submit": (
        "Blocked before submit",
        "A pre-submit gate would stop a new order before it reaches the broker.",
    ),
    "broker_state_unproven": (
        "Broker state unproven",
        "The backend cannot prove the broker/session/reconciliation evidence required for a safe submit.",
    ),
    "account_frozen": (
        "Account frozen",
        "Account-wide unresolved exposure is active; no sibling bot on this account may submit.",
    ),
    "waiting_for_owner_generation": (
        "Waiting for owner generation",
        "The AccountOwner generation or phase is not proven accepting, so single-writer submission is not proven.",
    ),
    "submit_outcome_uncertain": (
        "Submit outcome uncertain",
        "An intent may already have reached the broker; probe/reconcile before any retry.",
    ),
}

_TRADER_GUIDANCE_COPY: dict[str, tuple[str, str, str, str]] = {
    "ready_to_submit": (
        "This bot is ready to submit paper orders.",
        "All backend submit-readiness proofs are currently satisfied.",
        "Submission gates are satisfied",
        "The surface is allowed to say safe to submit because the broker, submit lane, owner generation, and reconciliation proofs are all present.",
    ),
    "monitor_only": (
        "This bot is safe to monitor, not safe to submit right now.",
        "The current state is observable, but at least one non-critical condition means order submission should not be treated as active.",
        "Observation is okay; trading is not active",
        "Keep watching the bot, but do not interpret the Overview as a trade-permission signal.",
    ),
    "submission_blocked": (
        "A pre-submit gate is blocking this bot.",
        "The backend would stop a new order before it reaches the broker.",
        "Order submission is blocked before broker placement",
        "This is a controlled block, not proof that a broker order exists.",
    ),
    "broker_state_unproven": (
        "Broker state is not proven enough to submit.",
        "The backend cannot prove the broker/session/reconciliation facts needed before a submit.",
        "Do not treat stale or missing broker evidence as live truth",
        "Reconnect or reconcile until the broker evidence is fresh and explicit.",
    ),
    "account_frozen": (
        "This account has an active freeze.",
        "An account-wide unresolved-exposure artifact is present, so every sibling bot must treat submission as stopped.",
        "Account-wide stop sign",
        "Resolve the account exposure before any bot on this account submits.",
    ),
    "waiting_for_owner_generation": (
        "AccountOwner is not accepting submits yet.",
        "The current AccountOwner generation/phase is missing or not in the accepting phase.",
        "Single-writer proof is incomplete",
        "Wait for AccountOwner to reach accepting, or recover the owner lane before trading.",
    ),
    "submit_outcome_uncertain": (
        "A previous submit outcome is uncertain.",
        "An ACK_FAILED_UNCERTAIN or equivalent unresolved submit condition is active in the durable evidence.",
        "Do not blind-retry",
        "Probe or reconcile the broker before any retry so the bot cannot duplicate or orphan an order.",
    ),
    "attention_required": (
        "This bot needs operator attention.",
        "One or more backend-authored facts are not in their ready state.",
        "Review the independent facts below",
        "The summary does not replace the underlying process, broker, safety, and account facts.",
    ),
    "unknown": (
        "The bot state is not fully known.",
        "The backend is missing enough evidence that it cannot author a stronger trader summary.",
        "Unknown is not safe",
        "Treat missing evidence as a reason to inspect the raw artifacts or runbook.",
    ),
}


def _fact(
    label: str,
    value: object,
    *,
    source: str | None = None,
    gate_id: str | None = None,
    ts_ms: int | None = None,
) -> OperatorSurfaceEvidenceFact:
    return OperatorSurfaceEvidenceFact(
        label=label,
        value=str(value),
        source=source,
        gate_id=gate_id,
        ts_ms=ts_ms,
        ts_ms_resolved=ts_ms is not None,
    )


def _append_unique(codes: list[str], code: str) -> None:
    if code not in codes:
        codes.append(code)


def _hard_blocking_readiness_gates(readiness_gates: list[OperatorGate]) -> list[OperatorGate]:
    return [gate for gate in readiness_gates if gate.status != "pass" and gate.severity == "hard"]


def _is_reconciliation_ready(reconciliation: OperatorSurfaceReconciliation | None) -> bool:
    return reconciliation is not None and reconciliation.state in _READY_RECONCILIATION_STATES


def _account_owner_ready(account_owner: OperatorSurfaceAccountOwner | None) -> bool:
    return account_owner is not None and account_owner.generation is not None and account_owner.phase == "accepting"


def _submit_readiness_remediation(
    code: str,
    *,
    broker: OperatorSurfaceBroker,
    reconciliation: OperatorSurfaceReconciliation | None,
) -> object:
    if code == "safe_to_submit":
        return NoPrimaryRemediationAction(kind="none", reason="READY")
    if code == "safe_to_monitor":
        return NoPrimaryRemediationAction(kind="none", reason="MONITOR_ONLY")
    if code == "submit_outcome_uncertain":
        return InvokeEndpointAction(kind="invoke_endpoint", endpoint="reconcile_instance")
    if code == "broker_state_unproven" and (
        reconciliation is None or reconciliation.state not in _READY_RECONCILIATION_STATES
    ):
        return InvokeEndpointAction(kind="invoke_endpoint", endpoint="reconcile_instance")
    if code == "broker_state_unproven" and broker.connection != "CONNECTED":
        return OpenRunbookAction(kind="open_runbook", slug="broker-reconnect")
    if code == "account_frozen":
        return OpenRunbookAction(kind="open_runbook", slug="watchdog-halt")
    if code == "waiting_for_owner_generation":
        return OpenRunbookAction(kind="open_runbook", slug="broker-instance-operator-surface")
    if code == "blocked_before_submit":
        return OpenRunbookAction(kind="open_runbook", slug="broker-instance-operator-surface")
    return OpenRunbookAction(kind="open_runbook", slug="broker-instance-operator-surface")


def _submit_readiness_evidence(
    *,
    host_process: OperatorSurfaceHostProcess,
    broker: OperatorSurfaceBroker,
    trading_session: OperatorSurfaceTradingSession,
    account_owner: OperatorSurfaceAccountOwner | None,
    account_freeze: AccountFreezeEvidence | None,
    guard_state: ResumeGuardState,
    reconciliation: OperatorSurfaceReconciliation | None,
    readiness_gates: list[OperatorGate],
    daily_order_cap: OperatorSurfaceDailyOrderCap,
) -> list[OperatorSurfaceEvidenceFact]:
    facts = [
        _fact("host_process.state", host_process.state, source="operator_surface"),
        _fact("broker.safety_verdict", broker.safety_verdict, source="operator_surface"),
        _fact("broker.connection", broker.connection, source="operator_surface"),
        _fact(
            "submission_capability.state",
            guard_state.submission_capability.state,
            source="resume_guard_state",
        ),
        _fact("uncertain_intent.state", guard_state.uncertain_intent.state, source="intent_wal"),
        _fact(
            "reconciliation.state",
            reconciliation.state if reconciliation is not None else "NOT_AVAILABLE",
            source="reconciliation_receipt",
            ts_ms=reconciliation.last_reconcile_ms if reconciliation is not None else None,
        ),
        _fact("trading_session.phase", trading_session.phase, source="operator_surface", ts_ms=trading_session.as_of_ms),
    ]
    if daily_order_cap.used is not None or daily_order_cap.limit is not None:
        facts.append(
            _fact(
                "daily_order_cap",
                f"{daily_order_cap.used if daily_order_cap.used is not None else 'unknown'}/"
                f"{daily_order_cap.limit if daily_order_cap.limit is not None else 'unknown'}",
                source="readiness",
            )
        )
    if account_owner is None:
        facts.append(_fact("account_owner.phase", "unknown", source="account_artifacts"))
    else:
        facts.append(
            _fact(
                "account_owner.phase",
                account_owner.phase,
                source=account_owner.source or "account_artifacts",
                ts_ms=account_owner.recorded_at_ms,
            )
        )
        facts.append(
            _fact(
                "account_owner.generation",
                account_owner.generation if account_owner.generation is not None else "unknown",
                source=account_owner.source or "account_artifacts",
                ts_ms=account_owner.recorded_at_ms,
            )
        )
    if account_freeze is not None:
        gate = account_freeze.to_gate_result()
        facts.append(
            _fact(
                "account_freeze",
                account_freeze.reason,
                source=account_freeze.source,
                gate_id=gate.gate_id,
                ts_ms=account_freeze.recorded_at_ms,
            )
        )
    for gate in _hard_blocking_readiness_gates(readiness_gates):
        facts.append(
            _fact(
                f"readiness.{gate.name}",
                gate.detail,
                source=gate.gate_result.source,
                gate_id=gate.gate_result.gate_id,
                ts_ms=gate.gate_result.evidence_at_ms,
            )
        )
    return facts


def _author_submit_readiness(
    *,
    host_process: OperatorSurfaceHostProcess,
    broker: OperatorSurfaceBroker,
    trading_session: OperatorSurfaceTradingSession,
    account_owner: OperatorSurfaceAccountOwner | None,
    account_freeze: AccountFreezeEvidence | None,
    guard_state: ResumeGuardState,
    reconciliation: OperatorSurfaceReconciliation | None,
    readiness_gates: list[OperatorGate],
    daily_order_cap: OperatorSurfaceDailyOrderCap,
) -> OperatorSurfaceSubmitReadiness:
    hard_gates = _hard_blocking_readiness_gates(readiness_gates)
    codes: list[str] = []
    code = "safe_to_submit"

    if account_freeze is not None:
        code = "account_frozen"
        _append_unique(codes, "ACCOUNT_FROZEN")
    elif guard_state.uncertain_intent.state == "PRESENT":
        code = "submit_outcome_uncertain"
        _append_unique(codes, "UNRESOLVED_UNCERTAIN_INTENT")
    elif guard_state.uncertain_intent.state == "UNKNOWN":
        code = "broker_state_unproven"
        _append_unique(codes, "UNCERTAIN_INTENT_STATE_UNKNOWN")
    elif broker.safety_verdict != "PAPER_ONLY":
        code = "broker_state_unproven"
        _append_unique(codes, f"BROKER_SAFETY_{broker.safety_verdict}")
    elif broker.connection != "CONNECTED":
        code = "broker_state_unproven"
        _append_unique(codes, f"BROKER_CONNECTION_{broker.connection}")
    elif guard_state.submission_capability.state != "SATISFIED":
        code = "blocked_before_submit"
        _append_unique(codes, f"SUBMISSION_CAPABILITY_{guard_state.submission_capability.state}")
    elif not _account_owner_ready(account_owner):
        code = "waiting_for_owner_generation"
        if account_owner is None or account_owner.generation is None or account_owner.phase == "unknown":
            _append_unique(codes, "ACCOUNT_OWNER_GENERATION_UNPROVEN")
        else:
            _append_unique(codes, f"ACCOUNT_OWNER_PHASE_{account_owner.phase.upper()}")
    elif not _is_reconciliation_ready(reconciliation):
        code = "broker_state_unproven"
        _append_unique(codes, f"RECONCILIATION_{reconciliation.state if reconciliation is not None else 'NOT_AVAILABLE'}")
    elif hard_gates:
        code = "blocked_before_submit"
        for gate in hard_gates:
            _append_unique(codes, f"READINESS_GATE_{gate.name}")
    elif host_process.state != "RUNNING":
        code = "safe_to_monitor"
        _append_unique(codes, f"HOST_PROCESS_{host_process.state}")
    elif trading_session.permits_strategy_activity is not True:
        code = "safe_to_monitor"
        _append_unique(codes, f"TRADING_SESSION_{trading_session.phase}")

    label, explanation = _SUBMIT_READINESS_COPY[code]
    return OperatorSurfaceSubmitReadiness(
        code=code,  # type: ignore[arg-type]
        label=label,
        explanation=explanation,
        can_submit=code == "safe_to_submit",
        blocking_reason_codes=codes,
        template_id=f"operator_surface.submit_readiness.{code}",
        template_version=1,
    )


def _attention_groups(
    *,
    host_process: OperatorSurfaceHostProcess,
    broker: OperatorSurfaceBroker,
    trading_session: OperatorSurfaceTradingSession,
    account_owner: OperatorSurfaceAccountOwner | None,
    account_freeze: AccountFreezeEvidence | None,
    guard_state: ResumeGuardState,
    reconciliation: OperatorSurfaceReconciliation | None,
    readiness_gates: list[OperatorGate],
) -> list[OperatorSurfaceAttentionGroup]:
    groups: list[OperatorSurfaceAttentionGroup] = []

    def add(code: str, severity: str, headline: str, explanation: str) -> None:
        if any(group.code == code for group in groups):
            return
        groups.append(
            OperatorSurfaceAttentionGroup(
                code=code,
                severity=severity,  # type: ignore[arg-type]
                headline=headline,
                explanation=explanation,
            )
        )

    if account_freeze is not None:
        add("account_frozen", "critical", "Account freeze active", account_freeze.reason)
    if guard_state.uncertain_intent.state == "PRESENT":
        intents = ", ".join(guard_state.uncertain_intent.unresolved_intent_ids) or "unknown intent"
        add("submit_outcome_uncertain", "critical", "Submit outcome uncertain", f"Unresolved intents: {intents}.")
    if broker.connection != "CONNECTED":
        add("broker_connection", "warning", "Broker disconnected or unknown", f"Connection is {broker.connection}.")
    if broker.safety_verdict != "PAPER_ONLY":
        add("broker_safety", "critical", "Broker safety is not paper-only", f"Safety verdict is {broker.safety_verdict}.")
    if account_owner is None or account_owner.phase != "accepting" or account_owner.generation is None:
        phase = "unknown" if account_owner is None else account_owner.phase
        add("account_owner", "warning", "AccountOwner not proven accepting", f"AccountOwner phase is {phase}.")
    if reconciliation is None or reconciliation.state not in _READY_RECONCILIATION_STATES:
        state = "NOT_AVAILABLE" if reconciliation is None else reconciliation.state
        add("reconciliation", "warning", "Reconciliation is not fresh-clean", f"Reconciliation state is {state}.")
    for gate in _hard_blocking_readiness_gates(readiness_gates):
        add(f"readiness.{gate.name}", "warning", gate.name.replace("_", " ").capitalize(), gate.detail)
    if host_process.state != "RUNNING":
        add("host_process", "info", "Bot process is not running", f"Host process is {host_process.state}.")
    if trading_session.permits_strategy_activity is not True:
        add("trading_session", "info", "Trading session not accepting strategy activity", trading_session.phase)
    return groups


def _situation_code_for_submit_readiness(code: str, groups: list[OperatorSurfaceAttentionGroup]) -> str:
    if code == "safe_to_submit":
        return "ready_to_submit"
    if code == "safe_to_monitor":
        return "monitor_only"
    if code == "blocked_before_submit":
        return "submission_blocked"
    if code in {
        "broker_state_unproven",
        "account_frozen",
        "waiting_for_owner_generation",
        "submit_outcome_uncertain",
    }:
        return code
    if groups:
        return "attention_required"
    return "unknown"


def _author_trader_guidance(
    *,
    submit_readiness: OperatorSurfaceSubmitReadiness,
    host_process: OperatorSurfaceHostProcess,
    broker: OperatorSurfaceBroker,
    trading_session: OperatorSurfaceTradingSession,
    account_owner: OperatorSurfaceAccountOwner | None,
    account_freeze: AccountFreezeEvidence | None,
    guard_state: ResumeGuardState,
    reconciliation: OperatorSurfaceReconciliation | None,
    readiness_gates: list[OperatorGate],
    daily_order_cap: OperatorSurfaceDailyOrderCap,
) -> OperatorSurfaceTraderGuidance:
    groups = _attention_groups(
        host_process=host_process,
        broker=broker,
        trading_session=trading_session,
        account_owner=account_owner,
        account_freeze=account_freeze,
        guard_state=guard_state,
        reconciliation=reconciliation,
        readiness_gates=readiness_gates,
    )
    situation_code = _situation_code_for_submit_readiness(submit_readiness.code, groups)
    headline, explanation, risk_headline, risk_explanation = _TRADER_GUIDANCE_COPY[situation_code]
    return OperatorSurfaceTraderGuidance(
        situation_code=situation_code,  # type: ignore[arg-type]
        headline=headline,
        explanation=explanation,
        risk_headline=risk_headline,
        risk_explanation=risk_explanation,
        primary_remediation=_submit_readiness_remediation(
            submit_readiness.code,
            broker=broker,
            reconciliation=reconciliation,
        ),  # type: ignore[arg-type]
        additional_attention_groups=groups,
        advanced_evidence=_submit_readiness_evidence(
            host_process=host_process,
            broker=broker,
            trading_session=trading_session,
            account_owner=account_owner,
            account_freeze=account_freeze,
            guard_state=guard_state,
            reconciliation=reconciliation,
            readiness_gates=readiness_gates,
            daily_order_cap=daily_order_cap,
        ),
        template_id=f"operator_surface.trader_guidance.{situation_code}",
        template_version=1,
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
    desired_state: DesiredStateView | None = None,
    guard_state: ResumeGuardState | None = None,
    runtime_freshness: RuntimeFreshness | None = None,
    control_plane_state: DaemonConnectivityState | None = None,
    latest_mutation: MutationAttempt | None = None,
    broker_observation_consistency: BrokerObservationConsistency | None = None,
    host_start_command: str | None = None,
    start_run_id: str | None = None,
    account_freeze: AccountFreezeEvidence | None = None,
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
    incident_headline_notice: object | None = None,
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
        start_run_id=start_run_id,
        start_defaults=start_defaults,
        poisoned=poisoned,
        host_start_command=host_start_command,
        now_ms=now_ms,
        account_freeze=account_freeze,
    )
    broker_projection = _project_broker(safety_verdict_final, broker_connection_state)
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
    submit_readiness = _author_submit_readiness(
        host_process=host_process,
        broker=broker_projection,
        trading_session=trading_session,
        account_owner=account_owner,
        account_freeze=account_freeze,
        guard_state=resolved_guards,
        reconciliation=reconciliation_projection,
        readiness_gates=readiness_gates,
        daily_order_cap=daily_order_cap,
    )
    trader_guidance = _author_trader_guidance(
        submit_readiness=submit_readiness,
        host_process=host_process,
        broker=broker_projection,
        trading_session=trading_session,
        account_owner=account_owner,
        account_freeze=account_freeze,
        guard_state=resolved_guards,
        reconciliation=reconciliation_projection,
        readiness_gates=readiness_gates,
        daily_order_cap=daily_order_cap,
    )

    return OperatorSurface(
        host_process=host_process,
        prior_run=_project_prior_run(last_exit),
        broker=broker_projection,
        configuration=_project_configuration(start_defaults, sizing, instance_broker_self_consistent),
        current_risk=_project_current_risk(broker),
        daily_order_cap=daily_order_cap,
        action_plan=_project_action_plan(action_plan),
        account_owner=account_owner,
        submit_readiness=submit_readiness,
        trader_guidance=trader_guidance,
        actions=actions,
        trading_session=trading_session,
        readiness_gates=readiness_gates,
        runtime_freshness=_project_runtime_freshness(runtime_freshness),
        control_plane=_project_control_plane(control_plane_state),
        broker_observation_consistency=broker_observation_consistency,
        reconciliation=reconciliation_projection,
        incident_headline=incident_headline_notice if isinstance(incident_headline_notice, OperatorNotice) else None,
        broker_activity_health=broker_activity_health,
    )
