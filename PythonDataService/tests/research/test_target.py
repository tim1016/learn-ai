"""Tests for forward-log-return computation.

Pin the contract guarantees that the v2 review surfaced as critical:

* ``horizon_minutes`` means **wall-clock minutes**, not "rows offset"
* missing / irregular bars cause NaN, not a silent off-by-one target
* trading-day boundaries are session-local (America/New_York), not UTC
* schema violations fail fast (no silent dedupe / inf coercion).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import pytest

from app.research.target import (
    compute_15min_forward_return,
    compute_forward_log_return,
    validate_return_series,
)


class TestComputeForwardLogReturn:
    def test_single_day_forward_return_values_and_audit_metadata(self, sample_bars_single_day: list[dict]) -> None:
        result = compute_forward_log_return(sample_bars_single_day, horizon_minutes=15)

        assert len(result.values) == len(sample_bars_single_day), "single day produces one value per bar"
        assert result.valid_count > 0, "single day produces valid returns"
        assert result.horizon_minutes == 15, "horizon_minutes is 15"
        assert result.bar_minutes == 1, "bar_minutes is 1"
        assert result.horizon_bars == 15, "horizon_bars is 15"

        # Last 15 bars run off the end of the series.
        assert result.values.iloc[-15:].isna().all(), "the last horizon bars are NaN"

        df = pd.DataFrame(sample_bars_single_day).sort_values("timestamp").reset_index(drop=True)
        first_valid = result.values.first_valid_index()
        if first_valid is not None:
            expected = np.log(df.loc[first_valid + 15, "close"] / df.loc[first_valid, "close"])
            np.testing.assert_allclose(
                result.values.iloc[first_valid], expected, atol=1e-10, err_msg="return values are log returns"
            )

        assert result.target_name == "forward_log_return_15m", "target metadata: target_name"
        assert result.timezone == "America/New_York", "target metadata: timezone"
        assert result.total_count == len(sample_bars_single_day), "target metadata: total_count"
        assert isinstance(result.invalid_reason_counts, dict), "target metadata: invalid_reason_counts is a dict"
        # The drop reasons sum to (total - valid).
        assert (
            sum(result.invalid_reason_counts.values()) + result.valid_count
            == result.total_count
        ), "target metadata: the drop reasons sum to (total - valid)"

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

    def test_small_missing_gap_is_tolerated_within_overshoot_and_dropped_in_strict_mode(self) -> None:
        """One missing minute in the forward window stretches the elapsed
        time from 15 → 16 minutes. The default ``max_overshoot_minutes=5``
        accepts that — Polygon Starter routinely drops 1-3 low-liquidity
        minutes per session and rejecting all of them was killing > 30 %
        of valid bars on real data.

        ``max_overshoot_minutes=0`` is the legacy-strict gate: any
        non-exact match drops as ``window_gap``. Useful when the caller
        wants the strictest possible interpretation (e.g. a regression
        test asserting feed integrity)."""
        full = [
            {"timestamp": 1_700_000_000_000 + i * 60_000, "close": 100.0 + i}
            for i in range(40)
        ]
        bars = [b for i, b in enumerate(full) if i != 4]  # drop t=4

        result = compute_forward_log_return(bars, horizon_minutes=15)
        strict = compute_forward_log_return(
            bars, horizon_minutes=15, max_overshoot_minutes=0
        )

        # No window_gap drops — 16-minute deltas land inside [15, 20].
        assert result.invalid_reason_counts.get("window_gap", 0) == 0, "small gap is tolerated: no window_gap drops"
        # And we still get valid forward returns for early rows.
        assert result.values.iloc[0:5].notna().all(), "small gap is tolerated: early rows stay valid"
        # With strict gate, the 16-minute window for the early rows is rejected.
        assert strict.invalid_reason_counts.get("window_gap", 0) > 0, "max_overshoot_minutes=0 is strict: window_gap drops"

    def test_large_missing_gap_above_overshoot_is_dropped(self) -> None:
        """An 8-minute gap inside the forward window pushes the elapsed
        wall-clock time from 15 → 23 minutes — outside the 5-minute
        tolerance — so it's NaN and attributed to ``window_gap``.
        This still catches the structural "75-minute target on 5-minute
        bars" bug: a 5-minute caller asking for 15 minutes who somehow
        received a 75-minute window would land at +60 minutes overshoot,
        far outside any sensible tolerance."""
        full = [
            {"timestamp": 1_700_000_000_000 + i * 60_000, "close": 100.0 + i}
            for i in range(60)
        ]
        # Drop 8 consecutive minutes (t=4..t=11) inside the forward window
        # of the early rows. Row 0 → row 7 still has elapsed = 15 min,
        # but row 0 → row 15 has elapsed = 15 + 8 = 23 min, > tolerance.
        bars = [b for i, b in enumerate(full) if not (4 <= i < 12)]

        result = compute_forward_log_return(bars, horizon_minutes=15)

        assert result.invalid_reason_counts.get("window_gap", 0) > 0

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

    @pytest.mark.parametrize(
        ("make_bars", "horizon_minutes", "match"),
        [
            pytest.param(
                lambda: [
                    {"timestamp": 1_700_000_000_000 + i * 5 * 60_000, "close": 100.0 + i}
                    for i in range(20)
                ],
                7,
                "not an integer multiple",
                id="horizon_not_multiple_of_bar_minutes",
            ),
            pytest.param(
                lambda: [
                    {"timestamp": 1_700_000_000_000, "close": 100.0},
                    {"timestamp": 1_700_000_000_000, "close": 101.0},  # duplicate
                    {"timestamp": 1_700_000_000_060_000, "close": 102.0},
                ],
                15,
                "duplicate timestamps",
                id="duplicate_timestamps",
            ),
            pytest.param(
                lambda: [
                    {"timestamp": 1_700_000_000_000 + i * 60_000, "close": (np.inf if i == 5 else 100.0 + i)}
                    for i in range(20)
                ],
                15,
                "non-finite/non-numeric",
                id="inf_close",
            ),
            pytest.param(
                lambda: [{"timestamp": 1_700_000_000_000 + i * 60_000} for i in range(20)],
                15,
                "missing required columns",
                id="missing_columns",
            ),
            pytest.param(lambda: [], 15, "empty", id="empty_bars"),
        ],
    )
    def test_invalid_bars_or_horizon_raise(
        self, make_bars: Callable[[], list[dict]], horizon_minutes: int, match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            compute_forward_log_return(make_bars(), horizon_minutes=horizon_minutes)


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
    @pytest.mark.parametrize(
        ("make_series", "expected"),
        [
            pytest.param(lambda: pd.Series(np.random.default_rng(0).normal(0, 0.01, 100)), True, id="valid_series_passes"),
            # v2 tightens the threshold from 0.30 → 0.70. A 30 %-coverage
            # series previously passed; it must fail now.
            pytest.param(
                lambda: pd.Series([np.nan] * 70 + list(np.random.default_rng(0).normal(0, 0.01, 30))),
                False,
                id="thirty_percent_coverage_now_fails",
            ),
            pytest.param(
                lambda: pd.Series(list(np.random.default_rng(0).normal(0, 0.01, 75)) + [np.nan] * 25),
                True,
                id="seventy_percent_coverage_passes",
            ),
            pytest.param(lambda: pd.Series([0.005] * 100), False, id="zero_variance_fails"),
            pytest.param(lambda: pd.Series([], dtype=float), False, id="empty_series_fails"),
        ],
    )
    def test_validate_return_series_verdict(self, make_series: Callable[[], pd.Series], expected: bool) -> None:
        assert validate_return_series(make_series()) is expected

    def test_min_non_nan_ratio_is_caller_overridable(self) -> None:
        series = pd.Series([np.nan] * 60 + list(np.random.default_rng(0).normal(0, 0.01, 40)))
        assert validate_return_series(series, min_non_nan_ratio=0.30) is True
        assert validate_return_series(series, min_non_nan_ratio=0.70) is False
