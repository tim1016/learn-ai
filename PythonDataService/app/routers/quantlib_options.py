"""QuantLib-backed option pricing endpoints for validation against legacy BS."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator, model_validator

from app.options.expiry import OptionExpiryMs, option_expiry_date
from app.services.bs_greeks import black_scholes_greeks, bs_european_price
from app.services.quantlib_pricer import (
    _QL_AVAILABLE,
    PricingEngine,
    price_option,
)
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

router = APIRouter()
logger = logging.getLogger(__name__)

# A QuantLib call runs in a worker thread, so a long one does not hold the event loop (#3187). QuantLib keeps its
# evaluation date in one process-wide setting that each call sets and then reads, which the loop used to keep to one
# call at a time. The lock keeps that. A call holds it for one price, never a whole request.
_QUANTLIB_CALL = threading.Lock()


def _one_call_at_a_time[T](price: Callable[..., T], /, **inputs: object) -> T:
    with _QUANTLIB_CALL:
        return price(**inputs)


# The bound the strategy and portfolio requests declare on their rate (`ge=0, le=0.5`). It is checked in a validator,
# not a `Field` constraint, so the published OpenAPI schema keeps its shape.
_RATE_MIN = 0.0
_RATE_MAX = 0.5

# /compare quotes each spot of its curve to 4 decimals (`round(spot, 4)`).
_SPOT_QUOTE_STEP = 0.0001


def _require_rate_in_bounds(rate: float) -> float:
    if not _RATE_MIN <= rate <= _RATE_MAX:
        raise ValueError(
            f"risk_free_rate must be between {_RATE_MIN} and {_RATE_MAX} (a decimal, 0.05 = 5%); got {rate}"
        )
    return rate


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class QuantLibPriceRequest(BaseModel):
    """Price a single European option via QuantLib."""

    spot: float = Field(..., gt=0, description="Current underlying price")
    strike: float = Field(..., gt=0, description="Strike price")
    risk_free_rate: float = Field(
        DEFAULT_RISK_FREE_RATE, description="Annualized risk-free rate (omit for the Python default)"
    )
    volatility: float = Field(..., gt=0, description="Annualized IV (decimal, e.g. 0.20)")
    expiration_ms: OptionExpiryMs = Field(
        ..., description="Expiry as the anchor instant: 16:00 New York time on the expiry date"
    )
    option_type: str = Field(..., pattern="^(call|put)$", description="call or put")
    evaluation_date: str | None = Field(None, description="Pricing date (default: today)")
    dividend_yield: float = Field(0.0, ge=0, description="Continuous dividend yield")
    engine: str = Field(
        "analytic_bs",
        description="Pricing engine: analytic_bs, binomial_crr, binomial_jr, binomial_lr, finite_diff, monte_carlo",
    )

    @field_validator("risk_free_rate")
    @classmethod
    def _rate_in_bounds(cls, rate: float) -> float:
        return _require_rate_in_bounds(rate)


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
    d1: float | None = None
    d2: float | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/price", response_model=QuantLibGreeksResponse)
async def quantlib_price(request: QuantLibPriceRequest):
    """Price a single European option and return all Greeks."""
    if not _QL_AVAILABLE:
        return QuantLibGreeksResponse(
            success=False,
            engine=request.engine,
            price=0,
            delta=0,
            gamma=0,
            theta=0,
            vega=0,
            rho=0,
            error="QuantLib not installed",
        )
    try:
        engine = PricingEngine(request.engine)
        eval_d = date.fromisoformat(request.evaluation_date) if request.evaluation_date else None
        exp_d = option_expiry_date(request.expiration_ms)

        result = await asyncio.to_thread(
            _one_call_at_a_time,
            price_option,
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
            success=False,
            engine=request.engine,
            price=0,
            delta=0,
            gamma=0,
            theta=0,
            vega=0,
            rho=0,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Pricing model comparison endpoint
# ---------------------------------------------------------------------------


class PricingCompareRequest(BaseModel):
    """Compare pricing models across a range of underlying prices."""

    spot: float = Field(..., gt=0, description="Current underlying price")
    strike: float = Field(..., gt=0, description="Strike price")
    volatility: float = Field(..., gt=0, description="Annualized IV (decimal)")
    expiration_ms: OptionExpiryMs = Field(
        ..., description="Expiry as the anchor instant: 16:00 New York time on the expiry date"
    )
    option_type: str = Field(..., pattern="^(call|put)$")
    risk_free_rate: float = Field(
        DEFAULT_RISK_FREE_RATE, description="Annualized risk-free rate (omit for the Python default)"
    )
    dividend_yield: float = Field(0.0, ge=0)
    evaluation_date: str | None = Field(None)
    spot_min: float | None = Field(None, description="Range start (default: spot * 0.80)")
    spot_max: float | None = Field(None, description="Range end (default: spot * 1.20)")
    num_points: int = Field(100, ge=10, le=500, description="Number of data points")

    @field_validator("risk_free_rate")
    @classmethod
    def _rate_in_bounds(cls, rate: float) -> float:
        return _require_rate_in_bounds(rate)

    def spot_bounds(self) -> tuple[float, float]:
        """The spot range the curves span: the request's bounds, else spot x 0.80 to spot x 1.20."""
        low = self.spot * 0.80 if self.spot_min is None else self.spot_min
        high = self.spot * 1.20 if self.spot_max is None else self.spot_max
        return low, high

    @model_validator(mode="after")
    def _spot_range_is_a_range(self) -> PricingCompareRequest:
        low, high = self.spot_bounds()
        # The curve is quoted to 4 decimals, so a start under one step would round to a spot of 0.
        if not _SPOT_QUOTE_STEP <= low < high:
            raise ValueError(
                f"the spot range must run upward from at least {_SPOT_QUOTE_STEP} (spot_min < spot_max); "
                f"got {low} to {high}"
            )
        return self


class PricingPointResult(BaseModel):
    """Single data point for one model at one spot price."""

    spot: float
    price: float
    delta: float
    gamma: float
    theta: float | None = Field(
        ..., description="Per calendar day; null when the engine does not provide it (Monte Carlo has no theta)"
    )
    vega: float | None = Field(
        ..., description="Per 1% IV move; null when the engine does not provide it (Monte Carlo has no vega)"
    )
    rho: float | None = Field(
        ..., description="Per 1% rate move; null when the engine does not provide it (Monte Carlo has no rho)"
    )


class PricingModelCurve(BaseModel):
    """Full curve for one pricing model."""

    model: str
    points: list[PricingPointResult]


class PricingModelUnavailable(BaseModel):
    """A model the comparison asked for and has no curve for."""

    model: str
    reason: str = Field(..., description="Why in the service's own words; never the engine's error text")


class PricingCompareResponse(BaseModel):
    """Comparison results for all pricing models."""

    success: bool
    strike: float
    option_type: str
    expiration_ms: OptionExpiryMs
    time_to_expiry_years: float
    risk_free_rate: float = Field(
        ..., description="The rate every curve was priced at, so a client overlay can price at the same one"
    )
    models: list[PricingModelCurve]
    unavailable_models: list[PricingModelUnavailable] = Field(
        default_factory=list,
        description="Each requested model that has no curve in `models`, with why; empty when every model ran",
    )
    error: str | None = None


@dataclass(frozen=True)
class _ComparePoint:
    """One engine's price and Greeks at one spot. A Greek the engine does not provide is None, never 0.0."""

    price: float
    delta: float
    gamma: float
    theta: float | None
    vega: float | None
    rho: float | None


_QUANTLIB_MISSING_REASON = "QuantLib is not installed on the data service"


def _round_if_provided(greek: float | None) -> float | None:
    return None if greek is None else round(greek, 6)


def _engine_failure_reason(error: Exception) -> str:
    # Our words plus the exception's class, never its text; the data service log holds the text in full.
    return f"the engine raised {type(error).__name__} while pricing this contract (see the data service log)"


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
) -> _ComparePoint:
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
        _build_process,
        _ensure_ql,
        _ql_date,
    )

    _ensure_ql()

    ql_eval = _ql_date(evaluation_date)
    ql.Settings.instance().evaluationDate = ql_eval

    ql_expiry = _ql_date(expiration_date)
    t_years = ql.Actual365Fixed().yearFraction(ql_eval, ql_expiry)

    if t_years <= 0:
        intrinsic = max(spot - strike, 0) if option_type == "call" else max(strike - spot, 0)
        d_val = (
            1.0
            if (option_type == "call" and spot > strike)
            else (-1.0 if option_type == "put" and spot < strike else 0.0)
        )
        return _ComparePoint(price=intrinsic, delta=d_val, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

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
        option.setPricingEngine(
            ql.MCEuropeanEngine(
                process,
                "pseudorandom",
                timeSteps=1,
                requiredTolerance=0.02,
                seed=42,
            )
        )
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
            process,
            payoff,
            exercise,
            engine,
            spot,
            bump,
            "delta",
            risk_free_rate,
            dividend_yield,
            volatility,
            ql_eval,
        )

    try:
        gamma_v = option.gamma()
    except RuntimeError:
        gamma_v = _quick_numeric_greek(
            process,
            payoff,
            exercise,
            engine,
            spot,
            bump,
            "gamma",
            risk_free_rate,
            dividend_yield,
            volatility,
            ql_eval,
        )

    # Monte Carlo has no analytic theta/vega/rho, and a numeric one is a further MC reprice per Greek (seconds at index
    # scale). Those three are not provided for it: None, which the response and the page show as a gap (#3221).
    is_monte_carlo = engine == PricingEngine.MONTE_CARLO

    theta_v: float | None
    try:
        theta_annual = option.theta()
        theta_v = theta_annual / 365.0
    except RuntimeError:
        theta_v = None

    vega_v: float | None
    try:
        vega_raw = option.vega()
        vega_v = vega_raw / 100.0
    except RuntimeError:
        vega_v = (
            None
            if is_monte_carlo
            else _quick_numeric_greek(
                process,
                payoff,
                exercise,
                engine,
                spot,
                bump,
                "vega",
                risk_free_rate,
                dividend_yield,
                volatility,
                ql_eval,
            )
        )

    rho_v: float | None
    try:
        rho_raw = option.rho()
        rho_v = rho_raw / 100.0
    except RuntimeError:
        rho_v = (
            None
            if is_monte_carlo
            else _quick_numeric_greek(
                process,
                payoff,
                exercise,
                engine,
                spot,
                bump,
                "rho",
                risk_free_rate,
                dividend_yield,
                volatility,
                ql_eval,
            )
        )

    return _ComparePoint(price=npv, delta=delta_v, gamma=gamma_v, theta=theta_v, vega=vega_v, rho=rho_v)


def _quick_numeric_greek(
    process,
    payoff,
    exercise,
    engine,
    spot,
    bump,
    greek_type,
    risk_free_rate,
    dividend_yield,
    volatility,
    ql_eval,
):
    """Fast numeric Greek via bump-and-revalue without full option rebuild.

    Formula: central differences of the engine's own price P — delta (P(S+h) - P(S-h)) / 2h; gamma
      (P(S+h) - 2P(S) + P(S-h)) / h²; vega (P(σ+k) - P(σ-k)) / (2k·100); rho (P(r+b) - P(r-b)) / (2b·100).
    Reference: first-order central finite differences of the Black-Scholes sensitivities, Hull §19; no external port.
    Canonical implementation: this function (the /compare bump; quantlib_pricer._numeric_* is the /price one).
    Validated against: tests/routers/test_quantlib_compare.py::test_lattice_engines_report_vega_and_rho_near_closed_form

    delta and gamma bump the spot, vega the volatility and rho the rate, each on the same tree/grid /compare prices at.
    """
    import QuantLib as ql

    from app.services.quantlib_pricer import _build_process

    def _reprice(s, vol=volatility, rate=risk_free_rate):
        p = _build_process(s, rate, dividend_yield, vol, ql_eval)
        opt = ql.VanillaOption(payoff, exercise)
        # Use analytic engine for numeric Greeks (fast, good enough for bump)
        if engine == PricingEngine.ANALYTIC_BS:
            opt.setPricingEngine(ql.AnalyticEuropeanEngine(p))
        elif engine in (PricingEngine.BINOMIAL_CRR, PricingEngine.BINOMIAL_JR, PricingEngine.BINOMIAL_LR):
            name = {"binomial_crr": "CRR", "binomial_jr": "JR", "binomial_lr": "LR"}[engine.value]
            opt.setPricingEngine(ql.BinomialVanillaEngine(p, name, 201))
        elif engine == PricingEngine.FINITE_DIFF:
            opt.setPricingEngine(ql.FdBlackScholesVanillaEngine(p, 201, 200))
        elif engine == PricingEngine.MONTE_CARLO:
            opt.setPricingEngine(
                ql.MCEuropeanEngine(
                    p,
                    "pseudorandom",
                    timeSteps=1,
                    requiredTolerance=0.05,
                    seed=42,
                )
            )
        return opt.NPV()

    if greek_type == "vega":
        # Central difference over one vol point either side (narrower when the vol is under two points), per 1% IV move.
        vol_bump = min(0.01, volatility / 2)
        return (_reprice(spot, vol=volatility + vol_bump) - _reprice(spot, vol=volatility - vol_bump)) / (
            2 * vol_bump * 100.0
        )
    if greek_type == "rho":
        rate_bump = 0.0001  # 1 bp either side, per 1% rate move
        return (_reprice(spot, rate=risk_free_rate + rate_bump) - _reprice(spot, rate=risk_free_rate - rate_bump)) / (
            2 * rate_bump * 100.0
        )

    p_up = _reprice(spot + bump)
    p_dn = _reprice(spot - bump)

    if greek_type == "delta":
        return (p_up - p_dn) / (2 * bump)
    elif greek_type == "gamma":
        p_mid = _reprice(spot)
        return (p_up - 2 * p_mid + p_dn) / (bump**2)
    return 0.0


@router.post("/compare", response_model=PricingCompareResponse)
async def pricing_compare(request: PricingCompareRequest):
    """Compare ALL pricing models across a spot price range.

    Returns curves for:
      1. python_bs         — Python analytical BS (canonical bs_greeks.py, at the request's dividend yield)
      2. quantlib_bs       — QuantLib Analytic European Engine (C++ via SWIG)
      3. quantlib_crr      — QuantLib Binomial Cox-Ross-Rubinstein (801 steps)
      4. quantlib_jr       — QuantLib Binomial Jarrow-Rudd (801 steps)
      5. quantlib_lr       — QuantLib Binomial Leisen-Reimer (801 steps)
      6. quantlib_fd       — QuantLib Finite Differences (801×800 grid)
      7. quantlib_mc       — QuantLib Monte Carlo (tol=0.001, seed=42)

    A model with no curve in `models` is named in `unavailable_models` with why (QuantLib not installed, or the
    engine raised). A Greek an engine does not provide (Monte Carlo theta, vega, rho) is null, never 0.0.

    The frontend additionally computes "Legacy BS" (Abramowitz & Stegun normCdf
    approximation, |error| < 1.5e-7) client-side and overlays it.
    """
    try:
        exp_d = option_expiry_date(request.expiration_ms)
        eval_d = date.fromisoformat(request.evaluation_date) if request.evaluation_date else et_date_at_ms(now_ms_utc())

        # Time to expiry in years, on QuantLib's own day-count convention
        delta_days = (exp_d - eval_d).days
        T = delta_days / 365.0
        if T <= 0:
            return PricingCompareResponse(
                success=False,
                strike=request.strike,
                option_type=request.option_type,
                expiration_ms=request.expiration_ms,
                time_to_expiry_years=0,
                risk_free_rate=request.risk_free_rate,
                models=[],
                error="Option has expired",
            )

        spot_min, spot_max = request.spot_bounds()
        step = (spot_max - spot_min) / (request.num_points - 1)

        spots = [round(spot_min + i * step, 4) for i in range(request.num_points)]

        models: list[PricingModelCurve] = []
        unavailable: list[PricingModelUnavailable] = []

        # ── Model 1: Python analytical BS (the canonical closed form, bs_greeks.py) ──
        is_call = request.option_type == "call"
        python_bs_points: list[PricingPointResult] = []
        for s in spots:
            g = black_scholes_greeks(
                spot=s,
                strike=request.strike,
                ttm_years=T,
                volatility=request.volatility,
                rate=request.risk_free_rate,
                dividend=request.dividend_yield,
                is_call=is_call,
            )
            price = bs_european_price(
                s,
                request.strike,
                T,
                request.risk_free_rate,
                request.volatility,
                is_call=is_call,
                dividend=request.dividend_yield,
            )
            python_bs_points.append(
                PricingPointResult(
                    spot=s,
                    price=round(price, 6),
                    delta=round(g.delta, 6),
                    gamma=round(g.gamma, 6),
                    theta=round(g.theta, 6),
                    vega=round(g.vega, 6),
                    rho=round(g.rho, 6),
                )
            )
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
            for engine_enum, model_name in ql_engines:
                engine_points: list[PricingPointResult] = []
                try:
                    for s in spots:
                        result = await asyncio.to_thread(
                            _one_call_at_a_time,
                            _price_option_for_compare,
                            spot=s,
                            strike=request.strike,
                            risk_free_rate=request.risk_free_rate,
                            volatility=request.volatility,
                            expiration_date=exp_d,
                            option_type=request.option_type,
                            evaluation_date=eval_d,
                            dividend_yield=request.dividend_yield,
                            engine=engine_enum,
                        )
                        engine_points.append(
                            PricingPointResult(
                                spot=s,
                                price=round(result.price, 6),
                                delta=round(result.delta, 6),
                                gamma=round(result.gamma, 6),
                                theta=_round_if_provided(result.theta),
                                vega=_round_if_provided(result.vega),
                                rho=_round_if_provided(result.rho),
                            )
                        )
                    models.append(PricingModelCurve(model=model_name, points=engine_points))
                    logger.info("[PricingCompare] Engine %s: %d points OK", model_name, len(engine_points))
                except Exception as eng_err:
                    logger.warning(
                        "[PricingCompare] Engine %s failed: %s",
                        model_name,
                        eng_err,
                    )
                    # The other engines still run; this one is named in the response with why it has no curve.
                    unavailable.append(
                        PricingModelUnavailable(model=model_name, reason=_engine_failure_reason(eng_err))
                    )
        else:
            unavailable.extend(
                PricingModelUnavailable(model=model_name, reason=_QUANTLIB_MISSING_REASON)
                for _, model_name in ql_engines
            )

        return PricingCompareResponse(
            success=True,
            strike=request.strike,
            option_type=request.option_type,
            expiration_ms=request.expiration_ms,
            time_to_expiry_years=round(T, 6),
            risk_free_rate=request.risk_free_rate,
            models=models,
            unavailable_models=unavailable,
        )

    except Exception as e:
        logger.error(f"[PricingCompare] Error: {e}", exc_info=True)
        return PricingCompareResponse(
            success=False,
            strike=request.strike,
            option_type=request.option_type,
            expiration_ms=request.expiration_ms,
            time_to_expiry_years=0,
            risk_free_rate=request.risk_free_rate,
            models=[],
            error=str(e),
        )
