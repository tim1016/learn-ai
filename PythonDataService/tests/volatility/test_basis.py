"""Tests for ACT/365 ↔ TRD/252 IV basis conversion (Step 1 of IV-RV alignment)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.engine.edge.vrp import compute_vrp
from app.volatility.basis import (
    convert_iv_act365_to_trading252,
    convert_iv_trading252_to_act365,
    nyse_trading_days_in_window,
)
from app.volatility.conventions import (
    CALENDAR_DAYS_PER_YEAR,
    TRADING_DAYS_PER_YEAR,
)


class TestNyseTradingDayCount:
    """Anchor the trading-day counter against the published NYSE schedule."""

    @pytest.mark.parametrize(
        ("asof", "expected_days"),
        [
            # 2024-03-04 Mon, 30-day window covers 03-04 .. 04-02 inclusive.
            # Good Friday 2024-03-29 is closed. Weekdays in window = 22, minus 1 holiday.
            pytest.param("2024-03-04", 21, id="normal_30d_window_from_march_4"),
            # 2024-11-25 Mon, 30-day window covers 11-25 .. 12-24.
            # Holidays: Thanksgiving Thu 11-28 closed (Black Friday 11-29 is early
            # close but trades — counted). Weekdays = 22, minus 1 holiday.
            pytest.param("2024-11-25", 21, id="thanksgiving_30d_window"),
            # 2024-12-23 Mon, 30-day window covers 12-23 .. 01-21-2025.
            # Closed: Christmas Wed 12-25, New Year's Wed 01-01,
            # National Day of Mourning for Jimmy Carter Thu 01-09-2025,
            # MLK Mon 01-20. Weekdays = 22, minus 4 closures = 18.
            pytest.param("2024-12-23", 18, id="dense_holiday_window"),
        ],
    )
    def test_30d_window_trading_day_count(self, asof, expected_days):
        n = nyse_trading_days_in_window(pd.Timestamp(asof), 30)
        assert n == expected_days

    def test_int_ms_utc_input(self):
        # 2024-03-04 14:30 UTC = 09:30 ET → ET date 03-04, same as naive 03-04.
        ts_ms = int(pd.Timestamp("2024-03-04 14:30", tz="UTC").value // 10**6)
        assert nyse_trading_days_in_window(ts_ms, 30) == 21, "int ms-UTC input resolves to the ET date"
        # A timestamp read out of an int64 array is np.int64, which is not a
        # Python int; it must take the same ms-UTC branch.
        assert nyse_trading_days_in_window(np.int64(ts_ms), 30) == 21, "np.int64 ms-UTC input takes the same branch"

    def test_bool_is_not_a_timestamp(self):
        with pytest.raises(TypeError, match="asof"):
            nyse_trading_days_in_window(True, 30)

    def test_late_utc_belongs_to_prior_et_date(self):
        # 2024-03-05 04:00 UTC = 2024-03-04 23:00 ET → ET date 03-04.
        ts_ms = int(pd.Timestamp("2024-03-05 04:00", tz="UTC").value // 10**6)
        n_late_utc = nyse_trading_days_in_window(ts_ms, 30)
        n_naive_morning = nyse_trading_days_in_window(pd.Timestamp("2024-03-04"), 30)
        assert n_late_utc == n_naive_morning

    def test_60d_typical_window(self):
        # ~60 calendar days ≈ 41-43 trading days depending on holidays.
        n = nyse_trading_days_in_window(pd.Timestamp("2024-03-04"), 60)
        assert 40 <= n <= 44


class TestConvertActToTrading:
    """Lock the conversion factor formula and direction."""

    @pytest.mark.parametrize(
        ("asof", "n_trading_days", "lower", "upper"),
        [
            # N=21, factor² = (30·252)/(365·21) = 7560/7665 ≈ 0.9863 → factor ≈ 0.9931
            pytest.param("2024-03-04", 21, 0.198, 0.20, id="factor_normal_week_below_one"),
            # N=18 (4 closures in Dec 23 → Jan 21 window).
            # factor² = (30·252)/(365·18) = 7560/6570 ≈ 1.1507  →  factor ≈ 1.0727
            # σ_TRD252 > σ_ACT365 because variance compressed into fewer days.
            pytest.param("2024-12-23", 18, 0.20, math.inf, id="factor_holiday_window_above_one"),
        ],
    )
    def test_factor_follows_the_trading_day_count(self, asof, n_trading_days, lower, upper):
        sigma = 0.20
        out = convert_iv_act365_to_trading252(sigma, pd.Timestamp(asof), 30)
        expected = sigma * math.sqrt(
            (30 * TRADING_DAYS_PER_YEAR) / (CALENDAR_DAYS_PER_YEAR * n_trading_days)
        )
        assert out == pytest.approx(expected, abs=1e-12), "converted vol follows the sqrt(30·252 / (365·N)) factor"
        assert lower < out < upper, "factor direction: below one for a normal week, above one for a holiday window"

    @pytest.mark.parametrize(
        ("sigma", "asof_date", "tenor"),
        [
            pytest.param(0.25, "2024-06-17", 30, id="round_trip_idempotent"),
            pytest.param(0.18, "2024-12-23", 60, id="round_trip_60d"),
        ],
    )
    def test_round_trip(self, sigma, asof_date, tenor):
        asof = pd.Timestamp(asof_date)
        x = convert_iv_act365_to_trading252(sigma, asof, tenor)
        y = convert_iv_trading252_to_act365(x, asof, tenor)
        assert y == pytest.approx(sigma, abs=1e-12)

    def test_zero_vol_passes_through(self):
        out = convert_iv_act365_to_trading252(0.0, pd.Timestamp("2024-03-04"), 30)
        assert out == 0.0

    @pytest.mark.parametrize(
        ("sigma", "tenor"),
        [
            pytest.param(-0.01, 30, id="negative_vol_raises"),
            pytest.param(0.20, 0, id="zero_tenor_raises"),
            pytest.param(0.20, -5, id="negative_tenor_raises"),
        ],
    )
    def test_invalid_input_raises(self, sigma, tenor):
        with pytest.raises(ValueError):
            convert_iv_act365_to_trading252(sigma, pd.Timestamp("2024-03-04"), tenor)


class TestVrpDocContractEnforcement:
    """Confirm consistent-basis VRP differs from mixed-basis VRP."""

    def test_vrp_differs_with_basis_in_holiday_window(self):
        asof = pd.Timestamp("2024-12-23")
        iv_act365 = pd.Series([0.20, 0.22, 0.18])
        iv_trd252 = iv_act365.apply(
            lambda s: convert_iv_act365_to_trading252(s, asof, 30)
        )
        rv = pd.Series([0.16, 0.18, 0.16])

        vrp_wrong = compute_vrp(iv_act365, rv)
        vrp_right = compute_vrp(iv_trd252, rv)

        # Holiday window → σ_TRD252 > σ_ACT365 → VRP larger.
        assert (vrp_right > vrp_wrong).all()
        # And the difference is non-trivial (>1% relative on the IV² term).
        rel_diff = ((iv_trd252**2 - iv_act365**2) / iv_act365**2).abs()
        assert (rel_diff > 0.01).all()
