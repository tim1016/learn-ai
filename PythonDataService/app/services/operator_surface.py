"""Operator-surface projection (PRD #607).

Pure-function projection of run state into the cockpit-facing
``OperatorSurface`` model.  Single source of truth for operational
verdicts, risk posture, structured daily-cap usage, action-plan
consumption, broker safety verdict + connection, prior-run
classification, host-process state, trading-session phase, and
per-action capability + reason codes.

Frontend renders these fields; it does not derive verdicts from raw
status fields.  See ``docs/runbooks/broker-instance-operator-surface.md``
for the authority distinction between server domain eligibility,
Angular transient request state, Angular presentation, and the
host-process lifecycle that lives outside the cockpit's authority.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from app.schemas.live_runs import (
    DesiredStateView,
    InstanceBrokerView,
    InstanceLastExit,
    InstanceProcessView,
    InstanceSizing,
    InstanceStartDefaults,
    LiveBinding,
    OperatorSurface,
    OperatorSurfaceActionPlan,
    OperatorSurfaceBroker,
    OperatorSurfaceConfiguration,
    OperatorSurfaceCurrentRisk,
    OperatorSurfaceDailyOrderCap,
    OperatorSurfaceHostProcess,
    OperatorSurfacePriorRun,
    OperatorSurfaceTradingSession,
    ReadinessVector,
)
from app.services.operator_capability import evaluate_all_actions

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
    "IDLE": (
        "Host runner is reachable but no subprocess is attached to this "
        "instance. Start it from the host runner."
    ),
    "WAITING_FOR_HOST": (
        "Intent is RUNNING, but no host subprocess is attached to this "
        "instance. Start it outside the app to actuate trading."
    ),
    "UNREACHABLE": (
        "Host runner daemon is not reachable. The cockpit cannot confirm "
        "any subprocess state from here."
    ),
}


def _project_host_process(
    process: InstanceProcessView,
    desired_state: DesiredStateView | None,
) -> OperatorSurfaceHostProcess:
    state = _DAEMON_STATE_TO_HOST_PROCESS_STATE.get(process.state, "UNREACHABLE")
    # WAITING_FOR_HOST: daemon reachable + no tracked subprocess + the
    # operator has set durable intent to RUNNING.  Distinct from STARTING
    # (the cockpit cannot start anything; ADR-0003 / ADR-0007).
    if state == "IDLE" and desired_state is not None and desired_state.state == "RUNNING":
        state = "WAITING_FOR_HOST"
    notice = None if state == "RUNNING" else _HOST_PROCESS_NOTICE_BY_STATE.get(state)
    # ``copyable_command`` stays ``None`` until the server can author a
    # safe one per instance context (#608 host-process authority).
    return OperatorSurfaceHostProcess(state=state, notice=notice, copyable_command=None)


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


def _project_broker(
    configured_mode: Literal["paper", "live"] | None,
    connection_state: BrokerConnectionStateInput | None,
) -> OperatorSurfaceBroker:
    # ``connection`` is whether the broker session is up; ``safety_verdict``
    # is whether we're allowed to trade.  They are independent and must
    # not be composed.  ADR-0011's ``BrokerSafetyVerdict.final_verdict``
    # is the conceptual ancestor for ``safety_verdict``; the operator's
    # paper / live mode is the sole input today.
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

    if configured_mode == "paper":
        safety_verdict = "PAPER_ONLY"
    elif configured_mode == "live":
        safety_verdict = "UNSAFE"
    else:
        safety_verdict = "UNKNOWN"

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

    if (
        start_defaults is None
        or start_defaults.max_orders_per_day is None
        or start_defaults.max_orders_per_day <= 0
    ):
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


def _project_trading_session(
    *,
    now_ms: int,
    strategy_session_policy: Literal["rth_only"] | None = None,
) -> OperatorSurfaceTradingSession:
    """Compute the trading-session phase from server-side NYC wall-clock.

    Until per-strategy session policies are declarative
    (``strategy_session_policy`` will read them then), the default is
    RTH-only.  Weekends collapse to CLOSED.  ``permits_strategy_activity``
    follows the policy: ``rth_only`` -> True iff phase==RTH.
    """
    now_utc = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC)
    now_ny = now_utc.astimezone(_NY)
    wall = now_ny.time()
    weekday = now_ny.weekday()  # 0=Mon ... 6=Sun

    phase: str
    permits: bool | None
    next_transition_ms: int | None = None

    if weekday >= 5 or wall < _PRE_OPEN:
        phase = "CLOSED"
        permits = False
    elif wall < _RTH_OPEN:
        phase = "PRE"
        permits = False
    elif wall < _RTH_CLOSE:
        phase = "RTH"
        permits = True
    elif wall < _POST_CLOSE:
        phase = "POST"
        permits = False
    else:
        phase = "CLOSED"
        permits = False

    # If the strategy explicitly opted out of the default permission
    # mapping (future: opts in to extended hours), respect it.
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
# compose
# ---------------------------------------------------------------------------


def compute_operator_surface(
    *,
    process: InstanceProcessView,
    last_exit: InstanceLastExit | None = None,
    configured_mode: Literal["paper", "live"] | None = None,
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
    now_ms: int,
) -> OperatorSurface:
    """Build the operator-surface projection for one instance.

    The function is intentionally pure: every input is a primitive value
    or an already-resolved view model.  The router does the source
    blending (settings, readiness gate, broker health, sidecars,
    desired-state sidecar, now_ms) and hands the result in.
    """

    owned_positions_empty = broker is None or not any(
        qty != 0 for qty in broker.owned_positions.values()
    )

    return OperatorSurface(
        host_process=_project_host_process(process, desired_state),
        prior_run=_project_prior_run(last_exit),
        broker=_project_broker(configured_mode, broker_connection_state),
        configuration=_project_configuration(
            start_defaults, sizing, instance_broker_self_consistent
        ),
        current_risk=_project_current_risk(broker),
        daily_order_cap=_project_daily_order_cap(readiness),
        action_plan=_project_action_plan(action_plan),
        actions=evaluate_all_actions(
            process=process,
            live_binding=live_binding,
            poisoned=poisoned,
            owned_positions_empty=owned_positions_empty,
        ),
        trading_session=_project_trading_session(now_ms=now_ms),
    )
