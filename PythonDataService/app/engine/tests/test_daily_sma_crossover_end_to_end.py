"""End-to-end proof: daily resolution flows all the way through the engine.

This is the Phase 2 Block F acceptance test. It exercises the full daily
pipeline:

    LeanDailyDataReader(reference mount)
        → BacktestEngine
            → TradeBarConsolidator(timedelta(days=1))  [pass-through]
                → SmaCrossoverAlgorithm
                    → LoggedTrade[]

We run ``SmaCrossoverAlgorithm`` with ``resolution_minutes=1440`` (the
same binding the registry uses for ``daily_sma_crossover``) against AAPL
from 2018-01-01 to 2021-03-31 with 10/30 windows chosen to produce a
handful of crosses in the available range.

The contract is: **exact trade-set determinism** against the LEAN
reference data. Because the reference data is bit-exact and the
strategy is deterministic, any regression in the reader, consolidator,
or strategy code will change the result. If you change the input data
deliberately (e.g. refresh the reference mount), re-run this file and
update ``EXPECTED_TRADES`` below.

One specific trade worth flagging: trade #9 (2020-04-16 entry, 2020-09-02
exit) shows a large LOSS because the LEAN reference data is *unadjusted*
and AAPL had a 4-for-1 split on 2020-08-31. The strategy correctly sees
this as a price crash and exits on the resulting "death cross". This is
an honest reflection of what happens when you backtest unadjusted data —
not a bug, and we keep it in the test so the artifact is documented.

Run with::

    cd PythonDataService
    python -m app.engine.tests.test_daily_sma_crossover_end_to_end
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.engine.data.lean_format import LeanDailyDataReader  # noqa: E402
from app.engine.engine import BacktestEngine  # noqa: E402
from app.engine.execution.fill_model import FillModel  # noqa: E402
from app.engine.execution.order import FillMode  # noqa: E402
from app.engine.strategy.algorithms.sma_crossover import (  # noqa: E402
    SmaCrossoverAlgorithm,
)

LEAN_DATA_ROOT = Path("/sessions/ecstatic-hopeful-volta/mnt/Lean/Data")

# Pinned against the reference mount's current AAPL daily zip. If the
# reference data is refreshed, re-run and paste the new expected values.
EXPECTED_TRADE_COUNT = 11
EXPECTED_WIN_COUNT = 5
EXPECTED_RESULTS = [
    "LOSS", "LOSS", "LOSS", "WIN", "LOSS",
    "WIN", "WIN", "WIN", "LOSS", "LOSS", "WIN",
]
# Entry / exit dates uniquely identify the trades even if prices drift.
EXPECTED_ENTRY_DATES = [
    date(2018, 2, 27), date(2018, 4, 19), date(2018, 5, 9),
    date(2018, 7, 17), date(2018, 10, 2),
    date(2019, 1, 30), date(2019, 6, 18), date(2019, 8, 22),
    date(2020, 4, 16), date(2020, 10, 13), date(2020, 11, 17),
]


def run_end_to_end() -> None:
    strategy = SmaCrossoverAlgorithm(
        symbol="AAPL",
        short_window=10,
        long_window=30,
        resolution_minutes=1440,  # 1 day — matches daily_sma_crossover registry binding
    )

    # Override the strategy's default (intraday) date range with a window
    # that both fits the AAPL reference coverage and produces crosses.
    original_initialize = strategy.initialize

    def _wrapped_initialize() -> None:
        original_initialize()
        strategy.set_start_date(2018, 1, 1)
        strategy.set_end_date(2021, 3, 31)
        strategy.set_cash(100000)

    strategy.initialize = _wrapped_initialize  # type: ignore[assignment]

    reader = LeanDailyDataReader(LEAN_DATA_ROOT)
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(
            mode=FillMode.SIGNAL_BAR_CLOSE,
            commission_per_order=Decimal("1"),
        ),
    )
    result = engine.run(strategy)

    trades = strategy.trade_log
    print(f"trades: {len(trades)}")
    for i, t in enumerate(trades, 1):
        print(
            f"  {i:>2}. {t.entry_time.date()} @ {t.entry_price:>8.2f}  ->  "
            f"{t.exit_time.date()} @ {t.exit_price:>8.2f}  "
            f"PnL={t.pnl_pts:>8.2f}  {t.result}"
        )
    print(
        f"final equity: ${float(result.final_equity):,.2f}  "
        f"net profit: ${float(result.net_profit):,.2f}"
    )

    # ------------------------------------------------------------------ #
    # Assertions
    # ------------------------------------------------------------------ #
    if len(trades) != EXPECTED_TRADE_COUNT:
        print(
            f"FAIL: expected {EXPECTED_TRADE_COUNT} trades, got {len(trades)}"
        )
        sys.exit(1)

    actual_results = [t.result for t in trades]
    if actual_results != EXPECTED_RESULTS:
        print(f"FAIL: result sequence mismatch")
        print(f"  expected: {EXPECTED_RESULTS}")
        print(f"  actual:   {actual_results}")
        sys.exit(1)

    actual_entry_dates = [t.entry_time.date() for t in trades]
    if actual_entry_dates != EXPECTED_ENTRY_DATES:
        print(f"FAIL: entry date sequence mismatch")
        print(f"  expected: {EXPECTED_ENTRY_DATES}")
        print(f"  actual:   {actual_entry_dates}")
        sys.exit(1)

    wins = sum(1 for t in trades if t.result == "WIN")
    if wins != EXPECTED_WIN_COUNT:
        print(f"FAIL: expected {EXPECTED_WIN_COUNT} wins, got {wins}")
        sys.exit(1)

    # Non-zero final equity proves the portfolio was actually touched by fills
    # (a silent "consolidator never fired" bug would leave equity pristine).
    if float(result.final_equity) == float(result.initial_cash):
        print(
            f"FAIL: final equity unchanged from initial "
            f"(${float(result.final_equity):,.2f}) — fills likely never applied"
        )
        sys.exit(1)

    print(
        f"PASS: daily SMA crossover ran end-to-end against AAPL 2018..2021 "
        f"({len(trades)} trades, {wins} wins, result sequence pinned)"
    )


if __name__ == "__main__":
    run_end_to_end()
