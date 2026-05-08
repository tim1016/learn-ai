"""Generators for Phase 3 golden fixtures: Research Primitives and Reliability.

RP-001  compute_information_coefficient  — literature_formula (Spearman IC)
RP-002  compute_quantile_analysis        — hand_computed (bin-mean monotonicity)
RP-003  _empirical_position              — literature_formula (Phipson-Smyth p-value)
RP-004  compute_train_zscore             — hand_computed (z-score arithmetic)
REL-001 compute_information_coefficient  — hand_computed (hit_rate sign-consistency)
REL-004 compute_ic_decay_curve          — literature_formula (Spearman per horizon)
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "PythonDataService" / "tests" / "fixtures"))
sys.path.insert(0, str(REPO_ROOT / "PythonDataService"))

from golden_support.hashing import compute_hashes  # noqa: E402
from golden_support.io import write_arrow  # noqa: E402

GENERATION_DATE = date(2026, 5, 8).isoformat()

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# 2024-01-02 09:30:00 ET = 2024-01-02 14:30:00 UTC
_DAY0_BASE_MS = 1704202200000
_BAR_INTERVAL_MS = 15 * 60 * 1000
_DAY_SECONDS = 24 * 60 * 60 * 1000


def _timestamps_for_days(n_days: int, bars_per_day: int) -> list[int]:
    ts = []
    for d in range(n_days):
        day_base = _DAY0_BASE_MS + d * _DAY_SECONDS
        for b in range(bars_per_day):
            ts.append(day_base + b * _BAR_INTERVAL_MS)
    return ts


def _gbm_close(n_bars: int, seed: int, s0: float = 100.0, sigma: float = 0.01) -> np.ndarray:
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(0.0, sigma, n_bars)
    return (s0 * np.exp(np.cumsum(log_returns))).astype(np.float64)


def _ema_series(closes: np.ndarray, period: int) -> np.ndarray:
    """Standard exponential smoothing: k = 2/(1+period). Not Wilder (k=1/period)."""
    k = 2.0 / (1 + period)
    ema = np.full(len(closes), np.nan, dtype=np.float64)
    ema[0] = closes[0]
    for i in range(1, len(closes)):
        ema[i] = k * closes[i] + (1 - k) * ema[i - 1]
    return ema


# ---------------------------------------------------------------------------
# Shared data for RP-001 / REL-001
# ---------------------------------------------------------------------------

_RP001_N_DAYS = 4
_RP001_BARS_PER_DAY = 10


def _rp001_data() -> tuple[list[int], np.ndarray, np.ndarray]:
    n = _RP001_N_DAYS * _RP001_BARS_PER_DAY
    feature = np.random.default_rng(42).normal(0.0, 1.0, n).astype(np.float64)
    returns = np.random.default_rng(43).normal(0.0, 0.01, n).astype(np.float64)
    ts = _timestamps_for_days(_RP001_N_DAYS, _RP001_BARS_PER_DAY)
    return ts, feature, returns


# ---------------------------------------------------------------------------
# Oracle functions (independent of canonical code)
# ---------------------------------------------------------------------------

def _oracle_ic_per_day(
    ts_ms: list[int],
    feature: np.ndarray,
    target: np.ndarray,
) -> tuple[list[float], list[str]]:
    from scipy import stats as sp_stats
    import pandas as pd

    dates = pd.to_datetime(ts_ms, unit="ms").date
    day_ics: list[float] = []
    day_labels: list[str] = []
    for day in sorted(set(dates)):
        mask = np.array([d == day for d in dates])
        f, r = feature[mask], target[mask]
        valid = ~(np.isnan(f) | np.isnan(r))
        f_v, r_v = f[valid], r[valid]
        if len(f_v) < 5:
            continue
        if f_v.std() < 1e-12 or r_v.std() < 1e-12:
            continue
        corr, _ = sp_stats.spearmanr(f_v, r_v)
        if not np.isnan(corr):
            day_ics.append(float(corr))
            day_labels.append(str(day))
    return day_ics, day_labels


def _oracle_mean_t_hitrate(daily_ics: list[float]) -> tuple[float, float, float]:
    arr = np.array(daily_ics, dtype=np.float64)
    n = len(arr)
    if n < 2:
        return float(arr[0]) if n == 1 else 0.0, 0.0, 0.0
    mean_ic = float(arr.mean())
    std_ic = float(arr.std(ddof=1))
    t_stat = float(mean_ic / (std_ic / np.sqrt(n))) if std_ic > 1e-15 else 0.0
    sign = 1.0 if mean_ic >= 0 else -1.0
    hit_rate = float(np.mean(arr * sign > 0))
    return mean_ic, t_stat, hit_rate


def _oracle_quantile_bins(
    feature: np.ndarray,
    returns: np.ndarray,
    n_bins: int,
) -> tuple[list[float], float]:
    import pandas as pd

    df = pd.DataFrame({"f": feature, "r": returns}).dropna()
    df["q"] = pd.qcut(df["f"], q=n_bins, labels=False, duplicates="drop")
    bin_means = df.groupby("q")["r"].mean().sort_index().values.astype(np.float64)
    n_steps = len(bin_means) - 1
    if n_steps == 0:
        return list(bin_means), 0.0
    increasing = sum(1 for i in range(n_steps) if bin_means[i + 1] > bin_means[i])
    return list(bin_means), float(increasing / n_steps)


def _oracle_phipson_smyth(parent: float, null_values: list[float]) -> tuple[float, float]:
    arr = np.asarray(null_values, dtype=np.float64)
    n = arr.size
    percentile = float(np.mean(arr < parent))
    p_value = float((1 + np.sum(arr >= parent)) / (n + 1))
    return percentile, p_value


def _oracle_zscore(
    feature: np.ndarray,
    train_mask: np.ndarray,
    flip_sign: bool,
) -> np.ndarray:
    train_vals = feature[train_mask & ~np.isnan(feature)]
    mu = float(train_vals.mean())
    sigma = float(train_vals.std(ddof=1))
    if sigma < 1e-10 or np.isnan(sigma):
        return np.full(len(feature), np.nan, dtype=np.float64)
    z = (feature - mu) / sigma
    if flip_sign:
        z = -z
    return z.astype(np.float64)


def _oracle_ic_decay(
    ts_ms: list[int],
    closes: np.ndarray,
    ema: np.ndarray,
    max_horizon: int,
) -> list[tuple[int, float]]:
    from scipy import stats as sp_stats
    import pandas as pd

    ts_arr = np.array(ts_ms, dtype=np.int64)
    dates = pd.to_datetime(ts_arr, unit="ms").date
    n = len(ts_arr)
    results = []

    for horizon in range(1, max_horizon + 1):
        fwd_returns = np.full(n, np.nan)
        for i in range(n - horizon):
            if dates[i] != dates[i + horizon]:
                continue
            if closes[i] > 0 and closes[i + horizon] > 0:
                fwd_returns[i] = np.log(closes[i + horizon] / closes[i])

        day_ics: list[float] = []
        for day in sorted(set(dates)):
            mask = np.array([d == day for d in dates])
            f, r = ema[mask], fwd_returns[mask]
            valid = ~(np.isnan(f) | np.isnan(r))
            f_v, r_v = f[valid], r[valid]
            if len(f_v) < 5:
                continue
            if f_v.std() < 1e-12 or r_v.std() < 1e-12:
                continue
            corr, _ = sp_stats.spearmanr(f_v, r_v)
            if not np.isnan(corr):
                day_ics.append(float(corr))

        mean_ic = float(np.mean(day_ics)) if day_ics else 0.0
        results.append((horizon, mean_ic))

    return results


# ---------------------------------------------------------------------------
# RP-001: Information Coefficient
# ---------------------------------------------------------------------------

def generate_rp001(version_dir: Path, justification: str = "") -> dict:
    ts, feature, target = _rp001_data()
    daily_ics, daily_dates = _oracle_ic_per_day(ts, feature, target)
    mean_ic, t_stat, _ = _oracle_mean_t_hitrate(daily_ics)
    n = len(daily_ics)

    inp = pa.table(
        {
            "timestamps_ms": pa.array(ts, type=pa.int64()),
            "feature": pa.array(feature.tolist(), type=pa.float64()),
            "target_return": pa.array(target.tolist(), type=pa.float64()),
        }
    )
    out_fields: dict = {
        "oracle_mean_ic": pa.array([mean_ic], type=pa.float64()),
        "oracle_t_stat": pa.array([t_stat], type=pa.float64()),
        "oracle_n_days": pa.array([n], type=pa.int64()),
    }
    for i in range(n):
        out_fields[f"oracle_daily_ic_{i}"] = pa.array([daily_ics[i]], type=pa.float64())
        out_fields[f"oracle_daily_date_{i}"] = pa.array([daily_dates[i]], type=pa.string())
    out = pa.table(out_fields)

    write_arrow(inp, version_dir / "input.arrow")
    write_arrow(out, version_dir / "output.arrow")
    content_h, file_h = compute_hashes(version_dir, ["input.arrow", "output.arrow"])

    (version_dir / "attribution.md").write_text(
        f"""# RP-001 — Information Coefficient (Spearman IC)

Generated: {GENERATION_DATE}
Oracle: literature_formula — scipy.stats.spearmanr per calendar day; mean ± t-stat
Canonical: PythonDataService/app/research/validation/ic.py::compute_information_coefficient

## Formula

IC_d = Spearman(rank(feature_d), rank(return_d))  for each trading day d
mean_IC = (1/N) × sum(IC_d)
t = mean_IC / (std_IC(ddof=1) / sqrt(N))

Reference: López de Prado, Advances in Financial Machine Learning (2018) §8.

## Input

{_RP001_N_DAYS} trading days × {_RP001_BARS_PER_DAY} bars each = {_RP001_N_DAYS * _RP001_BARS_PER_DAY} bars.
Timestamps: 2024-01-02..2024-01-05, 09:30 ET, 15-min cadence (int64 ms UTC).
Feature: seeded N(0,1), seed=42. Target returns: seeded N(0,0.01), seed=43.

## Oracle computed values

mean_IC: {mean_ic:.9f}
t-stat:  {t_stat:.9f}
N days:  {n}
daily ICs: {[f'{x:.9f}' for x in daily_ics]}

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  {content_h['input.arrow']}
output.arrow: {content_h['output.arrow']}
""",
        encoding="utf-8",
    )
    return {"input.arrow": content_h["input.arrow"], "output.arrow": content_h["output.arrow"]}


# ---------------------------------------------------------------------------
# RP-002: Quantile Monotonicity
# ---------------------------------------------------------------------------

_RP002_N_OBS = 200
_RP002_N_BINS = 5


def generate_rp002(version_dir: Path, justification: str = "") -> dict:
    rng = np.random.default_rng(77)
    feature = rng.normal(0.0, 1.0, _RP002_N_OBS).astype(np.float64)
    noise = rng.normal(0.0, 0.005, _RP002_N_OBS)
    target = (feature * 0.003 + noise).astype(np.float64)

    bin_means, mono_ratio = _oracle_quantile_bins(feature, target, _RP002_N_BINS)
    is_monotonic = bool(mono_ratio >= 0.75)

    inp = pa.table(
        {
            "feature": pa.array(feature.tolist(), type=pa.float64()),
            "target_return": pa.array(target.tolist(), type=pa.float64()),
            "n_bins": pa.array([_RP002_N_BINS] * _RP002_N_OBS, type=pa.int64()),
        }
    )
    out_fields: dict = {
        "oracle_is_monotonic": pa.array([is_monotonic], type=pa.bool_()),
        "oracle_monotonicity_ratio": pa.array([mono_ratio], type=pa.float64()),
    }
    for i, bm in enumerate(bin_means):
        out_fields[f"oracle_bin_mean_{i}"] = pa.array([bm], type=pa.float64())
    out = pa.table(out_fields)

    write_arrow(inp, version_dir / "input.arrow")
    write_arrow(out, version_dir / "output.arrow")
    content_h, file_h = compute_hashes(version_dir, ["input.arrow", "output.arrow"])

    (version_dir / "attribution.md").write_text(
        f"""# RP-002 — Quantile Monotonicity

Generated: {GENERATION_DATE}
Oracle: hand_computed — pd.qcut into {_RP002_N_BINS} quantiles, mean return per bin
Canonical: PythonDataService/app/research/validation/quantile.py::compute_quantile_analysis

## Formula

bins = pd.qcut(feature, q={_RP002_N_BINS})
bin_mean[i] = mean(target[feature in bin_i])
monotonicity_ratio = count(bin_mean[i+1] > bin_mean[i]) / ({_RP002_N_BINS} - 1)
is_monotonic = monotonicity_ratio >= 0.75

## Input

{_RP002_N_OBS} observations. Feature: N(0,1) seed=77.
Target: feature×0.003 + N(0,0.005) — weakly monotonic by design.

## Oracle computed values

is_monotonic: {is_monotonic}
monotonicity_ratio: {mono_ratio:.9f}
bin means: {[f'{m:.9f}' for m in bin_means]}

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  {content_h['input.arrow']}
output.arrow: {content_h['output.arrow']}
""",
        encoding="utf-8",
    )
    return {"input.arrow": content_h["input.arrow"], "output.arrow": content_h["output.arrow"]}


# ---------------------------------------------------------------------------
# RP-003: Phipson-Smyth P-value
# ---------------------------------------------------------------------------

_RP003_N_NULL = 200
_RP003_OBSERVED_IC = 0.12


def generate_rp003(version_dir: Path, justification: str = "") -> dict:
    rng = np.random.default_rng(99)
    null_dist = rng.normal(0.0, 0.05, _RP003_N_NULL).astype(np.float64)
    observed = float(_RP003_OBSERVED_IC)

    percentile, p_value = _oracle_phipson_smyth(observed, list(null_dist))

    inp_fields: dict = {"observed_ic": pa.array([observed], type=pa.float64())}
    for i, v in enumerate(null_dist):
        inp_fields[f"null_ic_{i}"] = pa.array([float(v)], type=pa.float64())
    inp = pa.table(inp_fields)

    out = pa.table(
        {
            "oracle_percentile": pa.array([percentile], type=pa.float64()),
            "oracle_p_value": pa.array([p_value], type=pa.float64()),
        }
    )

    write_arrow(inp, version_dir / "input.arrow")
    write_arrow(out, version_dir / "output.arrow")
    content_h, file_h = compute_hashes(version_dir, ["input.arrow", "output.arrow"])

    (version_dir / "attribution.md").write_text(
        f"""# RP-003 — Phipson-Smyth Permutation P-value

Generated: {GENERATION_DATE}
Oracle: literature_formula — Phipson-Smyth (2010) p = (1+count(null≥obs))/(N+1)
Canonical: PythonDataService/app/research/baselines/runner.py::_empirical_position

## Formula

percentile = count(null < parent) / N          (fraction strictly less than)
p_value    = (1 + count(null >= parent)) / (N + 1)   — Phipson-Smyth small-sample

Reference: Phipson, B. & Smyth, G.K. (2010). Permutation p-values should never
be zero: calculating exact p-values when permutations are randomly drawn.
Statistical Applications in Genetics and Molecular Biology 9(1), Article 39.

## Input

Observed IC: {observed}
Null distribution: {_RP003_N_NULL} values, N(0, 0.05) seed=99.

## Oracle computed values

percentile: {percentile:.9f}
p_value:    {p_value:.9f}

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  {content_h['input.arrow']}
output.arrow: {content_h['output.arrow']}
""",
        encoding="utf-8",
    )
    return {"input.arrow": content_h["input.arrow"], "output.arrow": content_h["output.arrow"]}


# ---------------------------------------------------------------------------
# RP-004: Signal Z-score
# ---------------------------------------------------------------------------

_RP004_N_BARS = 50
_RP004_N_TRAIN = 35


def generate_rp004(version_dir: Path, justification: str = "") -> dict:
    rng = np.random.default_rng(55)
    feature = rng.normal(5.0, 2.0, _RP004_N_BARS).astype(np.float64)
    train_mask = np.array([i < _RP004_N_TRAIN for i in range(_RP004_N_BARS)])

    oracle_z = _oracle_zscore(feature, train_mask, flip_sign=False)
    oracle_z_flipped = _oracle_zscore(feature, train_mask, flip_sign=True)

    train_mu = float(feature[:_RP004_N_TRAIN].mean())
    train_sigma = float(feature[:_RP004_N_TRAIN].std())

    inp = pa.table(
        {
            "feature": pa.array(feature.tolist(), type=pa.float64()),
            "train_mask": pa.array(train_mask.tolist(), type=pa.bool_()),
            "n_train": pa.array([_RP004_N_TRAIN] * _RP004_N_BARS, type=pa.int64()),
        }
    )
    out_fields: dict = {}
    for i in range(_RP004_N_BARS):
        out_fields[f"oracle_z_{i}"] = pa.array([float(oracle_z[i])], type=pa.float64())
        out_fields[f"oracle_z_flipped_{i}"] = pa.array([float(oracle_z_flipped[i])], type=pa.float64())
    out = pa.table(out_fields)

    write_arrow(inp, version_dir / "input.arrow")
    write_arrow(out, version_dir / "output.arrow")
    content_h, file_h = compute_hashes(version_dir, ["input.arrow", "output.arrow"])

    (version_dir / "attribution.md").write_text(
        f"""# RP-004 — Signal Z-score (Train-period Standardization)

Generated: {GENERATION_DATE}
Oracle: hand_computed — direct formula: z = (x - mu_train) / sigma_train
Canonical: PythonDataService/app/research/signal/standardize.py::compute_train_zscore

## Formula

mu_train    = mean(feature[train_mask])
sigma_train = std(feature[train_mask])    (pandas/numpy ddof=1 default)
z           = (feature - mu_train) / sigma_train
z_flipped   = -z   (when flip_sign=True, for negative-IC signals)

## Input

{_RP004_N_BARS} bars. Feature: N(5, 2) seed=55. Train: first {_RP004_N_TRAIN} bars.

## Oracle computed parameters

mu_train:    {train_mu:.9f}
sigma_train: {train_sigma:.9f}

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  {content_h['input.arrow']}
output.arrow: {content_h['output.arrow']}
""",
        encoding="utf-8",
    )
    return {"input.arrow": content_h["input.arrow"], "output.arrow": content_h["output.arrow"]}


# ---------------------------------------------------------------------------
# REL-001: IC Hit Rate (Win-rate Stability)
# ---------------------------------------------------------------------------

def generate_rel001(version_dir: Path, justification: str = "") -> dict:
    ts, feature, target = _rp001_data()
    daily_ics, _ = _oracle_ic_per_day(ts, feature, target)
    mean_ic, t_stat, hit_rate = _oracle_mean_t_hitrate(daily_ics)
    n = len(daily_ics)

    inp = pa.table(
        {
            "timestamps_ms": pa.array(ts, type=pa.int64()),
            "feature": pa.array(feature.tolist(), type=pa.float64()),
            "target_return": pa.array(target.tolist(), type=pa.float64()),
        }
    )
    out = pa.table(
        {
            "oracle_mean_ic": pa.array([mean_ic], type=pa.float64()),
            "oracle_hit_rate": pa.array([hit_rate], type=pa.float64()),
            "oracle_n_days": pa.array([n], type=pa.int64()),
        }
    )

    write_arrow(inp, version_dir / "input.arrow")
    write_arrow(out, version_dir / "output.arrow")
    content_h, file_h = compute_hashes(version_dir, ["input.arrow", "output.arrow"])

    sign_desc = "positive (mean_ic >= 0)" if mean_ic >= 0 else "negative (mean_ic < 0)"
    (version_dir / "attribution.md").write_text(
        f"""# REL-001 — IC Hit Rate (Win-rate Stability)

Generated: {GENERATION_DATE}
Oracle: hand_computed — count(daily_ic × sign(mean_ic) > 0) / N
Canonical: PythonDataService/app/research/validation/ic.py::compute_information_coefficient (hit_rate field)

## Formula

expected_sign = sign(mean_IC) = {'+1' if mean_ic >= 0 else '-1'}  ({sign_desc})
hit_rate = count(daily_ic_d × expected_sign > 0) / N

## Input

Same synthetic data as RP-001: {_RP001_N_DAYS} days × {_RP001_BARS_PER_DAY} bars each.

## Oracle computed values

mean_ic:  {mean_ic:.9f}
hit_rate: {hit_rate:.9f}
n_days:   {n}

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  {content_h['input.arrow']}
output.arrow: {content_h['output.arrow']}
""",
        encoding="utf-8",
    )
    return {"input.arrow": content_h["input.arrow"], "output.arrow": content_h["output.arrow"]}


# ---------------------------------------------------------------------------
# REL-004: IC Decay Curve (EMA-10 signal)
# ---------------------------------------------------------------------------

_REL004_N_DAYS = 5
_REL004_BARS_PER_DAY = 20
_REL004_MAX_HORIZON = 5
_REL004_EMA_PERIOD = 10


def generate_rel004(version_dir: Path, justification: str = "") -> dict:
    n_bars = _REL004_N_DAYS * _REL004_BARS_PER_DAY
    ts = _timestamps_for_days(_REL004_N_DAYS, _REL004_BARS_PER_DAY)
    closes = _gbm_close(n_bars, seed=7, s0=100.0, sigma=0.005)
    ema = _ema_series(closes, _REL004_EMA_PERIOD)

    oracle_curve = _oracle_ic_decay(ts, closes, ema, _REL004_MAX_HORIZON)

    inp = pa.table(
        {
            "timestamp": pa.array(ts, type=pa.int64()),
            "close": pa.array(closes.tolist(), type=pa.float64()),
            "ema10": pa.array(ema.tolist(), type=pa.float64()),
            "ema_period": pa.array([_REL004_EMA_PERIOD] * n_bars, type=pa.int64()),
            "max_horizon": pa.array([_REL004_MAX_HORIZON] * n_bars, type=pa.int64()),
        }
    )
    out_fields: dict = {}
    for h, ic in oracle_curve:
        out_fields[f"oracle_horizon_{h}_ic"] = pa.array([ic], type=pa.float64())
    out = pa.table(out_fields)

    write_arrow(inp, version_dir / "input.arrow")
    write_arrow(out, version_dir / "output.arrow")
    content_h, file_h = compute_hashes(version_dir, ["input.arrow", "output.arrow"])

    curve_lines = "\n".join(f"  horizon={h}: IC={ic:.9f}" for h, ic in oracle_curve)
    (version_dir / "attribution.md").write_text(
        f"""# REL-004 — IC Decay Curve (EMA-{_REL004_EMA_PERIOD} Signal)

Generated: {GENERATION_DATE}
Oracle: literature_formula — direct Spearman rank-correlation per horizon,
  masking forward returns that cross a session boundary (mask_overnight=True)
Canonical: PythonDataService/app/research/indicator_reliability.py::compute_ic_decay_curve

## Formula

For each horizon h in 1..{_REL004_MAX_HORIZON}:
  fwd_return[i] = log(close[i+h] / close[i])  if same calendar day else NaN
  IC[h] = mean over days d of Spearman(ema[day_d], fwd_return[h][day_d])

EMA formula: k = 2/(1+{_REL004_EMA_PERIOD}); s_t = k×close_t + (1-k)×s_{{t-1}}, s_0 = close_0.
Note: standard exponential smoothing (k=2/(1+period)), NOT Wilder smoothing (k=1/period).
RSI uses Wilder; EMA uses standard. See app/engine/indicators/ema.py:11.

## Input

{_REL004_N_DAYS} trading days × {_REL004_BARS_PER_DAY} bars each = {n_bars} bars.
Close: GBM(S₀=100, σ=0.005, seed=7). Timestamps: 2024-01-02..2024-01-08, 15-min cadence.

## Oracle computed values (mean IC per horizon)

{curve_lines}

## Tolerance

atol=1e-9, rtol=0.0

## SHA-256

input.arrow:  {content_h['input.arrow']}
output.arrow: {content_h['output.arrow']}
""",
        encoding="utf-8",
    )
    return {"input.arrow": content_h["input.arrow"], "output.arrow": content_h["output.arrow"]}
