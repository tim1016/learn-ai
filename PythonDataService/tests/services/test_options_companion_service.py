"""Unit tests for pure helpers in options_companion_service.

External-boundary behavior (Polygon fetches, Greek computation) is covered by
integration tests; here we lock down the deterministic internals — strike
selection, column composition, timestamp conversion, and day mapping.
"""

from __future__ import annotations

from datetime import UTC

import pandas as pd
import pytest

from app.models.requests import OptionsCompanionConfig
from app.services.options_companion_service import (
    _bar_grid_floor_ms,
    _columns_for,
    _prior_day_close_map,
    _select_strikes,
    _sort_companion_rows,
    _underlying_close_map,
    _utc_ms_for_et_close,
)


def _c(ticker: str, strike: float) -> dict:
    return {"ticker": ticker, "strike_price": strike}


class TestSelectStrikes:
    def test_picks_atm_plus_n_each_side_when_enough_contracts(self):
        # 21 strikes spaced $1 apart, prior close at 110
        contracts = [_c(f"O:T{i}", 100.0 + i) for i in range(21)]
        selected = _select_strikes(contracts, prior_close=110.0, strikes_each_side=5)
        strikes = [c["strike_price"] for c in selected]
        # ATM=110, ±5 → [105..115]
        assert strikes == [105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0, 114.0, 115.0]
        assert len(selected) == 11

    def test_clips_at_edges_when_atm_near_start(self):
        # Prior close far below middle strike — ATM near index 0
        contracts = [_c(f"O:T{i}", 100.0 + i) for i in range(10)]
        selected = _select_strikes(contracts, prior_close=100.0, strikes_each_side=5)
        strikes = [c["strike_price"] for c in selected]
        # Only 5 above ATM available, no room below
        assert strikes == [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]

    def test_clips_at_edges_when_atm_near_end(self):
        contracts = [_c(f"O:T{i}", 100.0 + i) for i in range(10)]
        selected = _select_strikes(contracts, prior_close=109.0, strikes_each_side=5)
        strikes = [c["strike_price"] for c in selected]
        assert strikes == [104.0, 105.0, 106.0, 107.0, 108.0, 109.0]

    def test_empty_contracts_returns_empty(self):
        assert _select_strikes([], prior_close=100.0, strikes_each_side=5) == []

    def test_nearest_strike_chosen_when_exact_atm_missing(self):
        contracts = [_c("O:T1", 99.5), _c("O:T2", 100.25), _c("O:T3", 101.0)]
        selected = _select_strikes(contracts, prior_close=100.0, strikes_each_side=1)
        strikes = [c["strike_price"] for c in selected]
        # closest to 100.0 is 100.25 → ATM, then ±1
        assert 100.25 in strikes
        assert len(selected) == 3


class TestColumnsFor:
    def test_all_toggles_off_gives_core_only(self):
        cfg = OptionsCompanionConfig(
            enabled=True,
            include_ohlcv=False,
            include_vwap=False,
            include_transactions=False,
            include_open_interest=False,
            include_iv=False,
            include_delta=False,
            include_gamma=False,
            include_theta=False,
            include_vega=False,
            include_rho=False,
        )
        cols = _columns_for(cfg)
        assert cols == ["unix_ts", "iso_time", "contract_ticker", "expiration", "strike", "type"]

    def test_greeks_each_toggle_adds_own_column(self):
        cfg = OptionsCompanionConfig(
            enabled=True,
            include_ohlcv=False,
            include_vwap=False,
            include_transactions=False,
            include_open_interest=False,
            include_iv=False,
            include_delta=True,
            include_gamma=False,
            include_theta=False,
            include_vega=True,
            include_rho=False,
        )
        cols = _columns_for(cfg)
        assert "delta" in cols
        assert "vega" in cols
        assert "gamma" not in cols
        assert "rho" not in cols
        assert "theta" not in cols
        assert "iv" not in cols

    def test_defaults_include_ohlcv_iv_and_common_greeks(self):
        cfg = OptionsCompanionConfig(enabled=True)
        cols = _columns_for(cfg)
        for expected in ["open", "high", "low", "close", "volume", "vwap", "iv", "delta", "gamma", "theta", "vega"]:
            assert expected in cols, f"default config should include {expected}"
        # rho and open_interest default to off
        assert "rho" not in cols
        assert "open_interest" not in cols


class TestEtCloseConversion:
    def test_summer_close_is_20_00_utc(self):
        from datetime import date

        # July 15 2025 is EDT (UTC-4) → 16:00 ET == 20:00 UTC
        ts_ms = _utc_ms_for_et_close(date(2025, 7, 15))
        from datetime import datetime

        dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        assert dt.hour == 20
        assert dt.minute == 0

    def test_winter_close_is_21_00_utc(self):
        from datetime import date

        # January 15 2025 is EST (UTC-5) → 16:00 ET == 21:00 UTC
        ts_ms = _utc_ms_for_et_close(date(2025, 1, 15))
        from datetime import datetime

        dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        assert dt.hour == 21


class TestPriorDayCloseMap:
    def test_returns_last_close_per_trading_day(self):
        from datetime import date

        # 3 bars across 2 ET trading days
        # 2025-06-10 15:59 ET = 19:59 UTC = 1749585540000
        # 2025-06-10 16:00 ET (EOD) = 20:00 UTC = 1749585600000
        # 2025-06-11 09:30 ET = 13:30 UTC = 1749641400000
        df = pd.DataFrame(
            {
                "timestamp": [1749585540000, 1749585580000, 1749641400000],
                "close": [100.0, 101.5, 102.0],
            }
        )
        result = _prior_day_close_map(df)
        assert result[date(2025, 6, 10)] == pytest.approx(101.5)
        assert result[date(2025, 6, 11)] == pytest.approx(102.0)

    def test_empty_df_returns_empty_map(self):
        df = pd.DataFrame(columns=["timestamp", "close"])
        assert _prior_day_close_map(df) == {}


class TestBarGridFloor:
    def test_minute_grid_floors_to_minute_start(self):
        # 2025-06-10 09:30:37 UTC → floor to 09:30:00
        ts = 1749547837500  # 37.5s past a minute boundary (approximately)
        floored = _bar_grid_floor_ms(ts, "minute", 1)
        # Floor should drop to the minute boundary
        assert floored % 60_000 == 0
        assert floored <= ts < floored + 60_000

    def test_5minute_grid_lands_on_5minute_boundary(self):
        # 2025-06-10 09:32:00 UTC → floor to 09:30:00 for a 5-min bar
        ts = 1_749_547_920_000
        floored = _bar_grid_floor_ms(ts, "minute", 5)
        # 5-minute grid
        assert floored % 300_000 == 0
        assert floored <= ts < floored + 300_000

    def test_hour_grid_floors_to_hour_start(self):
        # Pick a ts mid-hour and confirm floor lands on the hour boundary
        aligned = 1_735_693_200_000  # 2025-01-01 01:00:00 UTC, hour-aligned
        mid_hour = aligned + 1_800_000  # + 30 min
        floored = _bar_grid_floor_ms(mid_hour, "hour", 1)
        assert floored == aligned
        assert floored % 3_600_000 == 0

    def test_day_grid_floors_to_utc_midnight(self):
        # Polygon day-bar timestamps are UTC midnight; confirm mid-day floors to midnight.
        midnight_utc = 1_735_689_600_000  # 2025-01-01 00:00:00 UTC
        mid_day = midnight_utc + 12 * 3_600_000
        floored = _bar_grid_floor_ms(mid_day, "day", 1)
        assert floored == midnight_utc
        assert floored % 86_400_000 == 0


class TestUnderlyingCloseMapAlignment:
    def test_option_ts_looked_up_via_floored_key_even_when_ticker_stored_at_exact_boundary(self):
        # Simulate a series at 1-min resolution. Keys in the lookup are floored to minute.
        df = pd.DataFrame(
            {
                "timestamp": [1_749_547_860_000, 1_749_547_920_000, 1_749_547_980_000],
                "close": [100.0, 101.0, 102.0],
            }
        )
        lookup = _underlying_close_map(df, "minute", 1)
        # An option bar at the exact same boundary hits directly
        assert lookup.get(_bar_grid_floor_ms(1_749_547_920_000, "minute", 1)) == pytest.approx(101.0)

    def test_5min_aggregate_keys_align(self):
        # 5-min resolution: bar-start at 09:30:00 UTC and 09:35:00 UTC
        df = pd.DataFrame(
            {
                "timestamp": [1_749_547_800_000, 1_749_548_100_000],
                "close": [200.0, 201.5],
            }
        )
        lookup = _underlying_close_map(df, "minute", 5)
        # Option bar timestamp landing at the exact 5-min boundary
        key = _bar_grid_floor_ms(1_749_548_100_000, "minute", 5)
        assert lookup[key] == pytest.approx(201.5)


class TestSortCompanionRows:
    """Companion CSVs must be monotonic in unix_ts after the build sort runs.

    Production accumulates rows in append order (day → strike → bar) which
    produces contract-grouped, time-resetting blocks; the writer relies on
    the caller having sorted by ``(unix_ts, contract_ticker)`` first.
    """

    def test_flattens_contract_blocks_into_chronological_stream(self):
        # Two contracts, each with three bars at the same timestamps —
        # mimics the per-contract block layout the user reported.
        rows = [
            {"unix_ts": 1000, "contract_ticker": "O:A", "close": 1.1},
            {"unix_ts": 2000, "contract_ticker": "O:A", "close": 1.2},
            {"unix_ts": 3000, "contract_ticker": "O:A", "close": 1.3},
            {"unix_ts": 1000, "contract_ticker": "O:B", "close": 2.1},
            {"unix_ts": 2000, "contract_ticker": "O:B", "close": 2.2},
            {"unix_ts": 3000, "contract_ticker": "O:B", "close": 2.3},
        ]
        _sort_companion_rows(rows)

        ts = [r["unix_ts"] for r in rows]
        assert ts == sorted(ts), "unix_ts column must be non-decreasing"
        # Tie-break is alphabetical on contract_ticker so identical
        # timestamps cluster predictably (A before B).
        assert rows[0:2] == [
            {"unix_ts": 1000, "contract_ticker": "O:A", "close": 1.1},
            {"unix_ts": 1000, "contract_ticker": "O:B", "close": 2.1},
        ]

    def test_already_sorted_input_is_unchanged(self):
        rows = [
            {"unix_ts": 1000, "contract_ticker": "O:A"},
            {"unix_ts": 1000, "contract_ticker": "O:B"},
            {"unix_ts": 2000, "contract_ticker": "O:A"},
        ]
        original = [dict(r) for r in rows]
        _sort_companion_rows(rows)
        assert rows == original

    def test_empty_list_is_noop(self):
        rows: list[dict] = []
        _sort_companion_rows(rows)
        assert rows == []
