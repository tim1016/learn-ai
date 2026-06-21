"""PRD #619-B B7 — backend freshness evaluator contract.

Asserts:

- Default thresholds (``RuntimeFreshnessConfig``) match the PRD §B
  defaults.
- command_loop: FRESH within 3s, STALE past 3s, demotes posture.
- broker probe: FRESH within 25s, UNKNOWN past 25s OR when probe time
  is None, demotes posture.
- bar_loop: threshold-based (no session_state) uses
  ``max(2 * expected_interval_ms, source_min_ms)``; closed session
  short-circuits to NOT_APPLICABLE; halted session → DEGRADED. The
  bar_loop status NEVER demotes posture (closed market is not a
  posture event).
- control_plane: FRESH within threshold, STALE past it, DEGRADED on
  boot_id mismatch even when fresh.
- Composition: posture_demoted is True iff command_loop OR broker OR
  control_plane signals it.
"""

from __future__ import annotations

import pytest

from app.engine.live.engine_runtime import (
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
    EngineRuntimeSnapshot,
)
from app.services.runtime_freshness import (
    RuntimeFreshnessConfig,
    evaluate_runtime_freshness,
)


def _snapshot(
    *,
    written_at_ms: int = 1_700_000_000_000,
    command_loop_heartbeat_ms: int | None = None,
    broker_probe_completed_ms: int | None = None,
    bar_loop_heartbeat_ms: int | None = None,
    latest_source_bar_ms: int | None = None,
    expected_interval_ms: int | None = 60_000,
    control_plane_lease_observed_ms: int | None = None,
    observed_daemon_boot_id: str | None = "daemon-001",
    expected_daemon_boot_id: str | None = "daemon-001",
) -> EngineRuntimeSnapshot:
    base = written_at_ms
    return EngineRuntimeSnapshot(
        strategy_instance_id="sid-1",
        run_id="run-1",
        pid=1234,
        process_start_identity="child-001",
        expected_daemon_boot_id=expected_daemon_boot_id,
        snapshot_seq=0,
        written_at_ms=base,
        command_loop=CommandLoopBlock(
            heartbeat_at_ms=command_loop_heartbeat_ms or base,
            state="RUNNING",
        ),
        broker=BrokerBlock(
            identity="PAPER_VERIFIED",
            submission_capability="PAPER_ORDERS_ENABLED",
            effective_posture="PAPER_EXECUTION",
            connection_state="connected",
            connection_epoch=1,
            connected_account="DU1234567",
            port_class="paper_port",
            observation_at_ms=base,
            probe_completed_at_ms=broker_probe_completed_ms,
            reconnect_attempt=0,
        ),
        bar_loop=BarLoopBlock(
            heartbeat_at_ms=bar_loop_heartbeat_ms or base,
            latest_source_bar_ms=latest_source_bar_ms,
            expected_interval_ms=expected_interval_ms,
        ),
        control_plane=ControlPlaneBlock(
            lease_observed_at_ms=control_plane_lease_observed_ms or base,
            observed_daemon_boot_id=observed_daemon_boot_id,
        ),
    )


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_default_config_matches_prd_thresholds() -> None:
    cfg = RuntimeFreshnessConfig()
    assert cfg.command_loop_stale_threshold_ms == 3_000
    assert cfg.broker_probe_stale_threshold_ms == 25_000
    assert cfg.bar_loop_source_min_ms == 30_000
    assert cfg.control_plane_stale_threshold_ms == 5_000


def test_config_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        RuntimeFreshnessConfig(unexpected_field=1)  # type: ignore[call-arg]


def test_config_rejects_zero_or_negative_thresholds() -> None:
    with pytest.raises(ValueError):
        RuntimeFreshnessConfig(command_loop_stale_threshold_ms=0)


# ---------------------------------------------------------------------------
# command_loop
# ---------------------------------------------------------------------------


def test_command_loop_fresh_within_threshold() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(written_at_ms=base, command_loop_heartbeat_ms=base)
    result = evaluate_runtime_freshness(snap, now_ms=base + 2_000)
    assert result.command_loop.state == "FRESH"


def test_command_loop_stale_past_threshold_demotes_posture() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(written_at_ms=base, command_loop_heartbeat_ms=base)
    result = evaluate_runtime_freshness(snap, now_ms=base + 3_001)
    assert result.command_loop.state == "STALE"
    assert "COMMAND_LOOP_STALE" in result.command_loop.stale_reason_codes
    assert result.posture_demoted is True


# ---------------------------------------------------------------------------
# broker probe
# ---------------------------------------------------------------------------


def test_broker_fresh_within_threshold() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(broker_probe_completed_ms=base)
    result = evaluate_runtime_freshness(snap, now_ms=base + 10_000)
    assert result.broker.state == "FRESH"


def test_broker_stale_past_threshold_is_unknown_and_demotes_posture() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(broker_probe_completed_ms=base)
    result = evaluate_runtime_freshness(snap, now_ms=base + 25_001)
    assert result.broker.state == "UNKNOWN"
    assert "BROKER_PROBE_STALE" in result.broker.stale_reason_codes
    assert result.posture_demoted is True


def test_broker_missing_probe_is_unknown() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(broker_probe_completed_ms=None)
    result = evaluate_runtime_freshness(snap, now_ms=base + 1)
    assert result.broker.state == "UNKNOWN"
    assert "BROKER_PROBE_MISSING" in result.broker.stale_reason_codes
    assert result.posture_demoted is True


# ---------------------------------------------------------------------------
# bar_loop — threshold-based + session-aware
# ---------------------------------------------------------------------------


def test_bar_loop_fresh_within_allowed_lag() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(
        bar_loop_heartbeat_ms=base,
        latest_source_bar_ms=base,
        expected_interval_ms=60_000,
    )
    # allowed lag = max(2 * 60_000, 30_000) = 120_000
    result = evaluate_runtime_freshness(snap, now_ms=base + 60_000)
    assert result.bar_loop.state == "FRESH"


def test_bar_loop_stale_past_allowed_lag() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(
        bar_loop_heartbeat_ms=base,
        latest_source_bar_ms=base,
        expected_interval_ms=60_000,
    )
    result = evaluate_runtime_freshness(snap, now_ms=base + 121_000)
    assert result.bar_loop.state == "STALE"
    assert "BAR_LOOP_HEARTBEAT_STALE" in result.bar_loop.stale_reason_codes


def test_bar_loop_stale_latest_bar_only() -> None:
    """Heartbeat fresh but the latest market data is old → STALE with
    ``BAR_LOOP_LATEST_BAR_STALE``."""
    base = 1_700_000_000_000
    snap = _snapshot(
        bar_loop_heartbeat_ms=base,
        latest_source_bar_ms=base - 200_000,
        expected_interval_ms=60_000,
    )
    result = evaluate_runtime_freshness(snap, now_ms=base)
    assert result.bar_loop.state == "STALE"
    assert "BAR_LOOP_LATEST_BAR_STALE" in result.bar_loop.stale_reason_codes


def test_bar_loop_allowed_lag_falls_back_to_source_min_when_interval_unknown() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(
        bar_loop_heartbeat_ms=base,
        latest_source_bar_ms=base,
        expected_interval_ms=None,
    )
    # allowed lag = max(0 * 2, 30_000) = 30_000
    result = evaluate_runtime_freshness(snap, now_ms=base + 30_001)
    assert result.bar_loop.state == "STALE"


def test_bar_loop_closed_session_is_not_applicable() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(
        bar_loop_heartbeat_ms=base - 999_999_999,
        broker_probe_completed_ms=base,  # avoid UNKNOWN-driven posture demotion
    )
    result = evaluate_runtime_freshness(
        snap, now_ms=base, session_state="CLOSED"
    )
    assert result.bar_loop.state == "NOT_APPLICABLE"
    assert "BAR_LOOP_SESSION_CLOSED" in result.bar_loop.stale_reason_codes
    # Closed market is not a posture event.
    assert result.posture_demoted is False


def test_bar_loop_halted_session_is_degraded() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(
        bar_loop_heartbeat_ms=base,
        broker_probe_completed_ms=base,  # isolate bar_loop signal
    )
    result = evaluate_runtime_freshness(
        snap, now_ms=base, session_state="HALTED"
    )
    assert result.bar_loop.state == "DEGRADED"
    assert "BAR_LOOP_SESSION_HALTED" in result.bar_loop.stale_reason_codes
    assert result.posture_demoted is False  # halt does not demote posture either


def test_bar_loop_stale_alone_does_not_demote_posture() -> None:
    """Posture demotion comes from command_loop / broker / control_plane
    only — a stale bar loop is informational."""
    base = 1_700_000_000_000
    snap = _snapshot(
        bar_loop_heartbeat_ms=base - 200_000,
        latest_source_bar_ms=base - 200_000,
        expected_interval_ms=60_000,
        broker_probe_completed_ms=base,
        command_loop_heartbeat_ms=base,
        control_plane_lease_observed_ms=base,
    )
    result = evaluate_runtime_freshness(snap, now_ms=base)
    assert result.bar_loop.state == "STALE"
    assert result.posture_demoted is False


# ---------------------------------------------------------------------------
# control_plane
# ---------------------------------------------------------------------------


def test_control_plane_fresh_within_threshold() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(control_plane_lease_observed_ms=base)
    result = evaluate_runtime_freshness(snap, now_ms=base + 3_000)
    assert result.control_plane.state == "FRESH"


def test_control_plane_stale_past_threshold_demotes_posture() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(control_plane_lease_observed_ms=base)
    result = evaluate_runtime_freshness(snap, now_ms=base + 5_001)
    assert result.control_plane.state == "STALE"
    assert "CONTROL_PLANE_LEASE_STALE" in result.control_plane.stale_reason_codes
    assert result.posture_demoted is True


def test_control_plane_boot_id_mismatch_is_degraded_and_demotes_posture() -> None:
    """Even a fresh observation is DEGRADED when the observed boot_id
    differs from expected."""
    base = 1_700_000_000_000
    snap = _snapshot(
        control_plane_lease_observed_ms=base,
        observed_daemon_boot_id="daemon-NEW",
        expected_daemon_boot_id="daemon-OLD",
    )
    result = evaluate_runtime_freshness(snap, now_ms=base + 100)
    assert result.control_plane.state == "DEGRADED"
    assert "CONTROL_PLANE_BOOT_ID_MISMATCH" in result.control_plane.stale_reason_codes
    assert result.posture_demoted is True


# ---------------------------------------------------------------------------
# Composition — all-fresh and multi-failure cases
# ---------------------------------------------------------------------------


def test_all_fresh_does_not_demote_posture() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(
        command_loop_heartbeat_ms=base,
        broker_probe_completed_ms=base,
        bar_loop_heartbeat_ms=base,
        latest_source_bar_ms=base,
        control_plane_lease_observed_ms=base,
    )
    result = evaluate_runtime_freshness(snap, now_ms=base + 100)
    assert result.command_loop.state == "FRESH"
    assert result.broker.state == "FRESH"
    assert result.bar_loop.state == "FRESH"
    assert result.control_plane.state == "FRESH"
    assert result.posture_demoted is False


def test_multiple_failures_demote_posture_once() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(
        command_loop_heartbeat_ms=base - 10_000,  # stale
        broker_probe_completed_ms=base - 30_000,  # stale
        bar_loop_heartbeat_ms=base,
        control_plane_lease_observed_ms=base - 10_000,  # stale
    )
    result = evaluate_runtime_freshness(snap, now_ms=base)
    assert result.command_loop.state == "STALE"
    assert result.broker.state == "UNKNOWN"
    assert result.control_plane.state == "STALE"
    assert result.posture_demoted is True


def test_custom_config_overrides_defaults() -> None:
    base = 1_700_000_000_000
    snap = _snapshot(command_loop_heartbeat_ms=base)
    # Tighter threshold: 500ms.
    cfg = RuntimeFreshnessConfig(command_loop_stale_threshold_ms=500)
    result = evaluate_runtime_freshness(snap, now_ms=base + 600, config=cfg)
    assert result.command_loop.state == "STALE"
