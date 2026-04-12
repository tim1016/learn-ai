"""QuantLib-backed option pricing endpoints for validation against legacy BS."""
from __future__ import annotations

import math
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
import logging

from app.services.quantlib_pricer import (
    PricingEngine,
    price_option,
    price_strategy,
    _QL_AVAILABLE,
)
from app.research.options.bs_solver import bs_price as scipy_bs_price

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class QuantLibPriceRequest(BaseModel):
    """Price a single European option via QuantLib."""
    spot: float = Field(..., gt=0, description="Current underlying price")
    strike: float = Field(..., gt=0, description="Strike price")
    risk_free_rate: float = Field(0.05, description="Annualized risk-free rate")
    volatility: float = Field(..., gt=0, description="Annualized IV (decimal, e.g. 0.20)")
    expiration_date: str = Field(..., description="Expiration date YYYY-MM-DD")
    option_type: str = Field(..., pattern="^(call|put)$", description="call or put")
    evaluation_date: Optional[str] = Field(None, description="Pricing date (default: today)")
    dividend_yield: float = Field(0.0, ge=0, description="Continuous dividend yield")
    engine: str = Field("analytic_bs", description="Pricing engine: analytic_bs, binomial_crr, binomial_jr, binomial_lr, finite_diff, monte_carlo")


class QuantLibGreeksResponse(BaseModel):
    """Single option pricing result."""
    success: bool
    engine: str
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    d1: Optional[float] = None
    d2: Optional[float] = None
    error: Optional[str] = None


class StrategyLegInput(BaseModel):
    strike: float = Field(..., gt=0)
    option_type: str = Field(..., pattern="^(call|put)$")
    position: str = Field(..., pattern="^(long|short)$")
    iv: float = Field(..., gt=0, description="Implied volatility (decimal)")
    premium: float = Field(0.0)
    quantity: int = Field(1, ge=1)
    expiration_date: str = Field(..., description="YYYY-MM-DD")


class QuantLibStrategyRequest(BaseModel):
    """Price a multi-leg strategy via QuantLib."""
    spot: float = Field(..., gt=0)
    legs: List[StrategyLegInput]
    risk_free_rate: float = Field(0.05)
    evaluation_date: Optional[str] = Field(None)
    dividend_yield: float = Field(0.0, ge=0)
    engine: str = Field("analytic_bs")


class StrategyLegResult(BaseModel):
    engine: str
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    d1: Optional[float] = None
    d2: Optional[float] = None


class QuantLibStrategyResponse(BaseModel):
    success: bool
    engine: str
    net_price: float
    net_delta: float
    net_gamma: float
    net_theta: float
    net_vega: float
    net_rho: float
    legs: List[StrategyLegResult]
    error: Optional[str] = None


class QuantLibStatusResponse(BaseModel):
    available: bool
    version: Optional[str] = None
    engines: List[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/status", response_model=QuantLibStatusResponse)
async def quantlib_status():
    """Check whether QuantLib is installed and list available engines."""
    version = None
    if _QL_AVAILABLE:
        import QuantLib as ql
        version = ql.__version__
    return QuantLibStatusResponse(
        available=_QL_AVAILABLE,
        version=version,
        engines=[e.value for e in PricingEngine],
    )


@router.post("/price", response_model=QuantLibGreeksResponse)
async def quantlib_price(request: QuantLibPriceRequest):
    """Price a single European option and return all Greeks."""
    if not _QL_AVAILABLE:
        return QuantLibGreeksResponse(
            success=False, engine=request.engine,
            price=0, delta=0, gamma=0, theta=0, vega=0, rho=0,
            error="QuantLib not installed",
        )
    try:
        engine = PricingEngine(request.engine)
        eval_d = date.fromisoformat(request.evaluation_date) if request.evaluation_date else None
        exp_d = date.fromisoformat(request.expiration_date)

        result = price_option(
            spot=request.spot,
            strike=request.strike,
            risk_free_rate=request.risk_free_rate,
            volatility=request.volatility,
            expiration_date=exp_d,
            option_type=request.option_type,
            evaluation_date=eval_d,
            dividend_yield=request.dividend_yield,
            engine=engine,
        )

        return QuantLibGreeksResponse(
            success=True,
            engine=result.engine,
            price=result.price,
            delta=result.delta,
            gamma=result.gamma,
            theta=result.theta,
            vega=result.vega,
            rho=result.rho,
            d1=result.d1,
            d2=result.d2,
        )

    except Exception as e:
        logger.error(f"[QuantLib] Error pricing option: {e}", exc_info=True)
        return QuantLibGreeksResponse(
            success=False, engine=request.engine,
            price=0, delta=0, gamma=0, theta=0, vega=0, rho=0,
            error=str(e),
        )


@router.post("/strategy", response_model=QuantLibStrategyResponse)
async def quantlib_strategy(request: QuantLibStrategyRequest):
    """Price a multi-leg options strategy and return aggregate Greeks."""
    if not _QL_AVAILABLE:
        return QuantLibStrategyResponse(
            success=False, engine=request.engine,
            net_price=0, net_delta=0, net_gamma=0, net_theta=0, net_vega=0, net_rho=0,
            legs=[], error="QuantLib not installed",
        )
    try:
        engine = PricingEngine(request.engine)
        eval_d = date.fromisoformat(request.evaluation_date) if request.evaluation_date else None

        legs_data = [
            {
                "strike": leg.strike,
                "option_type": leg.option_type,
                "position": leg.position,
                "iv": leg.iv,
                "premium": leg.premium,
                "quantity": leg.quantity,
                "expiration_date": leg.expiration_date,
            }
            for leg in request.legs
        ]

        result = price_strategy(
            spot=request.spot,
            legs=legs_data,
            risk_free_rate=request.risk_free_rate,
            evaluation_date=eval_d,
            dividend_yield=request.dividend_yield,
            engine=engine,
        )

        leg_results = [
            StrategyLegResult(
                engine=lr.engine, price=lr.price,
                delta=lr.delta, gamma=lr.gamma, theta=lr.theta,
                vega=lr.vega, rho=lr.rho, d1=lr.d1, d2=lr.d2,
            )
            for lr in result.legs
        ]

        return QuantLibStrategyResponse(
            success=True,
            engine=result.engine,
            net_price=result.net_price,
            net_delta=result.net_delta,
            net_gamma=result.net_gamma,
            net_theta=result.net_theta,
            net_vega=result.net_vega,
            net_rho=result.net_rho,
            legs=leg_results,
        )

    except Exception as e:
        logger.error(f"[QuantLib] Error pricing strategy: {e}", exc_info=True)
        return QuantLibStrategyResponse(
            success=False, engine=request.engine,
            net_price=0, net_delta=0, net_gamma=0, net_theta=0, net_vega=0, net_rho=0,
            legs=[], error=str(e),
        )


# ---------------------------------------------------------------------------
# Pricing model comparison endpoint
# ---------------------------------------------------------------------------

class PricingCompareRequest(BaseModel):
    """Compare pricing models across a range of underlying prices."""
    spot: float = Field(..., gt=0, description="Current underlying price")
    strike: float = Field(..., gt=0, description="Strike price")
    volatility: float = Field(..., gt=0, description="Annualized IV (decimal)")
    expiration_date: str = Field(..., description="Expiration date YYYY-MM-DD")
    option_type: str = Field(..., pattern="^(call|put)$")
    risk_free_rate: float = Field(0.05)
    dividend_yield: float = Field(0.0, ge=0)
    evaluation_date: Optional[str] = Field(None)
    spot_min: Optional[float] = Field(None, description="Range start (default: spot * 0.80)")
    spot_max: Optional[float] = Field(None, description="Range end (default: spot * 1.20)")
    num_points: int = Field(100, ge=10, le=500, description="Number of data points")


class PricingPointResult(BaseModel):
    """Single data point for one model at one spot price."""
    spot: float
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


class PricingModelCurve(BaseModel):
    """Full curve for one pricing model."""
    model: str
    points: List[PricingPointResult]


class PricingCompareResponse(BaseModel):
    """Comparison results for all pricing models."""
    success: bool
    strike: float
    option_type: str
    expiration_date: str
    time_to_expiry_years: float
    models: List[PricingModelCurve]
    error: Optional[str] = None


def _scipy_bs_greeks(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str,
) -> dict:
    """Compute price + Greeks using the scipy-based BS solver (Python analytical)."""
    from scipy.stats import norm as sp_norm

    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = max(S - K, 0) if option_type == "call" else max(K - S, 0)
        d_val = 1.0 if (option_type == "call" and S > K) else (-1.0 if option_type == "put" and S < K else 0.0)
        return {"price": intrinsic, "delta": d_val, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    discount = math.exp(-r * T)
    sqrt_t = math.sqrt(T)
    npd1 = sp_norm.pdf(d1)

    price = scipy_bs_price(S, K, T, r, sigma, option_type)

    if option_type == "call":
        delta = sp_norm.cdf(d1)
        theta = (-(S * npd1 * sigma) / (2 * sqrt_t) - r * K * discount * sp_norm.cdf(d2)) / 365.0
        rho = (K * T * discount * sp_norm.cdf(d2)) / 100.0
    else:
        delta = sp_norm.cdf(d1) - 1.0
        theta = (-(S * npd1 * sigma) / (2 * sqrt_t) + r * K * discount * sp_norm.cdf(-d2)) / 365.0
        rho = (-K * T * discount * sp_norm.cdf(-d2)) / 100.0

    gamma = npd1 / (S * sigma * sqrt_t)
    vega = (S * npd1 * sqrt_t) / 100.0

    return {"price": price, "delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho}


def _price_option_for_compare(
    spot: float,
    strike: float,
    risk_free_rate: float,
    volatility: float,
    expiration_date: date,
    option_type: str,
    evaluation_date: date,
    dividend_yield: float,
    engine: PricingEngine,
):
    """Thin wrapper around price_option with lighter params for bulk comparison.

    For the single-point /price endpoint we use 801-step trees and 801×800 FD
    grids for maximum accuracy.  In the /compare endpoint we evaluate 100 spot
    points × 7 engines, so we trade a tiny bit of precision for ~10× speed.

    Strategy:
    - Analytic BS: no change (already instant).
    - Binomial (CRR/JR/LR): 201 steps instead of 801 (~4× faster, still well
      converged for European options).
    - Finite Differences: 201×200 grid (~16× fewer cells).
    - Monte Carlo: tolerance=0.02 instead of 0.001 (~400× fewer paths on
      average). Still shows curve shape; noise band is the point.
    """
    import QuantLib as ql
    from app.services.quantlib_pricer import (
        _ensure_ql, _ql_date, _build_process, GreeksResult,
    )

    _ensure_ql()

    eval_d = evaluation_date or date.today()
    ql_eval = _ql_date(eval_d)
    ql.Settings.instance().evaluationDate = ql_eval

    ql_expiry = _ql_date(expiration_date)
    t_years = ql.Actual365Fixed().yearFraction(ql_eval, ql_expiry)

    if t_years <= 0:
        intrinsic = max(spot - strike, 0) if option_type == "call" else max(strike - spot, 0)
        d_val = 1.0 if (option_type == "call" and spot > strike) else (-1.0 if option_type == "put" and spot < strike else 0.0)
        return GreeksResult(engine=engine.value, price=intrinsic, delta=d_val,
                            gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    ql_type = ql.Option.Call if option_type == "call" else ql.Option.Put
    payoff = ql.PlainVanillaPayoff(ql_type, strike)
    exercise = ql.EuropeanExercise(ql_expiry)
    option = ql.VanillaOption(payoff, exercise)

    process = _build_process(spot, risk_free_rate, dividend_yield, volatility, ql_eval)

    # ── Attach engine with lighter params ──
    if engine == PricingEngine.ANALYTIC_BS:
        option.setPricingEngine(ql.AnalyticEuropeanEngine(process))
    elif engine == PricingEngine.BINOMIAL_CRR:
        option.setPricingEngine(ql.BinomialVanillaEngine(process, "CRR", 201))
    elif engine == PricingEngine.BINOMIAL_JR:
        option.setPricingEngine(ql.BinomialVanillaEngine(process, "JR", 201))
    elif engine == PricingEngine.BINOMIAL_LR:
        option.setPricingEngine(ql.BinomialVanillaEngine(process, "LR", 201))
    elif engine == PricingEngine.FINITE_DIFF:
        option.setPricingEngine(ql.FdBlackScholesVanillaEngine(process, 201, 200))
    elif engine == PricingEngine.MONTE_CARLO:
        option.setPricingEngine(ql.MCEuropeanEngine(
            process, "pseudorandom", timeSteps=1,
            requiredTolerance=0.02, seed=42,
        ))
    else:
        raise ValueError(f"Unknown engine: {engine}")

    npv = option.NPV()

    # Greeks — analytic when available, else numeric bump-and-revalue
    bump = spot * 0.001  # 0.1% bump

    try:
        delta_v = option.delta()
    except RuntimeError:
        # Numeric delta: (P(S+h) - P(S-h)) / (2h)
        delta_v = _quick_numeric_greek(
            process, payoff, exercise, engine, spot, bump, "delta",
            risk_free_rate, dividend_yield, volatility, ql_eval,
        )

    try:
        gamma_v = option.gamma()
    except RuntimeError:
        gamma_v = _quick_numeric_greek(
            process, payoff, exercise, engine, spot, bump, "gamma",
            risk_free_rate, dividend_yield, volatility, ql_eval,
        )

    try:
        theta_annual = option.theta()
        theta_v = theta_annual / 365.0
    except RuntimeError:
        theta_v = 0.0  # Skip numeric theta for speed in compare mode

    try:
        vega_raw = option.vega()
        vega_v = vega_raw / 100.0
    except RuntimeError:
        vega_v = 0.0  # Skip numeric vega for speed in compare mode

    try:
        rho_raw = option.rho()
        rho_v = rho_raw / 100.0
    except RuntimeError:
        rho_v = 0.0  # Skip numeric rho for speed in compare mode

    return GreeksResult(
        engine=engine.value, price=npv,
        delta=delta_v, gamma=gamma_v,
        theta=theta_v, vega=vega_v, rho=rho_v,
    )


def _quick_numeric_greek(
    process, payoff, exercise, engine, spot, bump, greek_type,
    risk_free_rate, dividend_yield, volatility, ql_eval,
):
    """Fast numeric delta/gamma via bump-and-revalue without full option rebuild."""
    import QuantLib as ql
    from app.services.quantlib_pricer import _build_process

    def _reprice(s):
        p = _build_process(s, risk_free_rate, dividend_yield, volatility, ql_eval)
        opt = ql.VanillaOption(payoff, exercise)
        # Use analytic engine for numeric Greeks (fast, good enough for bump)
        if engine == PricingEngine.ANALYTIC_BS:
            opt.setPricingEngine(ql.AnalyticEuropeanEngine(p))
        elif engine in (PricingEngine.BINOMIAL_CRR, PricingEngine.BINOMIAL_JR, PricingEngine.BINOMIAL_LR):
            name = {"binomial_crr": "CRR", "binomial_jr": "JR", "binomial_lr": "LR"}[engine.value]
            opt.setPricingEngine(ql.BinomialVanillaEngine(p, name, 101))
        elif engine == PricingEngine.FINITE_DIFF:
            opt.setPricingEngine(ql.FdBlackScholesVanillaEngine(p, 101, 100))
        elif engine == PricingEngine.MONTE_CARLO:
            opt.setPricingEngine(ql.MCEuropeanEngine(
                p, "pseudorandom", timeSteps=1, requiredTolerance=0.05, seed=42,
            ))
        return opt.NPV()

    p_up = _reprice(spot + bump)
    p_dn = _reprice(spot - bump)

    if greek_type == "delta":
        return (p_up - p_dn) / (2 * bump)
    elif greek_type == "gamma":
        p_mid = _reprice(spot)
        return (p_up - 2 * p_mid + p_dn) / (bump ** 2)
    return 0.0


@router.post("/compare", response_model=PricingCompareResponse)
async def pricing_compare(request: PricingCompareRequest):
    """Compare ALL pricing models across a spot price range.

    Returns curves for:
      1. python_bs         — Python analytical BS (scipy.stats.norm CDF)
      2. quantlib_bs       — QuantLib Analytic European Engine (C++ via SWIG)
      3. quantlib_crr      — QuantLib Binomial Cox-Ross-Rubinstein (801 steps)
      4. quantlib_jr       — QuantLib Binomial Jarrow-Rudd (801 steps)
      5. quantlib_lr       — QuantLib Binomial Leisen-Reimer (801 steps)
      6. quantlib_fd       — QuantLib Finite Differences (801×800 grid)
      7. quantlib_mc       — QuantLib Monte Carlo (tol=0.001, seed=42)

    The frontend additionally computes "Legacy BS" (Abramowitz & Stegun normCdf
    approximation, |error| < 1.5e-7) client-side and overlays it.
    """
    try:
        exp_d = date.fromisoformat(request.expiration_date)
        eval_d = date.fromisoformat(request.evaluation_date) if request.evaluation_date else date.today()

        # Time to expiry in years
        delta_days = (exp_d - eval_d).days
        T = delta_days / 365.0
        if T <= 0:
            return PricingCompareResponse(
                success=False, strike=request.strike, option_type=request.option_type,
                expiration_date=request.expiration_date, time_to_expiry_years=0,
                models=[], error="Option has expired",
            )

        spot_min = request.spot_min or request.spot * 0.80
        spot_max = request.spot_max or request.spot * 1.20
        step = (spot_max - spot_min) / (request.num_points - 1)

        spots = [round(spot_min + i * step, 4) for i in range(request.num_points)]

        models: List[PricingModelCurve] = []

        # ── Model 1: Python analytical BS (scipy.stats.norm) ──
        python_bs_points: List[PricingPointResult] = []
        for s in spots:
            g = _scipy_bs_greeks(s, request.strike, T, request.risk_free_rate, request.volatility, request.option_type)
            python_bs_points.append(PricingPointResult(
                spot=s, price=round(g["price"], 6), delta=round(g["delta"], 6),
                gamma=round(g["gamma"], 6), theta=round(g["theta"], 6),
                vega=round(g["vega"], 6), rho=round(g["rho"], 6),
            ))
        models.append(PricingModelCurve(model="python_bs", points=python_bs_points))

        # ── QuantLib engines (all 6) ──
        # For the bulk comparison we use lighter parameters for slow engines
        # to keep total wall-time under ~60 s.  The analytic engine needs no
        # tuning; binomials use 201 steps (still converged, ~4× faster than
        # 801); FD uses a 201×200 grid; MC uses tolerance=0.01 (10× looser).
        ql_engines = [
            (PricingEngine.ANALYTIC_BS, "quantlib_bs"),
            (PricingEngine.BINOMIAL_CRR, "quantlib_crr"),
            (PricingEngine.BINOMIAL_JR, "quantlib_jr"),
            (PricingEngine.BINOMIAL_LR, "quantlib_lr"),
            (PricingEngine.FINITE_DIFF, "quantlib_fd"),
            (PricingEngine.MONTE_CARLO, "quantlib_mc"),
        ]

        if _QL_AVAILABLE:
            import QuantLib as ql

            for engine_enum, model_name in ql_engines:
                engine_points: List[PricingPointResult] = []
                try:
                    for s in spots:
                        result = _price_option_for_compare(
                            spot=s, strike=request.strike,
                            risk_free_rate=request.risk_free_rate,
                            volatility=request.volatility,
                            expiration_date=exp_d,
                            option_type=request.option_type,
                            evaluation_date=eval_d,
                            dividend_yield=request.dividend_yield,
                            engine=engine_enum,
                        )
                        engine_points.append(PricingPointResult(
                            spot=s, price=round(result.price, 6),
                            delta=round(result.delta, 6),
                            gamma=round(result.gamma, 6),
                            theta=round(result.theta, 6),
                            vega=round(result.vega, 6),
                            rho=round(result.rho, 6),
                        ))
                    models.append(PricingModelCurve(model=model_name, points=engine_points))
                    logger.info("[PricingCompare] Engine %s: %d points OK", model_name, len(engine_points))
                except Exception as eng_err:
                    logger.warning(
                        "[PricingCompare] Engine %s failed: %s", model_name, eng_err,
                    )
                    # Skip this engine but continue with others

        return PricingCompareResponse(
            success=True,
            strike=request.strike,
            option_type=request.option_type,
            expiration_date=request.expiration_date,
            time_to_expiry_years=round(T, 6),
            models=models,
        )

    except Exception as e:
        logger.error(f"[PricingCompare] Error: {e}", exc_info=True)
        return PricingCompareResponse(
            success=False, strike=request.strike, option_type=request.option_type,
            expiration_date=request.expiration_date, time_to_expiry_years=0,
            models=[], error=str(e),
        )
