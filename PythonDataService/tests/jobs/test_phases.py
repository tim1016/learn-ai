"""Tests for the phase vocabulary helpers."""

from __future__ import annotations

from app.jobs import phases


class TestFriendlyLabels:
    def test_unknown_phase_falls_back_to_humanized_id(self) -> None:
        # Per-ticker dynamic phases (ticker_3_AAPL) aren't registered;
        # the helper should still produce a readable label.
        label = phases.friendly("cross_sectional", "ticker_3_AAPL")
        assert "Ticker" in label
        assert "AAPL" in label

    def test_unknown_job_type_falls_back(self) -> None:
        label = phases.friendly("does_not_exist", "some_phase")
        assert label == "Some Phase"
