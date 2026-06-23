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

from app.engine.live.daemon_connectivity_monitor import DaemonConnectivityState
from app.engine.live.daemon_transport import DaemonResultKind
from app.schemas.live_runs import (
    BrokerObservationConsistency,
    DesiredStateView,
    FocusAction,
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
    OperatorSurfaceActionPlan,
    OperatorSurfaceBroker,
    OperatorSurfaceConfiguration,
    OperatorSurfaceControlPlane,
    OperatorSurfaceCurrentRisk,
    OperatorSurfaceDailyOrderCap,
    OperatorSurfaceDomainFreshness,
    OperatorSurfaceHostProcess,
    OperatorSurfacePriorRun,
    OperatorSurfaceRuntimeFreshness,
    OperatorSurfaceTradingSession,
    ReadinessGate,
    ReadinessVector,
    RedeployAction,
)
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
    "STOPPING": "Host process is shutting down.",
    "EXITED": "Host process has exited. Restart it from the host runner to resume actuation.",
    "IDLE": ("Host runner is reachable but no subprocess is attached to this instance. Start it from the host runner."),
    "WAITING_FOR_HOST": (
        "Intent is RUNNING, but no host subprocess is attached to this "
        "instance. Start it outside the app to actuate trading."
    ),
    "UNREACHABLE": ("Host runner daemon is not reachable. The cockpit cannot confirm any subprocess state from here."),
}


def _project_host_process(
    process: InstanceProcessView,
    desired_state: DesiredStateView | None,
    host_start_command: str | None = None,
) -> OperatorSurfaceHostProcess:
    state = _DAEMON_STATE_TO_HOST_PROCESS_STATE.get(process.state, "UNREACHABLE")
    # WAITING_FOR_HOST: daemon reachable + no tracked subprocess + the
    # operator has set durable intent to RUNNING.
    if state == "IDLE" and desired_state is not None and desired_state.state == "RUNNING":
        state = "WAITING_FOR_HOST"
    notice = None if state == "RUNNING" else _HOST_PROCESS_NOTICE_BY_STATE.get(state)
    # ``copyable_command`` is authored ONLY for UNREACHABLE, and only when
    # trusted deployment configuration supplies a non-empty command. The
    # EXITED / IDLE / WAITING_FOR_HOST cases need a per-instance Start
    # action, NOT the daemon-start script (it would restart the host
    # service unnecessarily and not the exited subprocess). ADR 0013
    # amendment 2026-06-22; design doc "Deployment-model decision".
    copyable_command = (
        host_start_command
        if state == "UNREACHABLE" and host_start_command
        else None
    )
    return OperatorSurfaceHostProcess(state=state, notice=notice, copyable_command=copyable_command)


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
_OPEN_BROKER_RECONNECT_RUNBOOK = OpenRunbookAction(
    kind="open_runbook", slug="broker-reconnect"
)


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
        out.append(
            OperatorGate(
                name=gate.name,
                status=gate.status,
                severity=gate.severity,
                detail=gate.detail,
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
    return OperatorSurfaceRuntimeFreshness(
        posture_demoted=freshness.posture_demoted,
        stale_reason_codes=runtime_freshness_reason_codes(freshness),
        command_loop=_project_domain_freshness(freshness.command_loop),
        broker=_project_domain_freshness(freshness.broker),
        bar_loop=_project_domain_freshness(freshness.bar_loop),
        control_plane=_project_domain_freshness(freshness.control_plane),
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
        "Host daemon is unreachable. Verify the launcher process is running "
        "and that the daemon URL is correct.",
        "daemon-unreachable",
    ),
    "AUTH_FAILED": (
        "Host daemon rejected the data plane's credentials. Rotate or refresh "
        "the daemon token and restart the data plane.",
        "daemon-auth-failed",
    ),
    "PROTOCOL_ERROR": (
        "Host daemon returned a malformed or error response. It may be "
        "mid-restart — check the daemon logs.",
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
    notice, runbook_slug = _CONTROL_PLANE_NOTICE_TABLE.get(
        state.kind, (None, None)
    )
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

    return OperatorSurface(
        host_process=_project_host_process(process, desired_state, host_start_command),
        prior_run=_project_prior_run(last_exit),
        broker=_project_broker(safety_verdict_final, broker_connection_state),
        configuration=_project_configuration(start_defaults, sizing, instance_broker_self_consistent),
        current_risk=_project_current_risk(broker),
        daily_order_cap=_project_daily_order_cap(readiness),
        action_plan=_project_action_plan(action_plan),
        actions=evaluate_all_actions(
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
            desired_state=desired_state,
            guard_state=resolved_guards,
            runtime_freshness=runtime_freshness,
            latest_mutation=latest_mutation,
        ),
        trading_session=_project_trading_session(now_ms=now_ms),
        readiness_gates=project_readiness_gates(readiness),
        runtime_freshness=_project_runtime_freshness(runtime_freshness),
        control_plane=_project_control_plane(control_plane_state),
        broker_observation_consistency=broker_observation_consistency,
    )
