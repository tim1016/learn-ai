from __future__ import annotations

import numpy as np
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
    assert len(results) == 1
    result = results[0]
    assert {key: value for key, value in result.items() if key != "data"} == {
        "id": "sma_2",
        "panel": "main",
        "type": "line",
        "color": "#FF9800",
        "refs": [],
    }
    assert [point["t"] for point in result["data"]] == [
        1_700_000_060_000,
        1_700_000_120_000,
        1_700_000_180_000,
    ]
    assert result["data"][0]["value"] is None
    np.testing.assert_allclose(
        [point["value"] for point in result["data"][1:]],
        [101.0, 103.0],
        atol=1e-9,
        rtol=0,
    )


def test_compute_preserves_every_series_from_dataframe_indicators() -> None:
    _, results = ChartIndicatorService().compute(
        "SPY",
        _bars(),
        [{"name": "donchian", "params": {"lower_length": 2, "upper_length": 2}}],
    )

    assert len(results) == 1
    data = results[0]["data"]
    assert isinstance(data, dict)
    assert len(data) == 3
    lower_key = next(key for key in data if key.startswith("dcl"))
    middle_key = next(key for key in data if key.startswith("dcm"))
    upper_key = next(key for key in data if key.startswith("dcu"))
    assert data[lower_key][0]["value"] is None
    np.testing.assert_allclose(
        [point["value"] for point in data[lower_key][1:]],
        [98.0, 99.0],
        atol=1e-9,
        rtol=0,
    )
    np.testing.assert_allclose(
        [point["value"] for point in data[middle_key][1:]],
        [100.5, 102.0],
        atol=1e-9,
        rtol=0,
    )
    np.testing.assert_allclose(
        [point["value"] for point in data[upper_key][1:]],
        [103.0, 105.0],
        atol=1e-9,
        rtol=0,
    )


def test_compute_rejects_duplicate_bar_close_timestamps() -> None:
    bars = _bars()
    bars[2]["t"] = bars[1]["t"]

    with pytest.raises(CanonicalBarsError, match="duplicate timestamp"):
        ChartIndicatorService().compute(
            "SPY",
            bars,
            [{"name": "sma", "params": {"length": 2}}],
        )


@pytest.mark.parametrize(
    ("indicator", "message"),
    [
        ({"name": "not_real", "params": {}}, "unknown chart indicator"),
        ({"name": "sma", "params": {"window": 2}}, "unknown parameter"),
        ({"name": "sma", "params": {"length": 0}}, "must be between"),
        ({"name": "sma", "params": {"length": 2.5}}, "must be an integer"),
    ],
)
def test_compute_rejects_invalid_indicator_recipes(
    indicator: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ChartIndicatorService().compute("SPY", _bars(), [indicator])
