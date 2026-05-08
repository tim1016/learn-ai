"""Golden-fixture tests for Phase 3: Research Primitives and Indicator Reliability.

RP-001  compute_information_coefficient  — Spearman IC (mean, t-stat)
RP-002  compute_quantile_analysis        — bin-mean monotonicity
RP-003  _empirical_position              — Phipson-Smyth p-value
RP-004  compute_train_zscore             — train-period z-score
REL-001 compute_information_coefficient  — IC hit_rate (win-rate stability)
REL-004 compute_ic_decay_curve          — IC per horizon (EMA-10 decay)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.ipc as ipc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GOLDEN = Path(__file__).parent / "golden"


def _read(fixture_id: str, category: str, filename: str):
    path = GOLDEN / category / fixture_id / "v1" / filename
    with ipc.open_file(str(path)) as r:
        return r.read_all()


def _f(tbl, col: str) -> float:
    return float(tbl.column(col)[0].as_py())


def _arr(tbl, prefix: str, n: int) -> np.ndarray:
    return np.array([float(tbl.column(f"{prefix}{i}")[0].as_py()) for i in range(n)], dtype=np.float64)


# ---------------------------------------------------------------------------
# RP-001 — Information Coefficient
# ---------------------------------------------------------------------------

class TestRP001InformationCoefficient:
    _CAT = "research-primitives"
    _ID = "RP-001"
    _ATOL = 1e-9

    def _load(self):
        return _read(self._ID, self._CAT, "input.arrow"), _read(self._ID, self._CAT, "output.arrow")

    def test_row_count(self) -> None:
        inp, _ = self._load()
        assert len(inp) == 40, f"Expected 40 bars (4 days × 10), got {len(inp)}"

    def test_mean_ic_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.validation.ic import compute_information_coefficient

        feature = pd.Series(inp.column("feature").to_pylist())
        target = pd.Series(inp.column("target_return").to_pylist())
        timestamps = pd.Series(inp.column("timestamps_ms").to_pylist())

        result = compute_information_coefficient(feature, target, timestamps)

        oracle_mean = _f(out, "oracle_mean_ic")
        assert abs(result.mean_ic - oracle_mean) < self._ATOL, (
            f"mean_ic: canonical={result.mean_ic:.9f} oracle={oracle_mean:.9f} "
            f"diff={abs(result.mean_ic - oracle_mean):.2e}"
        )

    def test_t_stat_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.validation.ic import compute_information_coefficient

        feature = pd.Series(inp.column("feature").to_pylist())
        target = pd.Series(inp.column("target_return").to_pylist())
        timestamps = pd.Series(inp.column("timestamps_ms").to_pylist())

        result = compute_information_coefficient(feature, target, timestamps)
        oracle_t = _f(out, "oracle_t_stat")
        assert abs(result.ic_t_stat - oracle_t) < self._ATOL, (
            f"t_stat: canonical={result.ic_t_stat:.9f} oracle={oracle_t:.9f} "
            f"diff={abs(result.ic_t_stat - oracle_t):.2e}"
        )

    def test_n_days(self) -> None:
        _, out = self._load()
        n = int(out.column("oracle_n_days")[0].as_py())
        assert n == 4, f"Expected 4 daily ICs, got {n}"

    def test_daily_ics_match_oracle(self) -> None:
        inp, out = self._load()
        from app.research.validation.ic import compute_information_coefficient

        feature = pd.Series(inp.column("feature").to_pylist())
        target = pd.Series(inp.column("target_return").to_pylist())
        timestamps = pd.Series(inp.column("timestamps_ms").to_pylist())

        result = compute_information_coefficient(feature, target, timestamps)
        n = int(out.column("oracle_n_days")[0].as_py())
        oracle_ics = [_f(out, f"oracle_daily_ic_{i}") for i in range(n)]

        for i, (can, ora) in enumerate(zip(result.daily_ic_values, oracle_ics, strict=True)):
            assert abs(can - ora) < self._ATOL, (
                f"daily_ic[{i}]: canonical={can:.9f} oracle={ora:.9f}"
            )


# ---------------------------------------------------------------------------
# RP-002 — Quantile Monotonicity
# ---------------------------------------------------------------------------

class TestRP002QuantileMonotonicity:
    _CAT = "research-primitives"
    _ID = "RP-002"
    _ATOL = 1e-9
    _N_BINS = 5

    def _load(self):
        return _read(self._ID, self._CAT, "input.arrow"), _read(self._ID, self._CAT, "output.arrow")

    def test_row_count(self) -> None:
        inp, _ = self._load()
        assert len(inp) == 200, f"Expected 200 obs, got {len(inp)}"

    def test_is_monotonic_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.validation.quantile import compute_quantile_analysis

        feature = pd.Series(inp.column("feature").to_pylist())
        target = pd.Series(inp.column("target_return").to_pylist())

        result = compute_quantile_analysis(feature, target, n_bins=self._N_BINS)
        oracle_mono = bool(out.column("oracle_is_monotonic")[0].as_py())
        assert result.is_monotonic == oracle_mono, (
            f"is_monotonic: canonical={result.is_monotonic} oracle={oracle_mono}"
        )

    def test_monotonicity_ratio_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.validation.quantile import compute_quantile_analysis

        feature = pd.Series(inp.column("feature").to_pylist())
        target = pd.Series(inp.column("target_return").to_pylist())

        result = compute_quantile_analysis(feature, target, n_bins=self._N_BINS)
        oracle_ratio = _f(out, "oracle_monotonicity_ratio")
        assert abs(result.monotonicity_ratio - oracle_ratio) < self._ATOL, (
            f"monotonicity_ratio: canonical={result.monotonicity_ratio:.9f} "
            f"oracle={oracle_ratio:.9f}"
        )

    def test_bin_means_match_oracle(self) -> None:
        inp, out = self._load()
        from app.research.validation.quantile import compute_quantile_analysis

        feature = pd.Series(inp.column("feature").to_pylist())
        target = pd.Series(inp.column("target_return").to_pylist())

        result = compute_quantile_analysis(feature, target, n_bins=self._N_BINS)
        n_actual_bins = len(result.bins)
        for i in range(n_actual_bins):
            oracle_mean = _f(out, f"oracle_bin_mean_{i}")
            can_mean = result.bins[i].mean_return
            assert abs(can_mean - oracle_mean) < self._ATOL, (
                f"bin_mean[{i}]: canonical={can_mean:.9f} oracle={oracle_mean:.9f}"
            )

    def test_monotonic_with_positive_signal(self) -> None:
        _, out = self._load()
        assert bool(out.column("oracle_is_monotonic")[0].as_py()), (
            "Expected monotonic result: feature is positively correlated with returns by design"
        )


# ---------------------------------------------------------------------------
# RP-003 — Phipson-Smyth P-value
# ---------------------------------------------------------------------------

class TestRP003PhipsonSmyth:
    _CAT = "research-primitives"
    _ID = "RP-003"
    _ATOL = 1e-9
    _N_NULL = 200

    def _load(self):
        return _read(self._ID, self._CAT, "input.arrow"), _read(self._ID, self._CAT, "output.arrow")

    def test_row_count(self) -> None:
        inp, _ = self._load()
        assert len(inp) == 1, f"Expected 1-row input (scalar observed_ic + wide null cols), got {len(inp)}"

    def test_p_value_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.baselines.runner import _empirical_position

        observed = float(inp.column("observed_ic")[0].as_py())
        null_dist = [float(inp.column(f"null_ic_{i}")[0].as_py()) for i in range(self._N_NULL)]

        _, p_value = _empirical_position(observed, null_dist)
        oracle_p = _f(out, "oracle_p_value")
        assert abs(p_value - oracle_p) < self._ATOL, (
            f"p_value: canonical={p_value:.9f} oracle={oracle_p:.9f}"
        )

    def test_percentile_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.baselines.runner import _empirical_position

        observed = float(inp.column("observed_ic")[0].as_py())
        null_dist = [float(inp.column(f"null_ic_{i}")[0].as_py()) for i in range(self._N_NULL)]

        percentile, _ = _empirical_position(observed, null_dist)
        oracle_pct = _f(out, "oracle_percentile")
        assert abs(percentile - oracle_pct) < self._ATOL, (
            f"percentile: canonical={percentile:.9f} oracle={oracle_pct:.9f}"
        )

    def test_p_value_positive(self) -> None:
        _, out = self._load()
        p = _f(out, "oracle_p_value")
        assert p > 0, "Phipson-Smyth p-value must be > 0 (1 is added to numerator)"

    def test_p_value_bounded(self) -> None:
        _, out = self._load()
        p = _f(out, "oracle_p_value")
        assert 0 < p <= 1.0, f"p-value out of (0,1]: {p}"


# ---------------------------------------------------------------------------
# RP-004 — Signal Z-score
# ---------------------------------------------------------------------------

class TestRP004SignalZscore:
    _CAT = "research-primitives"
    _ID = "RP-004"
    _ATOL = 1e-9
    _N_BARS = 50
    _N_TRAIN = 35

    def _load(self):
        return _read(self._ID, self._CAT, "input.arrow"), _read(self._ID, self._CAT, "output.arrow")

    def test_row_count(self) -> None:
        inp, _ = self._load()
        assert len(inp) == self._N_BARS, f"Expected {self._N_BARS} bars, got {len(inp)}"

    def test_zscore_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.signal.standardize import compute_train_zscore

        feature = pd.Series(inp.column("feature").to_pylist())
        train_mask = pd.Series(inp.column("train_mask").to_pylist())

        result = compute_train_zscore(feature, train_mask, flip_sign=False)

        for i in range(self._N_BARS):
            oracle_z = _f(out, f"oracle_z_{i}")
            can_z = float(result.iloc[i])
            assert abs(can_z - oracle_z) < self._ATOL, (
                f"z[{i}]: canonical={can_z:.9f} oracle={oracle_z:.9f}"
            )

    def test_zscore_flipped_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.signal.standardize import compute_train_zscore

        feature = pd.Series(inp.column("feature").to_pylist())
        train_mask = pd.Series(inp.column("train_mask").to_pylist())

        result = compute_train_zscore(feature, train_mask, flip_sign=True)

        for i in range(self._N_BARS):
            oracle_z = _f(out, f"oracle_z_flipped_{i}")
            can_z = float(result.iloc[i])
            assert abs(can_z - oracle_z) < self._ATOL, (
                f"z_flipped[{i}]: canonical={can_z:.9f} oracle={oracle_z:.9f}"
            )

    def test_train_mean_is_zero_in_zscore(self) -> None:
        inp, _out = self._load()
        from app.research.signal.standardize import compute_train_zscore

        feature = pd.Series(inp.column("feature").to_pylist())
        train_mask = pd.Series(inp.column("train_mask").to_pylist())

        result = compute_train_zscore(feature, train_mask, flip_sign=False)
        train_z_mean = float(result[train_mask].mean())
        assert abs(train_z_mean) < 1e-9, (
            f"Mean of train z-scores should be 0.0, got {train_z_mean:.9f}"
        )

    def test_flip_negates_zscore(self) -> None:
        _inp, out = self._load()
        # oracle_z_flipped[i] == -oracle_z[i] for all i
        for i in range(self._N_BARS):
            z = _f(out, f"oracle_z_{i}")
            zf = _f(out, f"oracle_z_flipped_{i}")
            assert abs(z + zf) < self._ATOL, (
                f"z_flipped[{i}] should equal -z[{i}]: z={z:.9f} zf={zf:.9f}"
            )


# ---------------------------------------------------------------------------
# REL-001 — IC Hit Rate
# ---------------------------------------------------------------------------

class TestREL001ICHitRate:
    _CAT = "indicator-reliability"
    _ID = "REL-001"
    _ATOL = 1e-9

    def _load(self):
        return _read(self._ID, self._CAT, "input.arrow"), _read(self._ID, self._CAT, "output.arrow")

    def test_row_count(self) -> None:
        inp, _ = self._load()
        assert len(inp) == 40, f"Expected 40 bars (4 days × 10), got {len(inp)}"

    def test_hit_rate_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.validation.ic import compute_information_coefficient

        feature = pd.Series(inp.column("feature").to_pylist())
        target = pd.Series(inp.column("target_return").to_pylist())
        timestamps = pd.Series(inp.column("timestamps_ms").to_pylist())

        result = compute_information_coefficient(feature, target, timestamps)
        oracle_hr = _f(out, "oracle_hit_rate")
        assert abs(result.hit_rate - oracle_hr) < self._ATOL, (
            f"hit_rate: canonical={result.hit_rate:.9f} oracle={oracle_hr:.9f} "
            f"diff={abs(result.hit_rate - oracle_hr):.2e}"
        )

    def test_mean_ic_matches(self) -> None:
        inp, out = self._load()
        from app.research.validation.ic import compute_information_coefficient

        feature = pd.Series(inp.column("feature").to_pylist())
        target = pd.Series(inp.column("target_return").to_pylist())
        timestamps = pd.Series(inp.column("timestamps_ms").to_pylist())

        result = compute_information_coefficient(feature, target, timestamps)
        oracle_mean = _f(out, "oracle_mean_ic")
        assert abs(result.mean_ic - oracle_mean) < self._ATOL

    def test_hit_rate_bounded(self) -> None:
        _, out = self._load()
        hr = _f(out, "oracle_hit_rate")
        assert 0.0 <= hr <= 1.0, f"hit_rate out of [0,1]: {hr}"


# ---------------------------------------------------------------------------
# REL-004 — IC Decay Curve
# ---------------------------------------------------------------------------

class TestREL004ICDecayCurve:
    _CAT = "indicator-reliability"
    _ID = "REL-004"
    _ATOL = 1e-9
    _MAX_HORIZON = 5
    _EMA_PERIOD = 10

    def _load(self):
        return _read(self._ID, self._CAT, "input.arrow"), _read(self._ID, self._CAT, "output.arrow")

    def test_row_count(self) -> None:
        inp, _ = self._load()
        assert len(inp) == 100, f"Expected 100 bars (5 days × 20), got {len(inp)}"

    def test_decay_curve_matches_oracle(self) -> None:
        inp, out = self._load()
        from app.research.indicator_reliability import compute_ic_decay_curve

        df = pd.DataFrame(
            {
                "timestamp": inp.column("timestamp").to_pylist(),
                "close": inp.column("close").to_pylist(),
                "ema10": inp.column("ema10").to_pylist(),
            }
        )

        curve = compute_ic_decay_curve(df, "ema10", self._MAX_HORIZON)
        assert len(curve) == self._MAX_HORIZON, (
            f"Expected {self._MAX_HORIZON} decay points, got {len(curve)}"
        )
        for pt in curve:
            h = pt.horizon
            oracle_ic = _f(out, f"oracle_horizon_{h}_ic")
            assert abs(pt.ic - oracle_ic) < self._ATOL, (
                f"horizon={h}: canonical IC={pt.ic:.9f} oracle={oracle_ic:.9f} "
                f"diff={abs(pt.ic - oracle_ic):.2e}"
            )

    def test_horizons_are_sequential(self) -> None:
        inp, _out = self._load()
        from app.research.indicator_reliability import compute_ic_decay_curve

        df = pd.DataFrame(
            {
                "timestamp": inp.column("timestamp").to_pylist(),
                "close": inp.column("close").to_pylist(),
                "ema10": inp.column("ema10").to_pylist(),
            }
        )
        curve = compute_ic_decay_curve(df, "ema10", self._MAX_HORIZON)
        horizons = [pt.horizon for pt in curve]
        assert horizons == list(range(1, self._MAX_HORIZON + 1)), (
            f"Expected horizons 1..{self._MAX_HORIZON}, got {horizons}"
        )

    def test_ic_values_finite(self) -> None:
        _inp, out = self._load()
        for h in range(1, self._MAX_HORIZON + 1):
            ic = _f(out, f"oracle_horizon_{h}_ic")
            assert np.isfinite(ic), f"oracle IC for horizon={h} is not finite: {ic}"
