"""Duration-dependent Golden Search activity policy (ADR 0074).

Formula: minimum = ceil(rate * sum(selected NYSE sessions in year / all
  scheduled NYSE sessions in that year)); round once after summing years.
Reference: ADR 0074, expected trade frequency owner decision, 2026-10-01.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_activity.py.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from datetime import date
from fractions import Fraction
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from app.lean_sidecar.trading_calendar import expected_sessions, trading_session_count
from app.utils.session_anchors import et_date_at_ms

if TYPE_CHECKING:
    from app.research.golden_search.protocol import GoldenSearchProtocol

DEFAULT_EXPECTED_TRADES_PER_YEAR = 50


@lru_cache(maxsize=128)
def _year_sessions(year: int) -> int:
    return trading_session_count(date(year, 1, 1), date(year, 12, 31))


def window_activity(rate: int, start_ms: int, end_ms: int) -> dict[str, Any]:
    """Count scheduled sessions in a half-open interval; early closes count once."""
    if isinstance(rate, bool) or not isinstance(rate, int) or rate < 1:
        raise ValueError("Expected trade frequency must be a positive whole number of trades per year.")
    if end_ms <= start_ms:
        raise ValueError("The activity window must end after it starts.")
    selected = Counter(day.year for day in expected_sessions(et_date_at_ms(start_ms), et_date_at_ms(end_ms - 1)))
    years = [
        {"year": year, "selected_sessions": count, "year_sessions": _year_sessions(year)}
        for year, count in sorted(selected.items())
    ]
    duration = sum((Fraction(item["selected_sessions"], item["year_sessions"]) for item in years), Fraction())
    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "trading_sessions": sum(selected.values()),
        "minimum_trades": math.ceil(rate * duration),
        "years": years,
    }


def activity_plan(protocol: GoldenSearchProtocol) -> dict[str, Any] | None:
    """Preflight's counts, frozen in the receipt at lock; no performance data is read."""
    from app.research.golden_search.procedure_history import fold_windows
    from app.research.golden_search.protocol import recent_window_ms

    rate = protocol.expected_trades_per_year
    if rate is None:
        return None
    windows: list[dict[str, Any]] = []

    def add(key: str, label: str, start: int, end: int) -> None:
        windows.append({"key": key, "label": label, **window_activity(rate, start, end)})

    add("development", "Development", protocol.development_start_ms, protocol.development_end_ms)
    if protocol.recent_window:
        add("recent", "Recent fit", *recent_window_ms(protocol))
    add("final", "Final test", protocol.final_start_ms, protocol.final_end_ms)
    folds = fold_windows(protocol)
    for fold in folds:
        add(f"training_{fold.fold_index}", f"Fold {fold.fold_index + 1} training", fold.train_start_ms, fold.train_end_ms)
    if folds:
        add("forward", "All forward tests", folds[0].test_start_ms, folds[-1].test_end_ms)
    return {"expected_trades_per_year": rate, "windows": windows}


def minimum_trades(
    protocol: GoldenSearchProtocol,
    start_ms: int,
    end_ms: int,
    *,
    frozen: Mapping[str, Any] | None = None,
    final: bool = False,
) -> int:
    """Resolve a legacy flat floor or the frequency policy for this exact window."""
    if protocol.expected_trades_per_year is None:
        flat = protocol.exam_min_trades if final else protocol.policy.min_trades
        if flat is None:
            raise ValueError("A plan without an expected trade frequency needs its fixed trade floors.")
        return flat
    if frozen is None:
        return int(window_activity(protocol.expected_trades_per_year, start_ms, end_ms)["minimum_trades"])
    if frozen["expected_trades_per_year"] != protocol.expected_trades_per_year:
        raise ValueError("The study receipt's expected trade frequency does not match its frozen protocol.")
    for window in frozen["windows"]:
        if window["start_ms"] == start_ms and window["end_ms"] == end_ms:
            return int(window["minimum_trades"])
    raise ValueError("The study receipt has no activity policy for this evaluation window.")
