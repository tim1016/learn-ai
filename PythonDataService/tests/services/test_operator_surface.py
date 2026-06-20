"""Per-section unit tests for the ``operator_surface`` projection
(PRD #607 / Slice 1 / #608).

These tests exercise the pure-function projection directly with crafted
inputs so each section's mapping rules are pinned independently of the
REST endpoint integration tests in
``tests/routers/test_live_instances_operator_surface.py``.

Slice 1 cycles add per-section tests cumulatively; downstream slices
read the same module without changing it.
"""

from __future__ import annotations

import pytest

from app.schemas.live_runs import (
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


# ---------------------------------------------------------------------------
# Cycle 2 — host_process block mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("daemon_state", "expected_state", "expects_notice"),
    [
        ("running", "RUNNING", False),
        ("stopping", "RUNNING", False),  # still alive while shutting down
        ("idle", "STOPPED", True),
        ("exited", "STOPPED", True),
        ("unreachable", "UNKNOWN", True),
        ("nonsense", "UNKNOWN", True),  # unknown daemon state -> UNKNOWN, not crash
    ],
)
def test_host_process_state_mapping(
    daemon_state: str, expected_state: str, expects_notice: bool
) -> None:
    surface = compute_operator_surface(process=InstanceProcessView(state=daemon_state))

    assert surface.host_process.state == expected_state
    if expects_notice:
        assert isinstance(surface.host_process.notice, str)
        assert surface.host_process.notice
    else:
        assert surface.host_process.notice is None
    # Slice 1 contract: copyable_command is always None — the cockpit
    # must never receive a string the server can't safely author.
    assert surface.host_process.copyable_command is None


# ---------------------------------------------------------------------------
# Cycle 3 — prior_run.classification mapping
# ---------------------------------------------------------------------------


def _exit(**overrides: object) -> InstanceLastExit:
    base: dict = {"run_id": "run-x"}
    base.update(overrides)
    return InstanceLastExit(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("last_exit", "expected"),
    [
        # HALT_TRIGGERED wins over any other classification: a halted run
        # is the most operationally serious prior-run state.
        (_exit(halt_trigger="OUTSIDE_MUTATION", exit_code=1), "HALT_TRIGGERED"),
        (_exit(halt_trigger="OPERATOR_DECLARED"), "HALT_TRIGGERED"),
        # CLEAN: exit_code == 0 OR exit_reason == 'normal' AND no halt.
        (_exit(exit_code=0), "CLEAN"),
        (_exit(exit_reason="normal"), "CLEAN"),
        (_exit(exit_code=0, exit_reason="normal"), "CLEAN"),
        # EXITED_WITH_ERROR: exit_code present and non-zero, no halt.
        (_exit(exit_code=1), "EXITED_WITH_ERROR"),
        (_exit(exit_code=137), "EXITED_WITH_ERROR"),
        # No signal at all -> UNKNOWN.
        (_exit(), "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_prior_run_classification_mapping(
    last_exit: InstanceLastExit | None, expected: str
) -> None:
    surface = compute_operator_surface(process=_PROC, last_exit=last_exit)
    assert surface.prior_run.classification == expected


# ---------------------------------------------------------------------------
# Cycle 4 — broker.safety_verdict mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured_mode", "connection_state", "expected"),
    [
        # Healthy paper: connected + paper-mode env -> PAPER.
        ("paper", "connected", "PAPER"),
        # Healthy live: connected + live-mode env -> LIVE.
        ("live", "connected", "LIVE"),
        # Hard disconnect -> DISCONNECTED regardless of mode.
        ("paper", "disconnected", "DISCONNECTED"),
        ("live", "disconnected", "DISCONNECTED"),
        # Intermediate connection states (soft_lost, subscriptions_stale,
        # degraded_data_farm, reconnecting, recovering) collapse to DEGRADED.
        ("paper", "degraded", "DEGRADED"),
        ("live", "degraded", "DEGRADED"),
        # No connection signal at all -> UNKNOWN.
        ("paper", None, "UNKNOWN"),
        ("live", None, "UNKNOWN"),
        # No configured mode (settings unreadable) -> UNKNOWN even when
        # the connection looks fine; we cannot author a safe label.
        (None, "connected", "UNKNOWN"),
        (None, "disconnected", "DISCONNECTED"),  # disconnect always wins
        (None, None, "UNKNOWN"),
    ],
)
def test_broker_safety_verdict_mapping(
    configured_mode: str | None, connection_state: str | None, expected: str
) -> None:
    surface = compute_operator_surface(
        process=_PROC,
        configured_mode=configured_mode,  # type: ignore[arg-type]
        broker_connection_state=connection_state,  # type: ignore[arg-type]
    )
    assert surface.broker.safety_verdict == expected


# ---------------------------------------------------------------------------
# Cycle 5 — current_risk posture / pending / verdict / unrealized_pnl
# ---------------------------------------------------------------------------


def _broker(**overrides: object) -> InstanceBrokerView:
    base: dict = {"bot_order_namespace": "ns", "owned_positions": {}, "pending_order_count": 0}
    base.update(overrides)
    return InstanceBrokerView(**base)  # type: ignore[arg-type]


def test_current_risk_broker_none_renders_unknown_with_nulls() -> None:
    surface = compute_operator_surface(process=_PROC, broker=None)
    assert surface.current_risk.posture == "UNKNOWN"
    assert surface.current_risk.pending_order_count is None
    assert surface.current_risk.verdict == "UNKNOWN"
    assert surface.current_risk.unrealized_pnl is None


@pytest.mark.parametrize(
    ("owned", "expected_posture"),
    [
        ({}, "FLAT"),
        ({"SPY": 0}, "FLAT"),  # zero-qty entries ignored
        ({"SPY": 1}, "LONG"),
        ({"SPY": 10, "QQQ": 5}, "LONG"),
        ({"SPY": -3}, "SHORT"),
        ({"SPY": -1, "QQQ": -2}, "SHORT"),
        ({"SPY": 1, "QQQ": -1}, "MIXED"),
        ({"SPY": 0, "QQQ": -1}, "SHORT"),  # zero filtered before mixed-check
    ],
)
def test_current_risk_posture_derived_from_owned_positions(
    owned: dict[str, int], expected_posture: str
) -> None:
    surface = compute_operator_surface(process=_PROC, broker=_broker(owned_positions=owned))
    assert surface.current_risk.posture == expected_posture


@pytest.mark.parametrize(
    ("owned", "pending", "expected_verdict"),
    [
        ({}, 0, "READY"),  # FLAT and no pending -> READY
        ({}, 3, "ATTENTION"),  # FLAT but pending orders -> ATTENTION
        ({"SPY": 1}, 0, "ATTENTION"),  # held position -> ATTENTION
        ({"SPY": 1}, 2, "ATTENTION"),
    ],
)
def test_current_risk_verdict_rule(
    owned: dict[str, int], pending: int, expected_verdict: str
) -> None:
    surface = compute_operator_surface(
        process=_PROC, broker=_broker(owned_positions=owned, pending_order_count=pending)
    )
    assert surface.current_risk.verdict == expected_verdict


def test_current_risk_pending_order_count_zero_vs_null_distinction() -> None:
    # broker is None -> pending is null (not 0).
    assert compute_operator_surface(process=_PROC, broker=None).current_risk.pending_order_count is None
    # broker is present with explicit zero -> 0, not null.
    surface = compute_operator_surface(process=_PROC, broker=_broker(pending_order_count=0))
    assert surface.current_risk.pending_order_count == 0


def test_current_risk_unrealized_pnl_passes_through_broker_field() -> None:
    surface = compute_operator_surface(
        process=_PROC, broker=_broker(unrealized_pnl=-1234.56)
    )
    assert surface.current_risk.unrealized_pnl == -1234.56


# ---------------------------------------------------------------------------
# Cycle 6 — daily_order_cap reads structured fields, not gate prose
# ---------------------------------------------------------------------------


def _readiness(**overrides: object) -> ReadinessVector:
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
    surface = compute_operator_surface(process=_PROC, readiness=None)
    assert surface.daily_order_cap.used is None
    assert surface.daily_order_cap.limit is None


def test_daily_order_cap_reads_structured_fields_not_prose() -> None:
    # The gate detail prose disagrees with the structured fields on
    # purpose; the projection must read the structured fields.
    readiness = _readiness(
        orders_used=7,
        orders_cap=50,
        gates=[
            ReadinessGate(name="orders_cap", status="pass", severity="hard", detail="LIES 99 / 1"),
        ],
    )
    surface = compute_operator_surface(process=_PROC, readiness=readiness)
    assert surface.daily_order_cap.used == 7
    assert surface.daily_order_cap.limit == 50


def test_daily_order_cap_null_when_engine_did_not_emit_structured_fields() -> None:
    # Older / start_readiness vectors have neither field set.
    surface = compute_operator_surface(process=_PROC, readiness=_readiness())
    assert surface.daily_order_cap.used is None
    assert surface.daily_order_cap.limit is None


# ---------------------------------------------------------------------------
# Cycle 7 — action_plan consumption / anomaly_verdict
# ---------------------------------------------------------------------------


def test_action_plan_null_yields_unknown_unknown() -> None:
    surface = compute_operator_surface(process=_PROC, action_plan=None)
    assert surface.action_plan.consumption == "UNKNOWN"
    assert surface.action_plan.anomaly_verdict == "UNKNOWN"


def test_action_plan_present_yields_declarative_only_ready() -> None:
    surface = compute_operator_surface(process=_PROC, action_plan={"version": 1, "legs": []})
    assert surface.action_plan.consumption == "DECLARATIVE_ONLY"
    assert surface.action_plan.anomaly_verdict == "READY"


# ---------------------------------------------------------------------------
# Cycle 8 — configuration verdict + 5 named rules
# ---------------------------------------------------------------------------


def _start_defaults(**overrides: object) -> InstanceStartDefaults:
    base: dict = {
        "strategy": "spy_ema",
        "readonly": False,
        "hydrate_policy": "optional",
        "max_orders_per_day": 50,
        "ibkr_host": "host",
    }
    base.update(overrides)
    return InstanceStartDefaults(**base)  # type: ignore[arg-type]


def _sizing(**overrides: object) -> InstanceSizing:
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
    surface = compute_operator_surface(
        process=_PROC,
        start_defaults=None,
        sizing=None,
        instance_broker_self_consistent=None,
    )
    assert surface.configuration.verdict == "UNKNOWN"
    assert surface.configuration.reason_codes == []


def test_configuration_all_rules_pass_is_ready() -> None:
    surface = compute_operator_surface(
        process=_PROC,
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
    start_kwargs: dict, sizing_kwargs: dict, self_consistent: bool, expected_codes: set[str]
) -> None:
    surface = compute_operator_surface(
        process=_PROC,
        start_defaults=_start_defaults(**start_kwargs),
        sizing=_sizing(**sizing_kwargs),
        instance_broker_self_consistent=self_consistent,
    )
    assert surface.configuration.verdict == "ATTENTION"
    assert expected_codes.issubset(set(surface.configuration.reason_codes))


def test_configuration_sizing_entirely_missing_flags_both_sizing_codes() -> None:
    # ``InstanceSizing.sizing_provenance`` is non-nullable in the wire
    # schema, so the SIZING_PROVENANCE_MISSING rule can only fire when
    # the entire sizing block is absent (nothing deployed / pre-policy
    # run).  Sized together with SIZING_PRESET_MISSING in that path.
    surface = compute_operator_surface(
        process=_PROC,
        start_defaults=_start_defaults(),
        sizing=None,
        instance_broker_self_consistent=True,
    )
    assert surface.configuration.verdict == "ATTENTION"
    assert {"SIZING_PRESET_MISSING", "SIZING_PROVENANCE_MISSING"}.issubset(
        set(surface.configuration.reason_codes)
    )


# ---------------------------------------------------------------------------
# Cycle 9 — actions.* shape, effect discriminator, reason codes
# ---------------------------------------------------------------------------


def test_resume_pause_always_enabled_regardless_of_binding() -> None:
    no_binding = compute_operator_surface(process=_IDLE_PROC, live_binding=None)
    bound = compute_operator_surface(process=_PROC, live_binding=_LIVE)
    for surface in (no_binding, bound):
        assert surface.actions.resume.enabled is True
        assert surface.actions.resume.disabled_reason_code is None
        assert surface.actions.pause.enabled is True
        assert surface.actions.pause.disabled_reason_code is None


def test_resume_pause_effect_discriminator_flips_with_binding_and_state() -> None:
    # No binding -> durable only for both.
    no_binding = compute_operator_surface(process=_IDLE_PROC, live_binding=None)
    assert no_binding.actions.resume.effect == "DURABLE_ONLY"
    assert no_binding.actions.pause.effect == "DURABLE_ONLY"
    # Binding present + daemon RUNNING -> live actuation for both.
    bound = compute_operator_surface(process=_PROC, live_binding=_LIVE)
    assert bound.actions.resume.effect == "LIVE_ACTUATION"
    assert bound.actions.pause.effect == "LIVE_ACTUATION"
    # Binding present but daemon idle -> durable only (process not running).
    bound_idle = compute_operator_surface(process=_IDLE_PROC, live_binding=_LIVE)
    assert bound_idle.actions.resume.effect == "DURABLE_ONLY"


def test_flatten_and_pause_requires_live_binding() -> None:
    no_binding = compute_operator_surface(process=_PROC, live_binding=None, broker=_broker(owned_positions={"SPY": 1}))
    assert no_binding.actions.flatten_and_pause.enabled is False
    assert no_binding.actions.flatten_and_pause.disabled_reason_code == "NO_LIVE_BINDING"
    bound = compute_operator_surface(process=_PROC, live_binding=_LIVE, broker=_broker(owned_positions={"SPY": 1}))
    assert bound.actions.flatten_and_pause.enabled is True
    assert bound.actions.flatten_and_pause.effect == "LIVE_ACTUATION"


def test_flatten_and_pause_disabled_when_no_owned_positions() -> None:
    surface = compute_operator_surface(process=_PROC, live_binding=_LIVE, broker=_broker(owned_positions={}))
    assert surface.actions.flatten_and_pause.enabled is False
    assert surface.actions.flatten_and_pause.disabled_reason_code == "NO_OWNED_POSITIONS"


def test_mark_poisoned_rejects_without_binding() -> None:
    surface = compute_operator_surface(process=_PROC, live_binding=None)
    assert surface.actions.mark_poisoned.enabled is False
    assert surface.actions.mark_poisoned.disabled_reason_code == "NO_LIVE_BINDING"


def test_mark_poisoned_rejects_when_already_poisoned() -> None:
    surface = compute_operator_surface(process=_PROC, live_binding=_LIVE, poisoned=True)
    assert surface.actions.mark_poisoned.enabled is False
    assert surface.actions.mark_poisoned.disabled_reason_code == "ALREADY_POISONED"


def test_mark_poisoned_enabled_when_bound_and_not_poisoned() -> None:
    surface = compute_operator_surface(process=_PROC, live_binding=_LIVE, poisoned=False)
    assert surface.actions.mark_poisoned.enabled is True
    assert surface.actions.mark_poisoned.disabled_reason_code is None


# ---------------------------------------------------------------------------
# Cycle 10 — reason-code vocabulary closure
# ---------------------------------------------------------------------------


def test_reason_code_vocabulary_excludes_removed_codes() -> None:
    # #608 explicitly removes these tokens.  Regression-guard the closure.
    assert "BUSY_VERB_IN_FLIGHT" not in REASON_CODES
    assert "ALREADY_RUNNING" not in REASON_CODES
    assert "NOT_RUNNING" not in REASON_CODES


def test_reason_code_vocabulary_lists_documented_codes() -> None:
    documented = {
        "NO_LIVE_BINDING",
        "SAFETY_BLOCK_HALT",
        "RECONCILE_NOT_WIRED",
        "NO_OWNED_POSITIONS",
        "ALREADY_POISONED",
    }
    assert documented.issubset(REASON_CODES)


def test_evaluator_only_emits_codes_in_the_documented_vocabulary() -> None:
    # Sweep the input space we care about and collect every emitted code.
    emitted: set[str] = set()
    for action in ("resume", "pause", "flatten_and_pause", "mark_poisoned"):
        for binding in (None, _LIVE):
            for poisoned in (False, True):
                for owned_empty in (True, False):
                    cap = evaluate_action(
                        action,  # type: ignore[arg-type]
                        process=_PROC,
                        live_binding=binding,
                        poisoned=poisoned,
                        owned_positions_empty=owned_empty,
                    )
                    if cap.disabled_reason_code is not None:
                        emitted.add(cap.disabled_reason_code)
    assert emitted.issubset(REASON_CODES), f"orphan codes emitted: {emitted - REASON_CODES}"
