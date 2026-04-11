"""QuantLib-backed option pricer for validation against legacy Black-Scholes.

Uses the compiled QuantLib C++ library via SWIG Python bindings to compute
theoretical prices and Greeks (delta, gamma, theta, vega, rho) for European
vanilla options.  Supports multiple pricing engines so the frontend can
compare results side-by-side with the legacy Abramowitz & Stegun JS pricer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import List, Optional

logger = logging.getLogger(__name__)

try:
    import QuantLib as ql

    _QL_AVAILABLE = True
    logger.info("[QuantLib] QuantLib %s loaded successfully", ql.__version__)
except ImportError:
    _QL_AVAILABLE = False
    logger.warning(
        "[QuantLib] QuantLib not installed — quantlib pricing will be unavailable. "
        "Install with: pip install QuantLib"
    )


class PricingEngine(str, Enum):
    """Available QuantLib pricing engines."""
    ANALYTIC_BS = "analytic_bs"
    BINOMIAL_CRR = "binomial_crr"
    BINOMIAL_JR = "binomial_jr"
    BINOMIAL_LR = "binomial_lr"
    FINITE_DIFF = "finite_diff"
    MONTE_CARLO = "monte_carlo"


@dataclass
class GreeksResult:
    """Single-option pricing result with all Greeks."""
    engine: str
    price: float
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float   # per 1% IV move
    rho: float    # per 1% rate move
    # Optional diagnostics
    d1: Optional[float] = None
    d2: Optional[float] = None


@dataclass
class StrategyGreeksResult:
    """Multi-leg strategy pricing result."""
    engine: str
    net_price: float
    net_delta: float
    net_gamma: float
    net_theta: float
    net_vega: float
    net_rho: float
    legs: List[GreeksResult]


def _ensure_ql() -> None:
    if not _QL_AVAILABLE:
        raise RuntimeError(
            "QuantLib is not installed. Run: pip install QuantLib"
        )


def _ql_date(d: date) -> "ql.Date":
    """Convert Python date → QuantLib Date."""
    return ql.Date(d.day, d.month, d.year)


def _build_process(
    spot: float,
    risk_free_rate: float,
    dividend_yield: float,
    volatility: float,
    eval_date: "ql.Date",
) -> "ql.BlackScholesMertonProcess":
    """Construct a BSM process from scalar market data."""
    spot_handle = ql.QuoteHandle(ql.SimpleQuote(spot))
    flat_rf = ql.YieldTermStructureHandle(
        ql.FlatForward(eval_date, risk_free_rate, ql.Actual365Fixed())
    )
    flat_div = ql.YieldTermStructureHandle(
        ql.FlatForward(eval_date, dividend_yield, ql.Actual365Fixed())
    )
    flat_vol = ql.BlackVolTermStructureHandle(
        ql.BlackConstantVol(eval_date, ql.TARGET(), volatility, ql.Actual365Fixed())
    )
    return ql.BlackScholesMertonProcess(spot_handle, flat_div, flat_rf, flat_vol)


def _attach_engine(
    option: "ql.VanillaOption",
    process: "ql.BlackScholesMertonProcess",
    engine: PricingEngine,
) -> None:
    """Attach the requested pricing engine to the option."""
    if engine == PricingEngine.ANALYTIC_BS:
        option.setPricingEngine(ql.AnalyticEuropeanEngine(process))
    elif engine == PricingEngine.BINOMIAL_CRR:
        option.setPricingEngine(ql.BinomialVanillaEngine(process, "CRR", 801))
    elif engine == PricingEngine.BINOMIAL_JR:
        option.setPricingEngine(ql.BinomialVanillaEngine(process, "JR", 801))
    elif engine == PricingEngine.BINOMIAL_LR:
        option.setPricingEngine(ql.BinomialVanillaEngine(process, "LR", 801))
    elif engine == PricingEngine.FINITE_DIFF:
        option.setPricingEngine(
            ql.FdBlackScholesVanillaEngine(process, 801, 800)
        )
    elif engine == PricingEngine.MONTE_CARLO:
        option.setPricingEngine(
            ql.MCEuropeanEngine(
                process, "pseudorandom",
                timeSteps=1,
                requiredTolerance=0.001,
                seed=42,
            )
        )
    else:
        raise ValueError(f"Unknown pricing engine: {engine}")


def price_option(
    spot: float,
    strike: float,
    risk_free_rate: float,
    volatility: float,
    expiration_date: date,
    option_type: str,
    evaluation_date: Optional[date] = None,
    dividend_yield: float = 0.0,
    engine: PricingEngine = PricingEngine.ANALYTIC_BS,
) -> GreeksResult:
    """Price a single European option and compute all Greeks.

    Args:
        spot: Current underlying price
        strike: Option strike price
        risk_free_rate: Annualized risk-free rate (e.g. 0.05 for 5%)
        volatility: Annualized implied volatility (e.g. 0.20 for 20%)
        expiration_date: Option expiration date
        option_type: 'call' or 'put'
        evaluation_date: Pricing date (defaults to today)
        dividend_yield: Annualized continuous dividend yield
        engine: Which QuantLib pricing engine to use

    Returns:
        GreeksResult with price and all five Greeks
    """
    _ensure_ql()

    eval_d = evaluation_date or date.today()
    ql_eval = _ql_date(eval_d)
    ql.Settings.instance().evaluationDate = ql_eval

    ql_expiry = _ql_date(expiration_date)
    t_years = ql.Actual365Fixed().yearFraction(ql_eval, ql_expiry)

    if t_years <= 0:
        # Expired — return intrinsic value
        intrinsic = max(spot - strike, 0) if option_type == "call" else max(strike - spot, 0)
        return GreeksResult(
            engine=engine.value,
            price=intrinsic,
            delta=1.0 if (option_type == "call" and spot > strike) else (-1.0 if option_type == "put" and spot < strike else 0.0),
            gamma=0.0, theta=0.0, vega=0.0, rho=0.0,
        )

    ql_type = ql.Option.Call if option_type == "call" else ql.Option.Put
    payoff = ql.PlainVanillaPayoff(ql_type, strike)
    exercise = ql.EuropeanExercise(ql_expiry)
    option = ql.VanillaOption(payoff, exercise)

    process = _build_process(spot, risk_free_rate, dividend_yield, volatility, ql_eval)
    _attach_engine(option, process, engine)

    # Compute price
    npv = option.NPV()

    # Greeks — some engines don't support analytical Greeks, use try/except
    try:
        delta = option.delta()
    except RuntimeError:
        delta = _numeric_delta(spot, strike, risk_free_rate, volatility, expiration_date, option_type, eval_d, dividend_yield, engine)

    try:
        gamma = option.gamma()
    except RuntimeError:
        gamma = _numeric_gamma(spot, strike, risk_free_rate, volatility, expiration_date, option_type, eval_d, dividend_yield, engine)

    try:
        theta_annual = option.theta()
        theta_daily = theta_annual / 365.0
    except RuntimeError:
        theta_daily = _numeric_theta(spot, strike, risk_free_rate, volatility, expiration_date, option_type, eval_d, dividend_yield, engine)

    try:
        vega_raw = option.vega()
        vega_pct = vega_raw / 100.0  # per 1% IV move
    except RuntimeError:
        vega_pct = _numeric_vega(spot, strike, risk_free_rate, volatility, expiration_date, option_type, eval_d, dividend_yield, engine)

    try:
        rho_raw = option.rho()
        rho_pct = rho_raw / 100.0  # per 1% rate move
    except RuntimeError:
        rho_pct = _numeric_rho(spot, strike, risk_free_rate, volatility, expiration_date, option_type, eval_d, dividend_yield, engine)

    # d1/d2 for diagnostic comparison with legacy
    import math
    d1 = d2 = None
    if volatility > 0 and t_years > 0 and spot > 0 and strike > 0:
        d1 = (math.log(spot / strike) + (risk_free_rate - dividend_yield + 0.5 * volatility ** 2) * t_years) / (volatility * math.sqrt(t_years))
        d2 = d1 - volatility * math.sqrt(t_years)

    return GreeksResult(
        engine=engine.value,
        price=round(npv, 8),
        delta=round(delta, 8),
        gamma=round(gamma, 8),
        theta=round(theta_daily, 8),
        vega=round(vega_pct, 8),
        rho=round(rho_pct, 8),
        d1=round(d1, 8) if d1 is not None else None,
        d2=round(d2, 8) if d2 is not None else None,
    )


# ---------------------------------------------------------------------------
# Numeric Greek fallbacks (for engines without analytic Greeks)
# ---------------------------------------------------------------------------

_BUMP = 0.01  # 1% bump for finite differencing


def _reprice(spot, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine) -> float:
    """Helper to reprice without recursing into Greek computation."""
    _ensure_ql()
    ql_eval = _ql_date(eval_d)
    ql.Settings.instance().evaluationDate = ql_eval
    ql_type = ql.Option.Call if opt_type == "call" else ql.Option.Put
    payoff = ql.PlainVanillaPayoff(ql_type, strike)
    exercise = ql.EuropeanExercise(_ql_date(exp_date))
    option = ql.VanillaOption(payoff, exercise)
    process = _build_process(spot, r, div_y, vol, ql_eval)
    _attach_engine(option, process, engine)
    return option.NPV()


def _numeric_delta(spot, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine) -> float:
    bump = spot * _BUMP
    up = _reprice(spot + bump, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine)
    down = _reprice(spot - bump, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine)
    return (up - down) / (2 * bump)


def _numeric_gamma(spot, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine) -> float:
    bump = spot * _BUMP
    up = _reprice(spot + bump, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine)
    mid = _reprice(spot, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine)
    down = _reprice(spot - bump, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine)
    return (up - 2 * mid + down) / (bump ** 2)


def _numeric_theta(spot, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine) -> float:
    base = _reprice(spot, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine)
    next_day = eval_d + timedelta(days=1)
    shifted = _reprice(spot, strike, r, vol, exp_date, opt_type, next_day, div_y, engine)
    return shifted - base  # already per day


def _numeric_vega(spot, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine) -> float:
    bump = 0.01  # 1 vol point
    up = _reprice(spot, strike, r, vol + bump, exp_date, opt_type, eval_d, div_y, engine)
    down = _reprice(spot, strike, r, vol - bump, exp_date, opt_type, eval_d, div_y, engine)
    return (up - down) / 2.0  # per 1% move already


def _numeric_rho(spot, strike, r, vol, exp_date, opt_type, eval_d, div_y, engine) -> float:
    bump = 0.0001  # 1 bp
    up = _reprice(spot, strike, r + bump, vol, exp_date, opt_type, eval_d, div_y, engine)
    down = _reprice(spot, strike, r - bump, vol, exp_date, opt_type, eval_d, div_y, engine)
    return (up - down) / (2 * bump) / 100.0  # per 1% move


# ---------------------------------------------------------------------------
# Multi-leg strategy pricing
# ---------------------------------------------------------------------------

def price_strategy(
    spot: float,
    legs: list[dict],
    risk_free_rate: float,
    evaluation_date: Optional[date] = None,
    dividend_yield: float = 0.0,
    engine: PricingEngine = PricingEngine.ANALYTIC_BS,
) -> StrategyGreeksResult:
    """Price a multi-leg options strategy.

    Each leg dict must contain:
        strike, option_type ('call'/'put'), position ('long'/'short'),
        iv (decimal, e.g. 0.20), premium, quantity
    """
    leg_results: list[GreeksResult] = []
    net = {"price": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    for leg in legs:
        expiration = leg.get("expiration_date")
        if isinstance(expiration, str):
            expiration = date.fromisoformat(expiration)

        result = price_option(
            spot=spot,
            strike=leg["strike"],
            risk_free_rate=risk_free_rate,
            volatility=leg["iv"],
            expiration_date=expiration,
            option_type=leg["option_type"],
            evaluation_date=evaluation_date,
            dividend_yield=dividend_yield,
            engine=engine,
        )

        sign = 1 if leg["position"] == "long" else -1
        qty = leg.get("quantity", 1)

        net["price"] += result.price * sign * qty
        net["delta"] += result.delta * sign * qty
        net["gamma"] += result.gamma * sign * qty
        net["theta"] += result.theta * sign * qty
        net["vega"] += result.vega * sign * qty
        net["rho"] += result.rho * sign * qty

        leg_results.append(result)

    return StrategyGreeksResult(
        engine=engine.value,
        net_price=round(net["price"], 8),
        net_delta=round(net["delta"], 8),
        net_gamma=round(net["gamma"], 8),
        net_theta=round(net["theta"], 8),
        net_vega=round(net["vega"], 8),
        net_rho=round(net["rho"], 8),
        legs=leg_results,
    )
