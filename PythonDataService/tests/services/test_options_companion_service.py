"""Unit tests for pure helpers in options_companion_service.

External-boundary behavior (Polygon fetches, Greek computation) is covered by
integration tests; here we lock down the deterministic internals — slot
selection, slot labelling, column composition, timestamp conversion, day
mapping, and discontinuity tagging.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.models.requests import OptionsCompanionConfig
from app.options.expiry import option_expiry_anchor_ms
from app.services.options_companion_service import (
    _bar_grid_floor_ms,
    _columns_for,
    _mark_discontinuity,
    _prior_day_close_map,
    _select_strikes_with_slots,
    _slot_label,
    _underlying_close_map,
)


def _c(ticker: str, strike: float) -> dict:
    return {"ticker": ticker, "strike_price": strike}


class TestSlotLabel:
    @pytest.mark.parametrize(
        ("offset", "label"),
        [
            pytest.param(0, "atm", id="atm_zero_offset"),
            pytest.param(-1, "atm-01", id="negative_offsets_zero_padded_1"),
            pytest.param(-3, "atm-03", id="negative_offsets_zero_padded_3"),
            pytest.param(-12, "atm-12", id="negative_offsets_zero_padded_12"),
            pytest.param(1, "atm+01", id="positive_offsets_carry_explicit_plus_1"),
            pytest.param(3, "atm+03", id="positive_offsets_carry_explicit_plus_3"),
            pytest.param(12, "atm+12", id="positive_offsets_carry_explicit_plus_12"),
        ],
    )
    def test_slot_label(self, offset, label):
        assert _slot_label(offset) == label


class TestSelectStrikesWithSlots:
    @pytest.mark.parametrize(
        ("strikes", "prior_close", "strikes_each_side", "expected_offsets", "expected_strikes"),
        [
            # 21 strikes spaced $1 apart, prior close at 110: ATM=110 → offsets map to 107..113
            pytest.param(
                tuple(100.0 + i for i in range(21)),
                110.0,
                3,
                [-3, -2, -1, 0, 1, 2, 3],
                [107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0],
                id="returns_offset_contract_pairs_centered_on_atm",
            ),
            # ATM at index 0 — no strikes below; only offsets 0..+3 fit
            pytest.param(
                tuple(100.0 + i for i in range(10)),
                100.0,
                3,
                [0, 1, 2, 3],
                [100.0, 101.0, 102.0, 103.0],
                id="clipped_at_low_edge_omits_negative_offsets",
            ),
            # ATM at idx 9 (last) — only -3..0 fit
            pytest.param(
                tuple(100.0 + i for i in range(10)),
                109.0,
                3,
                [-3, -2, -1, 0],
                [106.0, 107.0, 108.0, 109.0],
                id="clipped_at_high_edge_omits_positive_offsets",
            ),
            pytest.param((), 100.0, 3, [], [], id="empty_contracts_returns_empty"),
            # closest to 100.0 is 100.25 → ATM, then ±1
            pytest.param(
                (99.5, 100.25, 101.0),
                100.0,
                1,
                [-1, 0, 1],
                [99.5, 100.25, 101.0],
                id="atm_picks_nearest_when_exact_strike_missing",
            ),
        ],
    )
    def test_slots_are_offsets_from_the_atm_strike(
        self, strikes, prior_close, strikes_each_side, expected_offsets, expected_strikes
    ):
        contracts = [_c(f"O:T{i}", strike) for i, strike in enumerate(strikes)]
        slotted = _select_strikes_with_slots(contracts, prior_close=prior_close, strikes_each_side=strikes_each_side)
        assert [s for s, _ in slotted] == expected_offsets, "the slot offsets run from the ATM strike, clipped at an edge"
        assert [c["strike_price"] for _, c in slotted] == expected_strikes, "each offset maps to its strike"


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
        assert cols == ["unix_ts", "contract_ticker", "strike", "expiration"]

    def test_discontinuity_inserts_after_timestamp(self):
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
        assert cols[:4] == ["unix_ts", "discontinuity", "contract_ticker", "strike"]

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
    @pytest.mark.parametrize(
        ("tickers", "expected"),
        [
            pytest.param(["O:A"], [1], id="first_row_is_one"),
            pytest.param(["O:A", "O:A", "O:A"], [1, 0, 0], id="subsequent_same_ticker_is_zero"),
            pytest.param(["O:A", "O:A", "O:B", "O:B", "O:C"], [1, 0, 1, 0, 1], id="change_of_ticker_marks_one"),
            pytest.param([], [], id="empty_rows_is_noop"),
        ],
    )
    def test_discontinuity_marks_the_first_row_and_each_change_of_ticker(self, tickers, expected):
        rows = [{"unix_ts": ts, "contract_ticker": ticker} for ts, ticker in enumerate(tickers, start=1)]
        _mark_discontinuity(rows)
        assert [r["discontinuity"] for r in rows] == expected


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
    @pytest.mark.parametrize(
        ("aligned", "into_the_bar_ms", "timespan", "multiplier", "period_ms"),
        [
            # 1749547837500 is 37.5 s into the minute that opens at 1749547800000
            pytest.param(1_749_547_800_000, 37_500, "minute", 1, 60_000, id="minute_grid_floors_to_minute_start"),
            # 1_749_547_920_000 is 2 min into the 5-minute bar that opens at 1_749_547_800_000
            pytest.param(
                1_749_547_800_000, 120_000, "minute", 5, 300_000, id="5minute_grid_lands_on_5minute_boundary"
            ),
            pytest.param(1_735_693_200_000, 1_800_000, "hour", 1, 3_600_000, id="hour_grid_floors_to_hour_start"),
            pytest.param(
                1_735_689_600_000, 12 * 3_600_000, "day", 1, 86_400_000, id="day_grid_floors_to_utc_midnight"
            ),
        ],
    )
    def test_a_grid_floors_a_time_to_the_aligned_start_of_its_bar(
        self, aligned, into_the_bar_ms, timespan, multiplier, period_ms
    ):
        ts = aligned + into_the_bar_ms
        floored = _bar_grid_floor_ms(ts, timespan, multiplier)
        assert floored == aligned, "a time inside the bar floors to the bar's aligned start"
        assert floored % period_ms == 0, "the floor lands on a bar boundary"
        assert floored <= ts < floored + period_ms, "the timestamp lies inside the bar the floor starts"


class TestUnderlyingCloseMapAlignment:
    @pytest.mark.parametrize(
        ("timestamps", "closes", "multiplier", "option_ts", "expected_close"),
        [
            pytest.param(
                (1_749_547_860_000, 1_749_547_920_000, 1_749_547_980_000),
                (100.0, 101.0, 102.0),
                1,
                1_749_547_920_000,
                101.0,
                id="option_ts_looked_up_via_floored_key",
            ),
            pytest.param(
                (1_749_547_800_000, 1_749_548_100_000),
                (200.0, 201.5),
                5,
                1_749_548_100_000,
                201.5,
                id="5min_aggregate_keys_align",
            ),
        ],
    )
    def test_an_option_timestamp_finds_its_underlying_close_by_the_floored_key(
        self, timestamps, closes, multiplier, option_ts, expected_close
    ):
        df = pd.DataFrame({"timestamp": list(timestamps), "close": list(closes)})
        lookup = _underlying_close_map(df, "minute", multiplier)
        assert lookup.get(_bar_grid_floor_ms(option_ts, "minute", multiplier)) == pytest.approx(expected_close)


class TestResolveTargetExpiry:
    """Strict-DTE policy: only an exact-match listed expiry is accepted."""

    @pytest.mark.parametrize(
        ("dte_distance", "target_is_listed", "expected"),
        [
            pytest.param(7, True, date(2025, 6, 17), id="returns_target_when_listed"),
            pytest.param(0, True, date(2025, 6, 10), id="zero_dte_uses_trading_day_directly"),
            pytest.param(7, False, None, id="returns_none_when_no_listed_expiry_at_target_date"),
        ],
    )
    def test_returns_the_listed_target_expiry_or_none(self, dte_distance, target_is_listed, expected):
        from datetime import date

        from app.services.options_companion_service import _resolve_target_expiry

        class _FakePolygon:
            def list_options_expirations(self, **kwargs):
                # Echo back the requested anchor as if it's listed, or list nothing at the target
                return [kwargs["expiration_ms_gte"]] if target_is_listed else []

        cfg = OptionsCompanionConfig(enabled=True, dte_distance=dte_distance)
        result = _resolve_target_expiry(_FakePolygon(), "SPY", date(2025, 6, 10), cfg)
        assert result == expected

    def test_lookup_failure_propagates_instead_of_reading_as_no_chain(self):
        from datetime import date

        from app.services.options_companion_service import _resolve_target_expiry

        class _FakePolygon:
            def list_options_expirations(self, **kwargs):
                raise RuntimeError("polygon timeout")

        cfg = OptionsCompanionConfig(enabled=True, dte_distance=0)
        with pytest.raises(RuntimeError, match="polygon timeout"):
            _resolve_target_expiry(_FakePolygon(), "SPY", date(2025, 6, 10), cfg)


class _FakePolygon:
    """Captures arguments and returns canned responses for the companion path."""

    def __init__(self, contracts: list[dict], option_bars: dict[str, list[dict]]):
        self._contracts = contracts
        self._option_bars = option_bars

    def list_options_contracts(self, **kwargs) -> list[dict]:
        return self._contracts

    def fetch_aggregates(self, ticker: str, **kwargs) -> list[dict]:
        return self._option_bars.get(ticker, [])

    def list_options_expirations(self, **kwargs) -> list[int]:
        # Echo back the requested target so dte_distance=0 (same-day) resolves.
        return [kwargs["expiration_ms_gte"]]


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

        assert (report["risk_free_rate"], report["dividend_yield"]) == (0.05, 0.0)
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


class TestPolygonOutageIsNotNoChain:
    """A Polygon call that failed is reported as a failed fetch, never as an empty chain (#3221)."""

    _TS_0930 = 1_749_562_200_000  # 2025-06-10 09:30 ET

    def _build(self, polygon):
        from app.services.options_companion_service import build_options_companion_csvs

        underlying_df = pd.DataFrame({"timestamp": [self._TS_0930], "close": [710.0]})
        config = OptionsCompanionConfig(
            enabled=True, include_calls=True, include_puts=True, strikes_each_side=1, dte_distance=0
        )
        return build_options_companion_csvs(
            underlying_bars_df=underlying_df,
            ticker="SPY",
            from_date="2025-06-10",
            to_date="2025-06-10",
            config=config,
            polygon=polygon,
            timespan="minute",
            multiplier=1,
        )

    def test_an_expiration_lookup_outage_names_itself_in_the_skipped_day(self):
        class _Down(_FakePolygon):
            def list_options_expirations(self, **kwargs):
                raise RuntimeError("Polygon did not answer expirations window 2025-06-10..2025-06-10")

        _files, report = self._build(_Down([], {}))

        (skipped,) = report["days_skipped"]
        assert "lookup failed" in skipped["reason"]
        assert "no listed expiry" not in skipped["reason"]
        assert report["totals"]["fetch_failures"] == {"expirations": 1}

    def test_a_day_with_no_listed_chain_still_says_so_and_counts_no_failure(self):
        class _NoChain(_FakePolygon):
            def list_options_expirations(self, **kwargs):
                return []

        _files, report = self._build(_NoChain([], {}))

        (skipped,) = report["days_skipped"]
        assert "no listed expiry" in skipped["reason"]
        assert report["totals"]["fetch_failures"] == {}

    def test_failed_contract_lists_and_bar_fetches_are_counted_not_silently_empty(self):
        contracts = [_c("O:T709", 709.0), _c("O:T710", 710.0), _c("O:T711", 711.0)]

        class _Flaky(_FakePolygon):
            def list_options_contracts(self, **kwargs):
                if kwargs["contract_type"] == "call":
                    raise RuntimeError("polygon 503")
                return self._contracts

            def fetch_aggregates(self, ticker, **kwargs):
                raise RuntimeError("polygon timeout")

        _files, report = self._build(_Flaky(contracts, {}))

        # Calls: the list failed (one failure). Puts: three slots chosen, three bar fetches failed.
        assert report["totals"]["fetch_failures"] == {"contracts": 1, "bars": 3}
        (day,) = report["per_day"]
        assert (day["calls_selected"], day["puts_selected"]) == (0, 3)
        assert day["fetch_failures"] == {"contracts": 1, "bars": 3}


class TestComputeRowGreeksStatusLabels:
    """``_compute_row_greeks`` returns ``(values, status_label)``. Status
    labels surface the per-bar reason the IV/Greeks are NaN and flow into
    the run report's ``iv_status`` counter, so a column with 100%-NaN
    values can be diagnosed by category. Pin the four guard branches."""

    def _config(self) -> OptionsCompanionConfig:
        return OptionsCompanionConfig(
            enabled=True,
            include_iv=True,
            include_delta=True,
            include_gamma=True,
            include_theta=True,
            include_vega=True,
            risk_free_rate=0.05,
            dividend_yield=0.0,
        )

    @pytest.mark.parametrize(
        ("option_close", "bar_ts", "expiry_close_offset_ms", "expected_status"),
        [
            # 2025-06-10 19:00 UTC = 15:00 ET; the expiry close is 1 hour before the bar
            pytest.param(1.50, 1_749_596_400_000, -60 * 60 * 1000, "expired", id="expired_status_when_bar_is_after_close"),
            # 2025-06-10 09:30 ET (RTH); the expiry close is 6 hours later
            pytest.param(
                0.0, 1_749_562_200_000, 6 * 60 * 60 * 1000, "no_price", id="no_price_status_when_option_close_is_zero"
            ),
            pytest.param(
                None, 1_749_562_200_000, 6 * 60 * 60 * 1000, "no_price", id="no_price_status_when_option_close_is_none"
            ),
        ],
    )
    def test_guard_branches_give_their_status_and_no_greeks(
        self, option_close, bar_ts, expiry_close_offset_ms, expected_status
    ):
        from app.services.options_companion_service import _compute_row_greeks

        out, status = _compute_row_greeks(
            option_close=option_close,
            underlying_spot=710.0,
            strike=709.0,
            bar_ts_ms=bar_ts,
            expiry_close_ms=bar_ts + expiry_close_offset_ms,
            is_call=True,
            config=self._config(),
        )
        assert status == expected_status, "the guard branch names its reason in the status label"
        assert all(v is None for v in out.values()), "a guarded row carries no IV or Greek values"

    def test_ok_status_populates_all_greeks(self):
        from app.services.options_companion_service import _compute_row_greeks

        bar_ts = 1_749_562_200_000  # 09:30 ET
        expiry_close = bar_ts + 6 * 60 * 60 * 1000  # 15:30 ET (~6h to expiry)
        out, status = _compute_row_greeks(
            option_close=1.50,  # ITM call: spot 710 > strike 709 by $1
            underlying_spot=710.0,
            strike=709.0,
            bar_ts_ms=bar_ts,
            expiry_close_ms=expiry_close,
            is_call=True,
            config=self._config(),
            vol_guess=0.20,
        )
        assert status == "ok"
        assert out["iv"] is not None
        assert out["delta"] is not None
        assert out["gamma"] is not None
        assert out["theta"] is not None
        assert out["vega"] is not None
        # ITM call: positive delta, positive gamma, negative theta
        assert out["delta"] > 0
        assert out["gamma"] > 0
        assert out["theta"] < 0


class TestProcessContractWarmStartIv:
    """The companion threads ``last_iv`` between sequential bars on the
    same contract so Newton converges in 2–4 iterations instead of from
    the cold ``vol_guess=0.25`` seed. Verify the seed actually advances.
    """

    def test_solver_called_with_advancing_vol_guess(self, monkeypatch):
        from app.services.options_companion_service import _process_contract, _SelectedContract

        ts_0930 = 1_749_562_200_000
        ts_0931 = ts_0930 + 60_000
        ts_0932 = ts_0931 + 60_000

        # Capture every call's vol_guess so we can assert the warm-start chain.
        captured: list[float] = []

        class _FakeIvResult:
            def __init__(self, iv: float):
                self.iv = iv

                class _Status:
                    value = "ok"

                self.status = _Status()

        def _fake_iv(*args, vol_guess, **kwargs):
            # Accept any keyword the real ``implied_volatility`` adds in the
            # future (e.g. ``min_ttm``) — the test only cares about how
            # ``vol_guess`` advances across sequential bars.
            captured.append(vol_guess)
            # Return progressively different IVs so warm-start can pick them up
            return _FakeIvResult(iv=0.20 + 0.01 * len(captured))

        monkeypatch.setattr(
            "app.services.options_companion_service.implied_volatility",
            _fake_iv,
        )

        contract = _SelectedContract(
            ticker="O:T709",
            strike=709.0,
            expiration=date(2025, 6, 10),
            contract_type="call",
            slot_offset=0,  # ATM in the slot ladder; not exercised by this test
        )

        bars = [
            {"timestamp": t, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 10}
            for t in (ts_0930, ts_0931, ts_0932)
        ]
        polygon = _FakePolygon(contracts=[], option_bars={"O:T709": bars})
        underlying_by_ts = {ts_0930: 710.0, ts_0931: 710.5, ts_0932: 711.0}

        rows, _ = _process_contract(
            contract=contract,
            trading_day=date(2025, 6, 10),
            to_date="2025-06-10",
            underlying_by_ts=underlying_by_ts,
            config=OptionsCompanionConfig(enabled=True, risk_free_rate=0.05, dividend_yield=0.0),
            timespan="minute",
            multiplier=1,
            polygon=polygon,
        )

        # Three bars → three IV solves.
        assert len(rows) == 3
        assert len(captured) == 3
        # Bar 0: cold seed (0.25 default).
        assert captured[0] == pytest.approx(0.25)
        # Bar 1: warm-start from bar 0's solved IV (0.21).
        assert captured[1] == pytest.approx(0.21)
        # Bar 2: warm-start from bar 1's solved IV (0.22).
        assert captured[2] == pytest.approx(0.22)


class TestFirstDayCloseSource:
    """Day-1 ``prior_close`` must be sourced from the first underlying bar's
    close, NOT from the EOD-of-same-day close (which would leak future
    information into ATM strike selection). Regresses the bias documented
    inline in build_options_companion_csvs."""

    def test_atm_ladder_centers_on_first_bar_close_not_eod(self):
        from app.services.options_companion_service import build_options_companion_csvs

        # Underlying drifts $100 → $110 within one trading day. EOD is $110;
        # first-bar close is $100. Strike selection MUST center on $100.
        ts_0930 = 1_749_562_200_000  # 2025-06-10 09:30 ET
        underlying = pd.DataFrame(
            {
                "timestamp": [ts_0930 + i * 60_000 for i in range(11)],
                "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0],
            }
        )

        # Wide strike ladder so both candidates ($100 and $110) are available.
        contracts = [{"ticker": f"O:T{int(s)}", "strike_price": float(s)} for s in range(90, 121)]

        # One trivial bar per contract so the row pipeline runs without
        # blowing up — the test only inspects which strikes were selected.
        option_bars = {
            f"O:T{int(s)}": [
                {"timestamp": ts_0930, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}
            ]
            for s in range(90, 121)
        }

        polygon = _FakePolygon(contracts, option_bars)
        config = OptionsCompanionConfig(
            enabled=True,
            include_calls=True,
            include_puts=False,
            strikes_each_side=2,  # ATM ± 2 → 5 slots
            dte_distance=0,  # same-day expiry
            include_iv=False,  # IV solving is irrelevant to this assertion
            include_delta=False,
            include_gamma=False,
            include_theta=False,
            include_vega=False,
        )

        slot_files, _ = build_options_companion_csvs(
            underlying_bars_df=underlying,
            ticker="SPY",
            from_date="2025-06-10",
            to_date="2025-06-10",
            config=config,
            polygon=polygon,
            timespan="minute",
            multiplier=1,
        )

        # New per-slot layout: one CSV per offset under calls/. Aggregate
        # across slot files to recover the full set of selected strikes.
        calls_paths = [p for p in slot_files if p.startswith("calls/")]
        assert calls_paths, "expected at least one calls/* slot CSV"
        selected_strikes: set[float] = set()
        for path in calls_paths:
            lines = slot_files[path].decode().strip().split("\n")
            header = lines[0].split(",")
            strike_idx = header.index("strike")
            for row in lines[1:]:
                selected_strikes.add(float(row.split(",")[strike_idx]))

        # ATM = first bar's close ($100), ± 2 strikes → [98, 99, 100, 101, 102].
        assert sorted(selected_strikes) == [98.0, 99.0, 100.0, 101.0, 102.0]
        # Sanity: must NOT include strikes that would have been selected
        # if EOD ($110) was used as the centering price.
        assert 110.0 not in selected_strikes
        assert 109.0 not in selected_strikes


class TestHalfDayCountdown:
    """A same-day contract on a half day counts time to the 13:00 close, not
    to the 16:00 anchor: at 12:55 New York time on Black Friday it has five
    minutes, not 3h05m (#2934). The countdown's end is the canonical
    ``last_session_close_ms`` (ADR 0076); this pins the companion's wiring."""

    def test_half_day_expiry_prices_five_minutes_out(self):
        from app.services.options_companion_service import _compute_row_greeks, build_options_companion_csvs

        # 2025-11-28 (Black Friday): the NYSE closes 13:00 ET.
        bar_1255_et = 1_764_352_500_000  # 12:55 ET (17:55 UTC)
        close_1300_et = 1_764_352_800_000  # 13:00 ET (18:00 UTC)

        underlying_df = pd.DataFrame({"timestamp": [bar_1255_et], "close": [710.0]})
        contracts = [{"ticker": "O:T710", "strike_price": 710.0}]
        option_bars = {
            "O:T710": [
                {
                    "timestamp": bar_1255_et, "open": 1.0, "high": 1.05, "low": 0.95,
                    "close": 1.05, "volume": 10, "vwap": 1.0, "transactions": 5,
                },
            ],
        }

        config = OptionsCompanionConfig(
            enabled=True,
            include_calls=True,
            include_puts=False,
            strikes_each_side=1,
            dte_distance=0,
            include_iv=True,
            include_delta=True,
            risk_free_rate=0.05,
            dividend_yield=0.0,
        )

        slot_files, report = build_options_companion_csvs(
            underlying_bars_df=underlying_df,
            ticker="SPY",
            from_date="2025-11-28",
            to_date="2025-11-28",
            config=config,
            polygon=_FakePolygon(contracts, option_bars),
            timespan="minute",
            multiplier=1,
        )

        # The slot file exists and the solve at 12:55 succeeded.
        assert "calls/atm.csv" in slot_files
        assert report["totals"]["iv_status"].get("ok", 0) >= 1
        row = slot_files["calls/atm.csv"].decode().strip().split("\n")[1].split(",")

        # The row's IV is the one solved at a five-minute TTM (the 13:00
        # close), not the 3h05m the 16:00 anchor would have given.
        at_five_minutes, status_five = _compute_row_greeks(
            option_close=1.05,
            underlying_spot=710.0,
            strike=710.0,
            bar_ts_ms=bar_1255_et,
            expiry_close_ms=close_1300_et,
            is_call=True,
            config=config,
        )

        at_three_hours, _ = _compute_row_greeks(
            option_close=1.05,
            underlying_spot=710.0,
            strike=710.0,
            bar_ts_ms=bar_1255_et,
            expiry_close_ms=option_expiry_anchor_ms(date(2025, 11, 28)),
            is_call=True,
            config=config,
        )
        assert status_five == "ok"
        assert at_five_minutes["iv"] != at_three_hours["iv"]

        header = slot_files["calls/atm.csv"].decode().strip().split("\n")[0].split(",")
        emitted = float(row[header.index("iv")])
        assert emitted == pytest.approx(at_five_minutes["iv"], rel=1e-9)
