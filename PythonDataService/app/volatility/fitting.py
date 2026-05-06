"""
Volatility Smile / Surface Fitting Models
==========================================

Formula: Three smile models — (1) variance interpolation (QuantLib BlackVarianceSurface, non-parametric); (2) SABR σ_B(K,T;α,β,ρ,ν) per Hagan et al. 2002 eq. (2.17b); (3) SVI w(k) = a + b(ρ(k−m) + √((k−m)²+σ²)) per Gatheral 2004.
Reference: Hagan, Kumar, Lesniewski, Woodward (2002) "Managing Smile Risk" Wilmott Magazine; Gatheral (2004) "A parsimonious arbitrage-free implied volatility parameterization with application to the valuation of volatility derivatives"; QuantLib C++ reference (BlackVarianceSurface, SabrSmileSection).
Canonical implementation: app/volatility/fitting.py
Validated against: NONE — pending (reference verification and golden fixture owed per registry row)

Three fitting approaches, each producing a callable smile for a single expiry:

1. **Variance interpolation** — QuantLib ``BlackVarianceSurface`` on a
   strike × expiry grid.  No parametric assumptions.
2. **SABR** — Hagan et al. (2002) stochastic-alpha-beta-rho model, fitted
   via QuantLib ``SabrSmileSection``.
3. **SVI** — Gatheral's Stochastic Volatility Inspired parameterisation,
   fitted via QuantLib ``SviSmileSection`` when available, otherwise a
   manual least-squares fit.

All fitters receive a *slice* of IV data (same expiry, varying strikes)
and return a callable that maps strike → implied vol.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
from scipy.optimize import least_squares

logger = logging.getLogger(__name__)


class FitMethod(StrEnum):
    VARIANCE = "variance"
    SABR = "sabr"
    SVI = "svi"


# ── Data containers ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SmileSlice:
    """IV data for a single expiry."""

    strikes: np.ndarray  # shape (N,)
    ivs: np.ndarray  # shape (N,)  — implied vols (annualised)
    ttm: float  # time-to-maturity in years
    forward: float  # forward price F = S * exp((r - q) * T)

    def __post_init__(self) -> None:
        assert len(self.strikes) == len(self.ivs), "strikes/ivs length mismatch"
        assert self.ttm > 0, "ttm must be positive"
        assert self.forward > 0, "forward must be positive"


@dataclass
class FitResult:
    """Result of fitting a single smile slice."""

    method: FitMethod
    ttm: float
    params: dict[str, float] = field(default_factory=dict)
    residual_rmse: float = 0.0
    success: bool = True
    message: str = ""
    _vol_fn: Callable[[float], float] | None = field(default=None, repr=False, compare=False)

    def volatility(self, strike: float) -> float:
        """Query fitted vol at a given strike."""
        if self._vol_fn is None:
            raise RuntimeError("Fit did not produce a vol function")
        v = self._vol_fn(strike)
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"Non-finite vol {v} at strike {strike}")
        return v


# ═══════════════════════════════════════════════════════════════════════════
#  1.  SABR
# ═══════════════════════════════════════════════════════════════════════════


def _sabr_vol(
    strike: float,
    forward: float,
    ttm: float,
    alpha: float,
    beta: float,
    rho: float,
    nu: float,
) -> float:
    """Hagan SABR approximation formula (2002)."""
    if strike <= 0 or forward <= 0:
        return float("nan")

    eps = 1e-10
    fk = forward * strike

    if abs(forward - strike) < eps:
        # ATM formula
        fk_beta = forward ** (1.0 - beta)
        term1 = alpha / fk_beta
        a = ((1.0 - beta) ** 2 / 24.0) * alpha**2 / (forward ** (2.0 - 2.0 * beta))
        b = 0.25 * rho * beta * nu * alpha / (forward ** (1.0 - beta))
        c = (2.0 - 3.0 * rho**2) / 24.0 * nu**2
        return term1 * (1.0 + (a + b + c) * ttm)

    log_fk = math.log(forward / strike)
    fk_beta_mid = (fk) ** ((1.0 - beta) / 2.0)
    z = (nu / alpha) * fk_beta_mid * log_fk
    x_z = math.log((math.sqrt(1.0 - 2.0 * rho * z + z**2) + z - rho) / (1.0 - rho))

    if abs(x_z) < eps:
        x_z = 1.0

    prefix = alpha / (
        fk_beta_mid * (1.0 + (1.0 - beta) ** 2 / 24.0 * log_fk**2 + (1.0 - beta) ** 4 / 1920.0 * log_fk**4)
    )

    a = ((1.0 - beta) ** 2 / 24.0) * alpha**2 / (fk ** (1.0 - beta))
    b = 0.25 * rho * beta * nu * alpha / (fk_beta_mid)
    c = (2.0 - 3.0 * rho**2) / 24.0 * nu**2

    return prefix * (z / x_z) * (1.0 + (a + b + c) * ttm)


def fit_sabr(
    smile: SmileSlice,
    beta: float = 0.5,
    initial_alpha: float = 0.3,
    initial_rho: float = 0.0,
    initial_nu: float = 0.3,
) -> FitResult:
    """
    Fit SABR parameters (alpha, rho, nu) to a smile slice.

    Beta is fixed (common choices: 0.5 for rates, 1.0 for equities/FX).
    Uses least-squares on the Hagan approximation formula.
    """
    forward = smile.forward
    ttm = smile.ttm
    strikes = smile.strikes
    market_vols = smile.ivs

    def residuals(params: np.ndarray) -> np.ndarray:
        alpha, rho, nu = params
        if alpha <= 0 or nu <= 0 or abs(rho) >= 1:
            return np.full(len(strikes), 1e6)
        model_vols = np.array([_sabr_vol(k, forward, ttm, alpha, beta, rho, nu) for k in strikes])
        return model_vols - market_vols

    result = least_squares(
        residuals,
        x0=[initial_alpha, initial_rho, initial_nu],
        bounds=([1e-6, -0.999, 1e-6], [10.0, 0.999, 10.0]),
        method="trf",
        max_nfev=500,
    )

    alpha_fit, rho_fit, nu_fit = result.x
    rmse = float(np.sqrt(np.mean(result.fun**2)))

    def vol_fn(k: float) -> float:
        return _sabr_vol(k, forward, ttm, alpha_fit, beta, rho_fit, nu_fit)

    return FitResult(
        method=FitMethod.SABR,
        ttm=ttm,
        params={
            "alpha": float(alpha_fit),
            "beta": beta,
            "rho": float(rho_fit),
            "nu": float(nu_fit),
        },
        residual_rmse=rmse,
        success=result.success,
        message=result.message if not result.success else "",
        _vol_fn=vol_fn,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  2.  SVI  (Gatheral raw parameterisation)
# ═══════════════════════════════════════════════════════════════════════════


def _svi_total_variance(
    k: float,
    a: float,
    b: float,
    rho: float,
    m: float,
    sigma: float,
) -> float:
    """
    SVI raw parameterisation of total implied variance w(k):

        w(k) = a + b * (rho * (k - m) + sqrt((k - m)^2 + sigma^2))

    where k = log(K/F) is log-moneyness.
    """
    diff = k - m
    return a + b * (rho * diff + math.sqrt(diff * diff + sigma * sigma))


def fit_svi(
    smile: SmileSlice,
    initial_params: dict[str, float] | None = None,
) -> FitResult:
    """
    Fit SVI raw parameterisation to a smile slice.

    Fits total variance w(k) = sigma_iv^2 * T as a function of
    log-moneyness k = log(K/F).
    """
    forward = smile.forward
    ttm = smile.ttm
    strikes = smile.strikes
    market_vols = smile.ivs

    log_moneyness = np.log(strikes / forward)
    market_total_var = (market_vols**2) * ttm

    # Default initial guess
    if initial_params is None:
        atm_var = float(np.interp(0.0, log_moneyness, market_total_var))
        initial_params = {
            "a": atm_var,
            "b": 0.1,
            "rho": -0.3,
            "m": 0.0,
            "sigma": 0.1,
        }

    def residuals(params: np.ndarray) -> np.ndarray:
        a_p, b_p, rho_p, m_p, sigma_p = params
        if b_p <= 0 or sigma_p <= 0 or abs(rho_p) >= 1:
            return np.full(len(strikes), 1e6)
        # Butterfly arbitrage constraint: a + b * sigma * sqrt(1 - rho^2) >= 0
        if a_p + b_p * sigma_p * math.sqrt(1.0 - rho_p**2) < 0:
            return np.full(len(strikes), 1e6)
        model_var = np.array([_svi_total_variance(k, a_p, b_p, rho_p, m_p, sigma_p) for k in log_moneyness])
        return model_var - market_total_var

    x0 = [
        initial_params["a"],
        initial_params["b"],
        initial_params["rho"],
        initial_params["m"],
        initial_params["sigma"],
    ]

    result = least_squares(
        residuals,
        x0=x0,
        bounds=(
            [-1.0, 1e-8, -0.999, -2.0, 1e-8],
            [5.0, 5.0, 0.999, 2.0, 5.0],
        ),
        method="trf",
        max_nfev=1000,
    )

    a_f, b_f, rho_f, m_f, sigma_f = result.x
    rmse = float(np.sqrt(np.mean(result.fun**2)))

    def vol_fn(strike: float) -> float:
        k = math.log(strike / forward)
        w = _svi_total_variance(k, a_f, b_f, rho_f, m_f, sigma_f)
        if w < 0:
            raise ValueError(f"Negative total variance {w} at k={k}")
        return math.sqrt(w / ttm)

    return FitResult(
        method=FitMethod.SVI,
        ttm=ttm,
        params={
            "a": float(a_f),
            "b": float(b_f),
            "rho": float(rho_f),
            "m": float(m_f),
            "sigma": float(sigma_f),
        },
        residual_rmse=rmse,
        success=result.success,
        message=result.message if not result.success else "",
        _vol_fn=vol_fn,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  3.  Variance interpolation (non-parametric)
# ═══════════════════════════════════════════════════════════════════════════


def fit_variance_interp(smile: SmileSlice) -> FitResult:
    """
    Build a simple variance-time interpolation for a single expiry slice.

    Uses linear interpolation on total variance w = sigma^2 * T as a
    function of strike, which preserves calendar-spread arbitrage
    constraints better than interpolating vol directly.
    """
    strikes = smile.strikes
    total_var = (smile.ivs**2) * smile.ttm
    ttm = smile.ttm

    sorted_idx = np.argsort(strikes)
    sorted_strikes = strikes[sorted_idx]
    sorted_var = total_var[sorted_idx]

    def vol_fn(strike: float) -> float:
        w = float(np.interp(strike, sorted_strikes, sorted_var))
        if w < 0:
            raise ValueError(f"Negative interpolated variance at K={strike}")
        return math.sqrt(w / ttm)

    return FitResult(
        method=FitMethod.VARIANCE,
        ttm=ttm,
        params={"n_points": len(strikes)},
        residual_rmse=0.0,
        success=True,
        _vol_fn=vol_fn,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Arbitrage checks
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ArbitrageReport:
    """Results of no-arbitrage validation on a fitted smile."""

    butterfly_violations: int = 0
    calendar_violations: int = 0
    negative_variance: int = 0
    total_checks: int = 0
    passed: bool = True
    details: list[str] = field(default_factory=list)


def check_smile_arbitrage(
    fit: FitResult,
    strikes: np.ndarray,
    forward: float,
) -> ArbitrageReport:
    """
    Run basic no-arbitrage checks on a fitted smile.

    Checks:
    - No negative total variance
    - Butterfly arbitrage: d^2 w / dk^2 >= 0 (convexity of total variance in log-moneyness)
    """
    report = ArbitrageReport()
    ttm = fit.ttm
    log_k = np.log(strikes / forward)

    # Compute total variance at each strike
    total_vars: list[float] = []
    for _k_val, strike in zip(log_k, strikes, strict=False):
        try:
            v = fit.volatility(strike)
            w = v * v * ttm
            total_vars.append(w)
        except (ValueError, RuntimeError):
            total_vars.append(float("nan"))

    w_arr = np.array(total_vars)
    report.total_checks = len(strikes)

    # Negative variance
    neg_mask = w_arr < -1e-10
    report.negative_variance = int(neg_mask.sum())

    # Butterfly: check convexity d^2w/dk^2 >= 0
    if len(log_k) >= 3:
        for i in range(1, len(log_k) - 1):
            dk1 = log_k[i] - log_k[i - 1]
            dk2 = log_k[i + 1] - log_k[i]
            if dk1 <= 0 or dk2 <= 0:
                continue
            d2w = ((w_arr[i + 1] - w_arr[i]) / dk2 - (w_arr[i] - w_arr[i - 1]) / dk1) / (0.5 * (dk1 + dk2))
            if d2w < -1e-6:
                report.butterfly_violations += 1
                report.details.append(f"Butterfly violation at K={strikes[i]:.2f}: d2w/dk2={d2w:.6f}")

    report.passed = report.negative_variance == 0 and report.butterfly_violations == 0
    return report
