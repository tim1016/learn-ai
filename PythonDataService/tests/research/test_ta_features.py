"""Tests for technical analysis feature computation."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.features.ta_features import TechnicalFeatures
from app.research.features.registry import FeatureName, list_available_features


class TestMomentum5m:
    def test_output_length(self, sample_bars_single_day: list[dict]) -> None:
        result = TechnicalFeatures.compute_feature("momentum_5m", sample_bars_single_day)
        assert len(result) == len(sample_bars_single_day)

    def test_first_5_bars_nan(self, sample_bars_single_day: list[dict]) -> None:
        result = TechnicalFeatures.compute_feature("momentum_5m", sample_bars_single_day)
        assert result.iloc[:5].isna().all()

    def test_values_are_reasonable(self, sample_bars_single_day: list[dict]) -> None:
        result = TechnicalFeatures.compute_feature("momentum_5m", sample_bars_single_day)
        valid = result.dropna()
        # Momentum should be small relative values (not hundreds)
        assert (valid.abs() < 1.0).all()


class TestRSI14:
    def test_bounded_0_100(self, sample_bars_single_day: list[dict]) -> None:
        result = TechnicalFeatures.compute_feature("rsi_14", sample_bars_single_day)
        valid = result.dropna()
        assert len(valid) > 0
        assert (valid >= 0).all() and (valid <= 100).all()


class TestRealizedVol30:
    def test_non_negative(self, sample_bars_single_day: list[dict]) -> None:
        result = TechnicalFeatures.compute_feature("realized_vol_30", sample_bars_single_day)
        valid = result.dropna()
        assert len(valid) > 0
        assert (valid >= 0).all()


class TestVolumeZscore:
    def test_centered_near_zero(self, sample_bars_single_day: list[dict]) -> None:
        result = TechnicalFeatures.compute_feature("volume_zscore", sample_bars_single_day)
        valid = result.dropna()
        assert len(valid) > 0
        # Z-scores should have mean near 0
        assert abs(valid.mean()) < 2.0


class TestMACDSignal:
    def test_produces_values(self, sample_bars_single_day: list[dict]) -> None:
        result = TechnicalFeatures.compute_feature("macd_signal", sample_bars_single_day)
        assert len(result) == len(sample_bars_single_day)
        # MACD needs warmup, but should eventually produce values
        assert result.notna().sum() > 0


class TestFeatureDispatcher:
    def test_all_registered_features_compute(self, sample_bars_single_day: list[dict]) -> None:
        for feature_name in list_available_features():
            result = TechnicalFeatures.compute_feature(feature_name, sample_bars_single_day)
            assert isinstance(result, pd.Series)
            assert len(result) == len(sample_bars_single_day)

    def test_unknown_feature_raises(self) -> None:
        bars = [{"timestamp": i * 1000, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1e6} for i in range(50)]
        with pytest.raises(ValueError, match="Unknown feature"):
            TechnicalFeatures.compute_feature("nonexistent_feature", bars)
