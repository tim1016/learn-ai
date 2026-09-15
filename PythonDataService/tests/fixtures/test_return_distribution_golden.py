"""RD-001 golden fixture — daily return distribution from minute bars.

Two independent sides, both required to agree with the **production
pipeline executed live in this test**:

1. The committed ``output.arrow``, produced by the generator's
   numpy/scipy oracle from ``input.arrow`` alone (the canonical module is
   not on the generator's import path).
2. The same oracle, imported from the generator and recomputed here.

The test reconstructs the production inputs from ``input.arrow`` (TradeBar
lists + calendar windows + scheduled sessions), runs
``extract_day_anchors → compute_daily_returns → build_return_distribution``
— the current implementation, not a recorded answer — and compares every
output column against both sides at atol=1e-9. A regression in the
canonical module therefore fails here against numbers it did not produce.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.ipc as ipc
import pytest

from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions, session_windows_ms_utc
from app.research.return_distribution import (
    build_return_distribution,
    compute_daily_returns,
    extract_day_anchors,
)
from scripts.fixture_generators.return_distribution import (
    BIN_WIDTH,
    N_SESSIONS,
    SPAN,
    oracle_bins,
    oracle_overlay,
    oracle_series,
    oracle_stats,
)

GOLDEN = Path(__file__).parent / "golden" / "return-distribution" / "RD-001" / "v1"
_ATOL = 1e-9
_PREFIX = {"close_to_close": "c2c", "session": "ses", "overnight": "on"}


def _read(name: str) -> pd.DataFrame:
    with ipc.open_file(str(GOLDEN / name)) as reader:
        return reader.read_all().to_pandas()


def _run_production_pipeline(bars: pd.DataFrame):
    """Reconstruct the production inputs and execute the current pipeline."""
    bars = bars.sort_values("start_ms").reset_index(drop=True)
    # Group by the ET trading date resolved exactly the way the lake reader
    # does (start_ms → ET date).
    et = pd.to_datetime(bars["start_ms"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    bars_by_day: dict[date, list[TradeBar]] = {}
    for row, day in zip(bars.itertuples(index=False), et.dt.date, strict=True):
        bars_by_day.setdefault(day, []).append(
            TradeBar(
                symbol="SPY",
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=int(row.volume),
                start_ms=int(row.start_ms),
                end_ms=int(row.start_ms) + 60_000,
            )
        )

    sessions = sorted(bars_by_day)
    windows = {
        w.session_date: (w.open_ms_utc, w.close_ms_utc)
        for w in session_windows_ms_utc(sessions[0], sessions[-1])
    }
    anchors = extract_day_anchors(bars_by_day, windows)
    days = compute_daily_returns(anchors, scheduled_sessions=expected_sessions(sessions[0], sessions[-1]))
    return build_return_distribution(
        days, bin_width_pct=BIN_WIDTH, span_pct=SPAN, adjustment="raw"
    )


@pytest.mark.parametrize("kind", ["close_to_close", "session", "overnight"])
def test_rd001_production_pipeline_matches_oracle_and_committed_output(kind: str) -> None:
    bars = _read("input.arrow")
    committed = _read("output.arrow")
    result = _run_production_pipeline(bars)

    live_values = oracle_series(bars)[kind]
    prefix = _PREFIX[kind]
    by_kind = {k.kind: k for k in result.kinds}
    production = by_kind[kind]

    # --- production vs the live oracle (independent recomputation) ---
    expected_stats = oracle_stats(live_values)
    assert production.stats.n_days == int(expected_stats["n_days"])
    assert production.stats.mean_pct == pytest.approx(expected_stats["mean_pct"], abs=_ATOL)
    assert production.stats.std_pct == pytest.approx(expected_stats["std_pct"], abs=_ATOL)
    assert production.stats.annualized_vol_pct == pytest.approx(expected_stats["ann_vol_pct"], abs=_ATOL)
    assert production.stats.skewness == pytest.approx(expected_stats["skew"], abs=_ATOL)
    assert production.stats.excess_kurtosis == pytest.approx(expected_stats["excess_kurt"], abs=_ATOL)
    assert production.stats.var_95_pct == pytest.approx(expected_stats["var95"], abs=_ATOL)
    assert production.stats.cvar_95_pct == pytest.approx(expected_stats["cvar95"], abs=_ATOL)
    assert production.stats.best_day.value_pct == pytest.approx(expected_stats["best_pct"], abs=_ATOL)
    assert production.stats.worst_day.value_pct == pytest.approx(expected_stats["worst_pct"], abs=_ATOL)

    expected_bins = oracle_bins(live_values)
    actual_bins = [b.count for b in production.histogram.bins]
    assert actual_bins == expected_bins

    expected_overlay = oracle_overlay(live_values)
    assert list(production.normal_expected_counts) == pytest.approx(expected_overlay, abs=_ATOL)

    # --- production vs the committed fixture (guards fixture rot) ---
    for name, expected in expected_stats.items():
        assert committed[f"{prefix}_{name}"][0] == pytest.approx(expected, abs=_ATOL), f"{prefix}_{name}"
    for i, count in enumerate(expected_bins):
        assert int(committed[f"{prefix}_bin{i}_count"][0]) == count, f"{prefix}_bin{i}_count"
    for i, expected in enumerate(expected_overlay):
        assert committed[f"{prefix}_norm{i}"][0] == pytest.approx(expected, abs=_ATOL), f"{prefix}_norm{i}"

    # --- the stamped per-day bin identity matches the counted histogram ---
    ki = [k.kind for k in result.kinds].index(kind)
    for bin_index, bin_model in enumerate(production.histogram.bins):
        stamped = sum(1 for d in result.days if d.bin_indices[ki] == bin_index)
        assert stamped == bin_model.count, (kind, bin_index)


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
    assert len(dates) == N_SESSIONS
    assert date(2024, 7, 3) in dates  # the 13:00-ET half-day
    windows = {
        w.session_date: (w.open_ms_utc, w.close_ms_utc)
        for w in session_windows_ms_utc(dates[0], dates[-1])
    }
    is_pre = (bars["start_ms"] < et.dt.date.map({d: w[0] for d, w in windows.items()})).to_numpy()
    is_post = (bars["start_ms"] >= et.dt.date.map({d: w[1] for d, w in windows.items()})).to_numpy()
    assert int(np.count_nonzero(is_pre)) > 0, "fixture input has no pre-market bars"
    assert int(np.count_nonzero(is_post)) > 0, "fixture input has no after-hours bars"
