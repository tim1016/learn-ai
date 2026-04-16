"""NEXT_BAR_OPEN fill-mode validation for the SPY EMA crossover strategy.

Phase 1 of the LEAN-engine plan requires validating the NEXT_BAR_OPEN
fill model against LEAN's own output within tolerance — not bit-exactly,
since fills legitimately differ from the signal-bar-close mode.

This module runs **two** checks:

1. **Regression check against committed baseline.**
   The current engine output for NEXT_BAR_OPEN is frozen in
   ``fixtures/spy_engine_next_bar_open_baseline.csv``. Re-running the
   engine should reproduce that file bit-exactly (same 63 trades, same
   prices, same indicator snapshots). This guards against unintended
   drift in the engine internals when changing unrelated code.

2. **Tolerance check against LEAN reference** (optional).
   If ``fixtures/spy_lean_next_bar_open_stats.json`` exists, its
   portfolio statistics (final_equity, net_profit, max_drawdown_pct,
   sharpe_ratio, ...) are compared against the engine's output within
   configurable per-metric tolerances. If the file does not exist the
   check is SKIPPED with a message explaining how to generate it from
   LEAN. This is the Phase 1 "secondary validation" gate.

Run with::

    cd PythonDataService
    python -m app.engine.tests.test_spy_next_bar_open_validation
"""

from __future__ import annotations

import csv
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.results.statistics import summarize
from app.engine.strategy.algorithms.spy_ema_crossover import (
    SpyEmaCrossoverAlgorithm,
)

LEAN_DATA_ROOT = Path("/sessions/ecstatic-hopeful-volta/mnt/Lean/Data")
FIXTURES_DIR = Path(__file__).parent / "fixtures"
BASELINE_CSV = FIXTURES_DIR / "spy_engine_next_bar_open_baseline.csv"
LEAN_STATS_JSON = FIXTURES_DIR / "spy_lean_next_bar_open_stats.json"


def _fmt(value: Decimal, places: int) -> str:
    """C#-compatible F{places} formatting using ROUND_HALF_UP."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    quant = Decimal(10) ** -places
    return str(value.quantize(quant, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Part 1: Regression check against committed engine baseline.
# ---------------------------------------------------------------------------
def load_baseline() -> list[dict[str, str]]:
    if not BASELINE_CSV.exists():
        raise FileNotFoundError(
            f"Baseline fixture not found at {BASELINE_CSV}. "
            "Regenerate it with: "
            "python -m app.engine.tests.test_spy_next_bar_open_validation --refresh-baseline"
        )
    with BASELINE_CSV.open() as f:
        return list(csv.DictReader(f))


def run_engine_next_bar_open() -> tuple[list, float, float, float]:
    """Run the SPY strategy in NEXT_BAR_OPEN mode.

    Returns (trades, initial_cash, final_equity, total_fees).
    """
    reader = LeanMinuteDataReader(LEAN_DATA_ROOT)
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(mode=FillMode.NEXT_BAR_OPEN),
    )
    strategy = SpyEmaCrossoverAlgorithm()
    result = engine.run(strategy)
    return (
        list(strategy.trade_log),
        float(result.initial_cash),
        float(result.final_equity),
        float(result.total_fees),
    )


def compare_to_baseline(trades: list) -> list[str]:
    """Compare engine output against the committed baseline CSV."""
    baseline = load_baseline()
    mismatches: list[str] = []

    if len(trades) != len(baseline):
        mismatches.append(f"count: engine produced {len(trades)} trades, baseline has {len(baseline)}")
        return mismatches

    for i, (engine_trade, baseline_row) in enumerate(zip(trades, baseline, strict=False), start=1):
        checks = [
            (
                "entry_time",
                engine_trade.entry_time.strftime("%Y-%m-%d %H:%M:%S%z"),
                baseline_row["entry"],
                lambda e, b: e.startswith(b.replace(" ", "T")) or e[:16] == b[:16],
            ),
            ("entry_price", _fmt(engine_trade.entry_price, 4), baseline_row["entry_price"], None),
            ("exit_price", _fmt(engine_trade.exit_price, 4), baseline_row["exit_price"], None),
            ("ema5", _fmt(engine_trade.ema5, 6), baseline_row["ema5"], None),
            ("ema10", _fmt(engine_trade.ema10, 6), baseline_row["ema10"], None),
            ("rsi", _fmt(engine_trade.rsi, 6), baseline_row["rsi"], None),
            ("pnl_pts", _fmt(engine_trade.pnl_pts, 6), baseline_row["pnl_pts"], None),
            ("pnl_pct", _fmt(engine_trade.pnl_pct, 8), baseline_row["pnl_pct"], None),
            ("result", engine_trade.result, baseline_row["result"], None),
        ]
        for name, engine_val, baseline_val, comparator in checks:
            if comparator is not None:
                ok = comparator(engine_val, baseline_val)
            else:
                ok = engine_val == baseline_val
            if not ok:
                mismatches.append(f"Trade #{i} ({baseline_row['entry']}): {name}: {engine_val} != {baseline_val}")
    return mismatches


# ---------------------------------------------------------------------------
# Part 2: LEAN reference comparison (tolerance-based).
# ---------------------------------------------------------------------------
# Per-metric tolerance (absolute or relative as noted in comments).
_LEAN_TOLERANCES: dict[str, float] = {
    "final_equity": 0.005,  # 0.5% relative
    "net_profit": 0.02,  # 2% relative — net profit is more sensitive
    "total_fees": 0.0,  # exact — commission model is deterministic
    "total_trades": 0.0,  # exact
    "winning_trades": 2.0,  # absolute: +/- 2 trades allowed
    "losing_trades": 2.0,
    "win_rate": 0.05,  # absolute: +/- 5 percentage points
    "profit_factor": 0.10,  # 10% relative
    "max_drawdown_pct": 0.005,  # absolute: +/- 0.5 percentage points
    "sharpe_ratio": 0.15,  # absolute
}


def _within_tolerance(key: str, engine_val: float, lean_val: float) -> bool:
    """Apply the per-key tolerance from _LEAN_TOLERANCES."""
    tol = _LEAN_TOLERANCES.get(key, 0.01)
    if tol == 0.0:
        return engine_val == lean_val
    if key in ("final_equity", "net_profit", "profit_factor"):
        # Relative
        denom = abs(lean_val) if lean_val != 0 else 1.0
        return abs(engine_val - lean_val) / denom <= tol
    # Absolute
    return abs(engine_val - lean_val) <= tol


def compare_to_lean_reference(engine_stats: dict, engine_equity: float, engine_trades: int) -> tuple[bool, list[str]]:
    """Compare engine NEXT_BAR_OPEN output to LEAN's reference stats.

    Returns (passed, messages). If the fixture file does not exist,
    returns (True, ["SKIPPED ..."]) — this is not a failure, it's a
    signal that the LEAN reference run has not yet been captured.
    """
    if not LEAN_STATS_JSON.exists():
        return True, [
            f"SKIPPED: LEAN reference stats file not found at {LEAN_STATS_JSON}.",
            "",
            "To populate this file:",
            "  1. Run LEAN with SpyEmaCrossoverAlgorithm and an alternate fill",
            "     model (e.g., ImmediateFillModel + BestEffortNextBarOpen) over",
            "     the same date range 2024-03-28 → 2026-03-27.",
            "  2. Extract portfolio statistics from LEAN's BacktestResult into",
            f"     {LEAN_STATS_JSON} with keys: final_equity, net_profit,",
            "     total_fees, total_trades, winning_trades, losing_trades,",
            "     win_rate, profit_factor, max_drawdown_pct, sharpe_ratio.",
            "  3. Re-run this test — it will compare within the tolerances",
            "     declared in _LEAN_TOLERANCES.",
        ]

    with LEAN_STATS_JSON.open() as f:
        lean = json.load(f)

    engine = {
        "final_equity": engine_equity,
        "total_trades": engine_trades,
        **engine_stats,
    }

    mismatches: list[str] = []
    for key in _LEAN_TOLERANCES:
        if key not in lean:
            mismatches.append(f"  {key}: LEAN fixture missing this key")
            continue
        if key not in engine:
            mismatches.append(f"  {key}: engine output missing this key")
            continue
        engine_val = float(engine[key])
        lean_val = float(lean[key])
        ok = _within_tolerance(key, engine_val, lean_val)
        status = "OK " if ok else "FAIL"
        mismatches.append(f"  [{status}] {key}: engine={engine_val}  lean={lean_val}  tol={_LEAN_TOLERANCES[key]}")
        if not ok:
            pass  # keep the report contiguous

    passed = all("[FAIL]" not in m for m in mismatches)
    return passed, mismatches


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_validation() -> bool:
    print("Running SPY NEXT_BAR_OPEN backtest...")
    trades, initial_cash, final_equity, total_fees = run_engine_next_bar_open()
    print(f"Engine: {len(trades)} trades, final_equity=${final_equity:,.2f}, fees=${total_fees:.2f}")

    # --- Part 1: regression vs baseline ---
    print("\n[1/2] Regression check against committed baseline...")
    mismatches = compare_to_baseline(trades)
    if mismatches:
        print(f"  FAIL: {len(mismatches)} mismatch(es)")
        for m in mismatches[:10]:
            print(f"    {m}")
        if len(mismatches) > 10:
            print(f"    ... ({len(mismatches) - 10} more)")
        baseline_ok = False
    else:
        print("  PASS: all trades match baseline")
        baseline_ok = True

    # --- Part 2: LEAN reference tolerance check ---
    print("\n[2/2] LEAN reference tolerance check...")
    trading_days = 504  # ~2 years * 252
    stats = summarize(
        initial_cash=initial_cash,
        final_equity=final_equity,
        trades=trades,
        trading_days=trading_days,
    )
    # The engine reports winning/losing trades from the trade log.
    wins = sum(1 for t in trades if t.result == "WIN")
    losses = sum(1 for t in trades if t.result == "LOSS")
    stats["winning_trades"] = wins
    stats["losing_trades"] = losses
    stats["total_fees"] = total_fees

    lean_ok, lean_msgs = compare_to_lean_reference(stats, final_equity, len(trades))
    for m in lean_msgs:
        print(m)

    print("\n" + "=" * 70)
    if baseline_ok and lean_ok:
        print("PASS: baseline matches; LEAN reference check passed or skipped.")
        return True
    else:
        print("FAIL")
        return False


if __name__ == "__main__":
    import sys

    if "--refresh-baseline" in sys.argv:
        # Regenerate the baseline CSV from the current engine output.
        trades, _, _, _ = run_engine_next_bar_open()
        BASELINE_CSV.parent.mkdir(parents=True, exist_ok=True)
        with BASELINE_CSV.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "trade_no",
                    "entry",
                    "entry_price",
                    "exit",
                    "exit_price",
                    "ema5",
                    "ema10",
                    "rsi",
                    "pnl_pts",
                    "pnl_pct",
                    "result",
                ]
            )
            for i, t in enumerate(trades, start=1):
                w.writerow(
                    [
                        i,
                        t.entry_time.strftime("%Y-%m-%d %H:%M"),
                        _fmt(t.entry_price, 4),
                        t.exit_time.strftime("%Y-%m-%d %H:%M"),
                        _fmt(t.exit_price, 4),
                        _fmt(t.ema5, 6),
                        _fmt(t.ema10, 6),
                        _fmt(t.rsi, 6),
                        _fmt(t.pnl_pts, 6),
                        _fmt(t.pnl_pct, 8),
                        t.result,
                    ]
                )
        print(f"Refreshed baseline at {BASELINE_CSV} with {len(trades)} trades")
        sys.exit(0)

    ok = run_validation()
    sys.exit(0 if ok else 1)
