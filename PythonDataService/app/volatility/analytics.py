"""
Volatility Surface Analytics
=============================

Formula: Risk reversal = IV(call, Δ=0.25) − IV(put, Δ=0.25); butterfly = (IV(call, Δ=0.25) + IV(put, Δ=0.25))/2 − IV(ATM); ATM strike solved via Brent on the fitted surface.
Reference: Standard volatility market conventions for risk-reversal and butterfly quoting (practitioner standard; see Hull §20 for smile metrics).
Canonical implementation: app/volatility/analytics.py
Validated against: NONE — pending (no golden fixture)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq


@dataclass(frozen=True)
class DeltaStrikeResult:
    """Result of solving for strike at a target delta."""

    strike: float
    log_moneyness: float
    iv: float
    delta: float
    converged: bool


@dataclass
class SkewMetrics:
    """Skew and smile metrics for a single expiry."""

    ttm: float
    dte_days: int
    atm_iv: float
    rr_25d: float | None
    bf_25d: float | None
    skew_slope: float
    call_25d: DeltaStrikeResult | None
    put_25d: DeltaStrikeResult | None


@dataclass
class HealthScore:
    """Composite surface health score."""

    total: int
    convergence_score: float
    rmse_score: float
    rejection_score: float
    arbitrage_score: float


def _bs_delta(
    spot: float,
    strike: float,
    ttm: float,
    rate: float,
    dividend: float,
    iv: float,
    is_call: bool,
) -> float:
    """
    Black-Scholes delta.

    Call delta: e^(-qT) * N(d1)
    Put delta: e^(-qT) * (N(d1) - 1)
    """
    if ttm <= 0 or iv <= 0:
        return float("nan")

    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * iv * iv) * ttm) / (iv * math.sqrt(ttm))

    from scipy.stats import norm

    nd1 = norm.cdf(d1)
    df = math.exp(-dividend * ttm)

    if is_call:
        return df * nd1
    else:
        return df * (nd1 - 1.0)


def find_delta_strike(
    surface,
    ttm: float,
    target_delta: float,
    is_call: bool,
    bracket_width: float = 0.5,
) -> DeltaStrikeResult | None:
    """
    Find strike K where bs_delta(S, K, T, r, σ(K)) = target_delta.

    Uses scipy.optimize.brentq bracketed on log-moneyness [-bracket_width, bracket_width].

    Args:
        surface: VolSurface instance
        ttm: Time to maturity (years)
        target_delta: Target delta value (e.g., 0.25 for 25Δ call)
        is_call: True for call, False for put
        bracket_width: Width of log-moneyness bracket (default 0.5)

    Returns:
        DeltaStrikeResult if solver converges, None otherwise.
    """
    if abs(target_delta) > 1:
        return None

    forward = surface.spot * math.exp((surface.rate - surface.dividend) * ttm)

    def objective(log_m: float) -> float:
        strike = forward * math.exp(log_m)
        try:
            iv = surface.volatility(strike, ttm)
            delta = _bs_delta(
                surface.spot,
                strike,
                ttm,
                surface.rate,
                surface.dividend,
                iv,
                is_call,
            )
            return delta - target_delta
        except (ValueError, RuntimeError):
            return float("nan")

    try:
        log_m_lo = -bracket_width
        log_m_hi = bracket_width

        f_lo = objective(log_m_lo)
        f_hi = objective(log_m_hi)

        if math.isnan(f_lo) or math.isnan(f_hi):
            return None

        if f_lo * f_hi > 0:
            return None

        log_m_result = brentq(objective, log_m_lo, log_m_hi, xtol=1e-6)

        strike = forward * math.exp(log_m_result)
        iv = surface.volatility(strike, ttm)
        delta = _bs_delta(
            surface.spot,
            strike,
            ttm,
            surface.rate,
            surface.dividend,
            iv,
            is_call,
        )

        return DeltaStrikeResult(
            strike=strike,
            log_moneyness=log_m_result,
            iv=iv,
            delta=delta,
            converged=True,
        )

    except (ValueError, RuntimeError):
        return None


def compute_skew_metrics(
    surface,
    ttm: float,
) -> SkewMetrics:
    """
    Compute skew metrics for a single expiry.

    Args:
        surface: VolSurface instance
        ttm: Time to maturity (years)

    Returns:
        SkewMetrics dataclass with ATM IV, 25D RR, 25D BF, and skew slope.
    """
    dte_days = round(ttm * 365)
    forward = surface.spot * math.exp((surface.rate - surface.dividend) * ttm)

    atm_iv = surface.volatility(forward, ttm)

    call_25d = find_delta_strike(surface, ttm, 0.25, is_call=True)
    put_25d = find_delta_strike(surface, ttm, -0.25, is_call=False)

    rr_25d = None
    if call_25d is not None and put_25d is not None:
        rr_25d = call_25d.iv - put_25d.iv

    bf_25d = None
    if call_25d is not None and put_25d is not None:
        bf_25d = 0.5 * (call_25d.iv + put_25d.iv) - atm_iv

    dk = 0.01 * forward
    k_lo = forward - dk
    k_hi = forward + dk
    try:
        vol_lo = surface.volatility(k_lo, ttm)
        vol_hi = surface.volatility(k_hi, ttm)
        skew_slope = (vol_hi - vol_lo) / (2.0 * dk)
    except (ValueError, RuntimeError):
        skew_slope = 0.0

    return SkewMetrics(
        ttm=ttm,
        dte_days=dte_days,
        atm_iv=atm_iv,
        rr_25d=rr_25d,
        bf_25d=bf_25d,
        skew_slope=skew_slope,
        call_25d=call_25d,
        put_25d=put_25d,
    )


def compute_health_score(surface) -> HealthScore:
    """
    Composite surface health score (0-100).

    Scoring:
    - Solver convergence rate (25%): 100 if >95%, linear to 0 at <70%
    - Fit RMSE avg (25%): 100 if <0.005, 0 if >0.05, linear between
    - Rejection rate (25%): 100 if <10%, 0 if >50%, linear between
    - Arbitrage violations (25%): 100 if 0, -10 per butterfly, -15 per calendar

    Returns:
        HealthScore dataclass with component scores and total.
    """
    diag = surface.diagnostics

    if not diag.slices:
        return HealthScore(
            total=0,
            convergence_score=0.0,
            rmse_score=0.0,
            rejection_score=0.0,
            arbitrage_score=0.0,
        )

    convergence_rate = (
        diag.n_total_solved / (diag.n_total_solved + diag.n_total_failed)
        if (diag.n_total_solved + diag.n_total_failed) > 0
        else 0.0
    )
    if convergence_rate > 0.95:
        convergence_score = 100.0
    elif convergence_rate < 0.70:
        convergence_score = 0.0
    else:
        convergence_score = (convergence_rate - 0.70) / (0.95 - 0.70) * 100.0

    rmse_values = [s.fit_rmse for s in diag.slices]
    avg_rmse = np.mean(rmse_values) if rmse_values else 0.05

    if avg_rmse < 0.005:
        rmse_score = 100.0
    elif avg_rmse > 0.05:
        rmse_score = 0.0
    else:
        rmse_score = (0.05 - avg_rmse) / (0.05 - 0.005) * 100.0

    rejection_rate = (
        diag.n_total_failed / (diag.n_total_solved + diag.n_total_failed)
        if (diag.n_total_solved + diag.n_total_failed) > 0
        else 0.0
    )

    if rejection_rate < 0.10:
        rejection_score = 100.0
    elif rejection_rate > 0.50:
        rejection_score = 0.0
    else:
        rejection_score = (0.50 - rejection_rate) / (0.50 - 0.10) * 100.0

    arbitrage_score = 100.0
    total_butterfly_violations = 0
    total_calendar_violations = 0

    for slice_diag in diag.slices:
        if slice_diag.arbitrage is not None:
            total_butterfly_violations += slice_diag.arbitrage.butterfly_violations
            total_calendar_violations += slice_diag.arbitrage.calendar_violations

    arbitrage_score -= 10 * total_butterfly_violations
    arbitrage_score -= 15 * total_calendar_violations
    arbitrage_score = max(0.0, arbitrage_score)

    total_score = round(0.25 * convergence_score + 0.25 * rmse_score + 0.25 * rejection_score + 0.25 * arbitrage_score)
    total_score = max(0, min(100, total_score))

    return HealthScore(
        total=total_score,
        convergence_score=convergence_score,
        rmse_score=rmse_score,
        rejection_score=rejection_score,
        arbitrage_score=arbitrage_score,
    )


def compute_put_call_parity_forward(
    option_records: list[dict],
) -> dict[float, float]:
    """
    Implied forward price from put-call parity: C - P = (F - K) * df.

    Groups records by TTM and matches calls/puts at same strike.
    Returns implied forward per expiry as a data quality check.

    Args:
        option_records: List of dicts with keys: strike, ttm, option_price, is_call

    Returns:
        Dictionary mapping ttm -> implied_forward
    """
    from collections import defaultdict

    by_ttm_strike: dict[tuple[float, float], dict[str, float]] = defaultdict(dict)

    for rec in option_records:
        key = (round(rec["ttm"], 6), rec["strike"])
        if rec["is_call"]:
            by_ttm_strike[key]["call_price"] = rec["option_price"]
        else:
            by_ttm_strike[key]["put_price"] = rec["option_price"]

    forwards_by_ttm: dict[float, list[float]] = defaultdict(list)

    for (ttm, strike), prices in by_ttm_strike.items():
        if "call_price" in prices and "put_price" in prices:
            call_price = prices["call_price"]
            put_price = prices["put_price"]

            cp_diff = call_price - put_price

            rate = 0.05
            df = math.exp(-rate * ttm)

            if abs(df) > 1e-10:
                implied_forward = strike + cp_diff / df
                forwards_by_ttm[ttm].append(implied_forward)

    result: dict[float, float] = {}
    for ttm, forwards in forwards_by_ttm.items():
        if forwards:
            result[ttm] = float(np.mean(forwards))

    return result
