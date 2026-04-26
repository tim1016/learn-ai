"""Portfolio scenario / live-Greeks computation.

Phase 2.1 of `docs/architecture/numerical-authority-migration-plan.md`:
Python becomes the canonical owner of portfolio scenario math. The
`.NET` services (`PortfolioRiskService.cs`, `PortfolioValuationService.cs`)
become passthroughs.

Math authority: `app/services/bs_greeks.py` for option pricing and Greeks
(closed-form, continuous-time; works at any TTM including 0DTE). See
`docs/architecture/options-math-authorities.md`.

Key decisions:
- Recompute Greeks at every scenario point. Do **not** shock-propagate
  from stored entry Greeks — that's the stale-Greek bug Phase 2 fixes.
- Stocks contribute trivially: delta=1, gamma=0, theta=0, vega=0,
  P&L = quantity * (effective_spot - entry_price).
- Options expired before the scenario time evaluate to intrinsic value
  with all Greeks = 0, and surface a warning (so the caller knows the
  scenario crossed expiration).
- Sub-second TTM is allowed (0DTE intraday). Negative TTM treated as
  expired.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from app.models.portfolio import (
    LegGreeks,
    OptionPosition,
    Position,
    ScenarioPoint,
    ScenarioRequest,
    ScenarioResponse,
    StockPosition,
)
from app.services.bs_greeks import black_scholes_greeks, bs_european_price

logger = logging.getLogger(__name__)

MS_PER_DAY: float = 86_400_000.0
DAYS_PER_YEAR: float = 365.0


def evaluate_scenario(request: ScenarioRequest) -> ScenarioResponse:
    """Evaluate a portfolio scenario over the request's grid.

    Every (spot_shock, time_shift, iv_shift) combination produces one
    ``ScenarioPoint``. Per-leg Greeks at each point are recomputed from
    current state — never propagated from entry-time stored values.
    """
    grid = request.grid
    warnings: list[str] = []

    points: list[ScenarioPoint] = []
    for spot_shock in grid.spot_shocks:
        for time_shift_days in grid.time_shifts_days:
            for iv_shift in grid.iv_shifts:
                point = _evaluate_point(
                    request=request,
                    spot_shock=spot_shock,
                    time_shift_days=time_shift_days,
                    iv_shift=iv_shift,
                    warnings=warnings,
                )
                points.append(point)

    return ScenarioResponse(
        as_of_ms=request.as_of_ms,
        symbol=request.positions[0].symbol,
        spot_price=request.spot_price,
        risk_free_rate=request.risk_free_rate,
        dividend_yield=request.dividend_yield,
        points=points,
        warnings=_dedup_preserving_order(warnings),
    )


def _evaluate_point(
    *,
    request: ScenarioRequest,
    spot_shock: float,
    time_shift_days: float,
    iv_shift: float,
    warnings: list[str],
) -> ScenarioPoint:
    """Evaluate one scenario point: per-leg Greeks + portfolio aggregates."""
    effective_spot = request.spot_price * (1.0 + spot_shock)
    scenario_ms = request.as_of_ms + int(time_shift_days * MS_PER_DAY)

    leg_results: list[LegGreeks] = []
    portfolio_pnl = 0.0
    agg_delta = 0.0
    agg_gamma = 0.0
    agg_theta = 0.0
    agg_vega = 0.0
    agg_rho = 0.0

    for position in request.positions:
        leg = _evaluate_leg(
            position=position,
            effective_spot=effective_spot,
            scenario_ms=scenario_ms,
            iv_shift=iv_shift,
            risk_free_rate=request.risk_free_rate,
            dividend_yield=request.dividend_yield,
            warnings=warnings,
        )
        leg_results.append(leg)

        # Aggregate. Stocks: multiplier=1. Options: multiplier=100 by default.
        multiplier = (
            position.multiplier if isinstance(position, OptionPosition) else 1.0
        )
        qty = position.quantity
        scaled = qty * multiplier

        # Per-share P&L:
        #   stock: effective_spot - entry_price
        #   option: theoretical - entry_premium
        per_share_pnl = leg.theoretical_price - position.entry_price
        portfolio_pnl += per_share_pnl * scaled

        agg_delta += leg.delta * scaled
        agg_gamma += leg.gamma * scaled
        agg_theta += leg.theta * scaled
        agg_vega += leg.vega * scaled
        agg_rho += leg.rho * scaled

    return ScenarioPoint(
        spot_shock=spot_shock,
        time_shift_days=time_shift_days,
        iv_shift=iv_shift,
        spot=effective_spot,
        portfolio_pnl=portfolio_pnl,
        aggregate_delta=agg_delta,
        aggregate_gamma=agg_gamma,
        aggregate_theta=agg_theta,
        aggregate_vega=agg_vega,
        aggregate_rho=agg_rho,
        legs=leg_results,
    )


def _evaluate_leg(
    *,
    position: Position,
    effective_spot: float,
    scenario_ms: int,
    iv_shift: float,
    risk_free_rate: float,
    dividend_yield: float,
    warnings: list[str],
) -> LegGreeks:
    """Recompute per-share theoretical price + Greeks for one position."""
    if isinstance(position, StockPosition):
        return LegGreeks(
            leg_id=position.leg_id,
            instrument="stock",
            theoretical_price=effective_spot,
            delta=1.0,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            rho=0.0,
        )

    # Option leg: recompute from current state.
    ttm_ms = position.expiration_ms - scenario_ms
    ttm_years = ttm_ms / MS_PER_DAY / DAYS_PER_YEAR
    sigma = max(position.current_iv + iv_shift, 0.0)

    if ttm_years <= 0.0 or sigma <= 0.0:
        # Expired, or vol pushed non-positive: intrinsic only, no Greeks.
        intrinsic = _intrinsic(
            spot=effective_spot,
            strike=position.strike,
            is_call=position.option_type == "call",
        )
        warnings.append(
            f"leg {position.leg_id or position.symbol+str(position.strike)}: "
            f"ttm_years={ttm_years:.6f}, sigma={sigma:.4f} → intrinsic-only valuation"
        )
        return LegGreeks(
            leg_id=position.leg_id,
            instrument="option",
            theoretical_price=intrinsic,
            delta=0.0,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            rho=0.0,
        )

    is_call = position.option_type == "call"
    price = bs_european_price(
        spot=effective_spot,
        strike=position.strike,
        ttm_years=ttm_years,
        rate=risk_free_rate,
        volatility=sigma,
        is_call=is_call,
        dividend=dividend_yield,
    )
    greeks = black_scholes_greeks(
        spot=effective_spot,
        strike=position.strike,
        ttm_years=ttm_years,
        volatility=sigma,
        rate=risk_free_rate,
        dividend=dividend_yield,
        is_call=is_call,
    )

    return LegGreeks(
        leg_id=position.leg_id,
        instrument="option",
        theoretical_price=price,
        delta=greeks.delta,
        gamma=greeks.gamma,
        theta=greeks.theta,
        vega=greeks.vega,
        rho=greeks.rho,
    )


def _intrinsic(*, spot: float, strike: float, is_call: bool) -> float:
    """Per-share intrinsic value for an expired (or zero-vol) option."""
    if is_call:
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _dedup_preserving_order(items: Iterable[str]) -> list[str]:
    """Drop duplicate warnings while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
