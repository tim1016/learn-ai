"""Trade-level parity for EMA Previous Crossover against pinned LEAN runs (ENG-010).

The rule is the owner's (#2902) and has no outside reference, so the oracle
is LEAN's execution of the trusted template: LEAN consolidates the minute
bars, computes the EMAs and the RSI, applies the same recent-cross rule, and
fills the orders. This pins that run so indicator, consolidation or
rule drift fails CI, and an edited template fails until LEAN is rerun.

The input is the committed cross-engine cell's own minute bars. The fixture
records their hash because the generator refused to write unless LEAN
reported receiving exactly those bars.

Dividends (#2903): in raw mode LEAN pays a cash dividend on a position held
over an ex-date, and the engine's LEAN-compatibility profile pays the same.
The run below derives its schedule from the capture's factor file — the same
input LEAN read — and requires it to carry every dividend the fixture
recorded LEAN paying. Every fill, fee and the final equity then match
exactly; that was necessary and sufficient when the credit was applied
test-side, so the dividend schedule is the whole of the engine's cash
difference. The QQQ cell collects no dividend on either side.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.dividends import dividends_from_factor_tree
from app.engine.engine import BacktestEngine, pin_strategy_window
from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction
from app.engine.execution.sizing import LeanSetHoldingsSizing
from app.engine.strategy.programs.ema_previous_crossover import (
    EmaPreviousCrossoverParams,
    build_ema_previous_crossover_program,
)
from app.lean_sidecar.parity_matrix.matrix import cell_by_id
from app.lean_sidecar.trusted_samples.ema_previous_crossover import EMA_PREVIOUS_CROSSOVER_SOURCE
from app.services.spec_strategy_runner import InMemoryDataReader
from scripts.generate_signal_program_trace_corpus import _minute_bars

_SERVICE_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE_DIR = _SERVICE_ROOT / "tests/fixtures/golden/strategy-parity/ENG-010/v1"
_CAPTURE_ROOT = _SERVICE_ROOT / "tests/fixtures/golden/cross-engine-studies/_lean_data_capture"
_INPUT = json.loads((_FIXTURE_DIR / "input.json").read_text(encoding="utf-8"))
_OUTPUT = json.loads((_FIXTURE_DIR / "output.json").read_text(encoding="utf-8"))
_CELL_IDS = sorted(_INPUT["cells"])

# Fill prices and final equity are Decimal on both sides and observed to
# agree exactly; the bound is the one every strategy-parity fixture states.
PRICE_ATOL = Decimal("0.000001")
PNL_ATOL = Decimal("0.000001")
# LEAN reports its own end equity to the cent.
LEAN_END_EQUITY_ATOL = Decimal("0.005")


def test_fixture_is_the_committed_twin_s_output() -> None:
    """The fills are only this template's while its source is the one LEAN ran."""
    assert hashlib.sha256(EMA_PREVIOUS_CROSSOVER_SOURCE.encode("utf-8")).hexdigest() == _OUTPUT["algorithm_source_sha256"]


@pytest.mark.parametrize("cell_id", _CELL_IDS)
def test_ema_previous_crossover_matches_lean_fills_and_equity(cell_id: str) -> None:
    cell = cell_by_id(cell_id)
    recorded = _INPUT["cells"][cell_id]
    oracle = _OUTPUT["cells"][cell_id]
    observations = _SERVICE_ROOT / recorded["observations_csv"]
    assert hashlib.sha256(observations.read_bytes()).hexdigest() == recorded["observations_sha256"]

    strategy = build_ema_previous_crossover_program(
        EmaPreviousCrossoverParams(symbol=cell.ticker, **_OUTPUT["parameters"])
    ).strategy
    pin_strategy_window(strategy, cell.start_date, cell.end_date, cash=Decimal(_INPUT["starting_cash"]))
    dividends = dividends_from_factor_tree(_CAPTURE_ROOT / cell.ticker, cell.ticker, cell.start_date, cell.end_date)
    # The fixture records only the dividends LEAN *paid* — it drops a row the
    # run held no shares through, as the QQQ cell does for its in-window row.
    # The engine's schedule carries every row and settles a flat position as
    # a no-op, so the receipt is containment: each dividend LEAN paid must be
    # in the schedule the engine reads, unchanged.
    derived = {(d.held_through_ms, str(d.per_share)) for d in dividends}
    assert {(dividend["held_through_ms"], dividend["per_share"]) for dividend in oracle["dividends"]} <= derived
    fee_model = IbkrEquityCommissionModel()
    result = BacktestEngine(
        data_source=InMemoryDataReader(_minute_bars(observations.parent.parent, cell.ticker)),  # type: ignore[arg-type]
        sizing_model=LeanSetHoldingsSizing(fee_model=fee_model),
        fill_model=FillModel(fee_model=fee_model, fill_stale_signal_at_current_open=True),
        dividends=dividends,
    ).run(strategy)

    oracle_fills = oracle["filled_order_events"]
    assert oracle_fills, "the cell exercises entries and exits"
    assert len(result.order_events) == len(oracle_fills)
    for actual, expected in zip(result.order_events, oracle_fills, strict=True):
        assert actual.filled_at_ms == expected["ms_utc"]
        assert _direction(actual.direction) == expected["direction"]
        assert actual.fill_quantity == expected["fill_quantity"]
        assert actual.fee == Decimal(expected["order_fee"])
        assert abs(actual.fill_price - Decimal(expected["fill_price"])) <= PRICE_ATOL
    assert abs(result.final_equity - Decimal(oracle["final_equity"])) <= PNL_ATOL
    assert abs(result.final_equity - Decimal(oracle["lean_reported_end_equity"])) <= LEAN_END_EQUITY_ATOL


def _direction(direction: Direction) -> str:
    if direction is Direction.LONG:
        return "buy"
    if direction is Direction.SHORT:
        return "sell"
    raise AssertionError(f"unexpected flat fill in long-only fixture: {direction}")
