"""Golden fixture generator for RD-001 — daily return distribution.

Deterministic synthetic minute bars over 60 real NYSE sessions (extended
hours on alternating days, half-days respected by the calendar), pushed
through the canonical ``app.research.return_distribution`` pipeline.

Regeneration (must be justified in the commit message per repo rules):
    cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.return_distribution

The output.arrow is produced by the canonical module; equivalence to an
independent oracle (numpy/scipy recomputation from input.arrow) is proved
live by tests/fixtures/test_return_distribution_golden.py.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
from pyarrow import ipc

from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions, session_windows_ms_utc
from app.research.return_distribution import (
    DEFAULT_BIN_WIDTH_PCT,
    DEFAULT_SPAN_PCT,
    build_return_distribution,
    compute_daily_returns,
    extract_day_anchors,
)

ET = ZoneInfo("America/New_York")
SEED = 20260914
N_SESSIONS = 60
OUT_DIR = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "golden" / "return-distribution" / "RD-001" / "v1"
)

_KIND_PREFIX = {"close_to_close": "c2c", "session": "ses", "overnight": "on"}


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


def _output_table(result) -> pa.Table:
    columns: dict[str, pa.Array] = {
        "bin_width_pct": pa.array([result.bin_width_pct], pa.float64()),
        "span_pct": pa.array([result.span_pct], pa.float64()),
    }
    for kind_dist in result.kinds:
        prefix = _KIND_PREFIX[kind_dist.kind]
        s = kind_dist.stats
        columns[f"{prefix}_n_days"] = pa.array([s.n_days], pa.int64())
        for name, value in (
            ("mean_pct", s.mean_pct),
            ("std_pct", s.std_pct),
            ("ann_vol_pct", s.annualized_vol_pct),
            ("skew", s.skewness),
            ("excess_kurt", s.excess_kurtosis),
            ("var95", s.var_95_pct),
            ("cvar95", s.cvar_95_pct),
            ("best_pct", s.best_day.value_pct),
            ("worst_pct", s.worst_day.value_pct),
        ):
            columns[f"{prefix}_{name}"] = pa.array([value], pa.float64())
        for i, b in enumerate(kind_dist.histogram.bins):
            columns[f"{prefix}_bin{i}_count"] = pa.array([b.count], pa.int64())
        for i, expected in enumerate(kind_dist.normal_expected_counts):
            columns[f"{prefix}_norm{i}"] = pa.array([expected], pa.float64())
    return pa.table(columns)


def main() -> None:
    bars_by_day, sessions = _synthetic_bars()
    span = session_windows_ms_utc(min(sessions), max(sessions))
    windows = {w.session_date: (w.open_ms_utc, w.close_ms_utc) for w in span}
    anchors = extract_day_anchors(bars_by_day, windows)
    days = compute_daily_returns(anchors)
    result = build_return_distribution(days, adjustment="raw")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with ipc.new_file(str(OUT_DIR / "input.arrow"), _input_table(bars_by_day).schema) as writer:
        writer.write_table(_input_table(bars_by_day))
    output = _output_table(result)
    with ipc.new_file(str(OUT_DIR / "output.arrow"), output.schema) as writer:
        writer.write_table(output)

    n_bins = len(result.kinds[0].histogram.bins)
    (OUT_DIR / "attribution.md").write_text(
        f"""# RD-001 — Daily return distribution from minute bars

## Layer 1 — market input provenance
Synthetic minute bars, generated by `scripts/fixture_generators/return_distribution.py`
(seed={SEED}): {N_SESSIONS} real NYSE sessions from {min(sessions).isoformat()} to
{max(sessions).isoformat()} (calendar-selected, so the July 2024 half-day is inside),
pre-market bars on even sessions, after-hours bars on every third session, RTH bars at
09:30 / 11:59 / 12:00 / 15:59 ET each session. Prices are rounded to the LEAN deci-cent
grid (4 decimals). No factor file: adjustment is `raw` — corporate-action semantics are
pinned by hand-computed unit tests, not by this fixture.

## Layer 2 — methodology provenance
Module docstring of `app/research/return_distribution.py`: log-space segment returns
displayed as simple percent; bins `[lo, hi)` at {DEFAULT_BIN_WIDTH_PCT} pp width over
±{DEFAULT_SPAN_PCT} pp plus open edge bins; sample std ddof=1; adjusted Fisher-Pearson
skew G1 / bias-corrected Fisher excess kurtosis G2; historical VaR-95 = linear-interpolated
5th percentile; CVaR-95 = mean of the tail at or below VaR; overlay = N·ΔΦ.

## Layer 3 — independent numerical oracle
`tests/fixtures/test_return_distribution_golden.py` recomputes every output column from
`input.arrow` with an independent numpy/scipy path (np.log arrays, boolean-mask binning,
scipy.stats skew/kurtosis bias=False, np.percentile method='linear', scipy.stats.norm.cdf).
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
    print(f"RD-001 written to {OUT_DIR}")


if __name__ == "__main__":
    main()
