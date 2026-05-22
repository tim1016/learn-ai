"""Cell runner — three-gate orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.lean_sidecar.parity_matrix.cell_runner import (
    CellRunReport,
    run_cell_gates,
)


def _make_minimal_pinned_cell(d: Path) -> Path:
    """Create a minimal cell directory with valid pinned LEAN outputs."""
    cell = d / "cell"
    cell.mkdir()
    lean = cell / "lean"
    lean.mkdir()
    (lean / "observations.csv").write_text("ms_utc,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8")
    (lean / "state.csv").write_text(
        "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\n1,1,1,1,1,above,HOLD\n", encoding="utf-8"
    )
    (lean / "orders.json").write_text("[]", encoding="utf-8")
    return cell


def _make_matching_engine_outputs(d: Path) -> Path:
    eng = d / "engine"
    eng.mkdir()
    (eng / "observations.csv").write_text("ms_utc,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8")
    (eng / "state.csv").write_text(
        "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\n1,1,1,1,1,above,HOLD\n", encoding="utf-8"
    )
    return eng


def test_all_gates_pass(tmp_path: Path) -> None:
    pinned = _make_minimal_pinned_cell(tmp_path)
    eng = _make_matching_engine_outputs(tmp_path)
    report = run_cell_gates(
        pinned_lean_dir=pinned / "lean",
        engine_output_dir=eng,
        engine_normalized_orders=[],
    )
    assert isinstance(report, CellRunReport)
    assert report.overall_passed is True
    assert report.observations.passed is True
    assert report.state is not None and report.state.passed is True
    assert report.trade is not None  # Gate 3 ran (with 0 fills both sides)


def test_gate1_failure_short_circuits(tmp_path: Path) -> None:
    pinned = _make_minimal_pinned_cell(tmp_path)
    eng = _make_matching_engine_outputs(tmp_path)
    # Break observations on engine side: different ms_utc.
    (eng / "observations.csv").write_text("ms_utc,open,high,low,close,volume\n2,1,1,1,1,1\n", encoding="utf-8")
    report = run_cell_gates(
        pinned_lean_dir=pinned / "lean",
        engine_output_dir=eng,
        engine_normalized_orders=[],
    )
    assert report.overall_passed is False
    assert report.observations.passed is False
    assert report.state is None
    assert report.trade is None


def test_gate2_failure_skips_gate3(tmp_path: Path) -> None:
    pinned = _make_minimal_pinned_cell(tmp_path)
    eng = _make_matching_engine_outputs(tmp_path)
    # Identical observations, divergent state (rsi=2 vs pinned rsi=1).
    (eng / "state.csv").write_text(
        "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\n1,1,1,1,2,above,HOLD\n", encoding="utf-8"
    )
    report = run_cell_gates(
        pinned_lean_dir=pinned / "lean",
        engine_output_dir=eng,
        engine_normalized_orders=[],
    )
    assert report.overall_passed is False
    assert report.observations.passed is True
    assert report.state is not None and report.state.passed is False
    assert report.trade is None


def test_load_pinned_orders_with_invalid_payload(tmp_path: Path) -> None:
    """orders.json must be a JSON array — anything else raises."""
    pinned = _make_minimal_pinned_cell(tmp_path)
    (pinned / "lean" / "orders.json").write_text('{"orders": []}', encoding="utf-8")
    eng = _make_matching_engine_outputs(tmp_path)
    with pytest.raises(ValueError, match="must be a JSON array"):
        run_cell_gates(
            pinned_lean_dir=pinned / "lean",
            engine_output_dir=eng,
            engine_normalized_orders=[],
        )
