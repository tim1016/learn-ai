"""Tests for the research experiment orchestrator."""
from __future__ import annotations

import pytest

from app.research.config import ResearchConfig
from app.research.runner import run_feature_research


class TestRunFeatureResearch:
    def test_success_with_valid_data(self, sample_bars_single_day: list[dict]) -> None:
        report = run_feature_research(
            ticker="TEST",
            feature_name="momentum_5m",
            bars=sample_bars_single_day,
            start_date="2024-01-01",
            end_date="2024-01-01",
        )

        assert report.ticker == "TEST"
        assert report.feature_name == "momentum_5m"
        assert report.bars_used == len(sample_bars_single_day)
        assert report.error is None
        assert isinstance(report.mean_ic, float)
        assert isinstance(report.adf_pvalue, float)
        assert isinstance(report.passed_validation, bool)

    def test_all_features_run_without_error(self, sample_bars_single_day: list[dict]) -> None:
        features = ["momentum_5m", "rsi_14", "realized_vol_30", "volume_zscore", "macd_signal"]
        for feature in features:
            report = run_feature_research(
                ticker="TEST",
                feature_name=feature,
                bars=sample_bars_single_day,
                start_date="2024-01-01",
                end_date="2024-01-01",
            )
            assert report.error is None, f"Feature {feature} failed: {report.error}"

    def test_insufficient_bars_returns_error(self) -> None:
        bars = [
            {"timestamp": i * 60_000, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1e6}
            for i in range(10)
        ]

        report = run_feature_research(
            ticker="TEST",
            feature_name="momentum_5m",
            bars=bars,
            start_date="2024-01-01",
            end_date="2024-01-01",
        )

        assert report.error is not None
        assert report.passed_validation is False
        assert "Not enough bars" in report.error

    def test_unknown_feature_returns_error(self, sample_bars_single_day: list[dict]) -> None:
        report = run_feature_research(
            ticker="TEST",
            feature_name="nonexistent_feature",
            bars=sample_bars_single_day,
            start_date="2024-01-01",
            end_date="2024-01-01",
        )

        assert report.error is not None
        assert report.passed_validation is False

    def test_report_has_quantile_bins(self, sample_bars_single_day: list[dict]) -> None:
        report = run_feature_research(
            ticker="TEST",
            feature_name="rsi_14",
            bars=sample_bars_single_day,
            start_date="2024-01-01",
            end_date="2024-01-01",
        )

        if report.error is None:
            assert isinstance(report.quantile_bins, list)
            if report.quantile_bins:
                assert "bin_number" in report.quantile_bins[0]
                assert "mean_return" in report.quantile_bins[0]

    def test_custom_config(self, sample_bars_single_day: list[dict]) -> None:
        config = ResearchConfig(
            horizon=10,
            n_bins=3,
            min_series_length=50,
        )

        report = run_feature_research(
            ticker="TEST",
            feature_name="momentum_5m",
            bars=sample_bars_single_day,
            start_date="2024-01-01",
            end_date="2024-01-01",
            config=config,
        )

        assert report.error is None
