"""
Implied Volatility Solver
=========================

Formula: Find σ such that BS(S, K, T, r, q, σ) = market_premium. Three-stage cascade: (1) pure-Python Newton-Raphson with vega step (primary — quadratic convergence, warm-start friendly); (2) QuantLib's ``VanillaOption.impliedVolatility()`` for T ≥ 1 calendar day when NR fails (QuantLib day-count arithmetic cannot resolve sub-day TTM); (3) scipy.optimize.brentq fallback for sub-day TTM or QuantLib non-convergence. Brenner-Subrahmanyam closed-form approximation seeds the initial guess (σ₀ ≈ √(2π/T) · price/S for ATM).
Reference: Hull §19.11 (implied volatility); Brent (1973) "Algorithms for Minimization Without Derivatives" §4 for Brent's method; Brenner-Subrahmanyam (1988) Financial Analysts Journal for the seed approximation.
Canonical implementation: this file (the canonical IV solver per docs/math-sources-of-truth.md § Options pricing and Greeks). Companion: `app/services/quantlib_pricer.py::implied_volatility` (QuantLib bisection — second path for callers wanting the QuantLib pricer in the loop). Cross-engine parity is pending-fixture.
Validated against: NONE — pending cross-engine parity fixture between this file and the quantlib_pricer.py companion. Behavior tests exist; equivalence proof does not.

Solver order: Newton-Raphson (custom Python) → QuantLib ``impliedVolatility``
(T ≥ 1 day only) → scipy Brent. QuantLib's date arithmetic cannot resolve
sub-day TTM (a 0.75-day option rounds to 1 full day), so sub-day calls skip
stage 2 and go directly to Brent.

Design goals
------------
- Deterministic: same inputs → same IV every time.
- Robust: handles deep OTM / ITM, near-expiry, and sparse-data edge cases.
- Transparent: every solve attempt returns a typed result with diagnostics.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import StrEnum

import QuantLib as ql
from scipy.optimize import brentq
from scipy.stats import norm

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

MIN_IV: float = 0.005  # 0.5 %  floor
MAX_IV: float = 5.0  # 500 %  ceiling
DEFAULT_IV_GUESS: float = 0.25
QUANTLIB_MAX_ITER: int = 200
QUANTLIB_TOLERANCE: float = 1e-8
# 1 minute, in years. The data-lab companion solves IV per minute on 0DTE
# contracts where ttm is a fraction of a day; the previous "1 calendar day"
# floor silently returned EXPIRED for every 0DTE bar (see
# docs/references/reconciliations/data-lab-spy-2026-04-17-to-2026-04-24.md
# § Finding 3.1). The Brent fallback handles continuous TTM correctly.
MIN_TIME_TO_EXPIRY: float = 1.0 / (365.0 * 24.0 * 60.0)
MIN_OPTION_PRICE: float = 0.001  # reject near-zero premiums


class SolveStatus(StrEnum):
    OK = "ok"
    QUANTLIB_OK = "quantlib_ok"
    BRENT_FALLBACK = "brent_fallback"
    NEWTON_OK = "newton_ok"
    INTRINSIC_VIOLATION = "intrinsic_violation"
    PRICE_TOO_LOW = "price_too_low"
    EXPIRED = "expired"
    CONVERGENCE_FAILURE = "convergence_failure"
    INPUT_ERROR = "input_error"


@dataclass(frozen=True)
class ImpliedVolResult:
    """Result of a single IV solve attempt."""

    iv: float | None
    status: SolveStatus
    iterations: int = 0
    message: str = ""
    option_price: float = 0.0
    intrinsic: float = 0.0


# ── Helpers ──────────────────────────────────────────────────────────────────


def _intrinsic_value(
    spot: float,
    strike: float,
    is_call: bool,
    rate: float,
    ttm: float,
    dividend: float = 0.0,
) -> float:
    """Discounted intrinsic value for a European option.

    Uses continuous-dividend adjusted forward: S·exp(-q·T) - K·exp(-r·T).
    Without the dividend term the lower bound is overestimated for dividend-paying
    underlyings, causing false INTRINSIC_VIOLATION on deep ITM options.
    """
    spot_df = math.exp(-dividend * ttm)
    rate_df = math.exp(-rate * ttm)
    if is_call:
        return max(spot * spot_df - strike * rate_df, 0.0)
    return max(strike * rate_df - spot * spot_df, 0.0)


def _build_ql_process(
    spot: float,
    rate: float,
    dividend: float,
    vol_guess: float,
    eval_date: ql.Date,
) -> ql.BlackScholesMertonProcess:
    """Construct a BSM process for QuantLib pricing / IV solving."""
    day_count = ql.Actual365Fixed()
    calendar = ql.NullCalendar()

    spot_handle = ql.QuoteHandle(ql.SimpleQuote(spot))
    rate_ts = ql.YieldTermStructureHandle(ql.FlatForward(eval_date, ql.QuoteHandle(ql.SimpleQuote(rate)), day_count))
    div_ts = ql.YieldTermStructureHandle(ql.FlatForward(eval_date, ql.QuoteHandle(ql.SimpleQuote(dividend)), day_count))
    vol_ts = ql.BlackVolTermStructureHandle(
        ql.BlackConstantVol(eval_date, calendar, ql.QuoteHandle(ql.SimpleQuote(vol_guess)), day_count)
    )
    return ql.BlackScholesMertonProcess(spot_handle, div_ts, rate_ts, vol_ts)


# ── Primary solver ───────────────────────────────────────────────────────────


def implied_volatility(
    option_price: float,
    spot: float,
    strike: float,
    ttm: float,
    rate: float = 0.05,
    dividend: float = 0.0,
    is_call: bool = True,
    vol_guess: float = DEFAULT_IV_GUESS,
    min_ttm: float | None = None,
) -> ImpliedVolResult:
    """
    Solve for implied volatility using QuantLib, with Brent fallback.

    Parameters
    ----------
    option_price : float
        Observed market price of the option.
    spot : float
        Current price of the underlying.
    strike : float
        Option strike price.
    ttm : float
        Time to maturity in years (must be > 0).
    rate : float
        Continuously-compounded risk-free rate.
    dividend : float
        Continuously-compounded dividend yield.
    is_call : bool
        True for call, False for put.
    vol_guess : float
        Initial volatility guess for the solver.
    min_ttm : float, optional
        Override the module-level ``MIN_TIME_TO_EXPIRY`` floor. Lower this for
        intraday-resolution callers (e.g. the 0DTE options companion, where
        TTM is measured in hours). Default is the module floor (1 calendar day).

    Returns
    -------
    ImpliedVolResult
        Dataclass with ``iv``, ``status``, and diagnostics.
    """
    # ── Input guards ─────────────────────────────────────────────────────
    if spot <= 0 or strike <= 0:
        return ImpliedVolResult(
            iv=None,
            status=SolveStatus.INPUT_ERROR,
            message=f"spot={spot}, strike={strike} must be positive",
        )

    effective_min_ttm = min_ttm if min_ttm is not None else MIN_TIME_TO_EXPIRY
    if ttm < effective_min_ttm:
        return ImpliedVolResult(
            iv=None,
            status=SolveStatus.EXPIRED,
            message=f"ttm={ttm:.6f} below minimum {effective_min_ttm}",
        )

    intrinsic = _intrinsic_value(spot, strike, is_call, rate, ttm, dividend)

    if option_price < MIN_OPTION_PRICE:
        return ImpliedVolResult(
            iv=None,
            status=SolveStatus.PRICE_TOO_LOW,
            message=f"option_price={option_price} < {MIN_OPTION_PRICE}",
            option_price=option_price,
            intrinsic=intrinsic,
        )

    if option_price < intrinsic - 1e-6:
        return ImpliedVolResult(
            iv=None,
            status=SolveStatus.INTRINSIC_VIOLATION,
            message=f"price={option_price:.4f} < intrinsic={intrinsic:.4f}",
            option_price=option_price,
            intrinsic=intrinsic,
        )

    # ── Newton-Raphson with vega step (primary, fast) ────────────────────
    # Quadratic convergence from a reasonable seed; per-bar callers thread
    # the previous bar's solved IV in via ``vol_guess`` so the warm start
    # cuts iterations to 3–5 and the data-lab options companion stays well
    # under per-bar Brent's ~2 ms cost on a wide [0.005, 5.0] bracket.
    newton_iv = _newton_iv_solve(
        option_price=option_price,
        spot=spot,
        strike=strike,
        ttm=ttm,
        rate=rate,
        dividend=dividend,
        is_call=is_call,
        initial_guess=vol_guess,
    )
    if newton_iv is not None:
        return ImpliedVolResult(
            iv=newton_iv,
            status=SolveStatus.NEWTON_OK,
            option_price=option_price,
            intrinsic=intrinsic,
        )

    # ── QuantLib solve ───────────────────────────────────────────────────
    # QuantLib's serial-day arithmetic only has day resolution, so any
    # sub-day TTM gets rounded — a 0.75-day option would be priced as a
    # full 1-day expiry. Skip QL entirely for any TTM under one calendar
    # day and go straight to the closed-form Brent fallback, which works
    # in continuous time.
    if ttm < 1.0 / 365.0:
        return _brent_fallback(
            option_price=option_price,
            spot=spot,
            strike=strike,
            ttm=ttm,
            rate=rate,
            dividend=dividend,
            is_call=is_call,
            intrinsic=intrinsic,
        )

    try:
        eval_date = ql.Date(15, 1, 2020)  # arbitrary anchor for determinism
        ql.Settings.instance().evaluationDate = eval_date

        option_type = ql.Option.Call if is_call else ql.Option.Put
        payoff = ql.PlainVanillaPayoff(option_type, strike)

        # Compute expiry date from TTM
        expiry_serial = eval_date.serialNumber() + round(ttm * 365)
        expiry_date = ql.Date(expiry_serial)
        exercise = ql.EuropeanExercise(expiry_date)

        option = ql.VanillaOption(payoff, exercise)

        process = _build_ql_process(spot, rate, dividend, vol_guess, eval_date)
        option.setPricingEngine(ql.AnalyticEuropeanEngine(process))

        iv = option.impliedVolatility(
            option_price,
            process,
            QUANTLIB_TOLERANCE,
            QUANTLIB_MAX_ITER,
            MIN_IV,
            MAX_IV,
        )

        if math.isfinite(iv) and MIN_IV <= iv <= MAX_IV:
            return ImpliedVolResult(
                iv=iv,
                status=SolveStatus.QUANTLIB_OK,
                option_price=option_price,
                intrinsic=intrinsic,
            )

    except RuntimeError as exc:
        logger.debug("QuantLib solver failed for S=%.2f K=%.2f: %s", spot, strike, exc)
    except Exception as exc:
        logger.warning("Unexpected QuantLib error: %s", exc)

    # ── Brent fallback ───────────────────────────────────────────────────
    return _brent_fallback(
        option_price=option_price,
        spot=spot,
        strike=strike,
        ttm=ttm,
        rate=rate,
        dividend=dividend,
        is_call=is_call,
        intrinsic=intrinsic,
    )


def _newton_iv_solve(
    option_price: float,
    spot: float,
    strike: float,
    ttm: float,
    rate: float,
    dividend: float,
    is_call: bool,
    initial_guess: float = DEFAULT_IV_GUESS,
    max_iter: int = 30,
    tol: float = 1e-7,
) -> float | None:
    """Newton-Raphson IV solver using vega as the local slope.

    Returns the solved IV on convergence, or ``None`` if the iteration
    walks out of ``[MIN_IV, MAX_IV]`` or the local vega collapses (deep
    OTM with sub-day TTM, where price is insensitive to vol). The caller
    falls back to QuantLib or Brent on ``None``.
    """
    sigma = max(MIN_IV, min(MAX_IV, initial_guess if initial_guess > 0 else DEFAULT_IV_GUESS))
    sqrt_t = math.sqrt(ttm)
    disc_q = math.exp(-dividend * ttm)
    disc_r = math.exp(-rate * ttm)
    log_sk = math.log(spot / strike)
    drift_t = (rate - dividend) * ttm

    for _ in range(max_iter):
        sigma_sqrt_t = sigma * sqrt_t
        d1 = (log_sk + drift_t + 0.5 * sigma * sigma * ttm) / sigma_sqrt_t
        d2 = d1 - sigma_sqrt_t
        if is_call:
            price = spot * disc_q * float(norm.cdf(d1)) - strike * disc_r * float(norm.cdf(d2))
        else:
            price = strike * disc_r * float(norm.cdf(-d2)) - spot * disc_q * float(norm.cdf(-d1))
        diff = price - option_price
        if abs(diff) < tol:
            if MIN_IV <= sigma <= MAX_IV and math.isfinite(sigma):
                return sigma
            return None
        vega = spot * disc_q * float(norm.pdf(d1)) * sqrt_t
        if vega < 1e-10:
            return None
        sigma -= diff / vega
        if not math.isfinite(sigma) or sigma <= MIN_IV * 0.5 or sigma >= MAX_IV * 2.0:
            return None
        sigma = max(MIN_IV, min(MAX_IV, sigma))
    return None


def _brent_fallback(
    option_price: float,
    spot: float,
    strike: float,
    ttm: float,
    rate: float,
    dividend: float,
    is_call: bool,
    intrinsic: float,
) -> ImpliedVolResult:
    """Scipy Brent root-finding fallback when QuantLib fails."""

    def _bs_price_for_vol(sigma: float) -> float:
        """Black-Scholes price as a function of vol, for root finding."""
        sqrt_t = math.sqrt(ttm)
        d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * sigma * sigma) * ttm) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t

        disc_q = math.exp(-dividend * ttm)
        disc_r = math.exp(-rate * ttm)

        if is_call:
            return spot * disc_q * float(norm.cdf(d1)) - strike * disc_r * float(norm.cdf(d2))
        return strike * disc_r * float(norm.cdf(-d2)) - spot * disc_q * float(norm.cdf(-d1))

    def objective(sigma: float) -> float:
        return _bs_price_for_vol(sigma) - option_price

    try:
        iv, result = brentq(
            objective,
            MIN_IV,
            MAX_IV,
            xtol=1e-10,
            rtol=1e-10,
            maxiter=200,
            full_output=True,
        )
        if math.isfinite(iv) and MIN_IV <= iv <= MAX_IV:
            return ImpliedVolResult(
                iv=iv,
                status=SolveStatus.BRENT_FALLBACK,
                iterations=result.iterations,
                option_price=option_price,
                intrinsic=intrinsic,
                message="Brent fallback succeeded",
            )
    except (ValueError, RuntimeError) as exc:
        logger.debug("Brent fallback also failed: %s", exc)

    return ImpliedVolResult(
        iv=None,
        status=SolveStatus.CONVERGENCE_FAILURE,
        message="Both QuantLib and Brent solvers failed to converge",
        option_price=option_price,
        intrinsic=intrinsic,
    )


# ── Batch solver ─────────────────────────────────────────────────────────────


def solve_iv_chain(
    records: list[dict],
    spot: float,
    rate: float = 0.05,
    dividend: float = 0.0,
) -> list[dict]:
    """
    Solve IV for an entire option chain.

    Each record dict must contain: strike, ttm, option_price, is_call.
    Returns a new list of dicts with ``iv`` and ``iv_status`` appended.
    """
    results: list[dict] = []
    for rec in records:
        res = implied_volatility(
            option_price=rec["option_price"],
            spot=spot,
            strike=rec["strike"],
            ttm=rec["ttm"],
            rate=rate,
            dividend=dividend,
            is_call=rec["is_call"],
        )
        out = {**rec, "iv": res.iv, "iv_status": res.status.value}
        results.append(out)
    return results
