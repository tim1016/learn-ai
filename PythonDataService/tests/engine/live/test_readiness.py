"""Tests for the readiness vector builder (ADR 0005)."""

from __future__ import annotations

from app.engine.live.readiness import (
    build_live_readiness,
    build_start_readiness,
    derive_verdict,
    gate,
)


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


def test_live_readiness_force_flat_blocks() -> None:
    assert _live(force_flat_active=True)["verdict"] == "BLOCKED"


def test_live_readiness_broker_disconnected_blocks() -> None:
    assert _live(broker_connected=False)["verdict"] == "BLOCKED"


def test_live_readiness_bar_source_fallback_is_degraded_soft() -> None:
    v = _live(bar_source="polygon_backfill", expected_bar_source="ibkr_realtime")
    dp = next(g for g in v["gates"] if g["name"] == "data_provenance")
    assert dp["status"] == "fail" and dp["severity"] == "soft"
    assert v["verdict"] == "DEGRADED"  # hard gates pass, soft warns


def test_start_readiness_stopped_blocks() -> None:
    v = build_start_readiness(
        as_of_ms=1, desired_state="STOPPED", poisoned=False, halted=False, reconcile_passed=True
    )
    assert v["kind"] == "start_readiness"
    assert v["source"] == "backend_derived"
    assert v["live_readiness_available"] is False
    assert v["verdict"] == "BLOCKED"


def test_start_readiness_running_clear_is_ready() -> None:
    v = build_start_readiness(
        as_of_ms=1, desired_state="RUNNING", poisoned=False, halted=False, reconcile_passed=True
    )
    assert v["verdict"] == "READY"


def test_start_readiness_poisoned_blocks() -> None:
    v = build_start_readiness(
        as_of_ms=1, desired_state="RUNNING", poisoned=True, halted=False, reconcile_passed=True
    )
    assert v["verdict"] == "BLOCKED"


def test_start_readiness_no_intent_is_degraded() -> None:
    v = build_start_readiness(
        as_of_ms=1, desired_state=None, poisoned=False, halted=False, reconcile_passed=None
    )
    assert v["verdict"] == "DEGRADED"
