"""Trade-level parity for EMA Previous Crossover against pinned LEAN runs (ENG-010).

The rule is the owner's (#2902) and has no outside reference, so the oracle
is LEAN's execution of the trusted template: LEAN consolidates the minute
bars, computes the EMAs and the RSI, applies the same one-entry-bar-per-cross
rule, and fills the orders. This pins that run so indicator, consolidation or
rule drift fails CI, and an edited template fails until LEAN is rerun.

The input is the committed cross-engine cell's own minute bars. The fixture
records their hash because the generator refused to write unless LEAN
reported receiving exactly those bars.

Accepted divergence -- dividends (owner, 2026-10-05). In raw mode LEAN pays a
cash dividend on a position held over an ex-date; the Python engine pays none
(#2903). The
SPY cell holds 157 shares over the 2026-03-20 ex-date, so LEAN's cash is
$282.60 higher from then on and some later orders are one share larger: a
QUANTITY_MISMATCH with a known cause. Nothing is loosened for it. The test
credits each dividend the fixture records to the engine's cash on the
ex-date's first minute bar and then requires every fill, fee and the final
equity to match exactly. The credit is necessary (without it the SPY cell diverges at
its 141st fill) and sufficient, so the dividend is the whole of the
difference. The QQQ cell collects no dividend and is credited nothing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine, pin_strategy_window
from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction
from app.engine.execution.sizing import LeanSetHoldingsSizing
from app.engine.strategy.base import Strategy
from app.engine.strategy.programs.ema_previous_crossover import (
    EmaPreviousCrossoverParams,
    build_ema_previous_crossover_program,
)
from app.lean_sidecar.trusted_samples.ema_previous_crossover import EMA_PREVIOUS_CROSSOVER_SOURCE
from app.services.spec_strategy_runner import InMemoryDataReader
from scripts.generate_signal_program_trace_corpus import _minute_bars

_SERVICE_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE_DIR = _SERVICE_ROOT / "tests/fixtures/golden/strategy-parity/ENG-010/v1"
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
    cell = _INPUT["cells"][cell_id]
    oracle = _OUTPUT["cells"][cell_id]
    observations = _SERVICE_ROOT / cell["observations_csv"]
    assert hashlib.sha256(observations.read_bytes()).hexdigest() == cell["observations_sha256"]

    strategy = build_ema_previous_crossover_program(
        EmaPreviousCrossoverParams(symbol=cell["ticker"], **_OUTPUT["parameters"])
    ).strategy
    pin_strategy_window(
        strategy,
        date.fromisoformat(cell["start_date"]),
        date.fromisoformat(cell["end_date"]),
        cash=Decimal(_INPUT["starting_cash"]),
    )
    _credit_lean_dividends(strategy, oracle["dividends"])
    fee_model = IbkrEquityCommissionModel()
    result = BacktestEngine(
        data_source=InMemoryDataReader(_minute_bars(observations.parent.parent, cell["ticker"])),  # type: ignore[arg-type]
        sizing_model=LeanSetHoldingsSizing(fee_model=fee_model),
        fill_model=FillModel(fee_model=fee_model, fill_stale_signal_at_current_open=True),
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


def _credit_lean_dividends(strategy: Strategy, dividends: list[dict]) -> None:
    """Pay the engine the cash LEAN paid: each dividend on the first minute bar after the close it was held through."""
    unpaid = sorted(dividends, key=lambda dividend: dividend["held_through_ms"])

    def on_minute_bar(bar: TradeBar) -> None:
        assert strategy.ctx is not None
        while unpaid and unpaid[0]["held_through_ms"] < bar.end_ms:
            strategy.ctx.portfolio.cash += Decimal(unpaid.pop(0)["cash"])

    strategy.on_minute_bar = on_minute_bar  # type: ignore[method-assign]


def _direction(direction: Direction) -> str:
    if direction is Direction.LONG:
        return "buy"
    if direction is Direction.SHORT:
        return "sell"
    raise AssertionError(f"unexpected flat fill in long-only fixture: {direction}")
