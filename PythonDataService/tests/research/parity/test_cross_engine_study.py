"""Cross-engine matrix parity test — Engine Lab live vs pinned LEAN.

Each cell loads pinned LEAN orders.json + state.csv + observations.csv,
runs Engine Lab live against the shared _lean_data_capture/<TICKER>/
data folder, and asserts all three gates pass via the cell runner.

Markers:
  * ``cross_engine_smoke`` — applied to W3mo and W6mo cells (8 of 16); runs on every PR.
  * ``slow``               — applied to W12mo and W24mo cells (8 of 16);
                              run pre-push / on-demand.

Until a cell is regenerated, its test skips with a
"fixture missing" message. That is the intended state of this test until
fixtures are pinned.

Reference: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.lean_sidecar.cross_runner import (
    CrossRunOrderEvent,
    run_engine_lab_on_workspace,
)
from app.lean_sidecar.parity_matrix.cell_runner import run_cell_gates
from app.lean_sidecar.parity_matrix.matrix import CELLS, Cell, WindowLabel

# tests/research/parity/test_cross_engine_study.py → parents[3] = PythonDataService/
FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "golden" / "cross-engine-studies"
STRATEGY_CLASS_NAME = "EmaCrossoverSignalAlgorithm"
INITIAL_CASH = Decimal(100000)


def _markers_for(cell: Cell) -> list:
    if cell.window_label in {WindowLabel.W3MO, WindowLabel.W6MO}:
        return [pytest.mark.cross_engine_smoke]
    return [pytest.mark.slow]


def _parametrize_cells() -> list:
    return [pytest.param(c, id=c.cell_id, marks=_markers_for(c)) for c in CELLS]


@pytest.mark.parametrize("cell", _parametrize_cells())
def test_cross_engine_cell(cell: Cell, tmp_path: Path) -> None:
    cell_dir = FIXTURE_ROOT / "cells" / cell.cell_id
    if not cell_dir.is_dir():
        pytest.skip(
            f"fixture missing — run `python scripts/regenerate_cross_engine_study.py --cell {cell.cell_id}` to generate"
        )

    pinned_lean_dir = cell_dir / "lean"
    if not pinned_lean_dir.is_dir():
        pytest.skip(
            f"pinned lean/ missing in {cell_dir} — cell directory exists but "
            f"lean/ is absent; re-run "
            f"`python scripts/regenerate_cross_engine_study.py "
            f"--cell {cell.cell_id}`"
        )

    capture = FIXTURE_ROOT / "_lean_data_capture" / cell.ticker
    if not capture.is_dir():
        pytest.skip(f"capture missing for {cell.ticker} — run the Polygon capture step")

    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    engine_orders = _run_engine_for_cell(cell, capture, engine_dir)

    report = run_cell_gates(
        pinned_lean_dir=pinned_lean_dir,
        engine_output_dir=engine_dir,
        engine_normalized_orders=engine_orders,
    )

    if not report.overall_passed:
        msg_lines = [f"Cell {cell.cell_id} failed parity:"]
        if not report.observations.passed:
            msg_lines.append(f"  Gate 1 (observations): {len(report.observations.failures)} failures")
            for f in report.observations.failures[:5]:
                msg_lines.append(f"    row={f.row_index} field={f.field}: {f.reason}")
        elif report.state is not None and not report.state.passed:
            # Reached only when Gate 1 passed (otherwise cell_runner sets state=None).
            msg_lines.append(f"  Gate 2 (state): {len(report.state.failures)} failures")
            for f in report.state.failures[:5]:
                msg_lines.append(f"    row={f.row_index} field={f.field}: {f.reason}")
        elif report.trade is not None and not report.trade.passed:
            # Reached only when Gates 1 and 2 passed.
            msg_lines.append(
                f"  Gate 3 (trade): {getattr(report.trade, 'gating_divergent_count', '?')} gating divergences"
            )
        pytest.fail("\n".join(msg_lines))


def _run_engine_for_cell(cell: Cell, capture: Path, output_dir: Path) -> list[CrossRunOrderEvent]:
    """Run Engine Lab for one cell against the shared capture.

    Wires app.lean_sidecar.cross_runner.run_engine_lab_on_workspace to
    point at the shared _lean_data_capture/<TICKER>/ directory, with
    the EmaCrossoverSignalAlgorithm strategy resolved by class name.

    ``output_dir`` is threaded to the strategy constructor so it emits
    observations.csv + state.csv for Gate 1 and Gate 2.
    """
    result = run_engine_lab_on_workspace(
        workspace_path=capture,
        strategy_class_name=STRATEGY_CLASS_NAME,
        symbol=cell.ticker,
        start_date=cell.start_date,
        end_date=cell.end_date,
        initial_cash=INITIAL_CASH,
        output_dir=output_dir,
    )
    return list(result.order_events)
