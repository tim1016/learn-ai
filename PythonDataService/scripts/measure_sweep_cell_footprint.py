"""Measure one Grid Search cell's resident-memory footprint (#1941).

The in-flight figure a sweep runs at moves only with a new measurement
(issue #1941); this script is that measurement. One process per mode —
allocator effects do not cancel out across modes in a single process:

    .venv/bin/python scripts/measure_sweep_cell_footprint.py --mode summary
    .venv/bin/python scripts/measure_sweep_cell_footprint.py --mode full

Seeds a synthetic SPY minute lake (two years of scheduled sessions — the
calendar decides each day's bounds, early closes included — ~190k bars)
under a temporary write root, then runs ONE cell —
``ema_crossover_signal``, minute bars, through the same
``execute_engine_backtest`` entry point a sweep drives — and reports
resident memory at three points: after imports and lake seed, the
kernel-accounted peak (``ru_maxrss``, stdlib only — no undeclared
dependency), and retained after the response is dropped and the allocator
asked to give back. Prices are a seeded daily random walk with intraday
wiggle, so the EMAs cross and the run trades; the footprint depends on
the bar count, not the price path.
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import random
import resource
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app.config import settings  # noqa: E402
from app.data_lake.path_policy import lake_subpath  # noqa: E402
from app.engine.data.lean_format import write_lean_day_zip  # noqa: E402
from app.engine.data.trade_bar import TradeBar  # noqa: E402
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_minute_et  # noqa: E402
from app.routers.engine import EngineBacktestRequest, execute_engine_backtest  # noqa: E402

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
SYMBOL = "SPY"
START, END = date(2024, 9, 5), date(2026, 9, 3)
WARMUP_SESSIONS = 5
SESSION_OPEN_MINUTE_ET = 9 * 60 + 30


def _seed_lake(root: Path) -> tuple[date, date]:
    """Two years of scheduled-session minute bars; returns (data_start, data_end)."""
    lake_dir = root / lake_subpath("polygon_split_adjusted")
    lake_dir.mkdir(parents=True)
    rng = random.Random(1941)
    base = Decimal("500")
    sessions = list(expected_sessions(START, END))
    for day in sessions:
        base *= Decimal("1") + Decimal(str(rng.uniform(-0.01, 0.011)))
        # The calendar owns the session bounds — early closes seed 09:30→13:00,
        # not a fabricated full day (temporal-rigor: no hardcoded session times).
        bar_count = session_close_minute_et(day) - SESSION_OPEN_MINUTE_ET
        open_et = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EASTERN)
        bars: list[TradeBar] = []
        for i in range(bar_count):
            wiggle = Decimal(str(rng.uniform(-0.4, 0.4)))
            o = base + wiggle
            c = o + Decimal(str(rng.uniform(-0.3, 0.3)))
            hi = max(o, c) + Decimal("0.2")
            lo = min(o, c) - Decimal("0.2")
            start = open_et + timedelta(minutes=i)
            bars.append(
                TradeBar(
                    symbol=SYMBOL,
                    time=start,
                    end_time=start + timedelta(minutes=1),
                    open=o,
                    high=hi,
                    low=lo,
                    close=c,
                    volume=1_000 + i,
                )
            )
        write_lean_day_zip(lake_dir, SYMBOL, day, bars)
    return sessions[0], sessions[-1]


def _current_rss() -> int:
    """Resident bytes right now — ``/proc`` on Linux, ``ps`` elsewhere (stdlib only)."""
    proc_status = Path("/proc/self/status")
    if proc_status.exists():
        for line in proc_status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True)
    return int(out.strip()) * 1024


def _peak_rss() -> int:
    """Peak resident bytes of the process so far, from the kernel's accounting.

    ``ru_maxrss`` is kilobytes on Linux and bytes on darwin.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _mb(value: int) -> int:
    return value // (1024 * 1024)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("summary", "full"), required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    with tempfile.TemporaryDirectory(prefix="sweep-cell-measure-") as tmp:
        root = Path(tmp)
        settings.LEAN_DATA_WRITE_ROOT = str(root)
        data_start, data_end = _seed_lake(root)
        gc.collect()
        baseline = _current_rss()
        logger.info(f"mode={args.mode} baseline after imports + lake seed: {_mb(baseline)} MB")

        sessions = list(expected_sessions(data_start, data_end))
        evaluation_start = sessions[WARMUP_SESSIONS]
        request = EngineBacktestRequest(
            strategy_name="ema_crossover_signal",
            params={"symbol": SYMBOL},
            from_date=evaluation_start.isoformat(),
            to_date=data_end.isoformat(),
            warmup_from_date=data_start.isoformat(),
            save_study=False,
            auto_fetch=False,
            summary_only=args.mode == "summary",
        )

        started = time.monotonic()
        response = execute_engine_backtest(
            request=request,
            on_phase=lambda phase: None,
            on_log=lambda message: None,
        )
        elapsed = time.monotonic() - started
        assert response.success, response.error
        logger.info(
            f"cell: bars_consumed={response.bars_consumed} trades={response.total_trades} "
            f"equity_points={len(response.equity_curve)} wall={elapsed:.1f}s"
        )
        peak = _peak_rss()
        logger.info(f"peak (ru_maxrss): {_mb(peak)} MB ({_mb(peak - baseline)} MB above baseline)")

        del response
        gc.collect()
        retained = _current_rss()
        logger.info(f"retained after response dropped + gc: {_mb(retained)} MB ({_mb(retained - baseline)} MB above baseline)")


if __name__ == "__main__":
    main()
