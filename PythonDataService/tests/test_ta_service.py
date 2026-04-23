"""Unit tests for TechnicalAnalysisService"""

from app.services.ta_service import TechnicalAnalysisService
from tests.conftest import make_sample_bars


def test_sma_returns_correct_structure():
    bars = make_sample_bars(30)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "sma", "window": 10}])

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
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "ema", "window": 10}])

    assert len(results) == 1
    assert results[0]["name"] == "ema"
    # EMA has a warmup period but produces more points than SMA
    assert len(results[0]["data"]) > 0


def test_rsi_values_in_valid_range():
    bars = make_sample_bars(50)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "rsi", "window": 14}])

    assert len(results) == 1
    assert results[0]["name"] == "rsi"
    assert len(results[0]["data"]) > 0
    # RSI should be between 0 and 100
    for point in results[0]["data"]:
        assert 0 <= point["value"] <= 100


def test_unknown_indicator_is_skipped():
    bars = make_sample_bars(30)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "unknown_indicator", "window": 10}])

    assert len(results) == 0


def test_macd_returns_three_components():
    bars = make_sample_bars(50)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "macd", "window": 26}])

    assert len(results) == 1
    assert results[0]["name"] == "macd"
    assert len(results[0]["data"]) > 0
    point = results[0]["data"][0]
    assert "value" in point
    assert "signal" in point
    assert "histogram" in point
    assert isinstance(point["value"], float)


def test_stoch_returns_k_and_d_lines():
    bars = make_sample_bars(50)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "stoch", "window": 14}])

    assert len(results) == 1
    assert results[0]["name"] == "stoch"
    assert results[0]["window"] == 14
    assert len(results[0]["data"]) > 0
    point = results[0]["data"][0]
    assert "timestamp" in point
    assert "value" in point
    assert isinstance(point["value"], float)


def test_stoch_values_in_valid_range():
    bars = make_sample_bars(50)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "stoch", "window": 14}])

    for point in results[0]["data"]:
        assert 0 <= point["value"] <= 100, f"%K out of range: {point['value']}"
        if point["signal"] is not None:
            assert 0 <= point["signal"] <= 100, f"%D out of range: {point['signal']}"


def test_stoch_has_signal_line():
    bars = make_sample_bars(50)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "stoch", "window": 14}])

    signals = [p for p in results[0]["data"] if p.get("signal") is not None]
    assert len(signals) > 0, "Stochastic should have %D (signal) values"


def test_stoch_custom_window():
    bars = make_sample_bars(50)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "stoch", "window": 5}])

    assert results[0]["window"] == 5
    assert len(results[0]["data"]) > 0


def test_multiple_indicators_calculated():
    bars = make_sample_bars(30)
    results = TechnicalAnalysisService.calculate_indicators(
        bars,
        [
            {"name": "sma", "window": 5},
            {"name": "ema", "window": 10},
        ],
    )

    assert len(results) == 2
    assert results[0]["name"] == "sma"
    assert results[1]["name"] == "ema"


def test_stoch_with_other_indicators():
    bars = make_sample_bars(50)
    results = TechnicalAnalysisService.calculate_indicators(
        bars,
        [
            {"name": "rsi", "window": 14},
            {"name": "stoch", "window": 14},
            {"name": "macd", "window": 26},
        ],
    )

    assert len(results) == 3
    names = [r["name"] for r in results]
    assert "rsi" in names
    assert "stoch" in names
    assert "macd" in names


# ------------------------------------------------------------------
# Indicator Table (generate_indicator_table) tests
# ------------------------------------------------------------------


def test_generate_indicator_table_returns_all_columns():
    bars = make_sample_bars(250)
    rows = TechnicalAnalysisService.generate_indicator_table(bars, ema_periods=[5, 10, 20])

    assert len(rows) == 250
    row = rows[-1]
    expected_keys = [
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "bb_basis",
        "bb_upper",
        "bb_lower",
        "supertrend_up",
        "supertrend_down",
        "ema_5",
        "ema_10",
        "ema_20",
        "rsi",
        "rsi_ma",
        "macd",
        "macd_histogram",
        "macd_signal",
        "adx",
    ]
    for key in expected_keys:
        assert key in row, f"Missing key: {key}"


def test_generate_indicator_table_nan_replaced_with_none():
    bars = make_sample_bars(30)
    rows = TechnicalAnalysisService.generate_indicator_table(bars, ema_periods=[5, 10])

    # First row should have None for indicators that need warmup
    first = rows[0]
    assert first["bb_basis"] is None  # BB needs 20 bars
    assert first["adx"] is None  # ADX needs warmup
    # No NaN should leak through
    for row in rows:
        for k, v in row.items():
            if isinstance(v, float):
                import math

                assert not math.isnan(v), f"NaN found in row for key {k}"


def test_generate_indicator_table_ema_values_populated():
    bars = make_sample_bars(250)
    rows = TechnicalAnalysisService.generate_indicator_table(bars, ema_periods=[5, 10, 20, 50])

    # Last row should have all EMAs populated
    last = rows[-1]
    assert last["ema_5"] is not None
    assert last["ema_10"] is not None
    assert last["ema_20"] is not None
    assert last["ema_50"] is not None


def test_generate_indicator_table_rsi_range():
    bars = make_sample_bars(250)
    rows = TechnicalAnalysisService.generate_indicator_table(bars)

    for row in rows:
        if row["rsi"] is not None:
            assert 0 <= row["rsi"] <= 100, f"RSI out of range: {row['rsi']}"


def test_generate_indicator_table_supertrend_exclusive():
    """Supertrend should show up or down, never both at the same time."""
    bars = make_sample_bars(250)
    rows = TechnicalAnalysisService.generate_indicator_table(bars)

    for row in rows:
        up = row.get("supertrend_up")
        down = row.get("supertrend_down")
        # At most one should be populated
        assert not (up is not None and down is not None), "Supertrend up and down should not both be present"


# ---------------------------------------------------------------------------
# None-guard regression (audit § 1.1 / PR 3)
#
# Before fix: ta.sma/ta.ema/ta.rsi return None when window > len(df), and
# _calc_* passed None to _series_to_points which called .iloc on it, producing
# HTTP 500 'NoneType' object has no attribute 'iloc'.
# After fix: None/empty returns [] so the endpoint responds 200 with empty data.
# ---------------------------------------------------------------------------


def test_sma_window_exceeds_bars_returns_empty():
    bars = make_sample_bars(2)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "sma", "window": 5}])

    assert len(results) == 1
    assert results[0]["data"] == []


def test_ema_window_exceeds_bars_returns_empty():
    bars = make_sample_bars(2)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "ema", "window": 5}])

    assert len(results) == 1
    assert results[0]["data"] == []


def test_rsi_window_exceeds_bars_returns_empty():
    bars = make_sample_bars(2)
    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "rsi", "window": 5}])

    assert len(results) == 1
    assert results[0]["data"] == []


def test_rsi_warmup_region_is_masked():
    """Regression for audit § 1.2 — pandas-ta RSI disagrees with streaming Wilders
    by up to 14 points in the warmup region. Mask until 3*period bars.
    """
    window = 14
    bars = make_sample_bars(100)  # enough to have output beyond the mask

    results = TechnicalAnalysisService.calculate_indicators(bars, [{"name": "rsi", "window": window}])

    assert len(results) == 1
    data = results[0]["data"]
    # Mask covers bar indices [0, 3*window) => 42. Emitted timestamps must all
    # come from bar index >= 42.
    emitted_timestamps = {p["timestamp"] for p in data}
    masked_bar_timestamps = {b["timestamp"] for b in bars[: 3 * window]}
    assert not (emitted_timestamps & masked_bar_timestamps), (
        "RSI warmup region (bars 0..3*window) must not appear in output"
    )
