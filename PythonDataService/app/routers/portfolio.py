"""Portfolio scenario / live-Greeks endpoints.

Phase 2.1 of `docs/architecture/numerical-authority-migration-plan.md`.

Two endpoints:

- ``POST /api/portfolio/scenario`` — evaluate a portfolio across a grid of
  (spot, time, IV) scenarios. Returns per-leg Greeks + aggregates at every
  grid point. Used for what-if analysis.

- ``POST /api/portfolio/live-greeks`` — convenience wrapper that calls
  /scenario with a 1×1×1 grid (current state only). Used for the
  position-summary card path that the .NET ``PortfolioRiskService``
  currently fills with stale entry Greeks.

Both endpoints recompute Greeks from current state — never propagate from
stored entry-time values. That's the bug Phase 2 fixes.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status

from app.models.portfolio import (
    LiveGreeksRequest,
    ScenarioGrid,
    ScenarioRequest,
    ScenarioResponse,
)
from app.services.portfolio_scenario import evaluate_scenario

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/scenario", response_model=ScenarioResponse)
async def portfolio_scenario(request: ScenarioRequest) -> ScenarioResponse:
    """Evaluate a portfolio across a grid of scenario points."""
    try:
        n_points = len(request.grid.spot_shocks) * len(request.grid.time_shifts_days) * len(request.grid.iv_shifts)
        logger.info(
            "[Portfolio] Scenario for %s: %d positions × %d points",
            request.positions[0].symbol,
            len(request.positions),
            n_points,
        )
        # evaluate_scenario is CPU-bound (BS pricing per grid point per leg);
        # offload to a thread so concurrent requests / large grids don't stall
        # the event loop.
        result = await asyncio.to_thread(evaluate_scenario, request)
        if result.warnings:
            logger.info(
                "[Portfolio] Scenario completed with %d warning(s); first: %s",
                len(result.warnings),
                result.warnings[0],
            )
        return result
    except ValueError as e:
        # Validation errors (e.g. mixed underlyings) → 400.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error("[Portfolio] Scenario error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Portfolio scenario failed: {e!s}",
        )


@router.post("/live-greeks", response_model=ScenarioResponse)
async def portfolio_live_greeks(request: LiveGreeksRequest) -> ScenarioResponse:
    """Recompute Greeks for current portfolio state.

    Convenience wrapper around `/scenario` with a 1×1×1 (current-state-only)
    grid. The response shape is identical: a single ``ScenarioPoint`` with
    per-leg Greeks at the current spot/time/IV.
    """
    scenario_request = ScenarioRequest(
        as_of_ms=request.as_of_ms,
        spot_price=request.spot_price,
        risk_free_rate=request.risk_free_rate,
        dividend_yield=request.dividend_yield,
        positions=request.positions,
        grid=ScenarioGrid(),  # default 1×1×1
    )
    return await portfolio_scenario(scenario_request)
