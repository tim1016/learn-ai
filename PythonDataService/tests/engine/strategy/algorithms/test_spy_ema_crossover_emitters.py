"""Engine-Lab EMA crossover MUST emit observations.csv + state.csv with
the same column schema as the LEAN trusted sample, at full Decimal precision."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.algorithms.spy_ema_crossover import (
    SpyEmaCrossoverAlgorithm,
)
from app.engine.strategy.base import StrategyContext

NY = ZoneInfo("America/New_York")


def _spy_minute(
    dt: datetime,
    *,
    open_: str = "500.0",
    high: str = "501.0",
    low: str = "499.0",
    close: str = "500.5",
) -> TradeBar:
    return TradeBar(
        symbol="SPY",
        time=dt,
        end_time=dt + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000_000,
    )


class _SyntheticStream:
    """data_source.iter_bars contract: returns a fresh iterator each call."""

    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start_date: date, end_date: date) -> Iterator[TradeBar]:
        return iter(self._bars)


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


def test_observations_csv_row_count_matches_minute_bar_input(tmp_path: Path) -> None:
    """Feed N minute bars through the engine; observations.csv must have
    exactly N data rows (header excluded).

    This guards against the C1 bug where a 1-minute passthrough consolidator
    silently drops the last bar of each session because TradeBarConsolidator
    only flushes the working bar when the *next* bar arrives with a time gap
    >= the period — the session-close bar has no successor.
    """
    d = date(2026, 1, 6)
    # 30 consecutive minute bars starting at 09:30 ET — one trading session
    # sub-span, all within the same day.
    bars = [_spy_minute(datetime(d.year, d.month, d.day, 9, 30 + i, tzinfo=NY)) for i in range(30)]
    n_bars = len(bars)

    strategy = SpyEmaCrossoverAlgorithm(symbol="SPY", output_dir=tmp_path)
    # _SyntheticStream.iter_bars ignores the date range; the strategy's
    # initialize() sets a 2-year default range which is fine here.
    engine = BacktestEngine(data_source=_SyntheticStream(bars))
    engine.run(strategy)

    obs_path = tmp_path / "observations.csv"
    assert obs_path.exists()
    with obs_path.open("r", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    # First row is the header; every subsequent row is one minute bar.
    assert len(rows) == n_bars + 1, (
        f"Expected {n_bars + 1} rows (1 header + {n_bars} data rows) "
        f"but got {len(rows)}. "
        "Likely C1 regression: session-close bar was dropped."
    )
    # Timestamps must be strictly increasing (no duplicates, no gaps).
    ts_values = [int(r[0]) for r in rows[1:]]
    assert ts_values == sorted(set(ts_values)), "Timestamps in observations.csv are not strictly increasing"


def test_state_csv_emits_only_after_warmup(tmp_path: Path) -> None:
    """Feed enough bars to produce several consolidated 15-min bars; state.csv
    must have rows only after all three indicators are ready.

    RSI(14) needs 14 price deltas (15 close values) → 15 consolidated 15-min
    bars → 15 * 15 = 225 source minute bars for the first state.csv row.
    EMA(10) needs 10 samples (ready on bar 10).
    EMA(5) needs 5 samples (ready on bar 5).
    RSI(14) is the binding constraint: needs 15 consolidated bars.
    """
    d = date(2026, 1, 6)
    # Build 20 * 15 = 300 minute bars to produce 20 consolidated 15-min bars.
    # That is 5 bars past the RSI(14) warmup point.
    n_consolidated = 20
    bars: list[TradeBar] = []
    for i in range(n_consolidated * 15):
        start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY) + timedelta(minutes=i)
        bars.append(_spy_minute(start, close=str(Decimal("500") + i * Decimal("0.01"))))

    strategy = SpyEmaCrossoverAlgorithm(symbol="SPY", output_dir=tmp_path)
    # _SyntheticStream.iter_bars ignores the date range; the strategy's
    # initialize() sets a 2-year default range which is fine here.
    engine = BacktestEngine(data_source=_SyntheticStream(bars))
    engine.run(strategy)

    state_path = tmp_path / "state.csv"
    assert state_path.exists()
    with state_path.open("r", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    # Header + data rows.
    data_rows = rows[1:]
    # The 15-min consolidator fires a bar when the next input bar crosses
    # into a later period. At end-of-data the engine scans consolidators and
    # flushes the final *complete* bucket too (LEAN parity — LEAN emits that
    # bar as its data feed ends). For exactly N_CONSOLIDATED * 15 source
    # bars every bucket is a full period, so all N_CONSOLIDATED bars fire.
    #
    # RSI(14) binds at 15 consolidated bars: first state.csv row at
    # consolidated bar 15 (1-indexed). With 20 fired bars, that's bars
    # 15..20 = 6 rows.
    n_fired = n_consolidated  # incl. the final bucket, flushed by end-of-data scan
    rsi_ready_at = 15  # RSI(14) needs period + 1 = 15 samples
    expected_data_rows = max(0, n_fired - rsi_ready_at + 1)
    assert len(data_rows) == expected_data_rows, (
        f"Expected {expected_data_rows} state rows (warmup-gated, {n_fired} fired bars) but got {len(data_rows)}."
    )
