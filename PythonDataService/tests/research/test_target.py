"""Tests for forward-log-return computation.

Pin the contract guarantees that the v2 review surfaced as critical:

* ``horizon_minutes`` means **wall-clock minutes**, not "rows offset"
* missing / irregular bars cause NaN, not a silent off-by-one target
* trading-day boundaries are session-local (America/New_York), not UTC
* schema violations fail fast (no silent dedupe / inf coercion).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.target import (
    compute_15min_forward_return,
    compute_forward_log_return,
    validate_return_series,
)


class TestComputeForwardLogReturn:
    def test_single_day_produces_valid_returns(self, sample_bars_single_day: list[dict]) -> None:
        result = compute_forward_log_return(sample_bars_single_day, horizon_minutes=15)

        assert len(result.values) == len(sample_bars_single_day)
        assert result.valid_count > 0
        assert result.horizon_minutes == 15
        assert result.bar_minutes == 1
        assert result.horizon_bars == 15

    def test_last_horizon_bars_are_nan(self, sample_bars_single_day: list[dict]) -> None:
        result = compute_forward_log_return(sample_bars_single_day, horizon_minutes=15)

        # Last 15 bars run off the end of the series.
        assert result.values.iloc[-15:].isna().all()

    def test_no_cross_day_contamination(self, sample_bars_multi_day: list[dict]) -> None:
        result = compute_forward_log_return(sample_bars_multi_day, horizon_minutes=15)

        df = pd.DataFrame(sample_bars_multi_day).sort_values("timestamp").reset_index(drop=True)
        ts_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df["session"] = ts_utc.dt.tz_convert("America/New_York").dt.date

        for _date, day_df in df.groupby("session"):
            day_indices = day_df.index
            if len(day_indices) <= 15:
                continue
            for idx in day_indices[-15:]:
                assert pd.isna(result.values.iloc[idx]), (
                    f"Expected NaN at idx {idx} (session {_date}); "
                    f"got {result.values.iloc[idx]}"
                )

    def test_return_values_are_log_returns(self, sample_bars_single_day: list[dict]) -> None:
        result = compute_forward_log_return(sample_bars_single_day, horizon_minutes=15)

        df = pd.DataFrame(sample_bars_single_day).sort_values("timestamp").reset_index(drop=True)
        first_valid = result.values.first_valid_index()
        if first_valid is not None:
            expected = np.log(df.loc[first_valid + 15, "close"] / df.loc[first_valid, "close"])
            np.testing.assert_allclose(result.values.iloc[first_valid], expected, atol=1e-10)

    def test_horizon_minutes_is_time_not_bars_on_5min_bars(self) -> None:
        """A 5-minute-bar caller asking for horizon_minutes=15 must use a 3-bar offset."""
        bars = [
            {"timestamp": 1_700_000_000_000 + i * 5 * 60_000, "close": 100.0 + i}
            for i in range(20)
        ]

        result = compute_forward_log_return(bars, horizon_minutes=15)

        assert result.bar_minutes == 5
        assert result.horizon_bars == 3
        assert result.horizon_minutes == 15
        # Row 0 must reference row 3 (15 minutes later), not row 15.
        expected = np.log(bars[3]["close"] / bars[0]["close"])
        np.testing.assert_allclose(result.values.iloc[0], expected, atol=1e-12)

    def test_missing_bar_in_window_produces_nan_not_silent_15min(self) -> None:
        """Drop the bar at t=4 from a 1-minute series. Rows whose forward window
        crosses the gap must be NaN, not silently use a 16-minute target."""
        full = [
            {"timestamp": 1_700_000_000_000 + i * 60_000, "close": 100.0 + i}
            for i in range(40)
        ]
        # Drop t=4 (one missing minute).
        bars = [b for i, b in enumerate(full) if i != 4]

        result = compute_forward_log_return(bars, horizon_minutes=15)

        # The bar at index 0 (t=0) wants its window at t=15; with the gap,
        # row 14 of the truncated list lands at t=15 — but row 0's
        # forward partner is now ambiguous. The timestamp-delta gate
        # rejects whichever rows have a mismatched delta. Confirm at
        # least one row whose original window crossed the gap is NaN.
        assert result.invalid_reason_counts.get("window_gap", 0) > 0

    def test_horizon_not_multiple_of_bar_minutes_raises(self) -> None:
        bars = [
            {"timestamp": 1_700_000_000_000 + i * 5 * 60_000, "close": 100.0 + i}
            for i in range(20)
        ]
        with pytest.raises(ValueError, match="not an integer multiple"):
            compute_forward_log_return(bars, horizon_minutes=7)

    def test_session_date_uses_new_york_not_utc(self) -> None:
        """A bar at 16:30 ET (the next UTC date in DST) must share its
        session with the previous 15:30 ET bar. UTC-date masking would
        artificially split them; NY-tz masking keeps them together when
        they're in the same extended-hours session."""
        # 2024-06-03 (DST): 15:30 ET = 19:30 UTC; 16:30 ET = 20:30 UTC.
        # Both share UTC date 2024-06-03 and NY date 2024-06-03 — easy case.
        # Pick the harder case: 19:55 ET on 2024-06-03 = 23:55 UTC, and
        # 20:10 ET on 2024-06-03 = 00:10 UTC on 2024-06-04. UTC-date
        # masking would say "different days"; NY-tz masking says "same
        # session".
        ts1 = pd.Timestamp("2024-06-03 19:55:00", tz="America/New_York").value // 1_000_000
        bars = [
            {"timestamp": ts1 + i * 60_000, "close": 100.0 + i}
            for i in range(30)
        ]

        result = compute_forward_log_return(bars, horizon_minutes=15)

        # Row 0 at 19:55 ET. Row 15 at 20:10 ET. Both NY date = 2024-06-03.
        # If session is computed in UTC, they'd diverge (UTC dates differ
        # at 00:00 UTC = 20:00 ET). With NY masking, the row 0 forward
        # return is valid.
        assert not pd.isna(result.values.iloc[0])
        # No cross_session drops in the first half (all same NY day).
        assert result.invalid_reason_counts.get("cross_session", 0) == 0

    def test_duplicate_timestamps_raise(self) -> None:
        bars = [
            {"timestamp": 1_700_000_000_000, "close": 100.0},
            {"timestamp": 1_700_000_000_000, "close": 101.0},  # duplicate
            {"timestamp": 1_700_000_000_060_000, "close": 102.0},
        ]
        with pytest.raises(ValueError, match="duplicate timestamps"):
            compute_forward_log_return(bars, horizon_minutes=15)

    def test_inf_close_raises(self) -> None:
        bars = [
            {"timestamp": 1_700_000_000_000 + i * 60_000, "close": (np.inf if i == 5 else 100.0 + i)}
            for i in range(20)
        ]
        with pytest.raises(ValueError, match="non-finite/non-numeric"):
            compute_forward_log_return(bars, horizon_minutes=15)

    def test_missing_columns_raise(self) -> None:
        bars = [{"timestamp": 1_700_000_000_000 + i * 60_000} for i in range(20)]
        with pytest.raises(ValueError, match="missing required columns"):
            compute_forward_log_return(bars, horizon_minutes=15)

    def test_empty_bars_raise(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            compute_forward_log_return([], horizon_minutes=15)

    def test_target_metadata_is_audit_trail(self, sample_bars_single_day: list[dict]) -> None:
        result = compute_forward_log_return(sample_bars_single_day, horizon_minutes=15)

        assert result.target_name == "forward_log_return_15m"
        assert result.timezone == "America/New_York"
        assert result.total_count == len(sample_bars_single_day)
        assert isinstance(result.invalid_reason_counts, dict)
        # The drop reasons sum to (total - valid).
        assert (
            sum(result.invalid_reason_counts.values()) + result.valid_count
            == result.total_count
        )


class TestLegacyWrapper:
    """The bar-offset wrapper is retained for Signal Engine call sites that
    haven't migrated. Pin the back-compat shape: positional Series, length
    equal to sorted bars."""

    def test_returns_positional_series_aligned_with_sorted_bars(
        self, sample_bars_single_day: list[dict]
    ) -> None:
        returns = compute_15min_forward_return(sample_bars_single_day, horizon=15)

        assert len(returns) == len(sample_bars_single_day)
        # Positional index, not timestamp-indexed.
        assert returns.index.tolist() == list(range(len(sample_bars_single_day)))


class TestValidateReturnSeries:
    def test_valid_series_passes(self) -> None:
        series = pd.Series(np.random.default_rng(0).normal(0, 0.01, 100))
        assert validate_return_series(series) is True

    def test_thirty_percent_coverage_now_fails(self) -> None:
        """v2 tightens the threshold from 0.30 → 0.70. A 30 %-coverage
        series previously passed; it must fail now."""
        series = pd.Series([np.nan] * 70 + list(np.random.default_rng(0).normal(0, 0.01, 30)))
        assert validate_return_series(series) is False

    def test_seventy_percent_coverage_passes(self) -> None:
        series = pd.Series(
            list(np.random.default_rng(0).normal(0, 0.01, 75)) + [np.nan] * 25
        )
        assert validate_return_series(series) is True

    def test_zero_variance_fails(self) -> None:
        series = pd.Series([0.005] * 100)
        assert validate_return_series(series) is False

    def test_empty_series_fails(self) -> None:
        series = pd.Series([], dtype=float)
        assert validate_return_series(series) is False

    def test_min_non_nan_ratio_is_caller_overridable(self) -> None:
        series = pd.Series([np.nan] * 60 + list(np.random.default_rng(0).normal(0, 0.01, 40)))
        assert validate_return_series(series, min_non_nan_ratio=0.30) is True
        assert validate_return_series(series, min_non_nan_ratio=0.70) is False
