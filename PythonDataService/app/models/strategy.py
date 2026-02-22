"""Pydantic models for options strategy analysis"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from typing import Optional


class StrategyLeg(BaseModel):
    """A single option leg in a strategy."""
    strike: float = Field(..., gt=0, description="Strike price")
    option_type: str = Field(..., description="'call' or 'put'")
    position: str = Field(..., description="'long' or 'short'")
    premium: float = Field(..., ge=0, description="Per-share premium (always positive)")
    iv: float = Field(..., ge=0, le=5.0, description="Implied volatility as decimal (e.g. 0.25 for 25%)")
    quantity: int = Field(1, ge=1, description="Number of contracts")

    @field_validator("option_type")
    @classmethod
    def validate_option_type(cls, v: str) -> str:
        if v not in ("call", "put"):
            raise ValueError("option_type must be 'call' or 'put'")
        return v

    @field_validator("position")
    @classmethod
    def validate_position(cls, v: str) -> str:
        if v not in ("long", "short"):
            raise ValueError("position must be 'long' or 'short'")
        return v


class StrategyAnalyzeRequest(BaseModel):
    """Request to analyze an options strategy."""
    symbol: str = Field(..., min_length=1, max_length=20, description="Underlying ticker symbol")
    legs: list[StrategyLeg] = Field(..., min_length=1, max_length=8, description="Strategy legs")
    expiration_date: str = Field(..., description="Expiration date (YYYY-MM-DD)")
    spot_price: float = Field(..., gt=0, description="Current underlying price")
    risk_free_rate: float = Field(0.043, ge=0, le=0.5, description="Risk-free rate (default ~4.3%)")
    curve_points: int = Field(300, ge=50, le=1000, description="Number of payoff curve points")
    price_range_pct: float = Field(0.30, gt=0, le=1.0, description="Price range as fraction of spot (±)")


class PayoffPoint(BaseModel):
    """Single point on the payoff curve."""
    price: float
    pnl: float


class GreeksResult(BaseModel):
    """Aggregate Greeks for the entire strategy."""
    delta: float = 0
    gamma: float = 0
    theta: float = 0
    vega: float = 0


class StrategyAnalyzeResponse(BaseModel):
    """Full strategy analysis result."""
    success: bool
    symbol: str = ""
    spot_price: float = 0
    strategy_cost: float = 0
    pop: float = 0
    expected_value: float = 0
    max_profit: float = 0
    max_loss: float = 0
    breakevens: list[float] = []
    curve: list[PayoffPoint] = []
    greeks: GreeksResult = GreeksResult()
    error: Optional[str] = None
