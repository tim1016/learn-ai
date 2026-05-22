"""Engine-Lab EMA crossover MUST emit observations.csv + state.csv with
the same column schema as the LEAN trusted sample, at full Decimal precision."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.algorithms.spy_ema_crossover import (
    SpyEmaCrossoverAlgorithm,
)
from app.engine.strategy.base import StrategyContext


def test_constructor_accepts_output_dir(tmp_path: Path) -> None:
    s = SpyEmaCrossoverAlgorithm(symbol="SPY", output_dir=tmp_path)
    assert s._output_dir == tmp_path


def test_constructor_defaults_output_dir_to_none() -> None:
    s = SpyEmaCrossoverAlgorithm(symbol="SPY")
    assert s._output_dir is None


def test_initialize_creates_csvs_with_correct_headers(tmp_path: Path) -> None:
    """Stand-alone test: instantiate strategy + StrategyContext + initialize."""
    portfolio = Portfolio(initial_cash=Decimal("100000"))
    ctx = StrategyContext(portfolio=portfolio)
    s = SpyEmaCrossoverAlgorithm(symbol="SPY", output_dir=tmp_path)
    s.ctx = ctx
    s.initialize()

    obs_path = tmp_path / "observations.csv"
    state_path = tmp_path / "state.csv"
    assert obs_path.exists()
    assert state_path.exists()

    with obs_path.open("r", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == ["ms_utc", "open", "high", "low", "close", "volume"]

    with state_path.open("r", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == [
        "ts_ms_utc",
        "close",
        "ema_fast",
        "ema_slow",
        "rsi",
        "cross_state",
        "signal",
    ]

    # Close file handles deliberately so tmp_path cleanup doesn't warn.
    s.on_end_of_algorithm()


def test_no_csvs_when_output_dir_is_none() -> None:
    """Without output_dir, no files are created and emitter state stays None."""
    portfolio = Portfolio(initial_cash=Decimal("100000"))
    ctx = StrategyContext(portfolio=portfolio)
    s = SpyEmaCrossoverAlgorithm(symbol="SPY")
    s.ctx = ctx
    s.initialize()

    assert s._observations_writer is None
    assert s._state_writer is None


def test_on_end_of_algorithm_closes_handles(tmp_path: Path) -> None:
    """on_end_of_algorithm must close file handles and clear the references."""
    portfolio = Portfolio(initial_cash=Decimal("100000"))
    ctx = StrategyContext(portfolio=portfolio)
    s = SpyEmaCrossoverAlgorithm(symbol="SPY", output_dir=tmp_path)
    s.ctx = ctx
    s.initialize()

    assert s._observations_fp is not None
    assert s._state_fp is not None

    s.on_end_of_algorithm()

    assert s._observations_fp is None
    assert s._state_fp is None
