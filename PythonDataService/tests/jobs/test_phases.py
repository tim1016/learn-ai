"""Tests for the phase vocabulary helpers."""

from __future__ import annotations

from app.jobs import phases


class TestFriendlyLabels:
    def test_known_phase_returns_label(self) -> None:
        assert phases.friendly("feature_research", "compute_ic") == "Measuring information coefficient"
        assert phases.friendly("signal_engine", "backtest_grid") == "Sweeping backtest configurations"
        assert phases.friendly("cross_sectional", "starting") == "Starting cross-sectional study"

    def test_unknown_phase_falls_back_to_humanized_id(self) -> None:
        # Per-ticker dynamic phases (ticker_3_AAPL) aren't registered;
        # the helper should still produce a readable label.
        label = phases.friendly("cross_sectional", "ticker_3_AAPL")
        assert "Ticker" in label
        assert "AAPL" in label

    def test_unknown_job_type_falls_back(self) -> None:
        label = phases.friendly("does_not_exist", "some_phase")
        assert label == "Some Phase"

    def test_total_weight_sums_correctly(self) -> None:
        # signal_engine: 1+1+1+1+4+4+1+1+1 = 15
        assert phases.total_weight("signal_engine") == 15

    def test_total_weight_unknown_job_zero(self) -> None:
        assert phases.total_weight("nope") == 0
