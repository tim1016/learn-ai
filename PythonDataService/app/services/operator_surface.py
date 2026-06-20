"""Operator-surface projection (PRD #607 / Slice 1 / #608).

Pure-function projection of run state into the cockpit-facing
``OperatorSurface`` model.  Single source of truth for operational
verdicts, risk posture, structured daily-cap usage, action-plan
consumption, broker safety verdict, prior-run classification,
host-process state, and per-action capability + reason codes.

Frontend renders these fields; it does not derive verdicts from raw
status fields.  See ``docs/runbooks/broker-instance-operator-surface.md``
(updated as part of Slice 8) for the authority distinction between
server domain eligibility, Angular transient request state, Angular
presentation, and the host-process lifecycle that lives outside the
cockpit's authority.
"""

from __future__ import annotations

from typing import Literal

from app.schemas.live_runs import (
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
    ReadinessVector,
)
from app.services.operator_capability import evaluate_all_actions

# Server-side canonical input for broker connection state.  Composed by
# the router from the readiness ``broker_connection`` gate plus (when
# richer health is on the wire) the broker monitor's recovery overlay.
BrokerConnectionStateInput = Literal["connected", "disconnected", "degraded", "unknown"]


# ---------------------------------------------------------------------------
# host_process
# ---------------------------------------------------------------------------

_DAEMON_STATE_TO_HOST_PROCESS_STATE: dict[str, str] = {
    "running": "RUNNING",
    "stopping": "RUNNING",
    "idle": "STOPPED",
    "exited": "STOPPED",
    "unreachable": "UNKNOWN",
}

_HOST_PROCESS_NOTICE_BY_STATE: dict[str, str] = {
    "STOPPED": "Host process stopped. Start this instance from the host runner.",
    "CRASHED": "Host process crashed. Investigate logs before restarting from the host runner.",
    "STARTING": "Host process starting. The cockpit will pick up live actuation once it is up.",
    "UNKNOWN": "Host process state is unknown. The cockpit cannot confirm liveness from here.",
}


def _project_host_process(process: InstanceProcessView) -> OperatorSurfaceHostProcess:
    state = _DAEMON_STATE_TO_HOST_PROCESS_STATE.get(process.state, "UNKNOWN")
    notice = None if state == "RUNNING" else _HOST_PROCESS_NOTICE_BY_STATE.get(state)
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
# broker.safety_verdict
# ---------------------------------------------------------------------------


def _project_broker_safety(
    configured_mode: Literal["paper", "live"] | None,
    connection_state: BrokerConnectionStateInput | None,
) -> OperatorSurfaceBroker:
    if connection_state == "disconnected":
        return OperatorSurfaceBroker(safety_verdict="DISCONNECTED")
    if connection_state == "degraded":
        return OperatorSurfaceBroker(safety_verdict="DEGRADED")
    if connection_state == "connected" and configured_mode == "paper":
        return OperatorSurfaceBroker(safety_verdict="PAPER")
    if connection_state == "connected" and configured_mode == "live":
        return OperatorSurfaceBroker(safety_verdict="LIVE")
    return OperatorSurfaceBroker(safety_verdict="UNKNOWN")


# ---------------------------------------------------------------------------
# current_risk
# ---------------------------------------------------------------------------


def _derive_posture(owned_positions: dict[str, int]) -> str:
    # Zero-qty entries can linger in the engine's tally; ignore them.
    non_zero = {sym: qty for sym, qty in owned_positions.items() if qty != 0}
    if not non_zero:
        return "FLAT"
    sides = {("LONG" if qty > 0 else "SHORT") for qty in non_zero.values()}
    if len(sides) > 1:
        return "MIXED"
    return next(iter(sides))


def _project_current_risk(broker: InstanceBrokerView | None) -> OperatorSurfaceCurrentRisk:
    if broker is None:
        # Broker state unavailable -> posture UNKNOWN, pending null,
        # verdict UNKNOWN.  The cockpit reads these nulls explicitly
        # (see #612 §"Rendering rules").
        return OperatorSurfaceCurrentRisk(
            posture="UNKNOWN",
            pending_order_count=None,
            verdict="UNKNOWN",
            unrealized_pnl=None,
        )
    posture = _derive_posture(broker.owned_positions)
    pending = broker.pending_order_count
    # READY only when posture is FLAT AND pending is exactly 0 (known
    # empty, not null).  Anything else is ATTENTION.
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
    # Read the structured fields the engine sidecar now emits (#608
    # § "Engine sidecar precondition — structured cap fields").  The
    # gate prose ``"3 / 50 orders used"`` is no longer parsed.
    return OperatorSurfaceDailyOrderCap(used=readiness.orders_used, limit=readiness.orders_cap)


# ---------------------------------------------------------------------------
# action_plan
# ---------------------------------------------------------------------------


def _project_action_plan(action_plan: dict | None) -> OperatorSurfaceActionPlan:
    if action_plan is None:
        # Missing plan -> both fields UNKNOWN.  A missing plan is
        # evidence of nothing, not evidence of health (#608).
        return OperatorSurfaceActionPlan(consumption="UNKNOWN", anomaly_verdict="UNKNOWN")
    # Slice 4 (PRD #593) will flip consumption to ACTIVE and drive
    # anomaly_verdict from a real detector.  Until then, a present plan
    # is DECLARATIVE_ONLY and anomaly_verdict is READY.
    return OperatorSurfaceActionPlan(consumption="DECLARATIVE_ONLY", anomaly_verdict="READY")


# ---------------------------------------------------------------------------
# configuration rules
# ---------------------------------------------------------------------------


def _project_configuration(
    start_defaults: InstanceStartDefaults | None,
    sizing: InstanceSizing | None,
    instance_broker_self_consistent: bool | None,
) -> OperatorSurfaceConfiguration:
    """Apply the five named configuration rules (#608).

    Each failing rule appends one ``ALL_CAPS_SNAKE`` token to
    ``reason_codes``.  ``verdict`` is ``UNKNOWN`` when the inputs to
    every rule are themselves missing (nothing-deployed instance);
    ``ATTENTION`` when any rule fails; ``READY`` when none fail.
    """
    # Nothing deployed -> can't evaluate any rule; honest UNKNOWN.
    if start_defaults is None and sizing is None and instance_broker_self_consistent is None:
        return OperatorSurfaceConfiguration(verdict="UNKNOWN", reason_codes=[])

    reasons: list[str] = []

    # STRATEGY_KEY_MISSING — start_defaults.strategy is empty.
    if start_defaults is None or not (start_defaults.strategy or "").strip():
        reasons.append("STRATEGY_KEY_MISSING")

    # MAX_ORDERS_CAP_UNSET — start_defaults.max_orders_per_day is None
    # or non-positive.
    if (
        start_defaults is None
        or start_defaults.max_orders_per_day is None
        or start_defaults.max_orders_per_day <= 0
    ):
        reasons.append("MAX_ORDERS_CAP_UNSET")

    # SIZING_PRESET_MISSING — sizing.policy is None.
    if sizing is None or sizing.policy is None:
        reasons.append("SIZING_PRESET_MISSING")

    # SIZING_PROVENANCE_MISSING — sizing.sizing_provenance is missing
    # / unreadable.
    if sizing is None or not getattr(sizing, "sizing_provenance", None):
        reasons.append("SIZING_PROVENANCE_MISSING")

    # INSTANCE_BROKER_SELF_INCONSISTENT — the instance-broker
    # self-consistency gate (ADR-0005 / #398) is failing.  ``None``
    # means the gate could not be evaluated.
    if instance_broker_self_consistent is False:
        reasons.append("INSTANCE_BROKER_SELF_INCONSISTENT")

    verdict = "ATTENTION" if reasons else "READY"
    return OperatorSurfaceConfiguration(verdict=verdict, reason_codes=reasons)


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
) -> OperatorSurface:
    """Build the operator-surface projection for one instance.

    The function is intentionally pure: every input is a primitive value
    or an already-resolved view model.  The router does the source
    blending (settings, readiness gate, broker health, sidecars) and
    hands the result in.
    """

    owned_positions_empty = broker is None or not any(
        qty != 0 for qty in broker.owned_positions.values()
    )

    return OperatorSurface(
        host_process=_project_host_process(process),
        prior_run=_project_prior_run(last_exit),
        broker=_project_broker_safety(configured_mode, broker_connection_state),
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
    )
