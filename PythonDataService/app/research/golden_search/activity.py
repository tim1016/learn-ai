"""Duration-dependent Golden Search activity policy (ADR 0074 decision 9).

Formula: minimum = ceil(rate * sum(selected NYSE sessions in year / all
  scheduled NYSE sessions in that year)); round once after summing years.
Reference: ADR 0074 decision 9, the expected trade frequency owner decision of
  2026-10-01, specified in https://github.com/tim1016/learn-ai/issues/2811.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_activity.py.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from fractions import Fraction
from functools import lru_cache
from typing import Any

from app.lean_sidecar.trading_calendar import expected_sessions, trading_session_count
from app.research.golden_search.procedure_history import fold_windows
from app.research.golden_search.protocol import GoldenSearchProtocol, SelectionPolicy, recent_window_ms
from app.research.grid_search.service import window_dates

DEFAULT_EXPECTED_TRADES_PER_YEAR = 50

Window = tuple[int, int]


@lru_cache(maxsize=128)
def _year_sessions(year: int) -> int:
    return trading_session_count(date(year, 1, 1), date(year, 12, 31))


def window_activity(rate: int, start_ms: int, end_ms: int) -> dict[str, Any]:
    """Count the scheduled sessions the engine reads in ``[start_ms, end_ms)``; early closes count once."""
    if isinstance(rate, bool) or not isinstance(rate, int) or rate < 1:
        raise ValueError("Expected trade frequency must be a positive whole number of trades per year.")
    if end_ms <= start_ms:
        raise ValueError("The activity window must end after it starts.")
    selected = Counter(day.year for day in expected_sessions(*window_dates(start_ms, end_ms)))
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
    """Each evaluation window's floor, computed once at preflight and frozen in the receipt at lock."""
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
        # Fold tests are contiguous, so this one span is every scheduled fold's test time, failed folds included.
        add("forward", "All forward tests", folds[0].test_start_ms, folds[-1].test_end_ms)
    return {"expected_trades_per_year": rate, "windows": windows}


class TradeFloors:
    """The trade floor each evaluation window of one study must meet.

    A frequency plan reads only the floors its receipt froze at lock, so a
    later calendar update or code change never moves them, and a missing or
    mismatched receipt is refused rather than recomputed. A legacy plan keeps
    its two fixed floors: one for selection, one for the final test.
    """

    def __init__(self, protocol: GoldenSearchProtocol, receipt: Mapping[str, Any]) -> None:
        self._protocol = protocol
        self._frozen: dict[Window, int] | None = None
        rate = protocol.expected_trades_per_year
        if rate is None:
            return
        frozen = receipt.get("activity")
        if frozen is None:
            raise ValueError("The study receipt is missing its expected trade frequency policy.")
        if frozen["expected_trades_per_year"] != rate:
            raise ValueError("The study receipt's expected trade frequency does not match its frozen protocol.")
        self._frozen = {(int(item["start_ms"]), int(item["end_ms"])): int(item["minimum_trades"]) for item in frozen["windows"]}

    @property
    def frequency_based(self) -> bool:
        return self._frozen is not None

    def at(self, window: Window, *, final: bool = False) -> int:
        """The floor for ``window``; ``final`` selects a legacy plan's final-test floor."""
        if self._frozen is None:
            flat = self._protocol.exam_min_trades if final else self._protocol.policy.min_trades
            if flat is None:
                raise ValueError("A plan without an expected trade frequency needs its fixed trade floors.")
            return flat
        if window not in self._frozen:
            raise ValueError("The study receipt has no activity policy for this evaluation window.")
        return self._frozen[window]

    def policy(self, window: Window) -> SelectionPolicy:
        """The plan's selection policy with ``window``'s floor resolved."""
        return replace(self._protocol.policy, min_trades=self.at(window))
