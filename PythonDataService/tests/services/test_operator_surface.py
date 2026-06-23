"""Per-section unit tests for the ``operator_surface`` projection
(PRD #607, cockpit revision 2026-06-21).

The cockpit-revision contract:

- ``host_process.state`` is one of ``RUNNING / STOPPING / EXITED / IDLE
  / WAITING_FOR_HOST / UNREACHABLE``.  ``IDLE`` is the daemon-reachable-
  but-no-subprocess case; it upgrades to ``WAITING_FOR_HOST`` when the
  operator's durable intent is ``RUNNING``.
- ``broker`` carries two independent enums: ``safety_verdict``
  (``PAPER_ONLY / UNSAFE / UNKNOWN``) and ``connection`` (``CONNECTED /
  DISCONNECTED / UNKNOWN``).  Composing them is forbidden.
- ``trading_session`` is server-authored (phase + permission +
  next-transition + timezone + as_of_ms).
"""

from __future__ import annotations

import pytest

from app.schemas.live_runs import (
    DesiredStateView,
    InstanceBrokerView,
    InstanceLastExit,
    InstanceProcessView,
    InstanceSizing,
    InstanceStartDefaults,
    LiveBinding,
    ReadinessGate,
    ReadinessVector,
)
from app.services.operator_capability import REASON_CODES, evaluate_action
from app.services.operator_surface import compute_operator_surface

_PROC = InstanceProcessView(state="running")
_IDLE_PROC = InstanceProcessView(state="idle")
_LIVE = LiveBinding(run_id="run-live-x")
_NOW_MS = 1_700_000_000_000


def _surface(**overrides):
    """Build a surface with sane defaults; tests override one section."""
    kwargs = {"process": _PROC, "now_ms": _NOW_MS}
    kwargs.update(overrides)
    return compute_operator_surface(**kwargs)


# ---------------------------------------------------------------------------
# host_process — 5 base states + WAITING_FOR_HOST derivation
# ---------------------------------------------------------------------------


def _desired(state: str | None) -> DesiredStateView | None:
    if state is None:
        return None
    return DesiredStateView(state=state, path_status="ok")


@pytest.mark.parametrize(
    ("daemon_state", "expected", "expects_notice"),
    [
        ("running", "RUNNING", False),
        ("stopping", "STOPPING", True),
        ("exited", "EXITED", True),
        ("idle", "IDLE", True),
        ("unreachable", "UNREACHABLE", True),
        ("nonsense", "UNREACHABLE", True),
    ],
)
def test_host_process_base_state_mapping(daemon_state: str, expected: str, expects_notice: bool) -> None:
    surface = _surface(process=InstanceProcessView(state=daemon_state))
    assert surface.host_process.state == expected
    if expects_notice:
        assert surface.host_process.notice and isinstance(surface.host_process.notice, str)
    else:
        assert surface.host_process.notice is None
    assert surface.host_process.copyable_command is None


def test_host_process_idle_plus_desired_running_becomes_waiting_for_host() -> None:
    surface = _surface(process=_IDLE_PROC, desired_state=_desired("RUNNING"))
    assert surface.host_process.state == "WAITING_FOR_HOST"
    assert surface.host_process.notice is not None
    assert "Intent is RUNNING" in surface.host_process.notice


def test_host_process_idle_without_desired_running_stays_idle() -> None:
    # No durable intent at all -> IDLE.
    surface = _surface(process=_IDLE_PROC, desired_state=None)
    assert surface.host_process.state == "IDLE"

    # Durable PAUSED -> IDLE (operator has not asked it to run).
    surface = _surface(process=_IDLE_PROC, desired_state=_desired("PAUSED"))
    assert surface.host_process.state == "IDLE"


def test_host_process_running_ignores_desired_state_override() -> None:
    # The state enum reflects DAEMON reality; desired-state only
    # upgrades IDLE -> WAITING_FOR_HOST, never overrides RUNNING.
    surface = _surface(process=_PROC, desired_state=_desired("PAUSED"))
    assert surface.host_process.state == "RUNNING"


# ---------------------------------------------------------------------------
# host_process — copyable_command (ADR 0013 amendment 2026-06-22)
# ---------------------------------------------------------------------------


_HOST_CMD = "./start-live-daemon.sh --background"


def test_host_process_unreachable_with_configured_command_emits_it() -> None:
    surface = _surface(
        process=InstanceProcessView(state="unreachable"),
        host_start_command=_HOST_CMD,
    )
    assert surface.host_process.state == "UNREACHABLE"
    assert surface.host_process.copyable_command == _HOST_CMD


def test_host_process_unreachable_without_configured_command_stays_none() -> None:
    # Empty string -> no safe command can be authored -> emit None and
    # let the cockpit fall back to a runbook remediation.
    surface = _surface(
        process=InstanceProcessView(state="unreachable"),
        host_start_command="",
    )
    assert surface.host_process.state == "UNREACHABLE"
    assert surface.host_process.copyable_command is None


def test_host_process_unreachable_with_none_command_stays_none() -> None:
    # Default (no setting passed) also produces None.
    surface = _surface(process=InstanceProcessView(state="unreachable"))
    assert surface.host_process.state == "UNREACHABLE"
    assert surface.host_process.copyable_command is None


@pytest.mark.parametrize(
    "daemon_state",
    ["running", "stopping", "exited", "idle"],
)
def test_host_process_non_unreachable_never_emits_daemon_command(daemon_state: str) -> None:
    # The daemon-start command must not leak outside UNREACHABLE — for
    # EXITED / IDLE / WAITING_FOR_HOST, restarting the host service does
    # not restart the per-bot subprocess and would mislead the trader.
    surface = _surface(
        process=InstanceProcessView(state=daemon_state),
        host_start_command=_HOST_CMD,
    )
    assert surface.host_process.state != "UNREACHABLE"
    assert surface.host_process.copyable_command is None


def test_host_process_waiting_for_host_never_emits_daemon_command() -> None:
    # IDLE + durable RUNNING -> WAITING_FOR_HOST; same rule applies.
    surface = _surface(
        process=_IDLE_PROC,
        desired_state=_desired("RUNNING"),
        host_start_command=_HOST_CMD,
    )
    assert surface.host_process.state == "WAITING_FOR_HOST"
    assert surface.host_process.copyable_command is None


# ---------------------------------------------------------------------------
# prior_run
# ---------------------------------------------------------------------------


def _exit(**overrides):
    base: dict = {"run_id": "run-x"}
    base.update(overrides)
    return InstanceLastExit(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("last_exit", "expected"),
    [
        (_exit(halt_trigger="OUTSIDE_MUTATION", exit_code=1), "HALT_TRIGGERED"),
        (_exit(halt_trigger="OPERATOR_DECLARED"), "HALT_TRIGGERED"),
        (_exit(exit_code=0), "CLEAN"),
        (_exit(exit_reason="normal"), "CLEAN"),
        (_exit(exit_code=0, exit_reason="normal"), "CLEAN"),
        (_exit(exit_code=1), "EXITED_WITH_ERROR"),
        (_exit(exit_code=137), "EXITED_WITH_ERROR"),
        (_exit(), "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_prior_run_classification_mapping(last_exit, expected) -> None:
    assert _surface(last_exit=last_exit).prior_run.classification == expected


# ---------------------------------------------------------------------------
# broker — safety_verdict and connection are INDEPENDENT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("safety_verdict_final", "expected_verdict"),
    [
        ("paper-only", "PAPER_ONLY"),
        ("unsafe", "UNSAFE"),
        ("unknown", "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_broker_safety_verdict_consumes_reactive_final_verdict(safety_verdict_final, expected_verdict) -> None:
    # PRD #616: the safety verdict now consumes ADR-0011's reactive
    # ``BrokerSafetyVerdict.final_verdict`` instead of ``configured_mode``,
    # so a mid-session degradation flips the cockpit's SAFETY pill
    # immediately.  Independent of connection state.
    for connection_state in ("connected", "disconnected", "unknown", None):
        surface = _surface(
            safety_verdict_final=safety_verdict_final,
            broker_connection_state=connection_state,
        )
        assert surface.broker.safety_verdict == expected_verdict


@pytest.mark.parametrize(
    ("connection_state", "expected_connection"),
    [
        ("connected", "CONNECTED"),
        ("disconnected", "DISCONNECTED"),
        ("degraded", "DISCONNECTED"),  # collapses until richer health channel
        ("unknown", "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_broker_connection_independent_of_safety(connection_state, expected_connection) -> None:
    for safety_verdict_final in ("paper-only", "unsafe", "unknown", None):
        surface = _surface(
            safety_verdict_final=safety_verdict_final,
            broker_connection_state=connection_state,
        )
        assert surface.broker.connection == expected_connection


# ---------------------------------------------------------------------------
# current_risk
# ---------------------------------------------------------------------------


def _broker(**overrides) -> InstanceBrokerView:
    base: dict = {"bot_order_namespace": "ns", "owned_positions": {}, "pending_order_count": 0}
    base.update(overrides)
    return InstanceBrokerView(**base)  # type: ignore[arg-type]


def test_current_risk_broker_none_renders_unknown_with_nulls() -> None:
    surface = _surface(broker=None)
    assert surface.current_risk.posture == "UNKNOWN"
    assert surface.current_risk.pending_order_count is None
    assert surface.current_risk.verdict == "UNKNOWN"
    assert surface.current_risk.unrealized_pnl is None


@pytest.mark.parametrize(
    ("owned", "expected_posture"),
    [
        ({}, "FLAT"),
        ({"SPY": 0}, "FLAT"),
        ({"SPY": 1}, "LONG"),
        ({"SPY": 10, "QQQ": 5}, "LONG"),
        ({"SPY": -3}, "SHORT"),
        ({"SPY": -1, "QQQ": -2}, "SHORT"),
        ({"SPY": 1, "QQQ": -1}, "MIXED"),
        ({"SPY": 0, "QQQ": -1}, "SHORT"),
    ],
)
def test_current_risk_posture_derived_from_owned_positions(owned, expected_posture) -> None:
    surface = _surface(broker=_broker(owned_positions=owned))
    assert surface.current_risk.posture == expected_posture


@pytest.mark.parametrize(
    ("owned", "pending", "expected_verdict"),
    [
        ({}, 0, "READY"),
        ({}, 3, "ATTENTION"),
        ({"SPY": 1}, 0, "ATTENTION"),
        ({"SPY": 1}, 2, "ATTENTION"),
    ],
)
def test_current_risk_verdict_rule(owned, pending, expected_verdict) -> None:
    surface = _surface(broker=_broker(owned_positions=owned, pending_order_count=pending))
    assert surface.current_risk.verdict == expected_verdict


def test_current_risk_pending_order_count_zero_vs_null_distinction() -> None:
    assert _surface(broker=None).current_risk.pending_order_count is None
    assert _surface(broker=_broker(pending_order_count=0)).current_risk.pending_order_count == 0


def test_current_risk_unrealized_pnl_passes_through_broker_field() -> None:
    surface = _surface(broker=_broker(unrealized_pnl=-1234.56))
    assert surface.current_risk.unrealized_pnl == -1234.56


# ---------------------------------------------------------------------------
# daily_order_cap
# ---------------------------------------------------------------------------


def _readiness(**overrides) -> ReadinessVector:
    base: dict = {
        "kind": "live_readiness",
        "as_of_ms": 0,
        "source": "engine",
        "verdict": "READY",
        "summary": "",
        "gates": [],
    }
    base.update(overrides)
    return ReadinessVector(**base)  # type: ignore[arg-type]


def test_daily_order_cap_null_when_no_readiness() -> None:
    surface = _surface(readiness=None)
    assert surface.daily_order_cap.used is None
    assert surface.daily_order_cap.limit is None


def test_daily_order_cap_reads_structured_fields_not_prose() -> None:
    readiness = _readiness(
        orders_used=7,
        orders_cap=50,
        gates=[
            ReadinessGate(name="orders_cap", status="pass", severity="hard", detail="LIES 99 / 1"),
        ],
    )
    surface = _surface(readiness=readiness)
    assert surface.daily_order_cap.used == 7
    assert surface.daily_order_cap.limit == 50


def test_daily_order_cap_null_when_engine_did_not_emit_structured_fields() -> None:
    surface = _surface(readiness=_readiness())
    assert surface.daily_order_cap.used is None
    assert surface.daily_order_cap.limit is None


# ---------------------------------------------------------------------------
# action_plan
# ---------------------------------------------------------------------------


def test_action_plan_null_yields_unknown_unknown() -> None:
    surface = _surface(action_plan=None)
    assert surface.action_plan.consumption == "UNKNOWN"
    assert surface.action_plan.anomaly_verdict == "UNKNOWN"


def test_action_plan_present_yields_declarative_only_ready() -> None:
    surface = _surface(action_plan={"version": 1, "legs": []})
    assert surface.action_plan.consumption == "DECLARATIVE_ONLY"
    assert surface.action_plan.anomaly_verdict == "READY"


# ---------------------------------------------------------------------------
# configuration verdict + 5 named rules
# ---------------------------------------------------------------------------


def _start_defaults(**overrides) -> InstanceStartDefaults:
    base: dict = {
        "strategy": "spy_ema",
        "readonly": False,
        "hydrate_policy": "optional",
        "max_orders_per_day": 50,
        "ibkr_host": "host",
    }
    base.update(overrides)
    return InstanceStartDefaults(**base)  # type: ignore[arg-type]


def _sizing(**overrides) -> InstanceSizing:
    base: dict = {
        "policy": {"kind": "fixed_shares", "value": 10},
        "preset": "explicit",
        "governed_by": "live_config",
        "sizing_provenance": "live_override",
        "per_trade_audit": [],
    }
    base.update(overrides)
    return InstanceSizing(**base)  # type: ignore[arg-type]


def test_configuration_nothing_deployed_is_unknown() -> None:
    surface = _surface(
        start_defaults=None,
        sizing=None,
        instance_broker_self_consistent=None,
    )
    assert surface.configuration.verdict == "UNKNOWN"
    assert surface.configuration.reason_codes == []


def test_configuration_all_rules_pass_is_ready() -> None:
    surface = _surface(
        start_defaults=_start_defaults(),
        sizing=_sizing(),
        instance_broker_self_consistent=True,
    )
    assert surface.configuration.verdict == "READY"
    assert surface.configuration.reason_codes == []


@pytest.mark.parametrize(
    ("start_kwargs", "sizing_kwargs", "self_consistent", "expected_codes"),
    [
        ({"strategy": ""}, {}, True, {"STRATEGY_KEY_MISSING"}),
        ({"strategy": "   "}, {}, True, {"STRATEGY_KEY_MISSING"}),
        ({"max_orders_per_day": 0}, {}, True, {"MAX_ORDERS_CAP_UNSET"}),
        ({"max_orders_per_day": -1}, {}, True, {"MAX_ORDERS_CAP_UNSET"}),
        ({}, {"policy": None}, True, {"SIZING_PRESET_MISSING"}),
        ({}, {}, False, {"INSTANCE_BROKER_SELF_INCONSISTENT"}),
    ],
)
def test_configuration_individual_rules_flag_their_codes(
    start_kwargs, sizing_kwargs, self_consistent, expected_codes
) -> None:
    surface = _surface(
        start_defaults=_start_defaults(**start_kwargs),
        sizing=_sizing(**sizing_kwargs),
        instance_broker_self_consistent=self_consistent,
    )
    assert surface.configuration.verdict == "ATTENTION"
    assert expected_codes.issubset(set(surface.configuration.reason_codes))


def test_configuration_sizing_entirely_missing_flags_both_sizing_codes() -> None:
    surface = _surface(
        start_defaults=_start_defaults(),
        sizing=None,
        instance_broker_self_consistent=True,
    )
    assert surface.configuration.verdict == "ATTENTION"
    assert {"SIZING_PRESET_MISSING", "SIZING_PROVENANCE_MISSING"}.issubset(set(surface.configuration.reason_codes))


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


def test_resume_pause_under_clean_guards_and_paused_intent() -> None:
    # PRD #616 — Resume/Pause now consult the shared ``ResumeGuardState``
    # resolver AND the intent-state pair rules.  Under the
    # nothing-ever-deployed default (empty guard state, no desired
    # state sidecar), Resume is refused with ``ALREADY_RUNNING``
    # because absence is the effective-RUNNING default; Pause is
    # permitted.
    for binding in (None, _LIVE):
        surface = _surface(live_binding=binding, desired_state=_desired("PAUSED"))
        assert surface.actions.resume.enabled is True
        assert surface.actions.resume.disabled_reason_code is None
        assert surface.actions.pause.enabled is False
        assert surface.actions.pause.disabled_reason_code == "ALREADY_PAUSED"


def test_resume_pause_effect_discriminator_flips_with_binding_and_state() -> None:
    no_binding = _surface(process=_IDLE_PROC, live_binding=None, desired_state=_desired("PAUSED"))
    assert no_binding.actions.resume.effect == "DURABLE_ONLY"
    assert no_binding.actions.pause.effect == "DURABLE_ONLY"
    bound = _surface(process=_PROC, live_binding=_LIVE, desired_state=_desired("PAUSED"))
    assert bound.actions.resume.effect == "LIVE_ACTUATION"
    assert bound.actions.pause.effect == "LIVE_ACTUATION"
    bound_idle = _surface(process=_IDLE_PROC, live_binding=_LIVE, desired_state=_desired("PAUSED"))
    assert bound_idle.actions.resume.effect == "DURABLE_ONLY"


def test_actions_stop_present_with_intent_state_rules() -> None:
    # PRD #616 — ``actions.stop`` is now on the operator surface.
    surface = _surface(desired_state=_desired("PAUSED"))
    assert surface.actions.stop.enabled is True

    stopped_surface = _surface(desired_state=_desired("STOPPED"))
    assert stopped_surface.actions.stop.enabled is False
    assert stopped_surface.actions.stop.disabled_reason_code == "ALREADY_STOPPED"


def test_flatten_and_pause_requires_live_binding() -> None:
    no_binding = _surface(live_binding=None, broker=_broker(owned_positions={"SPY": 1}))
    assert no_binding.actions.flatten_and_pause.enabled is False
    assert no_binding.actions.flatten_and_pause.disabled_reason_code == "NO_LIVE_BINDING"
    bound = _surface(live_binding=_LIVE, broker=_broker(owned_positions={"SPY": 1}))
    assert bound.actions.flatten_and_pause.enabled is True
    assert bound.actions.flatten_and_pause.effect == "LIVE_ACTUATION"


def test_flatten_and_pause_disabled_when_no_owned_positions() -> None:
    surface = _surface(live_binding=_LIVE, broker=_broker(owned_positions={}))
    assert surface.actions.flatten_and_pause.enabled is False
    assert surface.actions.flatten_and_pause.disabled_reason_code == "NO_OWNED_POSITIONS"


def test_mark_poisoned_rejects_without_binding() -> None:
    surface = _surface(live_binding=None)
    assert surface.actions.mark_poisoned.enabled is False
    assert surface.actions.mark_poisoned.disabled_reason_code == "NO_LIVE_BINDING"


def test_mark_poisoned_rejects_when_already_poisoned() -> None:
    surface = _surface(live_binding=_LIVE, poisoned=True)
    assert surface.actions.mark_poisoned.enabled is False
    assert surface.actions.mark_poisoned.disabled_reason_code == "ALREADY_POISONED"


def test_mark_poisoned_enabled_when_bound_and_not_poisoned() -> None:
    surface = _surface(live_binding=_LIVE, poisoned=False)
    assert surface.actions.mark_poisoned.enabled is True
    assert surface.actions.mark_poisoned.disabled_reason_code is None


# ---------------------------------------------------------------------------
# trading_session
# ---------------------------------------------------------------------------


def _ny_ms(year, month, day, hour, minute, second=0) -> int:
    """ms-since-epoch for the given America/New_York wall clock."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    dt = datetime(year, month, day, hour, minute, second, tzinfo=ny)
    return int(dt.timestamp() * 1000)


# Tuesday 2026-06-23 is an arbitrary RTH weekday.
_RTH_MID = _ny_ms(2026, 6, 23, 12, 0)  # 12:00 ET Tue
_PRE_MARKET = _ny_ms(2026, 6, 23, 6, 0)  # 06:00 ET Tue
_RTH_OPEN_EDGE = _ny_ms(2026, 6, 23, 9, 30)  # 09:30 ET Tue
_RTH_CLOSE_EDGE = _ny_ms(2026, 6, 23, 16, 0)  # 16:00 ET Tue
_POST_MARKET = _ny_ms(2026, 6, 23, 18, 0)  # 18:00 ET Tue
_OVERNIGHT = _ny_ms(2026, 6, 23, 2, 0)  # 02:00 ET Tue
_SATURDAY = _ny_ms(2026, 6, 27, 12, 0)  # noon Sat


@pytest.mark.parametrize(
    ("now_ms", "expected_phase", "expected_permits"),
    [
        (_OVERNIGHT, "CLOSED", False),
        (_PRE_MARKET, "PRE", False),
        (_RTH_OPEN_EDGE, "RTH", True),
        (_RTH_MID, "RTH", True),
        (_RTH_CLOSE_EDGE, "POST", False),  # at exactly 16:00 RTH has ended
        (_POST_MARKET, "POST", False),
        (_SATURDAY, "CLOSED", False),
    ],
)
def test_trading_session_phase_and_permission(now_ms, expected_phase, expected_permits) -> None:
    surface = _surface(now_ms=now_ms)
    assert surface.trading_session.phase == expected_phase
    assert surface.trading_session.permits_strategy_activity is expected_permits
    assert surface.trading_session.timezone == "America/New_York"
    assert surface.trading_session.as_of_ms == now_ms


def test_trading_session_next_transition_ms_overnight_points_to_pre_open() -> None:
    # PRD #616 — replace hard-coded None with the real next boundary.
    surface = _surface(now_ms=_OVERNIGHT)
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 23, 4, 0)


def test_trading_session_next_transition_ms_pre_market_points_to_rth_open() -> None:
    surface = _surface(now_ms=_PRE_MARKET)
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 23, 9, 30)


def test_trading_session_next_transition_ms_rth_points_to_close() -> None:
    surface = _surface(now_ms=_RTH_MID)
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 23, 16, 0)


def test_trading_session_next_transition_ms_post_points_to_close_of_post() -> None:
    surface = _surface(now_ms=_POST_MARKET)
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 23, 20, 0)


def test_trading_session_next_transition_ms_weekend_points_to_monday_open() -> None:
    # Saturday noon → next transition is Monday 04:00 NY.
    surface = _surface(now_ms=_SATURDAY)
    expected = _ny_ms(2026, 6, 29, 4, 0)
    assert surface.trading_session.next_transition_ms == expected


def test_trading_session_next_transition_ms_after_post_points_to_next_day() -> None:
    # 21:00 Tue ET → CLOSED, next boundary is Wed 04:00 ET.
    now = _ny_ms(2026, 6, 23, 21, 0)
    surface = _surface(now_ms=now)
    assert surface.trading_session.phase == "CLOSED"
    assert surface.trading_session.next_transition_ms == _ny_ms(2026, 6, 24, 4, 0)


# ---------------------------------------------------------------------------
# readiness_gates — OperatorGate projection
# ---------------------------------------------------------------------------


def test_readiness_gates_empty_when_no_readiness() -> None:
    assert _surface(readiness=None).readiness_gates == []


def test_readiness_gates_passing_gate_has_no_action_but_documented_unavailable_reason() -> None:
    surface = _surface(
        readiness=_readiness(
            gates=[
                ReadinessGate(
                    name="broker_connection",
                    status="pass",
                    severity="hard",
                    detail="connected",
                )
            ]
        )
    )
    assert len(surface.readiness_gates) == 1
    gate = surface.readiness_gates[0]
    assert gate.status == "pass"
    assert gate.suggested_action is None
    assert gate.suggested_action_unavailable_reason == "GATE_PASSING"


def test_readiness_gates_failing_gate_has_authored_action_or_unavailable_reason() -> None:
    # PRD #616 mandate: every non-passing gate either ships an action
    # OR ships ``null`` + a documented unavailable reason.
    failing_gates = [
        ReadinessGate(name=name, status="fail", severity="hard", detail="x")
        for name in (
            "broker_connection",
            "poison_sentinel",
            "fleet_contamination",
            "daily_order_cap",
            "warmup",
            "calendar",
            "session",
            "instrument_surface",
            "indicator_state_hydration",
            "spec_signature",
            "intent_wal_clean",
            "positions_self_consistent",
            "halt_clear",
            "totally_invented_gate",
        )
    ]
    surface = _surface(readiness=_readiness(gates=failing_gates))
    assert len(surface.readiness_gates) == len(failing_gates)
    for projected in surface.readiness_gates:
        if projected.suggested_action is None:
            assert projected.suggested_action_unavailable_reason is not None, projected.name
            assert projected.suggested_action_unavailable_reason != ""
        else:
            assert projected.suggested_action_unavailable_reason is None, projected.name


def test_readiness_gates_unknown_gate_name_surfaces_unavailable_reason() -> None:
    # An unknown gate name fails closed visibly — the cockpit shows
    # the raw name and the unavailable reason rather than guessing a
    # remediation.
    surface = _surface(
        readiness=_readiness(
            gates=[
                ReadinessGate(
                    name="totally_invented_gate",
                    status="fail",
                    severity="hard",
                    detail="",
                )
            ]
        )
    )
    g = surface.readiness_gates[0]
    assert g.suggested_action is None
    assert g.suggested_action_unavailable_reason == "UNKNOWN_GATE_NAME"


def test_readiness_gates_preserves_engine_order() -> None:
    names = ["calendar", "broker_connection", "warmup"]
    surface = _surface(
        readiness=_readiness(gates=[ReadinessGate(name=n, status="pass", severity="hard", detail="") for n in names])
    )
    assert [g.name for g in surface.readiness_gates] == names


# ---------------------------------------------------------------------------
# reason-code vocabulary closure
# ---------------------------------------------------------------------------


def test_reason_code_vocabulary_excludes_removed_codes() -> None:
    # PRD #616 — these legacy codes are removed from the closed
    # vocabulary in favour of the structured ADR-0011-aligned codes.
    assert "BUSY_VERB_IN_FLIGHT" not in REASON_CODES
    assert "NOT_RUNNING" not in REASON_CODES
    assert "SAFETY_BLOCK_HALT" not in REASON_CODES
    assert "RECONCILE_NOT_WIRED" not in REASON_CODES


def test_reason_code_vocabulary_lists_documented_codes() -> None:
    # PRD #616 — the closed vocabulary is the union of the legacy
    # live-binding codes plus every ResumeGuardState code; PRD #619-C5
    # added OUTCOME_UNKNOWN and #619-D added the four
    # MUTATION_UNRESOLVED_* matrix codes.
    documented = {
        "NO_LIVE_BINDING",
        "NO_OWNED_POSITIONS",
        "ALREADY_POISONED",
        "ALREADY_STOPPED",
        "POSTURE_DEMOTED",
        "OUTCOME_UNKNOWN",
        # ResumeGuardState (PRD #616) closed vocabulary.
        "BROKER_SAFETY_UNSAFE",
        "BROKER_SAFETY_UNKNOWN",
        "RECONCILIATION_FAILED",
        "RECONCILIATION_STALE",
        "RECONCILIATION_NOT_AVAILABLE",
        "RECONCILIATION_UNKNOWN",
        "UNRESOLVED_UNCERTAIN_INTENT",
        "UNCERTAIN_INTENT_STATE_UNKNOWN",
        "ALREADY_RUNNING",
        "ALREADY_PAUSED",
        "STOPPED_REQUIRES_REDEPLOY",
        "REDEPLOY_REQUIRED",
        # PRD #619-D action-conflict matrix codes.
        "MUTATION_UNRESOLVED_START",
        "MUTATION_UNRESOLVED_STOP",
        "MUTATION_UNRESOLVED_FLATTEN",
        "MUTATION_UNRESOLVED_RESUME",
    }
    assert documented.issubset(REASON_CODES)


# ---------------------------------------------------------------------------
# control_plane — PRD #619-C3
# ---------------------------------------------------------------------------


def _conn_state(
    kind: str,
    *,
    attempt: int = 0,
    last_transition_ms: int = _NOW_MS,
    last_success_ms: int | None = _NOW_MS,
    daemon_boot_id: str | None = "boot-A",
):
    from app.engine.live.daemon_connectivity_monitor import DaemonConnectivityState

    return DaemonConnectivityState(
        kind=kind,
        attempt=attempt,
        last_transition_ms=last_transition_ms,
        last_success_ms=last_success_ms,
        observed_daemon_boot_id=daemon_boot_id,
    )


def test_control_plane_none_when_no_monitor_installed() -> None:
    surface = _surface()

    assert surface.control_plane is None


def test_control_plane_connected_has_no_notice_or_runbook() -> None:
    surface = _surface(control_plane_state=_conn_state("CONNECTED"))

    assert surface.control_plane is not None
    assert surface.control_plane.state == "CONNECTED"
    assert surface.control_plane.notice is None
    assert surface.control_plane.runbook_slug is None
    assert surface.control_plane.daemon_boot_id == "boot-A"


@pytest.mark.parametrize(
    ("kind", "expected_runbook"),
    [
        ("RETRYING", "daemon-retrying"),
        ("UNREACHABLE", "daemon-unreachable"),
        ("AUTH_FAILED", "daemon-auth-failed"),
        ("PROTOCOL_ERROR", "daemon-protocol-error"),
        ("INCOMPATIBLE_CONTRACT", "daemon-incompatible-contract"),
    ],
)
def test_control_plane_unhealthy_kinds_carry_notice_and_runbook(
    kind: str, expected_runbook: str
) -> None:
    surface = _surface(control_plane_state=_conn_state(kind, attempt=2))

    assert surface.control_plane is not None
    assert surface.control_plane.state == kind
    assert surface.control_plane.notice is not None
    assert isinstance(surface.control_plane.notice, str)
    assert surface.control_plane.runbook_slug == expected_runbook


def test_control_plane_forwards_monitor_observability_fields() -> None:
    state = _conn_state(
        "RETRYING",
        attempt=3,
        last_transition_ms=_NOW_MS + 500,
        last_success_ms=_NOW_MS - 1_000,
        daemon_boot_id="boot-deadbeef",
    )

    surface = _surface(control_plane_state=state)

    cp = surface.control_plane
    assert cp is not None
    assert cp.attempt == 3
    assert cp.last_transition_ms == _NOW_MS + 500
    assert cp.last_success_ms == _NOW_MS - 1_000
    assert cp.daemon_boot_id == "boot-deadbeef"


def test_control_plane_carries_initial_no_success_state() -> None:
    # Monitor freshly started, no probe yet: kind=RETRYING, attempt=0,
    # last_success_ms=None.
    state = _conn_state(
        "RETRYING",
        attempt=0,
        last_transition_ms=_NOW_MS,
        last_success_ms=None,
        daemon_boot_id=None,
    )

    surface = _surface(control_plane_state=state)

    cp = surface.control_plane
    assert cp is not None
    assert cp.state == "RETRYING"
    assert cp.last_success_ms is None
    assert cp.daemon_boot_id is None
    assert cp.notice is not None  # retrying-class notice still authored


def test_evaluator_only_emits_codes_in_the_documented_vocabulary() -> None:
    emitted: set[str] = set()
    for action in ("resume", "pause", "stop", "flatten_and_pause", "mark_poisoned"):
        for binding in (None, _LIVE):
            for poisoned in (False, True):
                for owned_empty in (True, False):
                    for intent_state in (None, "RUNNING", "PAUSED", "STOPPED"):
                        cap = evaluate_action(
                            action,  # type: ignore[arg-type]
                            process=_PROC,
                            live_binding=binding,
                            poisoned=poisoned,
                            owned_positions_empty=owned_empty,
                            desired_state=_desired(intent_state),
                        )
                        if cap.disabled_reason_code is not None:
                            emitted.add(cap.disabled_reason_code)
                        emitted.update(cap.disabled_reasons)
    assert emitted.issubset(REASON_CODES), f"orphan codes emitted: {emitted - REASON_CODES}"
