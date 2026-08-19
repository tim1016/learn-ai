"""Trade-level parity for EMA Crossover 2 bps against a pinned LEAN run."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction
from app.engine.execution.sizing import LeanSetHoldingsSizing
from app.engine.strategy.algorithms.ema_crossover_2_bps import EmaCrossover2BpsAlgorithm
from app.engine.strategy.spec import SpecAlgorithm, load_spec_from_path
from app.services.spec_strategy_runner import InMemoryDataReader

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures/golden/strategy-parity/ENG-007/v1"
)
SPEC_PATH = (
    Path(__file__).resolve().parents[3]
    / "app/engine/strategy/spec/fixtures/ema_crossover_2_bps.spec.json"
)
PRICE_ATOL = Decimal("0.000001")
PNL_ATOL = Decimal("0.000001")
_NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize("implementation", ["hand_coded", "strategy_spec"])
def test_ema_crossover_2_bps_matches_lean_fills_and_equity(implementation: str) -> None:
    inputs = pa.ipc.open_file(FIXTURE_DIR / "input.arrow").read_all().to_pylist()
    expected = _read_json(FIXTURE_DIR / "output.json")
    strategy = _strategy(implementation)
    fee_model = IbkrEquityCommissionModel()
    result = BacktestEngine(
        data_source=InMemoryDataReader(_bars(inputs)),  # type: ignore[arg-type]
        sizing_model=LeanSetHoldingsSizing(fee_model=fee_model),
        fill_model=FillModel(
            fee_model=fee_model,
            fill_stale_signal_at_current_open=True,
        ),
    ).run(strategy)

    assert len(result.order_events) == len(expected["filled_order_events"]) == 6
    price_errors: list[Decimal] = []
    for actual, oracle in zip(result.order_events, expected["filled_order_events"], strict=True):
        assert actual.filled_at_ms == oracle["ms_utc"]
        assert _direction(actual.direction) == oracle["direction"]
        assert actual.fill_quantity == oracle["fill_quantity"]
        assert actual.fee == Decimal(oracle["order_fee"])
        price_errors.append(abs(actual.fill_price - Decimal(oracle["fill_price"])))

    assert max(price_errors) <= PRICE_ATOL
    assert abs(result.final_equity - Decimal(expected["final_equity"])) <= PNL_ATOL


def _strategy(implementation: str) -> EmaCrossover2BpsAlgorithm | SpecAlgorithm:
    if implementation == "hand_coded":
        base = EmaCrossover2BpsAlgorithm(symbol="SPY")
        base_class = EmaCrossover2BpsAlgorithm
    else:
        spec = load_spec_from_path(SPEC_PATH)
        base = SpecAlgorithm(spec)
        base_class = SpecAlgorithm

    class _PinnedWindow(base_class):  # type: ignore[misc,valid-type]
        def initialize(self) -> None:
            super().initialize()
            self.start_date = datetime(2026, 2, 17, tzinfo=_NY)
            self.end_date = datetime(2026, 2, 26, 23, 59, 59, tzinfo=_NY)
            self.initial_cash = Decimal("100000")

    pinned = _PinnedWindow.__new__(_PinnedWindow)
    pinned.__dict__.update(base.__dict__)
    return pinned


def _bars(rows: list[dict]) -> list[TradeBar]:
    return [
        TradeBar(
            symbol=row["symbol"],
            time=datetime.fromtimestamp(row["time_ms_utc"] / 1_000, tz=_NY),
            end_time=datetime.fromtimestamp(row["end_time_ms_utc"] / 1_000, tz=_NY),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
        )
        for row in rows
    ]


def _direction(direction: Direction) -> str:
    if direction is Direction.LONG:
        return "buy"
    if direction is Direction.SHORT:
        return "sell"
    raise AssertionError(f"unexpected flat fill in long-only fixture: {direction}")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
