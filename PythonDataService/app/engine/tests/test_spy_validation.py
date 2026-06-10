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

Two ways to invoke:

* As a pytest case (auto-skipped if the LEAN data folder isn't present):

      podman exec polygon-data-service python -m pytest \\
          app/engine/tests/test_spy_validation.py -v -m slow

* As a manual script (errors loudly if the data folder isn't present):

      cd PythonDataService
      python -m app.engine.tests.test_spy_validation

The LEAN data root is resolved in this order:

1. ``LEAN_DATA_ROOT`` env var (the compose-mounted ``/lean-data`` inside
   ``polygon-data-service``). This is the path operators should override
   in ``.env`` if their LEAN data lives elsewhere.
2. ``<repo_root>/../Lean/Data`` on the host — the same source the
   compose file bind-mounts.

The old hardcoded ``/sessions/ecstatic-hopeful-volta/mnt/Lean/Data`` was a
remote-sandbox path that never resolved locally; it caused this test to
be invokable only inside the original handoff sandbox. See
``docs/handoffs/2026-06-09-lean-sidecar-applehv-sigill-and-parity-gates.md``.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.algorithms.spy_ema_crossover import (
    SpyEmaCrossoverAlgorithm,
)

EASTERN = ZoneInfo("America/New_York")
FIXTURE_CSV = Path(__file__).parent / "fixtures" / "spy_lean_trades.csv"

# Repo root is three parents up from this file:
# PythonDataService/app/engine/tests/test_spy_validation.py -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
# Default host-side LEAN data root: the same path compose.yaml bind-
# mounts to /lean-data in the container. Resolves to ``<repo>/../Lean/Data``.
_DEFAULT_LEAN_DATA_ROOT = _REPO_ROOT.parent / "Lean" / "Data"


def _resolve_lean_data_root() -> Path:
    """Resolve the LEAN data root from env or fall back to the host default.

    The env var takes precedence so the container can use ``/lean-data``
    (mounted by compose) and host invocations can override via ``.env``
    or shell. Returns a Path even if the directory doesn't exist; the
    caller decides whether absence is fatal (manual run) or a skip
    signal (pytest).
    """
    env_value = os.environ.get("LEAN_DATA_ROOT")
    if env_value:
        return Path(env_value)
    return _DEFAULT_LEAN_DATA_ROOT


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


def _compare_trades(
    actual_trades: list,  # type: ignore[type-arg]
    fixture: list[dict[str, str]],
) -> list[str]:
    """Return a list of human-readable mismatch strings, empty on parity."""
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

    if len(actual_trades) != len(fixture):
        mismatches.append(f"COUNT MISMATCH: engine={len(actual_trades)} expected={len(fixture)}")

    return mismatches


def _run_engine(lean_data_root: Path) -> tuple[list, object]:  # type: ignore[type-arg]
    """Drive the engine and return ``(trades, result)``.

    Shared between the pytest case and the manual ``__main__`` entry so
    both paths exercise the same orchestration.
    """
    reader = LeanMinuteDataReader(lean_data_root)
    strategy = SpyEmaCrossoverAlgorithm()
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(
            mode=FillMode.SIGNAL_BAR_CLOSE,
            commission_per_order=Decimal("1.00"),
        ),
    )
    result = engine.run(strategy)
    return strategy.trade_log, result


@pytest.mark.slow
def test_spy_ema_crossover_matches_lean_reference_trades() -> None:
    """Bit-exact equivalence vs the 63-trade LEAN reference log.

    Skipped when the LEAN data folder isn't reachable — this is the
    only test in the repo that runs the full 2-year SPY backtest, so
    a developer without the bound data shouldn't see a hard failure
    from this fixture. Set ``LEAN_DATA_ROOT`` (or run inside the
    polygon-data-service container which already exports it) to enable.

    Marked ``slow`` because the run materializes ~2 years of minute
    bars and takes several minutes; ``pytest -k "not slow"`` skips it.
    """
    lean_data_root = _resolve_lean_data_root()
    # Skip when the SPY minute corpus the validator needs is absent —
    # not just when the root dir exists. An empty mounted directory
    # (compose binds ``../Lean/Data`` even if it's empty) would
    # otherwise run the engine against zero bars and fail with a
    # spurious COUNT MISMATCH, masking the "data missing" reason.
    spy_minute_dir = lean_data_root / "equity" / "usa" / "minute" / "spy"
    if not spy_minute_dir.exists() or not any(spy_minute_dir.iterdir()):
        pytest.skip(
            f"SPY minute data missing at {spy_minute_dir}; set LEAN_DATA_ROOT "
            "or stage ../Lean/Data/equity/usa/minute/spy/ to enable this validation."
        )

    fixture = _load_fixture()
    actual_trades, _result = _run_engine(lean_data_root)

    mismatches = _compare_trades(actual_trades, fixture)
    assert not mismatches, "Engine trade log diverges from LEAN reference:\n  " + "\n  ".join(mismatches[:20])


def run_validation() -> None:
    """Manual entry — verbose console output, no pytest assertions.

    Preserved for ad-hoc operator runs and for the
    ``python -m app.engine.tests.test_spy_validation`` invocation in the
    original Phase 1 done definition.
    """
    fixture = _load_fixture()
    expected_count = len(fixture)
    print(f"Loaded {expected_count} expected trades from fixture")

    lean_data_root = _resolve_lean_data_root()
    if not lean_data_root.exists():
        raise FileNotFoundError(
            f"LEAN data root {lean_data_root} does not exist. Set LEAN_DATA_ROOT "
            "(or stage ../Lean/Data) before invoking this script."
        )

    print("Running backtest (this may take a few minutes)...")
    actual_trades, result = _run_engine(lean_data_root)
    print(f"Engine produced {len(actual_trades)} trades")
    print(f"Final equity: ${result.final_equity:.2f}")
    print(f"Net profit: ${result.net_profit:.2f}")
    print(f"Total fees: ${result.total_fees:.2f}")
    print()

    mismatches = _compare_trades(actual_trades, fixture)

    print("=" * 70)
    if not mismatches:
        print("PASS: All trades match LEAN reference bit-exactly.")
        return
    print(f"FAIL: {len(mismatches)} mismatch(es) found")
    for m in mismatches[:20]:
        print(f"  {m}")
    if len(mismatches) > 20:
        print(f"  ... and {len(mismatches) - 20} more")

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
