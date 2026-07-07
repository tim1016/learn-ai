"""Tests for the readiness vector builder (ADR 0005)."""

from __future__ import annotations

from app.engine.live.readiness import (
    build_live_readiness,
    build_live_readiness_emission,
    build_start_readiness,
    derive_verdict,
    gate,
)
from app.schemas.bot_events import GateStepResult, SourceAuthority


def test_derive_verdict_rules() -> None:
    assert derive_verdict([]) == "UNKNOWN"
    assert derive_verdict([gate("a", "pass", "hard", "")]) == "READY"
    assert derive_verdict([gate("a", "fail", "hard", "")]) == "BLOCKED"
    assert derive_verdict([gate("a", "unknown", "hard", "")]) == "DEGRADED"
    assert derive_verdict([gate("a", "pass", "hard", ""), gate("b", "fail", "soft", "")]) == "DEGRADED"
    # a hard fail dominates a soft warn
    assert derive_verdict([gate("a", "fail", "hard", ""), gate("b", "fail", "soft", "")]) == "BLOCKED"


def _live(**overrides):
    base = dict(
        as_of_ms=1_700_000_000_000,
        paused=False,
        broker_connected=True,
        submit_mode="live_paper",
        orders_used=1,
        orders_cap=4,
        in_session=True,
        force_flat_active=False,
        poisoned=False,
        bar_source="ibkr_realtime",
        expected_bar_source="ibkr_realtime",
    )
    base.update(overrides)
    return build_live_readiness(**base)


def _live_emission(evaluation_id: str, **overrides):
    base = dict(
        as_of_ms=1_700_000_000_000,
        paused=False,
        broker_connected=True,
        submit_mode="live_paper",
        orders_used=1,
        orders_cap=4,
        in_session=True,
        force_flat_active=False,
        poisoned=False,
        bar_source="ibkr_realtime",
        expected_bar_source="ibkr_realtime",
        evaluation_id=evaluation_id,
    )
    base.update(overrides)
    return build_live_readiness_emission(**base)


def test_live_readiness_all_clear_is_ready() -> None:
    v = _live()
    assert v["kind"] == "live_readiness"
    assert v["source"] == "engine"
    assert v["verdict"] == "READY"


def test_live_readiness_paused_blocks() -> None:
    v = _live(paused=True)
    assert v["verdict"] == "BLOCKED"
    ds = next(g for g in v["gates"] if g["name"] == "desired_state")
    assert ds["status"] == "fail" and ds["detail"] == "PAUSED"


def test_live_readiness_order_cap_reached_blocks() -> None:
    v = _live(orders_used=4, orders_cap=4)
    assert v["verdict"] == "BLOCKED"
    assert any(g["name"] == "orders_cap" and g["status"] == "fail" for g in v["gates"])


def test_live_readiness_gate_steps_capture_hard_blocks() -> None:
    emission = _live_emission("eval-20260623T143100Z", orders_used=4, orders_cap=4)

    order_cap = next(step for step in emission.gate_steps if step.gate_id == "orders_cap")
    assert order_cap.evaluation_id == "eval-20260623T143100Z"
    assert order_cap.gate_result is GateStepResult.BLOCK
    assert order_cap.source_authority is SourceAuthority.ENGINE_LOOP
    assert order_cap.facts["readiness_status"] == "fail"
    assert order_cap.facts["readiness_severity"] == "hard"


def test_live_readiness_gate_steps_do_not_promote_soft_warnings_to_blocks() -> None:
    emission = _live_emission(
        "eval-soft-warning",
        bar_source="polygon_backfill",
        expected_bar_source="ibkr_realtime",
    )

    data_provenance = next(step for step in emission.gate_steps if step.gate_id == "data_provenance")
    assert data_provenance.gate_result is GateStepResult.SKIP
    assert data_provenance.facts["readiness_status"] == "fail"
    assert data_provenance.facts["readiness_severity"] == "soft"


def test_live_readiness_account_registry_gate_blocks_with_canonical_result() -> None:
    gate_result = {
        "gate_id": "account.instance_registry",
        "status": "block",
        "source": "account_instance_registry",
        "operator_reason": "ACCOUNT_REGISTRY_STALE_RUN",
        "operator_next_step": "STOP_STALE_RUNNER",
        "evidence_at_ms": 1_700_000_000_000,
    }

    v = _live(account_registry_gate_result=gate_result)

    assert v["verdict"] == "BLOCKED"
    registry_gate = next(g for g in v["gates"] if g["name"] == "account_instance_registry")
    assert registry_gate["status"] == "fail"
    assert registry_gate["gate_result"] == gate_result


def test_live_readiness_force_flat_blocks() -> None:
    assert _live(force_flat_active=True)["verdict"] == "BLOCKED"


def test_live_readiness_broker_disconnected_blocks() -> None:
    assert _live(broker_connected=False)["verdict"] == "BLOCKED"


def test_live_readiness_bar_source_fallback_is_degraded_soft() -> None:
    v = _live(bar_source="polygon_backfill", expected_bar_source="ibkr_realtime")
    dp = next(g for g in v["gates"] if g["name"] == "data_provenance")
    assert dp["status"] == "fail" and dp["severity"] == "soft"
    assert v["verdict"] == "DEGRADED"  # hard gates pass, soft warns


def test_live_readiness_emits_structured_orders_used_and_cap() -> None:
    """PRD #607 / Slice 1 (#608) — the engine sidecar surfaces
    ``orders_used`` and ``orders_cap`` as structured top-level fields
    alongside the existing ``orders_cap`` gate prose so the operator
    surface projection consumes integers, not parsed strings."""
    v = _live(orders_used=3, orders_cap=50)
    assert v["orders_used"] == 3
    assert v["orders_cap"] == 50


def test_live_readiness_emits_structured_fields_even_when_cap_is_none() -> None:
    """When no cap is configured the gate is omitted, but the
    structured fields still surface (``orders_cap`` is ``None``); the
    projection then renders ``DAILY CAP —`` rather than an absence."""
    v = _live(orders_used=2, orders_cap=None)
    assert v["orders_used"] == 2
    assert v["orders_cap"] is None
    assert not any(g["name"] == "orders_cap" for g in v["gates"])


def test_start_readiness_stopped_blocks() -> None:
    v = build_start_readiness(as_of_ms=1, desired_state="STOPPED", poisoned=False, halted=False, reconcile_passed=True)
    assert v["kind"] == "start_readiness"
    assert v["source"] == "backend_derived"
    assert v["live_readiness_available"] is False
    assert v["verdict"] == "BLOCKED"
    stopped = next(g for g in v["gates"] if g["name"] == "desired_state")
    assert stopped["gate_result"] == {
        "gate_id": "desired_state",
        "status": "block",
        "source": "backend_derived",
        "operator_reason": "STOPPED — start refused until intent changes",
        "operator_next_step": "STOPPED — start refused until intent changes",
        "evidence_at_ms": 1,
    }


def test_start_readiness_running_clear_is_ready() -> None:
    v = build_start_readiness(as_of_ms=1, desired_state="RUNNING", poisoned=False, halted=False, reconcile_passed=True)
    assert v["verdict"] == "READY"


def test_start_readiness_poisoned_blocks() -> None:
    v = build_start_readiness(as_of_ms=1, desired_state="RUNNING", poisoned=True, halted=False, reconcile_passed=True)
    assert v["verdict"] == "BLOCKED"


def test_start_readiness_no_intent_is_degraded() -> None:
    v = build_start_readiness(as_of_ms=1, desired_state=None, poisoned=False, halted=False, reconcile_passed=None)
    assert v["verdict"] == "DEGRADED"
