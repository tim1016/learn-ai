"""Golden fixture validation for Phase 2 volatility fixtures.

Tests canonical implementations against hand-computed / literature oracles.

Fixtures covered:
  IV-001 — implied_volatility() solver round-trip
  IV-002 — fit_svi() total variance
  IV-003 — vix_style_iv30() constant-maturity interpolation
  IV-004 — OptionsFeatures.compute_iv_rank() rolling 60d
  RV-001 — close_to_close() realized vol
  RV-002 — hf_realized_vol_trd252() ABDL two-component RV
  RV-003 — convert_iv_act365_to_trading252() basis conversion
  RV-004 — replicate_expiry_variance() CBOE formula

Run standalone (no FastAPI app needed):
  python -m pytest tests/fixtures/test_volatility_fixtures.py -v --noconftest
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa

_SVC_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_SVC_ROOT))

from golden_support.registry import default as registry  # noqa: E402

from app.engine.edge.features_realtime.hf_realized_vol import hf_realized_vol_trd252  # noqa: E402
from app.engine.edge.features_realtime.realized_vol import close_to_close  # noqa: E402
from app.research.features.options_features import OptionsFeatures  # noqa: E402
from app.volatility.basis import convert_iv_act365_to_trading252  # noqa: E402
from app.volatility.fitting import SmileSlice, fit_svi  # noqa: E402
from app.volatility.solver import SolveStatus, implied_volatility  # noqa: E402
from app.volatility.vix_replication import OptionQuote, replicate_expiry_variance, vix_style_iv30  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _load(fixture_id: str) -> tuple[pa.Table, pa.Table, float, float]:
    files = registry.active_files(fixture_id)
    fixture_dir = registry.fixture_dir(fixture_id)
    mf = registry._manifest.by_id(fixture_id)
    inp = pa.ipc.open_file(fixture_dir / files.input).read_all()
    out = pa.ipc.open_file(fixture_dir / files.output).read_all()
    return inp, out, mf.tolerance.atol, mf.tolerance.rtol


def _assert_close(
    canonical: float,
    oracle: float,
    atol: float,
    rtol: float,
    label: str,
) -> None:
    tol = atol + rtol * abs(oracle)
    assert abs(canonical - oracle) <= tol, (
        f"{label}: canonical={canonical:.6e} oracle={oracle:.6e} "
        f"diff={abs(canonical-oracle):.2e} atol={atol:.2e} rtol={rtol:.2e}"
    )


def _assert_nan_series(canonical: list[float | None], oracle: list[float | None], atol: float, rtol: float, label: str) -> None:
    assert len(canonical) == len(oracle), f"{label}: length mismatch"
    for i, (c, o) in enumerate(zip(canonical, oracle, strict=True)):
        if o is None or (isinstance(o, float) and math.isnan(o)):
            assert c is None or (isinstance(c, float) and math.isnan(c)), (
                f"{label}[{i}]: expected NaN, got {c}"
            )
        else:
            assert c is not None, f"{label}[{i}]: expected {o}, got None"
            assert not (isinstance(c, float) and math.isnan(c)), f"{label}[{i}]: expected {o}, got NaN"
            _assert_close(c, o, atol, rtol, f"{label}[{i}]")


# ---------------------------------------------------------------------------
# IV-001: Implied Volatility Solver Round-Trip
# ---------------------------------------------------------------------------


class TestIV001SolverRoundTrip:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("IV-001")
        assert len(inp) == 12

    def test_solver_recovers_sigma_all_cases(self) -> None:
        inp, out, atol, rtol = _load("IV-001")
        for row in range(len(inp)):
            spot = float(inp["spot"][row].as_py())
            strike = float(inp["strike"][row].as_py())
            rate = float(inp["rate"][row].as_py())
            ttm = float(inp["ttm_years"][row].as_py())
            dividend = float(inp["dividend"][row].as_py())
            market_price = float(inp["market_price"][row].as_py())
            is_call = bool(inp["is_call"][row].as_py())
            sigma_oracle = float(out["sigma_oracle"][row].as_py())

            result = implied_volatility(
                option_price=market_price,
                spot=spot,
                strike=strike,
                ttm=ttm,
                rate=rate,
                dividend=dividend,
                is_call=is_call,
            )
            assert result.iv is not None, (
                f"Row {row}: solver returned None (status={result.status})"
            )
            _assert_close(result.iv, sigma_oracle, atol, rtol, f"IV-001 row={row}")

    def test_solver_converges_all_cases(self) -> None:
        inp, _out, _atol, _rtol = _load("IV-001")
        for row in range(len(inp)):
            spot = float(inp["spot"][row].as_py())
            strike = float(inp["strike"][row].as_py())
            rate = float(inp["rate"][row].as_py())
            ttm = float(inp["ttm_years"][row].as_py())
            dividend = float(inp["dividend"][row].as_py())
            market_price = float(inp["market_price"][row].as_py())
            is_call = bool(inp["is_call"][row].as_py())

            result = implied_volatility(
                option_price=market_price,
                spot=spot,
                strike=strike,
                ttm=ttm,
                rate=rate,
                dividend=dividend,
                is_call=is_call,
            )
            assert result.status in (
                SolveStatus.NEWTON_OK,
                SolveStatus.QUANTLIB_OK,
                SolveStatus.BRENT_FALLBACK,
            ), f"Row {row}: unexpected status {result.status}"

    def test_iv_positive_and_finite(self) -> None:
        inp, _out, _atol, _rtol = _load("IV-001")
        for row in range(len(inp)):
            spot = float(inp["spot"][row].as_py())
            strike = float(inp["strike"][row].as_py())
            rate = float(inp["rate"][row].as_py())
            ttm = float(inp["ttm_years"][row].as_py())
            dividend = float(inp["dividend"][row].as_py())
            market_price = float(inp["market_price"][row].as_py())
            is_call = bool(inp["is_call"][row].as_py())
            result = implied_volatility(
                option_price=market_price, spot=spot, strike=strike, ttm=ttm,
                rate=rate, dividend=dividend, is_call=is_call,
            )
            assert result.iv is not None and math.isfinite(result.iv) and result.iv > 0


# ---------------------------------------------------------------------------
# IV-002: SVI Total Variance Surface
# ---------------------------------------------------------------------------


def _svi_total_var(k: float, a: float, b: float, rho: float, m: float, sigma: float) -> float:
    diff = k - m
    return a + b * (rho * diff + math.sqrt(diff * diff + sigma * sigma))


class TestIV002SVISurface:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("IV-002")
        assert len(inp) == 21  # 3 param sets × 7 k-values

    def test_fit_svi_recovers_oracle_all_param_sets(self) -> None:
        inp, out, atol, rtol = _load("IV-002")

        # Group rows into 3 param-set blocks of 7
        n_per_set = 7
        n_sets = 3
        for s in range(n_sets):
            rows = range(s * n_per_set, (s + 1) * n_per_set)
            k_vals = np.array([float(inp["k"][r].as_py()) for r in rows])
            forward = float(inp["forward"][rows[0]].as_py())
            ttm = float(inp["ttm"][rows[0]].as_py())
            w_oracles = [float(out["w_oracle"][r].as_py()) for r in rows]

            # Build SmileSlice from oracle total variance
            strikes = forward * np.exp(k_vals)
            ivs = np.array([math.sqrt(w / ttm) for w in w_oracles])
            smile = SmileSlice(strikes=strikes, ivs=ivs, ttm=ttm, forward=forward)

            fit = fit_svi(smile)
            assert fit.success, f"Set {s}: fit_svi did not converge: {fit.message}"

            for i, (k, w_oracle) in enumerate(zip(k_vals, w_oracles, strict=True)):
                iv_fit = fit.volatility(float(strikes[i]))
                w_fit = iv_fit**2 * ttm
                tol = atol + rtol * abs(w_oracle)
                assert abs(w_fit - w_oracle) <= tol, (
                    f"Set {s} k={k:.3f}: w_fit={w_fit:.6e} oracle={w_oracle:.6e} "
                    f"diff={abs(w_fit-w_oracle):.2e}"
                )

    def test_oracle_formula_positive_for_all_rows(self) -> None:
        _inp, out, _atol, _rtol = _load("IV-002")
        for row in range(len(out)):
            w = float(out["w_oracle"][row].as_py())
            assert w > 0, f"Row {row}: oracle w(k)={w} must be positive"

    def test_svi_smile_shape_left_skew(self) -> None:
        """Param set 0 (rho=-0.7) must show left skew: w(-0.3) > w(0.3)."""
        _inp, out, _atol, _rtol = _load("IV-002")
        # Set 0, row 0 = k=-0.30; row 6 = k=+0.30
        w_left = float(out["w_oracle"][0].as_py())
        w_right = float(out["w_oracle"][6].as_py())
        assert w_left > w_right, f"Expected left skew: w(-0.30)={w_left:.6f} > w(0.30)={w_right:.6f}"


# ---------------------------------------------------------------------------
# IV-003: IV30 Constant-Maturity
# ---------------------------------------------------------------------------


class TestIV003IV30:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("IV-003")
        assert len(inp) == 3

    def test_vix_style_iv30_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("IV-003")
        for row in range(len(inp)):
            rate = float(inp["rate"][row].as_py())
            T1_cal = int(inp["T1_cal_days"][row].as_py())
            T2_cal = int(inp["T2_cal_days"][row].as_py())
            iv30_oracle = float(out["iv30_oracle"][row].as_py())

            # Reconstruct two chains from fixture — both call_mid and put_mid are
            # stored so vix_style_iv30 can recover the forward via put-call parity.
            n_strikes = 5
            chain1: list[OptionQuote] = []
            chain2: list[OptionQuote] = []
            for i in range(n_strikes):
                k1 = float(inp[f"e1_strike_{i}"][row].as_py())
                cm1 = float(inp[f"e1_call_mid_{i}"][row].as_py())
                pm1 = float(inp[f"e1_put_mid_{i}"][row].as_py())
                k2 = float(inp[f"e2_strike_{i}"][row].as_py())
                cm2 = float(inp[f"e2_call_mid_{i}"][row].as_py())
                pm2 = float(inp[f"e2_put_mid_{i}"][row].as_py())
                chain1.append(OptionQuote(k1, cm1, cm1, pm1, pm1))
                chain2.append(OptionQuote(k2, cm2, cm2, pm2, pm2))

            result = vix_style_iv30(
                chain1,
                chain2,
                rate1=rate,
                T1_calendar_days=T1_cal,
                rate2=rate,
                T2_calendar_days=T2_cal,
                target_calendar_days=30,
            )
            _assert_close(result, iv30_oracle, atol, rtol, f"IV-003 row={row}")

    def test_iv30_positive_all_cases(self) -> None:
        _inp, out, _atol, _rtol = _load("IV-003")
        for row in range(len(out)):
            iv30 = float(out["iv30_oracle"][row].as_py())
            assert iv30 > 0, f"Row {row}: iv30_oracle={iv30} must be positive"


# ---------------------------------------------------------------------------
# IV-004: IV Rank Rolling 60-Day Window
# ---------------------------------------------------------------------------


class TestIV004IVRank:
    _N_BARS = 80
    _WINDOW = 60
    _MIN_PERIODS = 30

    def _get_iv_and_oracle(self) -> tuple[np.ndarray, np.ndarray]:
        inp, out, _atol, _rtol = _load("IV-004")
        iv = np.array([float(inp[f"iv_{i}"][0].as_py()) for i in range(self._N_BARS)])
        oracle = np.array(
            [
                float(out[f"rank_{i}"][0].as_py())
                for i in range(self._N_BARS)
            ]
        )
        return iv, oracle

    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("IV-004")
        assert len(inp) == 1

    def test_iv_rank_matches_oracle(self) -> None:
        iv, oracle_arr = self._get_iv_and_oracle()
        _inp, _out, atol, rtol = _load("IV-004")

        # Build DataFrame for canonical
        df = pd.DataFrame({"iv_30d_atm": iv})
        canonical = OptionsFeatures.compute_iv_rank(
            df, window=self._WINDOW, min_periods=self._MIN_PERIODS
        ).values

        for i in range(self._N_BARS):
            c = float(canonical[i])
            o = float(oracle_arr[i])
            if np.isnan(o):
                assert np.isnan(c), f"Bar {i}: expected NaN, got {c}"
            else:
                assert not np.isnan(c), f"Bar {i}: expected {o}, got NaN"
                _assert_close(c, o, atol, rtol, f"IV-004 bar={i}")

    def test_half_before_min_periods(self) -> None:
        """Canonical returns 0.5 (not NaN) before min_periods.

        np.where(rolling_denom > 1e-10, rank, 0.5) — when denom = NaN (pre-warmup),
        NaN > 1e-10 is False so the result is 0.5, not NaN.
        """
        _iv, oracle_arr = self._get_iv_and_oracle()
        for i in range(self._MIN_PERIODS - 1):
            assert oracle_arr[i] == 0.5, f"Bar {i}: expected 0.5 before min_periods={self._MIN_PERIODS}, got {oracle_arr[i]}"

    def test_rank_bounded_zero_to_one(self) -> None:
        _iv, oracle_arr = self._get_iv_and_oracle()
        for i, r in enumerate(oracle_arr):
            if not np.isnan(r):
                assert 0.0 <= r <= 1.0, f"Bar {i}: rank={r} out of [0, 1]"


# ---------------------------------------------------------------------------
# RV-001: Close-to-Close Realized Volatility
# ---------------------------------------------------------------------------


class TestRV001CloseToClose:
    _N_BARS = 30
    _WINDOW = 10

    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("RV-001")
        assert len(inp) == 1

    def test_rv_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("RV-001")
        closes = np.array([float(inp[f"close_{i}"][0].as_py()) for i in range(self._N_BARS)])
        oracle = np.array([float(out[f"rv_{i}"][0].as_py()) for i in range(self._N_BARS)])

        bars = pd.DataFrame({"close": closes})
        canonical = close_to_close(bars, window=self._WINDOW).values

        for i in range(self._N_BARS):
            c = float(canonical[i])
            o = float(oracle[i])
            if np.isnan(o):
                assert np.isnan(c), f"Bar {i}: expected NaN, got {c}"
            else:
                assert not np.isnan(c), f"Bar {i}: expected {o:.6e}, got NaN"
                _assert_close(c, o, atol, rtol, f"RV-001 bar={i}")

    def test_nan_before_window(self) -> None:
        _inp, out, _atol, _rtol = _load("RV-001")
        oracle = [float(out[f"rv_{i}"][0].as_py()) for i in range(self._N_BARS)]
        for i in range(self._WINDOW):
            assert np.isnan(oracle[i]), f"Bar {i}: expected NaN before window={self._WINDOW}"

    def test_rv_positive_after_warmup(self) -> None:
        _inp, out, _atol, _rtol = _load("RV-001")
        oracle = [float(out[f"rv_{i}"][0].as_py()) for i in range(self._N_BARS)]
        for i in range(self._WINDOW, self._N_BARS):
            assert not np.isnan(oracle[i]) and oracle[i] > 0, f"Bar {i}: expected positive RV, got {oracle[i]}"


# ---------------------------------------------------------------------------
# RV-002: HF Two-Component Realized Volatility (ABDL)
# ---------------------------------------------------------------------------


class TestRV002HFRealizedVol:
    _N_BARS = 20  # 5 days × 4 bars
    _WINDOW = 3

    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("RV-002")
        assert len(inp) == self._N_BARS

    def test_hf_rv_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("RV-002")

        ts_ms = [int(inp["ts_ms"][i].as_py()) for i in range(self._N_BARS)]
        opens = [float(inp["open"][i].as_py()) for i in range(self._N_BARS)]
        closes_vals = [float(inp["close"][i].as_py()) for i in range(self._N_BARS)]
        volumes = [int(inp["volume"][i].as_py()) for i in range(self._N_BARS)]
        oracle = [float(out["rv_hf"][i].as_py()) for i in range(self._N_BARS)]

        # Build properly typed DataFrame (UTC tz-aware DatetimeIndex)
        index = pd.DatetimeIndex(
            [pd.Timestamp(t, unit="ms", tz="UTC") for t in ts_ms]
        )
        bars = pd.DataFrame(
            {"open": opens, "close": closes_vals, "volume": volumes},
            index=index,
        )
        canonical = hf_realized_vol_trd252(bars, window_trading_days=self._WINDOW).values

        for i in range(self._N_BARS):
            c = float(canonical[i])
            o = float(oracle[i])
            if np.isnan(o):
                assert np.isnan(c), f"Bar {i}: expected NaN, got {c}"
            else:
                assert not np.isnan(c), f"Bar {i}: expected {o:.6e}, got NaN"
                _assert_close(c, o, atol, rtol, f"RV-002 bar={i}")

    def test_nan_before_window_days(self) -> None:
        """First (window-1) trading days should produce NaN bars."""
        _inp, out, _atol, _rtol = _load("RV-002")
        oracle = [float(out["rv_hf"][i].as_py()) for i in range(self._N_BARS)]
        # First (window-1)*bars_per_day = 2*4 = 8 bars are NaN
        n_nan_bars = (self._WINDOW - 1) * 4
        for i in range(n_nan_bars):
            assert np.isnan(oracle[i]), f"Bar {i}: expected NaN (pre-window), got {oracle[i]}"


# ---------------------------------------------------------------------------
# RV-003: IV-RV Basis Conversion
# ---------------------------------------------------------------------------


class TestRV003BasisConversion:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("RV-003")
        assert len(inp) == 3

    def test_conversion_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("RV-003")
        for row in range(len(inp)):
            sigma_act365 = float(inp["sigma_act365"][row].as_py())
            D = int(inp["tenor_calendar_days"][row].as_py())
            asof_ms = int(inp["asof_ms"][row].as_py())
            sigma_oracle = float(out["sigma_trd252"][row].as_py())

            result = convert_iv_act365_to_trading252(
                sigma_act365=sigma_act365,
                asof=asof_ms,
                tenor_calendar_days=D,
            )
            _assert_close(result, sigma_oracle, atol, rtol, f"RV-003 row={row}")

    def test_pinned_trading_days_matches_calendar(self) -> None:
        """n_trading_pinned in fixture must match current pandas_market_calendars call."""
        import pandas_market_calendars as mcal
        inp, _out, _atol, _rtol = _load("RV-003")
        nyse = mcal.get_calendar("NYSE")
        for row in range(len(inp)):
            D = int(inp["tenor_calendar_days"][row].as_py())
            asof_ms = int(inp["asof_ms"][row].as_py())
            n_pinned = int(inp["n_trading_pinned"][row].as_py())
            ts = pd.Timestamp(asof_ms, unit="ms", tz="UTC").tz_convert("America/New_York").normalize().tz_localize(None)
            end_incl = ts + pd.Timedelta(days=D - 1)
            schedule = nyse.schedule(start_date=str(ts.date()), end_date=str(end_incl.date()))
            assert len(schedule) == n_pinned, (
                f"Row {row}: pinned N={n_pinned} != calendar N={len(schedule)}"
            )

    def test_conversion_produces_positive_finite(self) -> None:
        _inp, out, _atol, _rtol = _load("RV-003")
        for row in range(len(out)):
            sigma = float(out["sigma_trd252"][row].as_py())
            assert sigma > 0 and math.isfinite(sigma), f"Row {row}: invalid sigma_trd252={sigma}"


# ---------------------------------------------------------------------------
# RV-004: Model-Free Variance Replication (CBOE Formula)
# ---------------------------------------------------------------------------


class TestRV004ModelFreeVariance:
    def test_row_count(self) -> None:
        inp, _out, _atol, _rtol = _load("RV-004")
        assert len(inp) == 3

    def test_replicate_expiry_variance_matches_oracle(self) -> None:
        inp, out, atol, rtol = _load("RV-004")
        for row in range(len(inp)):
            rate = float(inp["rate"][row].as_py())
            T_cal = int(inp["T_cal_days"][row].as_py())
            n_strikes = int(inp["n_strikes"][row].as_py())
            sigma_sq_oracle = float(out["sigma_sq_oracle"][row].as_py())

            T_years = T_cal / 365.0
            # Provide full call and put mids for all strikes so _select_atm_strike
            # can use put-call parity to recover the correct forward.
            quotes: list[OptionQuote] = []
            for i in range(n_strikes):
                K = float(inp[f"strike_{i}"][row].as_py())
                call_mid = float(inp[f"call_mid_{i}"][row].as_py())
                put_mid = float(inp[f"put_mid_{i}"][row].as_py())
                quotes.append(OptionQuote(K, call_mid, call_mid, put_mid, put_mid))

            result = replicate_expiry_variance(quotes, rate=rate, T_years=T_years)
            _assert_close(
                result.sigma_squared_T, sigma_sq_oracle, atol, rtol,
                f"RV-004 row={row}"
            )

    def test_sigma_sq_positive_all_cases(self) -> None:
        _inp, out, _atol, _rtol = _load("RV-004")
        for row in range(len(out)):
            v = float(out["sigma_sq_oracle"][row].as_py())
            assert v > 0, f"Row {row}: sigma_sq_oracle={v} must be positive"
