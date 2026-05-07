"""Monte Carlo HTTP boundary.

Three endpoints under ``/api/research/strategy-runs/monte-carlo``:

  * ``POST   /``                — kick off an MC analysis, persist
    ``(config, result)``, and return both. Synchronous; fast for
    typical sim_count ≤ 5000 and trade counts in the dozens-to-hundreds.
  * ``GET    /{mc_id}``         — load a previously-persisted MC.
  * ``GET    /``                — list MCs, optionally filtered by
    ``parent_run_id`` / ``method`` / ``since_ms``.

Mounted *before* ``research_runs`` in ``app/main.py`` so the literal
``/monte-carlo`` segment wins against the parameterised
``GET /{run_id}`` route on the parent router.

Like ``walk_forward.py`` and ``research_runs.py``, this is a sync
handler — every operation is blocking I/O / NumPy CPU work and
FastAPI's threadpool is the right execution path. Async-def deferred
consistently with Phase A/C.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.research.monte_carlo import (
    MonteCarloAlreadyExistsError,
    MonteCarloConfig,
    MonteCarloCorruptError,
    MonteCarloNotFoundError,
    MonteCarloRequest,
    MonteCarloResult,
    list_monte_carlos,
    load_monte_carlo,
    run_monte_carlo,
    save_monte_carlo,
)
from app.research.monte_carlo.result import MonteCarloMethod
from app.routers.research_runs import get_artifacts_root

router = APIRouter()
logger = logging.getLogger(__name__)

# Server-side cap to prevent abuse / runaway compute. 10k simulations
# of a 200-trade run finishes in well under a second; higher values
# are usually a mis-typed config rather than a real research need.
_MAX_SIMULATION_COUNT = 10_000


# ---------------------------------------------------------------------------
# Request / response shapes.
# ---------------------------------------------------------------------------
class MonteCarloHttpRequest(BaseModel):
    """Inputs for ``POST /api/research/strategy-runs/monte-carlo``."""

    parent_run_id: str = Field(..., description="run_id of an existing persisted run")
    method: MonteCarloMethod = Field(
        ...,
        description=(
            "``reshuffle`` (permute trades, same multiset) or "
            "``resample`` (sample with replacement; allows projection)"
        ),
    )
    simulation_count: int = Field(1000, ge=1, le=_MAX_SIMULATION_COUNT)
    projection_trade_count: int = Field(
        0,
        ge=0,
        description=(
            "Length of each simulated path. 0 means 'use the parent's trade "
            "count'. >0 extends past the historical count — only valid for "
            "``resample``."
        ),
    )
    random_seed: int = Field(
        0,
        ge=0,
        description=(
            "Non-negative integer seed for ``numpy.random.default_rng``. "
            "Negative values raise inside NumPy and would surface as 500; "
            "rejecting them at Pydantic with 422 keeps the contract clean."
        ),
    )
    breach_thresholds: list[float] = Field(
        default_factory=list,
        description="Drawdown thresholds in [0, 1] to compute breach probabilities for",
    )


class MonteCarloResponse(BaseModel):
    config: MonteCarloConfig
    result: MonteCarloResult


class MonteCarloListResponse(BaseModel):
    monte_carlos: list[MonteCarloConfig]


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------
@router.post("", response_model=MonteCarloResponse)
def create_monte_carlo(
    request: MonteCarloHttpRequest,
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> MonteCarloResponse:
    """Run a Monte Carlo analysis, persist, and return ``(config, result)``."""
    mc_request = MonteCarloRequest(
        parent_run_id=request.parent_run_id,
        method=request.method,
        simulation_count=request.simulation_count,
        projection_trade_count=request.projection_trade_count,
        random_seed=request.random_seed,
        breach_thresholds=list(request.breach_thresholds),
    )

    config, result = run_monte_carlo(mc_request, artifacts_root=artifacts_root)

    try:
        save_monte_carlo(config, result, root=artifacts_root)
    except MonteCarloAlreadyExistsError as exc:
        logger.exception("[MC] monte_carlo_id collision: %s", config.monte_carlo_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"monte_carlo_id collision: {config.monte_carlo_id}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception(
            "[MC] failed to persist monte_carlo_id=%s",
            config.monte_carlo_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"monte-carlo completed but persistence failed: {exc}",
        ) from exc

    return MonteCarloResponse(config=config, result=result)


@router.get("/{mc_id}", response_model=MonteCarloResponse)
def get_monte_carlo(
    mc_id: str,
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> MonteCarloResponse:
    """Load a previously-persisted Monte Carlo."""
    try:
        config, result = load_monte_carlo(mc_id, root=artifacts_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except MonteCarloNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"monte-carlo not found: {mc_id}",
        ) from exc
    except MonteCarloCorruptError as exc:
        logger.exception("[MC] corrupt artifact for mc_id=%s", mc_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"monte-carlo artifact is corrupt: {exc}",
        ) from exc
    return MonteCarloResponse(config=config, result=result)


@router.get("", response_model=MonteCarloListResponse)
def list_monte_carlos_endpoint(
    parent_run_id: str | None = Query(None, description="Filter by parent run"),
    method: MonteCarloMethod | None = Query(None, description="reshuffle | resample"),
    since_ms: int | None = Query(
        None, ge=0, description="Only return MCs created at or after this ms-since-epoch"
    ),
    limit: int | None = Query(None, ge=1, description="Newest-first cap"),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> MonteCarloListResponse:
    """List persisted Monte Carlos, optionally filtered, newest first."""
    items = list_monte_carlos(
        root=artifacts_root,
        parent_run_id=parent_run_id,
        method=method,
        since_ms=since_ms,
        limit=limit,
    )
    return MonteCarloListResponse(monte_carlos=items)
