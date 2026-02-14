"""Unit tests for TechnicalAnalysisService"""
from app.services.ta_service import TechnicalAnalysisService
from tests.conftest import make_sample_bars


def test_sma_returns_correct_structure():
    bars = make_sample_bars(30)
    results = TechnicalAnalysisService.calculate_indicators(
        bars, [{"name": "sma", "window": 10}]
    )

    assert len(results) == 1
    assert results[0]["name"] == "sma"
    assert results[0]["window"] == 10
    # SMA with window=10 needs 10 data points, so result count = 30 - 10 + 1 = 21
    assert len(results[0]["data"]) == 21
    # Each point should have timestamp and value
    point = results[0]["data"][0]
    assert "timestamp" in point
    assert "value" in point
    assert isinstance(point["value"], float)


def test_ema_returns_correct_structure():
    bars = make_sample_bars(30)
    results = TechnicalAnalysisService.calculate_indicators(
        bars, [{"name": "ema", "window": 10}]
    )

    assert len(results) == 1
    assert results[0]["name"] == "ema"
    # EMA has a warmup period but produces more points than SMA
    assert len(results[0]["data"]) > 0


def test_rsi_values_in_valid_range():
    bars = make_sample_bars(50)
    results = TechnicalAnalysisService.calculate_indicators(
        bars, [{"name": "rsi", "window": 14}]
    )

    assert len(results) == 1
    assert results[0]["name"] == "rsi"
    assert len(results[0]["data"]) > 0
    # RSI should be between 0 and 100
    for point in results[0]["data"]:
        assert 0 <= point["value"] <= 100


def test_unknown_indicator_is_skipped():
    bars = make_sample_bars(30)
    results = TechnicalAnalysisService.calculate_indicators(
        bars, [{"name": "unknown_indicator", "window": 10}]
    )

    assert len(results) == 0


def test_multiple_indicators_calculated():
    bars = make_sample_bars(30)
    results = TechnicalAnalysisService.calculate_indicators(
        bars, [
            {"name": "sma", "window": 5},
            {"name": "ema", "window": 10},
        ]
    )

    assert len(results) == 2
    assert results[0]["name"] == "sma"
    assert results[1]["name"] == "ema"
