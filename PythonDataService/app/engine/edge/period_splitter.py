"""Time-period splits for cross-asset robustness validation.

Three modes (per docs/architecture/edge-feature-design.md §5.2):
- Rolling N-year windows           — slide a fixed-width window
- Calendar buckets                  — one bucket per calendar year
- Walk-forward (anchored)           — train [t0, t0+L], test (t0+L, t0+L+H], slide by H

All inputs and outputs in int64 ms UTC.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

MS_PER_DAY = 86_400_000
APPROX_MS_PER_YEAR = 365 * MS_PER_DAY
APPROX_MS_PER_MONTH = 30 * MS_PER_DAY

SplitMode = Literal["rolling", "calendar", "walkforward"]


@dataclass(frozen=True)
class TimePeriod:
    label: str
    start_ms: int
    end_ms: int


def rolling_windows(
    *, start_ms: int, end_ms: int, window_years: float = 2.0,
    step_months: float = 6.0,
) -> list[TimePeriod]:
    """Sliding fixed-width windows."""
    width = int(window_years * APPROX_MS_PER_YEAR)
    step = int(step_months * APPROX_MS_PER_MONTH)
    out: list[TimePeriod] = []
    cur = start_ms
    while cur + width <= end_ms:
        out.append(TimePeriod(
            label=f"rolling_{_iso(cur)}_{_iso(cur + width)}",
            start_ms=cur, end_ms=cur + width,
        ))
        cur += step
    if not out:
        out.append(TimePeriod(
            label=f"rolling_{_iso(start_ms)}_{_iso(end_ms)}",
            start_ms=start_ms, end_ms=end_ms,
        ))
    return out


def calendar_year_buckets(*, start_ms: int, end_ms: int) -> list[TimePeriod]:
    """One bucket per calendar year (UTC)."""
    start_year = datetime.fromtimestamp(start_ms / 1000.0, tz=UTC).year
    end_year = datetime.fromtimestamp(end_ms / 1000.0, tz=UTC).year
    out: list[TimePeriod] = []
    for year in range(start_year, end_year + 1):
        y_start = int(datetime(year, 1, 1, tzinfo=UTC).timestamp() * 1000)
        y_end = int(datetime(year + 1, 1, 1, tzinfo=UTC).timestamp() * 1000)
        clipped_start = max(y_start, start_ms)
        clipped_end = min(y_end, end_ms)
        if clipped_start < clipped_end:
            out.append(TimePeriod(
                label=f"cal_{year}",
                start_ms=clipped_start, end_ms=clipped_end,
            ))
    return out


def walk_forward(
    *, start_ms: int, end_ms: int, train_years: float = 2.0,
    test_months: float = 6.0,
) -> list[tuple[TimePeriod, TimePeriod]]:
    """Anchored walk-forward: returns list of (train, test) pairs."""
    train_w = int(train_years * APPROX_MS_PER_YEAR)
    test_w = int(test_months * APPROX_MS_PER_MONTH)
    out: list[tuple[TimePeriod, TimePeriod]] = []
    cur = start_ms
    while cur + train_w + test_w <= end_ms:
        train = TimePeriod(
            label=f"train_{_iso(cur)}_{_iso(cur + train_w)}",
            start_ms=cur, end_ms=cur + train_w,
        )
        test = TimePeriod(
            label=f"test_{_iso(cur + train_w)}_{_iso(cur + train_w + test_w)}",
            start_ms=cur + train_w, end_ms=cur + train_w + test_w,
        )
        out.append((train, test))
        cur += test_w
    return out


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).strftime("%Y%m%d")
