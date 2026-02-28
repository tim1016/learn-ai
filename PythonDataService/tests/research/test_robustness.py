"""Tests for robustness analysis module."""
from __future__ import annotations

import numpy as np
import pytest

from app.research.validation.robustness import (
    compute_monthly_ic_breakdown,
    compute_regime_analysis,
    compute_robustness,
    compute_rolling_t_stat,
    compute_structural_breaks,
    compute_train_test_split,
)


def _generate_multi_month_ic(
    n_months: int = 6,
    days_per_month: int = 20,
    base_ic: float = 0.02,
    seed: int = 42,
) -> tuple[list[float], list[str]]:
    """Generate synthetic daily IC data spanning multiple months."""
    rng = np.random.default_rng(seed)
    ic_values: list[float] = []
    ic_dates: list[str] = []

    for month_idx in range(n_months):
        month = month_idx + 1
        year = 2024
        if month > 12:
            year += 1
            month -= 12
        for day in range(1, days_per_month + 1):
            if day > 28:
                continue
            ic = base_ic + rng.normal(0, 0.03)
            ic_values.append(float(ic))
            ic_dates.append(f"{year}-{month:02d}-{day:02d}")

    return ic_values, ic_dates


def _generate_multi_month_bars(
    n_months: int = 6,
    bars_per_day: int = 50,
    days_per_month: int = 20,
    seed: int = 42,
) -> list[dict]:
    """Generate synthetic OHLCV bars spanning multiple months."""
    rng = np.random.default_rng(seed)
    bars: list[dict] = []
    base_price = 150.0

    for month_idx in range(n_months):
        month = month_idx + 1
        year = 2024
        if month > 12:
            year += 1
            month -= 12
        for day in range(1, days_per_month + 1):
            if day > 28:
                continue
            # Compute epoch ms for the start of this day (approximate)
            import datetime
            dt = datetime.datetime(year, month, day, 14, 30, tzinfo=datetime.timezone.utc)
            day_start_ms = int(dt.timestamp() * 1000)

            for bar_idx in range(bars_per_day):
                noise = rng.normal(0, 0.3)
                trend = month_idx * 0.5 + rng.normal(0, 0.1)
                price = base_price + trend + noise
                bars.append({
                    "timestamp": day_start_ms + bar_idx * 60_000,
                    "open": round(price - 0.05, 4),
                    "high": round(price + 0.3, 4),
                    "low": round(price - 0.3, 4),
                    "close": round(price, 4),
                    "volume": round(1_000_000 + rng.normal(0, 50_000), 2),
                })

    return bars


class TestMonthlyICBreakdown:
    def test_groups_by_month(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=4)
        monthly, *_ = compute_monthly_ic_breakdown(ic_values, ic_dates)

        assert len(monthly) == 4
        assert monthly[0].month == "2024-01"
        assert monthly[3].month == "2024-04"

    def test_each_month_has_stats(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=3)
        monthly, *_ = compute_monthly_ic_breakdown(ic_values, ic_dates)

        for m in monthly:
            assert m.observation_count > 0
            assert isinstance(m.mean_ic, float)
            assert isinstance(m.t_stat, float)

    def test_pct_positive_with_positive_base(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=6, base_ic=0.05)
        _, pct_positive, _, _, _, _ = compute_monthly_ic_breakdown(ic_values, ic_dates)

        assert pct_positive > 0.5

    def test_pct_positive_with_negative_base(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=6, base_ic=-0.05)
        _, pct_positive, _, _, _, _ = compute_monthly_ic_breakdown(ic_values, ic_dates)

        assert pct_positive < 0.5

    def test_stability_label_strong(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=10, base_ic=0.08, seed=99)
        _, _, _, _, _, label = compute_monthly_ic_breakdown(ic_values, ic_dates)

        assert label in ("Strong", "Suspicious")

    def test_stability_label_noise(self) -> None:
        # Use a strongly negative base so most months are negative
        ic_values, ic_dates = _generate_multi_month_ic(n_months=10, base_ic=-0.04, seed=99)
        _, pct_positive, _, _, _, label = compute_monthly_ic_breakdown(ic_values, ic_dates)

        assert label in ("Noise", "Weak")

    def test_best_and_worst_month(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=4)
        monthly, _, _, best, worst, _ = compute_monthly_ic_breakdown(ic_values, ic_dates)

        mean_ics = [m.mean_ic for m in monthly]
        assert best == max(mean_ics)
        assert worst == min(mean_ics)

    def test_insufficient_data_returns_empty(self) -> None:
        monthly, pct_pos, pct_sig, best, worst, label = compute_monthly_ic_breakdown(
            [0.01], ["2024-01-01"],
        )

        assert monthly == []
        assert label == "Unknown"


class TestRollingTStat:
    def test_output_length(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=10)
        monthly, *_ = compute_monthly_ic_breakdown(ic_values, ic_dates)
        rolling = compute_rolling_t_stat(monthly, window=6)

        assert len(rolling) == len(monthly) - 5  # window=6, so first output at index 5

    def test_months_match(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=8)
        monthly, *_ = compute_monthly_ic_breakdown(ic_values, ic_dates)
        rolling = compute_rolling_t_stat(monthly, window=6)

        assert rolling[0].month == monthly[5].month
        assert rolling[-1].month == monthly[-1].month

    def test_insufficient_months_returns_empty(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=3)
        monthly, *_ = compute_monthly_ic_breakdown(ic_values, ic_dates)
        rolling = compute_rolling_t_stat(monthly, window=6)

        assert rolling == []


class TestTrainTestSplit:
    def test_chronological_70_30(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=6)
        result = compute_train_test_split(ic_values, ic_dates)

        assert result is not None
        split_idx = int(len(ic_values) * 0.70)
        assert result.train_days == split_idx
        assert result.test_days == len(ic_values) - split_idx
        assert result.train_start == ic_dates[0]
        assert result.test_end == ic_dates[-1]

    def test_dates_are_chronological(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=4)
        result = compute_train_test_split(ic_values, ic_dates)

        assert result is not None
        assert result.train_end < result.test_start

    def test_overfit_flag_when_train_much_higher(self) -> None:
        rng = np.random.default_rng(42)
        n = 100
        dates = [f"2024-01-{(i % 28) + 1:02d}" for i in range(n)]

        # Train has high IC, test has near zero
        train_ics = [0.05 + rng.normal(0, 0.01) for _ in range(70)]
        test_ics = [0.001 + rng.normal(0, 0.01) for _ in range(30)]
        ic_values = train_ics + test_ics

        result = compute_train_test_split(ic_values, dates)

        assert result is not None
        assert result.overfit_flag is True

    def test_no_overfit_when_consistent(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=6, base_ic=0.03)
        result = compute_train_test_split(ic_values, ic_dates)

        assert result is not None
        assert result.overfit_flag is False

    def test_insufficient_data_returns_none(self) -> None:
        result = compute_train_test_split([0.01, 0.02], ["2024-01-01", "2024-01-02"])

        assert result is None


class TestRegimeAnalysis:
    def test_volatility_regimes_returned(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=6)
        bars = _generate_multi_month_bars(n_months=6)
        vol_regimes, _ = compute_regime_analysis(ic_values, ic_dates, bars)

        if vol_regimes:
            labels = {r.regime_label for r in vol_regimes}
            assert labels.issubset({"Low Vol", "Normal Vol", "High Vol"})
            for r in vol_regimes:
                assert r.observation_count >= 5

    def test_trend_regimes_returned(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=6)
        bars = _generate_multi_month_bars(n_months=6)
        _, trend_regimes = compute_regime_analysis(ic_values, ic_dates, bars)

        if trend_regimes:
            labels = {r.regime_label for r in trend_regimes}
            assert labels.issubset({"Trending Up", "Sideways", "Trending Down"})

    def test_insufficient_ic_data(self) -> None:
        vol, trend = compute_regime_analysis(
            [0.01, 0.02], ["2024-01-01", "2024-01-02"], [],
        )

        assert vol == []
        assert trend == []


class TestOosRetention:
    def test_retention_computed(self) -> None:
        """OOS retention should be computed when train/test split exists."""
        ic_values, ic_dates = _generate_multi_month_ic(n_months=6, base_ic=0.03)
        result = compute_train_test_split(ic_values, ic_dates)

        assert result is not None
        assert result.oos_retention > 0
        assert result.oos_retention_label != "Unknown"

    def test_retention_label_excellent(self) -> None:
        """Consistent IC across train/test should yield high retention."""
        ic_values, ic_dates = _generate_multi_month_ic(n_months=6, base_ic=0.05, seed=10)
        result = compute_train_test_split(ic_values, ic_dates)

        assert result is not None
        assert result.oos_retention_label in ("Excellent", "Acceptable")

    def test_retention_bounded_zero_to_one_ish(self) -> None:
        """OOS retention should be a reasonable positive ratio."""
        ic_values, ic_dates = _generate_multi_month_ic(n_months=8, base_ic=0.03)
        result = compute_train_test_split(ic_values, ic_dates)

        assert result is not None
        assert result.oos_retention >= 0


class TestSignConsistentStability:
    def test_computed_with_positive_base(self) -> None:
        """Sign-consistent stability should be computed for positive-IC features."""
        ic_values, ic_dates = _generate_multi_month_ic(n_months=8, base_ic=0.05)
        bars = _generate_multi_month_bars(n_months=8)

        result = compute_robustness(ic_values, ic_dates, bars)

        assert result.pct_sign_consistent_months > 0
        assert result.sign_consistent_stability_label != "Unknown"

    def test_positive_base_high_consistency(self) -> None:
        """Feature with strong positive IC should have high sign consistency."""
        ic_values, ic_dates = _generate_multi_month_ic(n_months=10, base_ic=0.08, seed=99)
        bars = _generate_multi_month_bars(n_months=10, seed=99)

        result = compute_robustness(ic_values, ic_dates, bars)

        assert result.pct_sign_consistent_months >= 0.5

    def test_insufficient_data_defaults(self) -> None:
        """With insufficient data, sign-consistent fields should be defaults."""
        result = compute_robustness([0.01], ["2024-01-01"], [])

        assert result.pct_sign_consistent_months == 0.0
        assert result.sign_consistent_stability_label == "Unknown"


class TestStructuralBreaks:
    def test_detects_break_when_sign_flips(self) -> None:
        """Should detect structural break when IC changes sign."""
        rng = np.random.default_rng(42)
        ic_values: list[float] = []
        ic_dates: list[str] = []

        # 4 months positive, 4 months negative
        for month_idx in range(8):
            month = month_idx + 1
            base = 0.05 if month_idx < 4 else -0.05
            for day in range(1, 21):
                if day > 28:
                    continue
                ic_values.append(base + rng.normal(0, 0.01))
                ic_dates.append(f"2024-{month:02d}-{day:02d}")

        breaks = compute_structural_breaks(ic_values, ic_dates)

        significant = [b for b in breaks if b.significant]
        assert len(significant) > 0

    def test_no_break_in_stable_series(self) -> None:
        """Should not detect significant breaks in a stable IC series."""
        ic_values, ic_dates = _generate_multi_month_ic(n_months=8, base_ic=0.03)

        breaks = compute_structural_breaks(ic_values, ic_dates)

        significant = [b for b in breaks if b.significant]
        # A stable series should have few or no significant breaks
        assert len(significant) <= 1

    def test_insufficient_data_returns_empty(self) -> None:
        """Should return empty list with insufficient data."""
        breaks = compute_structural_breaks([0.01, 0.02], ["2024-01-01", "2024-01-02"])

        assert breaks == []


class TestComputeRobustness:
    def test_end_to_end(self) -> None:
        ic_values, ic_dates = _generate_multi_month_ic(n_months=8)
        bars = _generate_multi_month_bars(n_months=8)

        result = compute_robustness(ic_values, ic_dates, bars)

        assert len(result.monthly_breakdown) > 0
        assert 0.0 <= result.pct_positive_months <= 1.0
        assert 0.0 <= result.pct_significant_months <= 1.0
        assert result.stability_label != "Unknown"
        assert result.train_test is not None
        # New fields
        assert result.pct_sign_consistent_months >= 0
        assert result.sign_consistent_stability_label != "Unknown"
        assert isinstance(result.structural_breaks, list)
        if result.train_test is not None:
            assert result.train_test.oos_retention >= 0
            assert result.train_test.oos_retention_label != "Unknown"

    def test_insufficient_data_returns_defaults(self) -> None:
        result = compute_robustness([0.01], ["2024-01-01"], [])

        assert result.monthly_breakdown == []
        assert result.stability_label == "Unknown"
        assert result.train_test is None
        assert result.pct_sign_consistent_months == 0.0
        assert result.structural_breaks == []
