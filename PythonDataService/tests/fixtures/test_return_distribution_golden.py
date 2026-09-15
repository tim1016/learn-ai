"""RD-001 golden fixture — daily return distribution from minute bars.

The oracle here is deliberately a different numerical path from the
canonical module: numpy array math (np.log/np.expm1, boolean-mask binning,
np.percentile) and scipy.stats moments/CDFs, where the canonical module
uses math.log/math.fsum loops. Agreement at atol=1e-9 therefore proves the
*definitions* line up, not just the arithmetic; divergence is bounded by
summation order (~1e-13 at n=60), four orders below the tolerance.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow.ipc as ipc
import pytest
from scipy import stats as scipy_stats

from app.lean_sidecar.trading_calendar import session_windows_ms_utc

ET = ZoneInfo("America/New_York")
GOLDEN = Path(__file__).parent / "golden" / "return-distribution" / "RD-001" / "v1"
BIN_WIDTH = 0.5
SPAN = 5.0
_ATOL = 1e-9


def _read(name: str) -> pd.DataFrame:
    with ipc.open_file(str(GOLDEN / name)) as reader:
        return reader.read_all().to_pandas()


def _oracle_series(bars: pd.DataFrame) -> dict[str, np.ndarray]:
    """Recompute the three return series (simple percent) independently."""
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


def _oracle_bins(values: np.ndarray) -> list[int]:
    """Bin counts with the fixture's membership rule: left edge v < −span,
    right edge v ≥ +span, inner bins [lo, hi) at BIN_WIDTH steps."""
    from itertools import pairwise

    edges = [-SPAN + i * BIN_WIDTH for i in range(int(2 * SPAN / BIN_WIDTH) + 1)]
    counts = [
        int(np.count_nonzero((values >= lo) & (values < hi))) for lo, hi in pairwise(edges)
    ]
    return [int(np.count_nonzero(values < -SPAN)), *counts, int(np.count_nonzero(values >= SPAN))]


def _oracle_stats(values: np.ndarray) -> dict[str, float]:
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


def _oracle_overlay(values: np.ndarray, bins: list[int]) -> list[float]:
    from itertools import pairwise

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


_PREFIX = {"close_to_close": "c2c", "session": "ses", "overnight": "on"}


@pytest.mark.parametrize("kind", ["close_to_close", "session", "overnight"])
def test_rd001_return_distribution_matches_numpy_scipy_oracle(kind: str) -> None:
    output = _read("output.arrow")
    series = _oracle_series(_read("input.arrow"))
    values = series[kind]
    prefix = _PREFIX[kind]

    stats = _oracle_stats(values)
    for name, expected in stats.items():
        actual = output[f"{prefix}_{name}"][0]
        assert actual == pytest.approx(expected, abs=_ATOL), f"{prefix}_{name}"

    bins = _oracle_bins(values)
    n_bins = len(bins)
    for i in range(n_bins):
        actual = int(output[f"{prefix}_bin{i}_count"][0])
        assert actual == bins[i], f"{prefix}_bin{i}_count"

    overlay = _oracle_overlay(values, bins)
    for i in range(n_bins):
        actual = output[f"{prefix}_norm{i}"][0]
        assert actual == pytest.approx(overlay[i], abs=_ATOL), f"{prefix}_norm{i}"


def test_rd001_bin_geometry_pinned() -> None:
    output = _read("output.arrow")
    assert output["bin_width_pct"][0] == pytest.approx(BIN_WIDTH, abs=0.0)
    assert output["span_pct"][0] == pytest.approx(SPAN, abs=0.0)
    # 20 inner bins + 2 open edge bins per kind.
    for prefix in ("c2c", "ses", "on"):
        assert sum(1 for c in output.columns if c.startswith(f"{prefix}_bin")) == 22


def test_rd001_input_spans_real_sessions_with_extended_hours() -> None:
    """The input must exercise the calendar and both extended segments:
    60 sessions including a half-day, with pre- and post-market bars."""
    bars = _read("input.arrow").sort_values("start_ms").reset_index(drop=True)
    et = pd.to_datetime(bars["start_ms"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    dates = sorted(set(et.dt.date))
    assert len(dates) == 60
    assert date(2024, 7, 3) in dates  # the 13:00-ET half-day
    windows = {
        w.session_date: (w.open_ms_utc, w.close_ms_utc)
        for w in session_windows_ms_utc(dates[0], dates[-1])
    }
    is_pre = (bars["start_ms"] < et.dt.date.map({d: w[0] for d, w in windows.items()})).to_numpy()
    is_post = (bars["start_ms"] >= et.dt.date.map({d: w[1] for d, w in windows.items()})).to_numpy()
    assert int(np.count_nonzero(is_pre)) > 0, "fixture input has no pre-market bars"
    assert int(np.count_nonzero(is_post)) > 0, "fixture input has no after-hours bars"
