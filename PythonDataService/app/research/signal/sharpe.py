"""Sharpe uncertainty and selection adjustment.

Formula: SE(SR)=sqrt(g'Ωg/T); Ω is Bartlett HAC of (r-μ,(r-μ)²-v).
DSR=Φ((SR-SR0)√(T-1)/√(1-skew·SR+(kurtosis-1)SR²/4)).
Reference: Lo (2002), eq. 14–15 and Appendix; Bailey & López de Prado
(2014), eq. 1–2 and Appendix 3, eq. 9; PDFs in references/papers/.
Canonical implementation: app/research/signal/sharpe.py
Validated against: tests/fixtures/test_sharpe_fixtures.py (RP-007, RP-008);
tests/fixtures/test_research_fixtures.py::TestRP005LoSharpeCI (IID only).

Lo uses population variance. We multiply his point and SE by √((T-1)/T)
to match the platform's sample-standard-deviation Sharpe. Annualization
scales this per-bar statistic, not the Sharpe of annual aggregated returns.
HAC lag products stay within supplied contiguous segments; cross-segment
covariance is assumed zero. Bandwidth and finite-sample conventions are
platform choices recorded in ADR 0073. Asymptotic inference requires stable
moments and weak dependence; it does not establish strategy profitability.
DSR uses raw T as in the paper, not an autocorrelation-adjusted sample size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
from scipy import stats

from app.research.validation.ic import _select_hac_lag

_BARS_PER_YEAR = 252 * 390


@dataclass
class SharpeCi:
    point: float = 0.0
    se: float = 0.0
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    confidence_level: float = 0.95
    n_eff_used: float = 0.0  # Legacy stored-report field; unused by HAC.
    valid: bool = False
    method: str = "lo_hac"
    sample_size: int = 0
    hac_lag: int = 0
    segment_count: int = 0
    unavailable_reason: str = ""


@dataclass
class DeflatedSharpe:
    raw_sharpe: float = 0.0
    expected_max_under_null: float = 0.0
    dsr_probability: float = 0.0
    n_trials: int = 0
    skewness: float = 0.0
    kurtosis: float = 0.0
    valid: bool = False
    method: str = "dsr_grid"
    effective_trials: float = 0.0
    trial_sharpe_std: float = 0.0  # Per-bar units, sample standard deviation.
    mean_trial_correlation: float = 0.0
    sample_size: int = 0
    unavailable_reason: str = ""


def _finish_ci(result: SharpeCi, point: float, se: float) -> SharpeCi:
    z = float(stats.norm.ppf((1 + result.confidence_level) / 2))
    lower, upper = point - z * se, point + z * se
    if not all(math.isfinite(value) for value in (point, se, lower, upper)):
        result.unavailable_reason = "The estimated Sharpe interval is not finite."
        return result
    result.point, result.se = point, se
    result.ci_lower, result.ci_upper = lower, upper
    result.valid = True
    return result


def compute_iid_sharpe_ci(
    bar_returns: np.ndarray,
    bars_per_year: int = _BARS_PER_YEAR,
    confidence_level: float = 0.95,
) -> SharpeCi:
    """Lo eq. 9–10 normal-IID formula, retained for the Table 1 fixture."""
    arr = np.asarray(bar_returns, dtype=float)
    result = SharpeCi(method="iid_normal", sample_size=arr.size, confidence_level=confidence_level)
    if arr.ndim != 1 or arr.size < 3 or not np.isfinite(arr).all():
        result.unavailable_reason = "At least three finite returns are required."
        return result
    if bars_per_year <= 0 or not 0 < confidence_level < 1:
        raise ValueError("Invalid annualization or confidence level")
    std = float(arr.std(ddof=1))
    if np.all(arr == arr[0]) or std <= 0:
        result.unavailable_reason = "Sharpe is undefined for constant returns."
        return result
    sr = float(arr.mean()) / std
    return _finish_ci(result, sr * math.sqrt(bars_per_year), math.sqrt((1 + sr * sr / 2) / arr.size * bars_per_year))


def compute_sharpe_ci(
    bar_returns: np.ndarray,
    bars_per_year: int = _BARS_PER_YEAR,
    confidence_level: float = 0.95,
    *,
    hac_lag: int | None = None,
    min_hac_lag: int = 0,
    segment_lengths: list[int] | None = None,
) -> SharpeCi:
    """Lo eq. 14–15 with joint mean/variance HAC and sample-SD scaling.

    Segment lengths must partition the aligned observations without gaps.
    Missing returns must be segmented upstream, never silently deleted here.
    """
    arr = np.asarray(bar_returns, dtype=float)
    n = arr.size
    result = SharpeCi(sample_size=n, confidence_level=confidence_level)
    if bars_per_year <= 0 or not 0 < confidence_level < 1 or min_hac_lag < 0:
        raise ValueError("Invalid annualization, confidence level, or minimum lag")
    if arr.ndim != 1 or n < 3 or not np.isfinite(arr).all():
        result.unavailable_reason = "At least three finite, aligned returns are required."
        return result
    lengths = [n] if segment_lengths is None else segment_lengths
    if not lengths or any(k < 1 or int(k) != k for k in lengths) or sum(lengths) != n:
        raise ValueError("Segment lengths must partition all returns")
    lag = max(_select_hac_lag(n), min_hac_lag) if hac_lag is None else hac_lag
    if not isinstance(lag, int) or lag < 0 or lag >= n:
        result.unavailable_reason = "The sample is too short for the requested HAC lag."
        return result
    result.hac_lag, result.segment_count = lag, len(lengths)
    if max(lengths) <= lag:
        result.unavailable_reason = "No contiguous segment supports the requested HAC lag."
        return result
    mu = float(arr.mean())
    centered = arr - mu
    v = float(np.mean(centered**2))
    if np.all(arr == arr[0]) or v <= 0:
        result.unavailable_reason = "Sharpe is undefined for constant returns."
        return result
    moments = np.column_stack((centered, centered**2 - v))
    omega = moments.T @ moments / n
    boundaries = np.cumsum([0, *lengths])
    for k in range(1, lag + 1):
        gamma = np.zeros((2, 2))
        for start, end in pairwise(boundaries):
            if end - start > k:
                gamma += moments[start + k : end].T @ moments[start : end - k] / n
        omega += (1 - k / (lag + 1)) * (gamma + gamma.T)
    gradient = np.array([1 / math.sqrt(v), -mu / (2 * v**1.5)])
    variance = float(gradient @ omega @ gradient / n)
    if not math.isfinite(variance) or variance <= 0:
        result.unavailable_reason = "The estimated Sharpe variance is not positive."
        return result
    scale = math.sqrt((n - 1) / n * bars_per_year)
    return _finish_ci(result, mu / math.sqrt(v) * scale, math.sqrt(variance) * scale)


def deflated_sharpe_from_moments(
    sharpe: float,
    sample_size: int,
    skewness: float,
    kurtosis: float,
    trial_sharpe_std: float,
    effective_trials: float,
) -> tuple[float, float]:
    """Paper eq. 1–2: return (null maximum, DSR), all Sharpes per period.

    Fractional effective counts below two are outside the useful domain
    of the extreme-value approximation. Exactly one trial has null max 0.
    """
    if (
        not all(math.isfinite(x) for x in (sharpe, skewness, kurtosis, trial_sharpe_std, effective_trials))
        or sample_size < 3
        or trial_sharpe_std < 0
        or effective_trials < 1
        or 1 < effective_trials < 2
    ):
        raise ValueError("Invalid DSR inputs or effective trial count between one and two")
    sr0 = 0.0
    if effective_trials >= 2:
        euler_gamma = 0.5772156649015329
        sr0 = trial_sharpe_std * (
            (1 - euler_gamma) * stats.norm.ppf(1 - 1 / effective_trials)
            + euler_gamma * stats.norm.ppf(1 - 1 / (effective_trials * math.e))
        )
    variance = 1 - skewness * sharpe + (kurtosis - 1) * sharpe**2 / 4
    if variance <= 0 or not math.isfinite(variance):
        raise ValueError("The DSR moment variance must be positive")
    return float(sr0), float(stats.norm.cdf((sharpe - sr0) * math.sqrt((sample_size - 1) / variance)))


def compute_deflated_sharpe(
    trial_returns: np.ndarray,
    selected_index: int,
    bars_per_year: int = _BARS_PER_YEAR,
) -> DeflatedSharpe:
    """DSR for every threshold × cost trial in one training grid.

    Rows are trials, columns identical observation timestamps. No pairwise
    deletion, no dropping constant trials. Appendix 3 eq. 9 estimates N from
    mean pairwise return correlation; negative mean correlation is capped
    at the actual grid count. This universe excludes prior research runs.
    Uses sample Sharpe dispersion and bias-corrected skew/Pearson kurtosis.
    """
    trials = np.asarray(trial_returns, dtype=float)
    if trials.ndim != 2 or not 0 <= selected_index < trials.shape[0] or bars_per_year <= 0:
        raise ValueError("Expected aligned trial rows, a selected row, and positive annualization")
    m, n = trials.shape
    result = DeflatedSharpe(n_trials=m, sample_size=n)
    if n < 4 or not np.isfinite(trials).all():
        result.unavailable_reason = "Every trial needs at least four finite, aligned returns."
        return result
    stds = trials.std(axis=1, ddof=1)
    if np.any(np.all(trials == trials[:, :1], axis=1)) or np.any(stds <= 0):
        result.unavailable_reason = "The grid includes a constant-return trial with undefined Sharpe."
        return result
    sharpes = trials.mean(axis=1) / stds
    raw_sharpe = float(sharpes[selected_index] * math.sqrt(bars_per_year))
    trial_sharpe_std = float(sharpes.std(ddof=1)) if m > 1 else 0.0
    rho = 0.0
    if m > 1:
        correlation = np.corrcoef(trials)
        rho = float(np.clip(correlation[np.triu_indices(m, 1)].mean(), -1, 1))
    selected = trials[selected_index]
    skewness = float(stats.skew(selected, bias=False))
    kurtosis = float(stats.kurtosis(selected, fisher=False, bias=False))
    if not all(math.isfinite(value) for value in (raw_sharpe, trial_sharpe_std, rho, skewness, kurtosis)):
        result.unavailable_reason = "The grid's Sharpe moments or correlations cannot be estimated reliably."
        return result
    result.raw_sharpe, result.trial_sharpe_std = raw_sharpe, trial_sharpe_std
    result.mean_trial_correlation = rho
    result.skewness, result.kurtosis = skewness, kurtosis
    result.effective_trials = min(float(m), max(1.0, rho + (1 - rho) * m))
    # Floating-point roundoff at perfect correlation must still yield N=1.
    if abs(result.effective_trials - 1) < 1e-12:
        result.effective_trials = 1.0
    try:
        sr0, probability = deflated_sharpe_from_moments(
            float(sharpes[selected_index]),
            n,
            result.skewness,
            result.kurtosis,
            result.trial_sharpe_std,
            result.effective_trials,
        )
    except ValueError as exc:
        result.unavailable_reason = str(exc)
        return result
    result.expected_max_under_null = sr0 * math.sqrt(bars_per_year)
    result.dsr_probability, result.valid = probability, True
    return result
