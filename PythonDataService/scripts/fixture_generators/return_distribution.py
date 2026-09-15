"""Golden fixture generator for RD-001 — daily return distribution.

Deterministic synthetic minute bars over 60 real NYSE sessions (extended
hours on alternating days, half-days respected by the calendar).

The committed ``output.arrow`` is produced by the **independent oracle
below** — numpy/scipy array math recomputed from ``input.arrow`` — never
by the canonical ``app.research.return_distribution`` module. The golden
test imports the same oracle from here and runs the *production* pipeline
against it, so a regression in the canonical implementation cannot leave
the fixture green: the two sides of the comparison are built by different
code paths.

Regeneration (must be justified in the commit message per repo rules):
    cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.return_distribution
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow as pa
from pyarrow import ipc
from scipy import stats as scipy_stats

from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions, session_windows_ms_utc

ET = ZoneInfo("America/New_York")
SEED = 20260914
N_SESSIONS = 60
BIN_WIDTH = 0.5
SPAN = 5.0
logger = logging.getLogger(__name__)
OUT_DIR = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "golden" / "return-distribution" / "RD-001" / "v1"
)

_KIND_PREFIX = {"close_to_close": "c2c", "session": "ses", "overnight": "on"}


# ---------------------------------------------------------------------------
# Independent numerical oracle (numpy/scipy) — shared with the golden test.
# ---------------------------------------------------------------------------


def oracle_series(bars: pd.DataFrame) -> dict[str, np.ndarray]:
    """Recompute the three return series (simple percent) from raw bars.

    A deliberately different numerical path from the canonical module:
    pandas grouping and np.log/np.expm1 arrays instead of anchor math.log
    loops. The first session's close-to-close/overnight are NaN (no
    previous close in the fixture).
    """
    bars = bars.sort_values("start_ms").reset_index(drop=True)
    et = pd.to_datetime(bars["start_ms"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    trading_dates = sorted(set(et.dt.date))
    windows = {
        w.session_date: (w.open_ms_utc, w.close_ms_utc)
        for w in session_windows_ms_utc(trading_dates[0], trading_dates[-1])
    }

    rth_open: list[float] = []
    rth_close: list[float] = []
    for d in trading_dates:
        day = bars[et.dt.date == d]
        open_ms, close_ms = windows[d]
        rth = day[(day["start_ms"] >= open_ms) & (day["start_ms"] < close_ms)]
        assert not rth.empty, f"oracle: no RTH bars on {d}"
        rth_open.append(float(rth.iloc[0]["open"]))
        rth_close.append(float(rth.iloc[-1]["close"]))

    rth_open_a = np.array(rth_open, dtype=np.float64)
    rth_close_a = np.array(rth_close, dtype=np.float64)
    prev_close = np.concatenate(([np.nan], rth_close_a[:-1]))
    return {
        "close_to_close": np.expm1(np.log(rth_close_a / prev_close)) * 100.0,
        "session": np.expm1(np.log(rth_close_a / rth_open_a)) * 100.0,
        "overnight": np.expm1(np.log(rth_open_a / prev_close)) * 100.0,
    }


def oracle_bins(values: np.ndarray) -> list[int]:
    """Bin counts with the fixture's membership rule: left edge v < −span,
    right edge v ≥ +span, inner bins [lo, hi) at BIN_WIDTH steps."""
    edges = [-SPAN + i * BIN_WIDTH for i in range(int(2 * SPAN / BIN_WIDTH) + 1)]
    counts = [
        int(np.count_nonzero((values >= lo) & (values < hi))) for lo, hi in pairwise(edges)
    ]
    return [int(np.count_nonzero(values < -SPAN)), *counts, int(np.count_nonzero(values >= SPAN))]


def oracle_stats(values: np.ndarray) -> dict[str, float]:
    clean = values[~np.isnan(values)]
    var95 = float(np.percentile(clean, 5))
    tail = clean[clean <= var95]
    return {
        "n_days": float(len(clean)),
        "mean_pct": float(np.mean(clean)),
        "std_pct": float(np.std(clean, ddof=1)),
        "ann_vol_pct": float(np.std(clean, ddof=1) * np.sqrt(252.0)),
        "skew": float(scipy_stats.skew(clean, bias=False)),
        "excess_kurt": float(scipy_stats.kurtosis(clean, fisher=True, bias=False)),
        "var95": var95,
        "cvar95": float(np.mean(tail)) if len(tail) else var95,
        "best_pct": float(np.max(clean)),
        "worst_pct": float(np.min(clean)),
    }


def oracle_overlay(values: np.ndarray) -> list[float]:
    clean = values[~np.isnan(values)]
    mu = float(np.mean(clean))
    sigma = float(np.std(clean, ddof=1))
    n = float(len(clean))
    edges = [-SPAN + i * BIN_WIDTH for i in range(int(2 * SPAN / BIN_WIDTH) + 1)]
    probs = [
        n * (scipy_stats.norm.cdf(hi, mu, sigma) - scipy_stats.norm.cdf(lo, mu, sigma))
        for lo, hi in pairwise(edges)
    ]
    left = n * scipy_stats.norm.cdf(-SPAN, mu, sigma)
    right = n * (1.0 - scipy_stats.norm.cdf(SPAN, mu, sigma))
    return [left, *probs, right]


def oracle_output_table(input_table: pa.Table) -> pa.Table:
    """The full expected output computed from the committed input alone."""
    bars = input_table.to_pandas()
    series = oracle_series(bars)
    columns: dict[str, pa.Array] = {
        "bin_width_pct": pa.array([BIN_WIDTH], pa.float64()),
        "span_pct": pa.array([SPAN], pa.float64()),
    }
    for kind, values in series.items():
        prefix = _KIND_PREFIX[kind]
        stats = oracle_stats(values)
        columns[f"{prefix}_n_days"] = pa.array([int(stats.pop("n_days"))], pa.int64())
        for name, expected in stats.items():
            columns[f"{prefix}_{name}"] = pa.array([expected], pa.float64())
        for i, count in enumerate(oracle_bins(values)):
            columns[f"{prefix}_bin{i}_count"] = pa.array([count], pa.int64())
        for i, expected in enumerate(oracle_overlay(values)):
            columns[f"{prefix}_norm{i}"] = pa.array([expected], pa.float64())
    return pa.table(columns)


# ---------------------------------------------------------------------------
# Synthetic input generation
# ---------------------------------------------------------------------------


def _et_ms(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp() * 1000)


def _bar(d: date, h: int, m: int, open_: float, close: float, volume: int = 100) -> TradeBar:
    o, c = Decimal(str(round(open_, 4))), Decimal(str(round(close, 4)))
    return TradeBar(
        symbol="SPY",
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=volume,
        start_ms=_et_ms(d, h, m),
        end_ms=_et_ms(d, h, m) + 60_000,
    )


def _synthetic_bars() -> tuple[dict[date, list[TradeBar]], list[date]]:
    sessions = expected_sessions(date(2024, 7, 1), date(2025, 6, 30))[:N_SESSIONS]
    rng = np.random.default_rng(SEED)
    bars: dict[date, list[TradeBar]] = {}
    price = 100.0
    for i, d in enumerate(sessions):
        day_bars: list[TradeBar] = []
        pre_open = price * (1.0 + float(rng.normal(0.0, 0.003)))
        if i % 2 == 0:
            day_bars.append(_bar(d, 8, 0, pre_open, pre_open * (1.0 + float(rng.normal(0.0, 0.001)))))
        open_ = pre_open * (1.0 + float(rng.normal(0.0, 0.003)))
        noon = open_ * (1.0 + float(rng.normal(0.0, 0.006)))
        close = noon * (1.0 + float(rng.normal(0.0, 0.006)))
        day_bars.extend(
            [
                _bar(d, 9, 30, open_, open_ * 1.0005),
                _bar(d, 11, 59, noon * 0.9995, noon),
                _bar(d, 12, 0, noon, noon * 1.0005),
                _bar(d, 15, 59, close * 0.9995, close),
            ]
        )
        if i % 3 == 0:
            day_bars.append(_bar(d, 16, 30, close, close * (1.0 + float(rng.normal(0.0, 0.002)))))
        bars[d] = day_bars
        price = close
    return bars, sessions


def _input_table(bars_by_day: dict[date, list[TradeBar]]) -> pa.Table:
    rows = {"start_ms": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    for d in sorted(bars_by_day):
        for bar in bars_by_day[d]:
            rows["start_ms"].append(bar.start_ms)
            rows["open"].append(float(bar.open))
            rows["high"].append(float(bar.high))
            rows["low"].append(float(bar.low))
            rows["close"].append(float(bar.close))
            rows["volume"].append(bar.volume)
    return pa.table(
        {
            "start_ms": pa.array(rows["start_ms"], pa.int64()),
            "open": pa.array(rows["open"], pa.float64()),
            "high": pa.array(rows["high"], pa.float64()),
            "low": pa.array(rows["low"], pa.float64()),
            "close": pa.array(rows["close"], pa.float64()),
            "volume": pa.array(rows["volume"], pa.int64()),
        }
    )


def main() -> None:
    bars_by_day, sessions = _synthetic_bars()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    input_table = _input_table(bars_by_day)
    with ipc.new_file(str(OUT_DIR / "input.arrow"), input_table.schema) as writer:
        writer.write_table(input_table)

    # The output is computed by the oracle from the input table alone — the
    # canonical module is never imported here, so the committed expectation
    # cannot inherit a canonical-module bug.
    output = oracle_output_table(input_table)
    with ipc.new_file(str(OUT_DIR / "output.arrow"), output.schema) as writer:
        writer.write_table(output)

    n_bins = len(oracle_bins(oracle_series(input_table.to_pandas())["session"]))
    (OUT_DIR / "attribution.md").write_text(
        f"""# RD-001 — Daily return distribution from minute bars

## Layer 1 — market input provenance
Synthetic minute bars, generated by `scripts/fixture_generators/return_distribution.py`
(seed={SEED}): {N_SESSIONS} real NYSE sessions from {min(sessions).isoformat()} to
{max(sessions).isoformat()} (calendar-selected, so the July 2024 half-day is inside),
pre-market bars on even sessions, after-hours bars on every third session, RTH bars at
09:30 / 11:59 / 12:00 / 15:59 ET each session. Prices are rounded to the LEAN deci-cent
grid (4 decimals). No factor file: adjustment is `raw` — corporate-action semantics are
pinned by hand-computed unit tests and the LEAN parity suite
(tests/data_lake/test_factor_files.py), not by this fixture.

## Layer 2 — methodology provenance
Module docstring of `app/research/return_distribution.py`: log-space segment returns
displayed as simple percent; bins `[lo, hi)` at {BIN_WIDTH} pp width over ±{SPAN} pp
plus open edge bins; sample std ddof=1; adjusted Fisher-Pearson skew G1 / bias-corrected
Fisher excess kurtosis G2; historical VaR-95 = linear-interpolated 5th percentile;
CVaR-95 = mean of the tail at or below VaR; overlay = N·ΔΦ.

## Layer 3 — independent numerical oracle
`output.arrow` is produced by the generator's numpy/scipy oracle (np.log arrays,
boolean-mask binning, scipy.stats skew/kurtosis bias=False, np.percentile
method='linear', scipy.stats.norm.cdf) recomputed from `input.arrow` — the canonical
module is not on the generator's import path. The golden test
(`tests/fixtures/test_return_distribution_golden.py`) imports the same oracle, executes
the current production pipeline over `input.arrow`, and requires agreement with both
the live oracle and the committed `output.arrow` at atol=1e-9.
Bins per kind: {n_bins} ({n_bins - 2} inner + 2 open edge).

## Regeneration
```
cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.return_distribution
```
A regeneration commit must state what changed in the canonical module and why
(numerical-rigor rule: never regenerate to make a test pass).
""",
        encoding="utf-8",
    )
    logger.info("RD-001 written to %s", OUT_DIR)


if __name__ == "__main__":
    main()
