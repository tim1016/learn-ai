"""End-to-end validation: reproduce LEAN's SPY EMA crossover trade log.

This is the Phase 1 done definition. It runs the engine over the exact SPY
date range LEAN used (2024-03-28 → 2026-03-27) and compares every trade to
``spy_lean_trades.csv`` extracted from LEAN's log.

The assertions are **bit-exact** for:
  * entry/exit timestamps
  * entry/exit prices (to 2 decimal places as they appear in the log)
  * EMA5, EMA10 (to 4 decimal places)
  * RSI (to 2 decimal places)
  * PnL points (2 dp) and PnL percent (6 dp)

Run with::

    cd PythonDataService
    python -m app.engine.tests.test_spy_validation
"""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.algorithms.spy_ema_crossover import (
    SpyEmaCrossoverAlgorithm,
)

EASTERN = ZoneInfo("America/New_York")
LEAN_DATA_ROOT = Path("/sessions/ecstatic-hopeful-volta/mnt/Lean/Data")
FIXTURE_CSV = Path(__file__).parent / "fixtures" / "spy_lean_trades.csv"


def _parse_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=EASTERN)


def _load_fixture() -> list[dict[str, str]]:
    with FIXTURE_CSV.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _fmt(value: Decimal, places: int) -> str:
    """Format a Decimal to the specified number of decimal places."""
    quant = Decimal(10) ** -places
    # Match C# decimal.ToString("F{places}"), which rounds half away from
    # zero (ROUND_HALF_UP for non-negative prices). Python's default
    # ROUND_HALF_EVEN disagrees with LEAN on midpoint values like 515.045.
    return str(value.quantize(quant, rounding=ROUND_HALF_UP))


def run_validation() -> None:
    fixture = _load_fixture()
    expected_count = len(fixture)
    print(f"Loaded {expected_count} expected trades from fixture")

    reader = LeanMinuteDataReader(LEAN_DATA_ROOT)
    strategy = SpyEmaCrossoverAlgorithm()
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(
            mode=FillMode.SIGNAL_BAR_CLOSE,
            commission_per_order=Decimal("1.00"),
        ),
    )
    print("Running backtest (this may take a few minutes)...")
    result = engine.run(strategy)

    actual_trades = strategy.trade_log
    print(f"Engine produced {len(actual_trades)} trades")
    print(f"Final equity: ${result.final_equity:.2f}")
    print(f"Net profit: ${result.net_profit:.2f}")
    print(f"Total fees: ${result.total_fees:.2f}")
    print()

    # ------------------------------------------------------------------
    # Compare row by row.
    # ------------------------------------------------------------------
    mismatches: list[str] = []

    min_len = min(len(actual_trades), len(fixture))
    for i in range(min_len):
        exp = fixture[i]
        act = actual_trades[i]
        trade_no = i + 1
        diffs: list[str] = []

        exp_entry = _parse_time(exp["entry"])
        exp_exit = _parse_time(exp["exit"])

        if act.entry_time != exp_entry:
            diffs.append(f"entry_time: {act.entry_time} != {exp_entry}")
        if act.exit_time != exp_exit:
            diffs.append(f"exit_time: {act.exit_time} != {exp_exit}")

        if _fmt(act.entry_price, 2) != exp["entry_price"]:
            diffs.append(f"entry_price: {_fmt(act.entry_price, 2)} != {exp['entry_price']}")
        if _fmt(act.exit_price, 2) != exp["exit_price"]:
            diffs.append(f"exit_price: {_fmt(act.exit_price, 2)} != {exp['exit_price']}")
        if _fmt(act.ema5, 4) != exp["ema5"]:
            diffs.append(f"ema5: {_fmt(act.ema5, 4)} != {exp['ema5']}")
        if _fmt(act.ema10, 4) != exp["ema10"]:
            diffs.append(f"ema10: {_fmt(act.ema10, 4)} != {exp['ema10']}")
        if _fmt(act.rsi, 2) != exp["rsi"]:
            diffs.append(f"rsi: {_fmt(act.rsi, 2)} != {exp['rsi']}")
        if _fmt(act.pnl_pts, 2) != exp["pnl_pts"]:
            diffs.append(f"pnl_pts: {_fmt(act.pnl_pts, 2)} != {exp['pnl_pts']}")
        if _fmt(act.pnl_pct, 6) != exp["pnl_pct"]:
            diffs.append(f"pnl_pct: {_fmt(act.pnl_pct, 6)} != {exp['pnl_pct']}")
        if act.result != exp["result"]:
            diffs.append(f"result: {act.result} != {exp['result']}")

        if diffs:
            mismatches.append(f"Trade #{trade_no} ({exp['entry']}): " + "; ".join(diffs))

    # Extra or missing trades
    if len(actual_trades) != len(fixture):
        mismatches.append(f"COUNT MISMATCH: engine={len(actual_trades)} expected={len(fixture)}")

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    print("=" * 70)
    if not mismatches:
        print("PASS: All trades match LEAN reference bit-exactly.")
        return
    print(f"FAIL: {len(mismatches)} mismatch(es) found")
    for m in mismatches[:20]:
        print(f"  {m}")
    if len(mismatches) > 20:
        print(f"  ... and {len(mismatches) - 20} more")

    # Also show the first actual trade vs first expected for debugging.
    if actual_trades:
        a = actual_trades[0]
        e = fixture[0]
        print()
        print("First engine trade:")
        print(f"  entry={a.entry_time} price={a.entry_price} ema5={a.ema5} ema10={a.ema10} rsi={a.rsi}")
        print("First fixture trade:")
        print(f"  entry={e['entry']} price={e['entry_price']} ema5={e['ema5']} ema10={e['ema10']} rsi={e['rsi']}")


if __name__ == "__main__":
    run_validation()
