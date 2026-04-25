"""Unit tests for pure helpers in options_companion_service.

External-boundary behavior (Polygon fetches, Greek computation) is covered by
integration tests; here we lock down the deterministic internals — slot
selection, slot labelling, column composition, timestamp conversion, day
mapping, and discontinuity tagging.
"""

from __future__ import annotations

from datetime import UTC

import pandas as pd
import pytest

from app.models.requests import OptionsCompanionConfig
from app.services.options_companion_service import (
    _bar_grid_floor_ms,
    _columns_for,
    _mark_discontinuity,
    _prior_day_close_map,
    _select_strikes_with_slots,
    _slot_label,
    _underlying_close_map,
    _utc_ms_for_et_close,
)


def _c(ticker: str, strike: float) -> dict:
    return {"ticker": ticker, "strike_price": strike}


class TestSlotLabel:
    def test_atm_zero_offset(self):
        assert _slot_label(0) == "atm"

    def test_negative_offsets_zero_padded(self):
        assert _slot_label(-1) == "atm-01"
        assert _slot_label(-3) == "atm-03"
        assert _slot_label(-12) == "atm-12"

    def test_positive_offsets_carry_explicit_plus(self):
        assert _slot_label(1) == "atm+01"
        assert _slot_label(3) == "atm+03"
        assert _slot_label(12) == "atm+12"


class TestSelectStrikesWithSlots:
    def test_returns_offset_contract_pairs_centered_on_atm(self):
        # 21 strikes spaced $1 apart, prior close at 110
        contracts = [_c(f"O:T{i}", 100.0 + i) for i in range(21)]
        slotted = _select_strikes_with_slots(contracts, prior_close=110.0, strikes_each_side=3)
        offsets = [s for s, _ in slotted]
        strikes = [c["strike_price"] for _, c in slotted]
        assert offsets == [-3, -2, -1, 0, 1, 2, 3]
        # ATM=110 → offsets map to 107..113
        assert strikes == [107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0]

    def test_clipped_at_low_edge_omits_negative_offsets(self):
        # ATM at index 0 — no strikes below
        contracts = [_c(f"O:T{i}", 100.0 + i) for i in range(10)]
        slotted = _select_strikes_with_slots(contracts, prior_close=100.0, strikes_each_side=3)
        offsets = [s for s, _ in slotted]
        # Only offsets 0..+3 fit
        assert offsets == [0, 1, 2, 3]

    def test_clipped_at_high_edge_omits_positive_offsets(self):
        contracts = [_c(f"O:T{i}", 100.0 + i) for i in range(10)]
        slotted = _select_strikes_with_slots(contracts, prior_close=109.0, strikes_each_side=3)
        offsets = [s for s, _ in slotted]
        # ATM at idx 9 (last) — only -3..0 fit
        assert offsets == [-3, -2, -1, 0]

    def test_empty_contracts_returns_empty(self):
        assert _select_strikes_with_slots([], prior_close=100.0, strikes_each_side=3) == []

    def test_atm_picks_nearest_when_exact_strike_missing(self):
        contracts = [_c("O:T1", 99.5), _c("O:T2", 100.25), _c("O:T3", 101.0)]
        slotted = _select_strikes_with_slots(contracts, prior_close=100.0, strikes_each_side=1)
        # closest to 100.0 is 100.25 → ATM, then ±1
        offset_to_strike = {s: c["strike_price"] for s, c in slotted}
        assert offset_to_strike[0] == 100.25
        assert -1 in offset_to_strike and offset_to_strike[-1] == 99.5
        assert 1 in offset_to_strike and offset_to_strike[1] == 101.0


class TestColumnsFor:
    def test_all_optional_toggles_off_gives_core_only(self):
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
            include_discontinuity=False,
        )
        cols = _columns_for(cfg)
        assert cols == ["unix_ts", "iso_time", "contract_ticker", "strike", "expiration"]

    def test_discontinuity_inserts_after_iso_time(self):
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
            include_discontinuity=True,
        )
        cols = _columns_for(cfg)
        assert cols[:5] == ["unix_ts", "iso_time", "discontinuity", "contract_ticker", "strike"]

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
            include_discontinuity=False,
        )
        cols = _columns_for(cfg)
        assert "delta" in cols
        assert "vega" in cols
        assert "gamma" not in cols
        assert "rho" not in cols
        assert "theta" not in cols
        assert "iv" not in cols

    def test_defaults_include_ohlcv_iv_common_greeks_and_discontinuity(self):
        cfg = OptionsCompanionConfig(enabled=True)
        cols = _columns_for(cfg)
        for expected in [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "vwap",
            "iv",
            "delta",
            "gamma",
            "theta",
            "vega",
            "discontinuity",
        ]:
            assert expected in cols, f"default config should include {expected}"
        # rho and open_interest default to off
        assert "rho" not in cols
        assert "open_interest" not in cols
        # `type` is no longer a column — side is encoded by folder.
        assert "type" not in cols


class TestMarkDiscontinuity:
    def test_first_row_is_one(self):
        rows = [{"unix_ts": 1, "contract_ticker": "O:A"}]
        _mark_discontinuity(rows)
        assert rows[0]["discontinuity"] == 1

    def test_subsequent_same_ticker_is_zero(self):
        rows = [
            {"unix_ts": 1, "contract_ticker": "O:A"},
            {"unix_ts": 2, "contract_ticker": "O:A"},
            {"unix_ts": 3, "contract_ticker": "O:A"},
        ]
        _mark_discontinuity(rows)
        assert [r["discontinuity"] for r in rows] == [1, 0, 0]

    def test_change_of_ticker_marks_one(self):
        rows = [
            {"unix_ts": 1, "contract_ticker": "O:A"},
            {"unix_ts": 2, "contract_ticker": "O:A"},
            {"unix_ts": 3, "contract_ticker": "O:B"},
            {"unix_ts": 4, "contract_ticker": "O:B"},
            {"unix_ts": 5, "contract_ticker": "O:C"},
        ]
        _mark_discontinuity(rows)
        assert [r["discontinuity"] for r in rows] == [1, 0, 1, 0, 1]

    def test_empty_rows_is_noop(self):
        rows: list[dict] = []
        _mark_discontinuity(rows)
        assert rows == []


class TestEtCloseConversion:
    def test_summer_close_is_20_00_utc(self):
        from datetime import date, datetime

        # July 15 2025 is EDT (UTC-4) → 16:00 ET == 20:00 UTC
        ts_ms = _utc_ms_for_et_close(date(2025, 7, 15))
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        assert dt.hour == 20
        assert dt.minute == 0

    def test_winter_close_is_21_00_utc(self):
        from datetime import date, datetime

        # January 15 2025 is EST (UTC-5) → 16:00 ET == 21:00 UTC
        ts_ms = _utc_ms_for_et_close(date(2025, 1, 15))
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        assert dt.hour == 21


class TestPriorDayCloseMap:
    def test_returns_last_close_per_trading_day(self):
        from datetime import date

        # 3 bars across 2 ET trading days
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
        ts = 1749547837500
        floored = _bar_grid_floor_ms(ts, "minute", 1)
        assert floored % 60_000 == 0
        assert floored <= ts < floored + 60_000

    def test_5minute_grid_lands_on_5minute_boundary(self):
        ts = 1_749_547_920_000
        floored = _bar_grid_floor_ms(ts, "minute", 5)
        assert floored % 300_000 == 0
        assert floored <= ts < floored + 300_000

    def test_hour_grid_floors_to_hour_start(self):
        aligned = 1_735_693_200_000
        mid_hour = aligned + 1_800_000
        floored = _bar_grid_floor_ms(mid_hour, "hour", 1)
        assert floored == aligned
        assert floored % 3_600_000 == 0

    def test_day_grid_floors_to_utc_midnight(self):
        midnight_utc = 1_735_689_600_000
        mid_day = midnight_utc + 12 * 3_600_000
        floored = _bar_grid_floor_ms(mid_day, "day", 1)
        assert floored == midnight_utc
        assert floored % 86_400_000 == 0


class TestUnderlyingCloseMapAlignment:
    def test_option_ts_looked_up_via_floored_key(self):
        df = pd.DataFrame(
            {
                "timestamp": [1_749_547_860_000, 1_749_547_920_000, 1_749_547_980_000],
                "close": [100.0, 101.0, 102.0],
            }
        )
        lookup = _underlying_close_map(df, "minute", 1)
        assert lookup.get(_bar_grid_floor_ms(1_749_547_920_000, "minute", 1)) == pytest.approx(101.0)

    def test_5min_aggregate_keys_align(self):
        df = pd.DataFrame(
            {
                "timestamp": [1_749_547_800_000, 1_749_548_100_000],
                "close": [200.0, 201.5],
            }
        )
        lookup = _underlying_close_map(df, "minute", 5)
        key = _bar_grid_floor_ms(1_749_548_100_000, "minute", 5)
        assert lookup[key] == pytest.approx(201.5)


class TestResolveTargetExpiry:
    """Strict-DTE policy: only an exact-match listed expiry is accepted."""

    def test_returns_target_when_listed(self):
        from datetime import date

        from app.services.options_companion_service import _resolve_target_expiry

        class _FakePolygon:
            def list_options_expirations(self, **kwargs):
                # Echo back the requested date as if it's listed
                return [kwargs["expiration_date_gte"]]

        cfg = OptionsCompanionConfig(enabled=True, dte_distance=7)
        result = _resolve_target_expiry(_FakePolygon(), "SPY", date(2025, 6, 10), cfg)
        assert result == date(2025, 6, 17)

    def test_returns_none_when_no_listed_expiry_at_target_date(self):
        from datetime import date

        from app.services.options_companion_service import _resolve_target_expiry

        class _FakePolygon:
            def list_options_expirations(self, **kwargs):
                return []  # nothing listed at target

        cfg = OptionsCompanionConfig(enabled=True, dte_distance=7)
        assert _resolve_target_expiry(_FakePolygon(), "SPY", date(2025, 6, 10), cfg) is None

    def test_zero_dte_uses_trading_day_directly(self):
        from datetime import date

        from app.services.options_companion_service import _resolve_target_expiry

        class _FakePolygon:
            def list_options_expirations(self, **kwargs):
                return [kwargs["expiration_date_gte"]]

        cfg = OptionsCompanionConfig(enabled=True, dte_distance=0)
        result = _resolve_target_expiry(_FakePolygon(), "SPY", date(2025, 6, 10), cfg)
        assert result == date(2025, 6, 10)

    def test_lookup_failure_returns_none_and_does_not_propagate(self):
        from datetime import date

        from app.services.options_companion_service import _resolve_target_expiry

        class _FakePolygon:
            def list_options_expirations(self, **kwargs):
                raise RuntimeError("polygon timeout")

        cfg = OptionsCompanionConfig(enabled=True, dte_distance=0)
        assert _resolve_target_expiry(_FakePolygon(), "SPY", date(2025, 6, 10), cfg) is None


class _FakePolygon:
    """Captures arguments and returns canned responses for the companion path."""

    def __init__(self, contracts: list[dict], option_bars: dict[str, list[dict]]):
        self._contracts = contracts
        self._option_bars = option_bars

    def list_options_contracts(self, **kwargs) -> list[dict]:
        return self._contracts

    def fetch_aggregates(self, ticker: str, **kwargs) -> list[dict]:
        return self._option_bars.get(ticker, [])

    def list_options_expirations(self, **kwargs) -> list[str]:
        # Echo back the requested target so dte_distance=0 (same-day) resolves.
        return [kwargs["expiration_date_gte"]]


class TestBuildOptionsCompanionTimestampAlignment:
    """End-to-end: pre-RTH option bars are dropped, surviving option rows
    share unix_ts with the underlying, and Greek/IV columns are populated.
    Regresses Finding 3.1 (100% NaN Greeks) and the user's requirement that
    'option contracts align in the timestamps' with the underlying.
    """

    def test_pre_rth_option_bars_dropped_and_greeks_populated(self):
        from app.services.options_companion_service import build_options_companion_csvs

        # 2025-06-10 09:30 ET == 13:30 UTC == 1_749_562_200_000 ms
        ts_0930 = 1_749_562_200_000
        ts_0931 = ts_0930 + 60_000
        ts_0932 = ts_0931 + 60_000
        ts_0915 = ts_0930 - 15 * 60_000
        ts_0925 = ts_0930 - 5 * 60_000

        underlying_df = pd.DataFrame({"timestamp": [ts_0930, ts_0931, ts_0932], "close": [710.0, 710.5, 711.0]})

        contracts = [
            {"ticker": "O:T709", "strike_price": 709.0},
            {"ticker": "O:T710", "strike_price": 710.0},
            {"ticker": "O:T711", "strike_price": 711.0},
        ]

        def _mk_bar(ts: int, price: float) -> dict:
            return {
                "timestamp": ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 100,
                "vwap": price,
                "transactions": 10,
            }

        option_bars = {
            # 709 (ITM) — has 2 pre-RTH bars that must be dropped
            "O:T709": [
                _mk_bar(ts_0915, 1.00),
                _mk_bar(ts_0925, 1.05),
                _mk_bar(ts_0930, 1.10),
                _mk_bar(ts_0931, 1.15),
                _mk_bar(ts_0932, 1.20),
            ],
            "O:T710": [_mk_bar(ts_0930, 0.50), _mk_bar(ts_0931, 0.55), _mk_bar(ts_0932, 0.60)],
            "O:T711": [_mk_bar(ts_0930, 0.20), _mk_bar(ts_0931, 0.22), _mk_bar(ts_0932, 0.25)],
        }

        polygon = _FakePolygon(contracts, option_bars)
        config = OptionsCompanionConfig(
            enabled=True,
            include_calls=True,
            include_puts=False,
            strikes_each_side=1,
            dte_distance=0,
            risk_free_rate=0.05,
            dividend_yield=0.0,
        )

        slot_files, report = build_options_companion_csvs(
            underlying_bars_df=underlying_df,
            ticker="SPY",
            from_date="2025-06-10",
            to_date="2025-06-10",
            config=config,
            polygon=polygon,
            timespan="minute",
            multiplier=1,
        )

        # Counters: 5 + 3 + 3 = 11 raw, 2 pre-RTH dropped on the 709 strike.
        assert report["totals"]["option_bars_raw"] == 11
        assert report["totals"]["option_bars_dropped"] == 2

        # Per-slot layout: one CSV per offset under calls/. Aggregate across.
        calls_paths = [p for p in slot_files if p.startswith("calls/")]
        assert len(calls_paths) == 3, "expected 3 slot files (offsets -1, 0, +1)"

        all_rows: list[list[str]] = []
        header: list[str] | None = None
        for path in calls_paths:
            lines = slot_files[path].decode().strip().split("\n")
            if header is None:
                header = lines[0].split(",")
            else:
                assert lines[0].split(",") == header, "all slot files must share the schema"
            all_rows.extend(line.split(",") for line in lines[1:])

        assert header is not None
        assert len(all_rows) == 9, "9 surviving option rows (3 contracts × 3 underlying ts)"

        ts_idx = header.index("unix_ts")
        emitted_ts = sorted({int(r[ts_idx]) for r in all_rows})
        assert emitted_ts == [ts_0930, ts_0931, ts_0932], (
            "option timestamps must mirror the underlying ticker's exactly"
        )

        # The 100%-NaN Greeks regression: at least one IV solve must succeed
        # and at least one delta must be populated. The ITM 709 contract
        # (price 1.10 with intrinsic 1.0) is the most conservative target.
        iv_idx = header.index("iv")
        delta_idx = header.index("delta")
        assert any(r[iv_idx] for r in all_rows), "iv column should not be 100% empty"
        assert any(r[delta_idx] for r in all_rows), "delta column should not be 100% empty"
        # The new per-day status breakdown must report at least one "ok" solve.
        assert report["totals"]["iv_status"].get("ok", 0) > 0

    def test_warning_emitted_when_greek_column_is_100pct_empty(self, caplog):
        """When all option closes are zero (no trades), every IV solve fails
        with status='no_price' and the warning surfaces empty_columns."""
        from app.services.options_companion_service import build_options_companion_csvs

        ts_0930 = 1_749_562_200_000
        ts_0931 = ts_0930 + 60_000
        underlying_df = pd.DataFrame({"timestamp": [ts_0930, ts_0931], "close": [710.0, 710.5]})
        contracts = [{"ticker": "O:T710", "strike_price": 710.0}]
        # Every option bar has close=0 → solver returns "no_price" → all greeks NaN
        option_bars = {
            "O:T710": [
                {"timestamp": ts_0930, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0},
                {"timestamp": ts_0931, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0},
            ]
        }
        polygon = _FakePolygon(contracts, option_bars)
        config = OptionsCompanionConfig(
            enabled=True,
            include_calls=True,
            include_puts=False,
            strikes_each_side=1,
            dte_distance=0,
        )

        import logging

        with caplog.at_level(logging.WARNING, logger="app.services.options_companion_service"):
            _slot_files, report = build_options_companion_csvs(
                underlying_bars_df=underlying_df,
                ticker="SPY",
                from_date="2025-06-10",
                to_date="2025-06-10",
                config=config,
                polygon=polygon,
                timespan="minute",
                multiplier=1,
            )

        assert "calls.iv" in report["empty_columns"]
        assert "calls.delta" in report["empty_columns"]
        assert any("100%% empty" in rec.message or "100% empty" in rec.message for rec in caplog.records), (
            "expected fail-loud warning for 100%-empty Greek columns"
        )
        assert report["totals"]["iv_status"].get("no_price", 0) == 2
