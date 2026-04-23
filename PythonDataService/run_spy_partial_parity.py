"""Partial SPY bit-exact parity check against the LEAN-derived fixture.

Throwaway diagnostic. Points the engine at the local lean-cache/ and
compares trades in the overlapping window against
app/engine/tests/fixtures/spy_lean_trades.csv. Writes a JSON blob of
results for the PDF report generator.

Run from PythonDataService/ with: python3 run_spy_partial_parity.py
"""

from __future__ import annotations

import csv
import json
import time as timing
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm

EASTERN = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


class RTHFilteredReader:
    """Wraps a LEAN minute reader to yield only 09:30-16:00 ET bars.

    Our local cache was fetched with extended hours; the committed LEAN
    fixture was produced from RTH-only data. Filtering at the reader
    level (vs. post-hoc on trades) is critical because EMA/RSI state
    must be accumulated from the same bar set the fixture saw —
    otherwise indicator values diverge even at RTH timestamps.
    """

    def __init__(self, inner: LeanMinuteDataReader) -> None:
        self._inner = inner

    def iter_bars(self, symbol, start, end):  # noqa: ANN001
        for bar in self._inner.iter_bars(symbol, start, end):
            local = bar.time.astimezone(EASTERN).time()
            if RTH_OPEN <= local < RTH_CLOSE:
                yield bar
HERE = Path(__file__).resolve().parent
CACHE_ROOT = HERE / "lean-cache"
FIXTURE = HERE / "app" / "engine" / "tests" / "fixtures" / "spy_lean_trades.csv"

# Engine run covers the full cached window; comparison window adds a
# 2-week warmup buffer so EMA/RSI recursions converge to the values
# they'd have had under a full-history run (difference decays
# exponentially; at 10x EMA window the initial-seed influence is
# ~10^-8, well below the fixture's 4-decimal precision).
RUN_START = (2025, 4, 21)
RUN_END = (2026, 3, 27)
COMPARE_FROM = "2025-05-05"


def _fmt(value, places: int) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return str(value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


def _parse_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=EASTERN)


def main() -> None:
    t0 = timing.time()
    raw_reader = LeanMinuteDataReader(CACHE_ROOT)
    reader = RTHFilteredReader(raw_reader)
    strategy = SpyEmaCrossoverAlgorithm()
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(
            mode=FillMode.SIGNAL_BAR_CLOSE,
            commission_per_order=Decimal("1.00"),
        ),
    )

    # Override the strategy's hardcoded dates to fit the cache window.
    original_init = strategy.initialize

    def _wrapped_init() -> None:
        original_init()
        strategy.set_start_date(*RUN_START)
        strategy.set_end_date(*RUN_END)

    strategy.initialize = _wrapped_init  # type: ignore[assignment]

    print(f"Cache: {CACHE_ROOT}")
    print(f"Engine run: {RUN_START} -> {RUN_END}")
    print(f"RTH filter: {RTH_OPEN} - {RTH_CLOSE} ET")
    print(f"Comparison window: entries >= {COMPARE_FROM}")
    print("Running backtest...")
    result = engine.run(strategy)
    runtime_s = timing.time() - t0
    print(f"Done in {runtime_s:.1f}s")
    print(f"Engine trades: {len(strategy.trade_log)}")
    print(f"Final equity: ${float(result.final_equity):,.2f}")
    print(f"Total fees:   ${float(result.total_fees):,.2f}")
    print()

    with FIXTURE.open() as f:
        fixture_all = list(csv.DictReader(f))
    fixture_in = [r for r in fixture_all if r["entry"] >= COMPARE_FROM]
    actual_in = [
        t for t in strategy.trade_log if t.entry_time.strftime("%Y-%m-%d %H:%M") >= COMPARE_FROM
    ]
    print(f"Fixture trades in window: {len(fixture_in)}")
    print(f"Actual  trades in window: {len(actual_in)}")

    mismatches: list[dict] = []
    matches = 0
    pairings = min(len(fixture_in), len(actual_in))
    for i in range(pairings):
        exp = fixture_in[i]
        act = actual_in[i]
        diffs: list[str] = []

        exp_entry = _parse_time(exp["entry"])
        exp_exit = _parse_time(exp["exit"])

        def check(name, actual_str, expected_str):
            if actual_str != expected_str:
                diffs.append(f"{name}: got {actual_str!r}, expected {expected_str!r}")

        if act.entry_time != exp_entry:
            diffs.append(f"entry_time: got {act.entry_time}, expected {exp_entry}")
        if act.exit_time != exp_exit:
            diffs.append(f"exit_time: got {act.exit_time}, expected {exp_exit}")
        check("entry_price", _fmt(act.entry_price, 2), exp["entry_price"])
        check("exit_price", _fmt(act.exit_price, 2), exp["exit_price"])
        try:
            check("ema5", _fmt(act.ema5, 4), exp["ema5"])
            check("ema10", _fmt(act.ema10, 4), exp["ema10"])
            check("rsi", _fmt(act.rsi, 2), exp["rsi"])
        except AttributeError as e:
            diffs.append(f"indicator missing on actual trade: {e}")
        check("pnl_pts", _fmt(act.pnl_pts, 2), exp["pnl_pts"])
        check("pnl_pct", _fmt(act.pnl_pct, 6), exp["pnl_pct"])
        check("result", act.result, exp["result"])

        if diffs:
            mismatches.append(
                {
                    "row_in_window": i + 1,
                    "expected_entry": exp["entry"],
                    "actual_entry": act.entry_time.strftime("%Y-%m-%d %H:%M"),
                    "diffs": diffs,
                }
            )
        else:
            matches += 1

    print(f"\nMatches:    {matches}/{pairings}")
    print(f"Mismatches: {len(mismatches)}")
    if mismatches:
        print("\nFirst 3 mismatches:")
        for m in mismatches[:3]:
            print(f"  row {m['row_in_window']} (expected entry {m['expected_entry']}):")
            for d in m["diffs"]:
                print(f"    - {d}")

    results = {
        "ran_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
        "runtime_seconds": round(runtime_s, 2),
        "cache_root": str(CACHE_ROOT),
        "cache_range": "2025-04-21 -> 2026-04-20",
        "engine_run_start": "-".join(str(p) for p in RUN_START),
        "engine_run_end": "-".join(str(p) for p in RUN_END),
        "comparison_window_from": COMPARE_FROM,
        "fixture_total_trades": len(fixture_all),
        "fixture_in_window": len(fixture_in),
        "engine_trades_total": len(strategy.trade_log),
        "engine_trades_in_window": len(actual_in),
        "pairings_compared": pairings,
        "matches": matches,
        "mismatches": len(mismatches),
        "mismatch_details": mismatches,
        "final_equity_usd": float(result.final_equity),
        "total_fees_usd": float(result.total_fees),
        "net_profit_usd": float(result.net_profit),
    }
    out = HERE / "_spy_partial_parity_results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nResults: {out}")


if __name__ == "__main__":
    main()
