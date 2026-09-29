"""Regression tests for strict finite chart-bar ingestion."""

from __future__ import annotations

import pytest

from app.services.chart_service import _preprocess_minute_bars
from app.services.dataset_service import CanonicalBarsError, resolve_indicator_window

_WINDOW = resolve_indicator_window("2026-01-05", max_lookback=0, bar_minutes=1)


def _bar(timestamp: int) -> dict[str, float | int]:
    return {
        "timestamp": timestamp,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1_000.0,
    }


def test_chart_preprocess_rejects_duplicate_timestamps() -> None:
    bars = [_bar(1_767_621_600_000), _bar(1_767_621_600_000)]

    with pytest.raises(CanonicalBarsError, match="duplicate timestamp"):
        _preprocess_minute_bars(bars, _WINDOW, "2026-01-05", "rth", False)


def test_chart_preprocess_rejects_non_monotonic_timestamps() -> None:
    bars = [_bar(1_767_621_660_000), _bar(1_767_621_600_000)]

    with pytest.raises(CanonicalBarsError, match="non-monotonic timestamp"):
        _preprocess_minute_bars(bars, _WINDOW, "2026-01-05", "rth", False)
