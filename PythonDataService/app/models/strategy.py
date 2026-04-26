"""Pydantic models for options strategy analysis.

Phase 1.1 of `docs/architecture/numerical-authority-migration-plan.md` extends
the response with optional current-time fields (current-time P&L curve,
Greek curves, per-leg diagnostics). These additions are gated behind
opt-in request flags so existing callers see zero change in payload shape
or response time.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class StrategyLeg(BaseModel):
    """A single option leg in a strategy."""

    leg_id: str | None = Field(None, description="Optional caller-supplied identifier echoed in diagnostics")
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
    # ------------------------------------------------------------------
    # Phase 1.1 opt-in extensions. Default False so existing callers see
    # no shape change. Once OptionsStrategyLabComponent is rewired (Phase 1.2)
    # to consume these fields it sets them all to True.
    # ------------------------------------------------------------------
    include_current_curve: bool = Field(
        False,
        description=(
            "If true, include `current_curve` — theoretical per-share P&L at "
            "today's vol surface (not at expiration) over the same price grid as `curve`."
        ),
    )
    include_greek_curves: bool = Field(
        False,
        description=(
            "If true, include `greek_curves` — aggregate delta/gamma/theta/vega per spot grid point."
        ),
    )
    include_leg_diagnostics: bool = Field(
        False,
        description=(
            "If true, include `leg_diagnostics` — per-leg current theoretical value and Greeks "
            "at the request's spot price (used by the Strategy Lab diagnostic table)."
        ),
    )
    what_if_time_shift_days: float = Field(
        0.0,
        ge=-3650.0,
        le=3650.0,
        description=(
            "If nonzero AND include_current_curve=true, evaluate the current curve at "
            "(today + shift) days. Lets the UI show 'curve in N days' without computing it client-side."
        ),
    )
    what_if_iv_shift: float = Field(
        0.0,
        ge=-5.0,
        le=5.0,
        description=(
            "If nonzero AND include_current_curve=true, evaluate the current curve with "
            "an additive IV shift applied to every leg (e.g. +0.05 = +5 vol points)."
        ),
    )


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


class CurrentCurvePoint(BaseModel):
    """Phase 1.1: theoretical per-share P&L at today's vol surface (not at expiry)."""

    price: float
    theoretical_value: float = Field(..., description="Theoretical strategy value per share, at today's vol")
    theoretical_pnl: float = Field(..., description="theoretical_value - strategy_cost (per share)")


class GreekCurvePoint(BaseModel):
    """Phase 1.1: aggregate Greeks at one spot grid point."""

    price: float
    delta: float
    gamma: float
    theta: float
    vega: float


class LegDiagnostic(BaseModel):
    """Phase 1.1: per-leg current theoretical value and Greeks at request spot."""

    leg_id: str | None = None
    strike: float
    option_type: str
    position: str
    quantity: int
    iv: float
    entry_premium: float
    current_theoretical: float
    current_delta: float
    current_gamma: float
    current_theta: float
    current_vega: float


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
    # ------------------------------------------------------------------
    # Phase 1.1 opt-in extensions. None unless the matching include_* flag
    # was set on the request.
    # ------------------------------------------------------------------
    current_curve: list[CurrentCurvePoint] | None = None
    greek_curves: list[GreekCurvePoint] | None = None
    leg_diagnostics: list[LegDiagnostic] | None = None
    error: str | None = None
