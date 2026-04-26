"""Pydantic models for portfolio scenario / live-Greeks endpoints.

These models implement Phase 2 of `docs/architecture/numerical-authority-migration-plan.md`:
move portfolio scenario / live-Greeks math out of `.NET` and into Python.
The `.NET` services become passthroughs (Phase 2.2); this is the canonical
shape they will call.

Design notes:
- Each position is self-describing. The Python service does not load from
  the .NET DB; the caller (`.NET`) projects DB state into these models.
- Stocks are represented as positions with `instrument="stock"` and no
  option fields. They contribute `delta=1, gamma=0, theta=0, vega=0` to
  scenario aggregates, computed from `quantity * spot_change`.
- Options use closed-form `bs_greeks.py` math. No QuantLib setup overhead;
  works at any TTM including 0DTE.
- Timestamps follow repo policy: int64 ms UTC at the wire boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class StockPosition(BaseModel):
    """A long or short stock position."""

    instrument: Literal["stock"] = "stock"
    symbol: str = Field(..., min_length=1, max_length=20)
    quantity: float = Field(..., description="Net quantity (negative for short)")
    entry_price: float = Field(..., gt=0, description="Avg entry price")
    leg_id: str | None = Field(None, description="Optional caller-supplied identifier echoed in the response")


class OptionPosition(BaseModel):
    """A single option leg (call or put, long or short)."""

    instrument: Literal["option"] = "option"
    symbol: str = Field(..., min_length=1, max_length=20, description="Underlying ticker")
    option_type: Literal["call", "put"]
    strike: float = Field(..., gt=0)
    expiration_ms: int = Field(..., description="Expiration as int64 ms since Unix epoch UTC")
    quantity: float = Field(..., description="Net quantity in *contracts* (negative for short)")
    multiplier: float = Field(100.0, gt=0, description="Contract multiplier (default 100)")
    entry_price: float = Field(..., ge=0, description="Per-share entry premium (always positive)")
    current_iv: float = Field(..., ge=0.0, le=5.0, description="Current implied volatility as decimal")
    leg_id: str | None = Field(None, description="Optional caller-supplied identifier echoed in the response")


Position = StockPosition | OptionPosition


class ScenarioGrid(BaseModel):
    """Defines the grid of scenarios to evaluate.

    A scenario is a (spot, time, iv-shift) triple. The default is a
    1×1×1 grid that evaluates current state only — the "live Greeks"
    use case. To compute a what-if surface, expand any of the three axes.
    """

    spot_shocks: list[float] = Field(
        default_factory=lambda: [0.0],
        description=(
            "Spot shocks as fractional moves from current spot "
            "(e.g. -0.05 = spot drops 5%, 0.0 = current, 0.05 = spot rises 5%)"
        ),
    )
    time_shifts_days: list[float] = Field(
        default_factory=lambda: [0.0],
        description="Time shifts in calendar days from now (0.0 = now, +7 = one week forward)",
    )
    iv_shifts: list[float] = Field(
        default_factory=lambda: [0.0],
        description="IV shifts as additive deltas (0.0 = current IV, 0.05 = +5 vol points)",
    )


class ScenarioRequest(BaseModel):
    """Request a portfolio scenario evaluation.

    `as_of_ms` is the evaluation timestamp; it determines TTM via
    (expiration_ms - as_of_ms). The .NET caller passes `now()` for live
    Greeks; passes a specific timestamp for what-if at a future date.
    """

    as_of_ms: int = Field(..., description="Evaluation timestamp (int64 ms UTC)")
    spot_price: float = Field(..., gt=0, description="Current underlying spot")
    risk_free_rate: float = Field(0.043, ge=0, le=0.5)
    dividend_yield: float = Field(0.0, ge=0, le=0.5)
    positions: list[Position] = Field(..., min_length=1, max_length=64)
    grid: ScenarioGrid = Field(default_factory=ScenarioGrid)

    @model_validator(mode="after")
    def _all_positions_same_underlying(self) -> ScenarioRequest:
        """Multi-underlying portfolios are out of scope for this endpoint;
        callers should split by underlying."""
        symbols = {p.symbol for p in self.positions}
        if len(symbols) > 1:
            raise ValueError(
                f"All positions must share one underlying for /portfolio/scenario; "
                f"got {sorted(symbols)}"
            )
        return self


class LegGreeks(BaseModel):
    """Greeks for a single leg at a single scenario point.

    Sign conventions match `bs_greeks.BSGreeks`:
    - delta: per share (option) or 1.0 (stock)
    - gamma: per share
    - theta: per calendar day
    - vega: per 1% IV move
    - rho: per 1% rate move

    Quantity-scaled aggregates are computed at the response level
    (`ScenarioPoint`); per-leg values here are *unscaled per-share* Greeks
    so the caller can re-aggregate however it wants.
    """

    leg_id: str | None = None
    instrument: Literal["stock", "option"]
    theoretical_price: float = Field(..., description="Theoretical per-share price at this scenario point")
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0


class ScenarioPoint(BaseModel):
    """Aggregate result for one (spot, time, iv) scenario point.

    Aggregates apply the position quantity and contract multiplier.
    For a 1-contract long ATM call with delta=0.5, the aggregate delta
    is `0.5 * 1 * 100 = 50`.
    """

    spot_shock: float
    time_shift_days: float
    iv_shift: float
    spot: float = Field(..., description="Effective spot at this scenario point")
    portfolio_pnl: float = Field(..., description="Aggregate P&L vs entry (sum over legs)")
    aggregate_delta: float = Field(0.0, description="Sum of (per-share delta * quantity * multiplier)")
    aggregate_gamma: float = 0.0
    aggregate_theta: float = 0.0
    aggregate_vega: float = 0.0
    aggregate_rho: float = 0.0
    legs: list[LegGreeks] = Field(default_factory=list)


class ScenarioResponse(BaseModel):
    """Full response: every scenario point with per-leg breakdown."""

    as_of_ms: int
    symbol: str
    spot_price: float
    risk_free_rate: float
    dividend_yield: float
    points: list[ScenarioPoint]
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Soft warnings (e.g., 'leg X expired before scenario time T, treated as intrinsic'). "
            "These do not invalidate results; they surface assumptions."
        ),
    )


class LiveGreeksRequest(BaseModel):
    """Convenience request for the common 'live Greeks at current state' case.

    Equivalent to ScenarioRequest with the default 1×1×1 grid.
    """

    as_of_ms: int
    spot_price: float = Field(..., gt=0)
    risk_free_rate: float = Field(0.043, ge=0, le=0.5)
    dividend_yield: float = Field(0.0, ge=0, le=0.5)
    positions: list[Position] = Field(..., min_length=1, max_length=64)
