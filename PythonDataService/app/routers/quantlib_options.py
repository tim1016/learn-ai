"""QuantLib-backed option pricing endpoints for validation against legacy BS."""
from __future__ import annotations

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
