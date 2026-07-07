"""Tests for fleet/account contamination (ADR 0005, #399)."""

from __future__ import annotations

from app.engine.live.fleet import compute_fleet_contamination


def test_clean_when_net_equals_sum_of_expecteds() -> None:
    result = compute_fleet_contamination(
        net_positions={"SPY": 100, "QQQ": 50},
        explained_by_instance={"spy_ema": {"SPY": 100}, "spy_vwap": {"QQQ": 50}},
    )
    assert result["verdict"] == "clean"
    assert result["residual"] == {}
    assert result["explained_total"] == {"SPY": 100, "QQQ": 50}


def test_residual_flags_foreign_position() -> None:
    # account holds 137 SPY but managed instances only explain 100
    result = compute_fleet_contamination(
        net_positions={"SPY": 137},
        explained_by_instance={"spy_ema": {"SPY": 100}},
    )
    assert result["verdict"] == "contaminated"
    assert result["residual"] == {"SPY": 37}
    assert "SPY +37" in result["summary"]
    assert "Unmanaged broker position" in result["summary"]


def test_negative_residual_flags_stale_managed_position() -> None:
    result = compute_fleet_contamination(
        net_positions={},
        explained_by_instance={"old_bot": {"SPY": 1}},
    )

    assert result["verdict"] == "contaminated"
    assert result["residual"] == {"SPY": -1}
    assert "Managed bot artifacts overstate broker position" in result["summary"]
    assert "SPY -1" in result["summary"]


def test_mixed_residual_flags_position_evidence_disagreement() -> None:
    result = compute_fleet_contamination(
        net_positions={"SPY": 137, "QQQ": 10},
        explained_by_instance={"a": {"SPY": 100}, "b": {"QQQ": 50}},
    )

    assert result["verdict"] == "contaminated"
    assert result["residual"] == {"QQQ": -40, "SPY": 37}
    assert "position evidence disagree" in result["summary"]


def test_sibling_positions_are_explained_not_contamination() -> None:
    # two managed instances on different symbols; neither is contamination
    result = compute_fleet_contamination(
        net_positions={"SPY": 100, "QQQ": 50},
        explained_by_instance={"a": {"SPY": 100}, "b": {"QQQ": 50}},
    )
    assert result["verdict"] == "clean"


def test_unknown_when_net_unavailable() -> None:
    result = compute_fleet_contamination(
        net_positions=None,
        explained_by_instance={"a": {"SPY": 100}},
    )
    assert result["verdict"] == "unknown"
    assert result["net_positions"] is None
    assert result["policy_blocks_starts"] is False


def test_policy_gate_blocks_only_when_enabled_and_contaminated() -> None:
    contaminated = compute_fleet_contamination(
        net_positions={"SPY": 137},
        explained_by_instance={"a": {"SPY": 100}},
        policy_blocks_starts=True,
    )
    assert contaminated["policy_blocks_starts"] is True

    clean = compute_fleet_contamination(
        net_positions={"SPY": 100},
        explained_by_instance={"a": {"SPY": 100}},
        policy_blocks_starts=True,
    )
    assert clean["policy_blocks_starts"] is False  # clean -> policy is moot
