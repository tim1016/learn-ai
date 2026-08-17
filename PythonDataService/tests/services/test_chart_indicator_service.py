from __future__ import annotations

import pytest

from app.services.chart_indicator_service import ChartIndicatorService
from app.services.dataset_service import CanonicalBarsError


def _bars() -> list[dict[str, int | float]]:
    return [
        {"t": 1_700_000_060_000, "o": 99.0, "h": 101.0, "l": 98.0, "c": 100.0, "v": 10.0},
        {"t": 1_700_000_120_000, "o": 100.0, "h": 103.0, "l": 99.0, "c": 102.0, "v": 11.0},
        {"t": 1_700_000_180_000, "o": 102.0, "h": 105.0, "l": 101.0, "c": 104.0, "v": 12.0},
    ]


def test_compute_preserves_bar_close_timestamps_and_canonical_indicator_values() -> None:
    symbol, results = ChartIndicatorService().compute(
        "spy",
        _bars(),
        [{"name": "sma", "params": {"length": 2}}],
    )

    assert symbol == "SPY"
    assert results == [
        {
            "id": "sma_2",
            "panel": "main",
            "type": "line",
            "color": "#FF9800",
            "data": [
                {"t": 1_700_000_060_000, "value": None},
                {"t": 1_700_000_120_000, "value": 101.0},
                {"t": 1_700_000_180_000, "value": 103.0},
            ],
            "refs": [],
        }
    ]


def test_compute_rejects_duplicate_bar_close_timestamps() -> None:
    bars = _bars()
    bars[2]["t"] = bars[1]["t"]

    with pytest.raises(CanonicalBarsError, match="duplicate timestamp"):
        ChartIndicatorService().compute(
            "SPY",
            bars,
            [{"name": "sma", "params": {"length": 2}}],
        )
