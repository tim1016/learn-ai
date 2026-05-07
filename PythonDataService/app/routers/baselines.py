"""Null-baselines HTTP boundary.

Three endpoints under ``/api/research/strategy-runs/baselines``:

  * ``POST   /``                 — kick off a baselines analysis,
    persist ``(config, result)``, return both. Synchronous; with
    ``random_ema_windows`` and ``sample_count`` ≤ 50 typical
    completion is single-digit seconds against synthetic data.
  * ``GET    /{baseline_id}``    — load a previously-persisted baseline.
  * ``GET    /``                 — list, filtered by ``parent_run_id`` /
    ``method`` / ``since_ms``.

Mounted **before** ``research_runs`` in ``app/main.py`` so the
literal ``/baselines`` segment wins against the parameterised
``GET /{run_id}`` route on the parent router.

Sync handler — same justification as ``walk_forward.py`` /
``monte_carlo.py``: every operation is blocking I/O / NumPy CPU work
and FastAPI's threadpool is the right execution path. Async-def
deferred consistently.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.research.baselines import (
    BaselineAlreadyExistsError,
    BaselineConfig,
    BaselineCorruptError,
    BaselineNotFoundError,
    BaselineRequest,
    BaselineResult,
    list_baselines,
    load_baseline,
    run_baselines,
    save_baseline,
)
from app.research.baselines.result import BaselineMethodLiteral
from app.routers.research_runs import (
    get_artifacts_root,
    get_data_source_factory,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Server-side cap. Each baseline is a full backtest; with the cap at
# 200 a worst-case ``random_ema_windows`` request finishes in tens
# of seconds against real LEAN data. Above-cap requests return 422.
_MAX_SAMPLE_COUNT = 200

# Per-method default sample counts, applied when the request omits
# ``sample_count``. ``buy_and_hold`` is deterministic and parameter-
# free — repeating it inflates the (1 + count) / (N + 1) p-value's
# denominator without adding statistical information. Random methods
# need ≥ ~30 for a stable null distribution.
_DEFAULT_SAMPLE_COUNT_BY_METHOD: dict[BaselineMethodLiteral, int] = {
    "buy_and_hold": 1,
    "random_ema_windows": 30,
}


# ---------------------------------------------------------------------------
# Request / response shapes.
# ---------------------------------------------------------------------------
class BaselineHttpRequest(BaseModel):
    """Inputs for ``POST /api/research/strategy-runs/baselines``."""

    parent_run_id: str = Field(..., description="run_id of an existing persisted run")
    method: BaselineMethodLiteral = Field(
        ...,
        description=(
            "``buy_and_hold`` (single trade, hold the window) or "
            "``random_ema_windows`` (sample EMA fast/slow pairs from a "
            "bounded family — tests the parent's specific window choice "
            "against random alternatives)"
        ),
    )
    sample_count: int | None = Field(
        None,
        ge=1,
        le=_MAX_SAMPLE_COUNT,
        description=(
            "Number of baseline runs. Default is method-dependent: "
            "``buy_and_hold`` defaults to 1 (deterministic, parameter-"
            "free — replicates would only duplicate work and inflate "
            "the small-sample p-value's N denominator); "
            "``random_ema_windows`` defaults to 30, the smallest count "
            "that gives a stable null distribution. Explicit values are "
            "honoured for both methods."
        ),
    )
    random_seed: int = Field(
        0,
        ge=0,
        description="Non-negative integer seed for the parameter sampler",
    )
    fast_range: tuple[int, int] = Field(
        (3, 12),
        description="``random_ema_windows``: inclusive ``(lo, hi)`` for fast EMA",
    )
    slow_range: tuple[int, int] = Field(
        (10, 30),
        description="``random_ema_windows``: inclusive ``(lo, hi)`` for slow EMA",
    )


class BaselineResponse(BaseModel):
    config: BaselineConfig
    result: BaselineResult


class BaselineListResponse(BaseModel):
    baselines: list[BaselineConfig]


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------
@router.post("", response_model=BaselineResponse)
def create_baselines(
    request: BaselineHttpRequest,
    data_source_factory=Depends(get_data_source_factory),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> BaselineResponse:
    """Run a null-baseline analysis, persist, return ``(config, result)``."""
    sample_count = (
        request.sample_count
        if request.sample_count is not None
        else _DEFAULT_SAMPLE_COUNT_BY_METHOD[request.method]
    )
    baseline_request = BaselineRequest(
        parent_run_id=request.parent_run_id,
        method=request.method,
        sample_count=sample_count,
        random_seed=request.random_seed,
        fast_range=tuple(request.fast_range),
        slow_range=tuple(request.slow_range),
    )

    config, result = run_baselines(
        baseline_request,
        data_source_factory=data_source_factory,
        artifacts_root=artifacts_root,
    )

    try:
        save_baseline(config, result, root=artifacts_root)
    except BaselineAlreadyExistsError as exc:
        logger.exception("[BASELINES] baseline_id collision: %s", config.baseline_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"baseline_id collision: {config.baseline_id}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception(
            "[BASELINES] failed to persist baseline_id=%s",
            config.baseline_id,
        )
        # Don't interpolate ``exc`` into the response detail —
        # ``OSError``/``PermissionError`` from the storage layer
        # include the resolved on-disk path, which is server
        # infrastructure the client should not see. The full trace
        # is in the structured log above.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="baselines completed but persistence failed; see server logs",
        ) from exc

    return BaselineResponse(config=config, result=result)


@router.get("/{baseline_id}", response_model=BaselineResponse)
def get_baseline(
    baseline_id: str,
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> BaselineResponse:
    """Load a previously-persisted baselines run."""
    try:
        config, result = load_baseline(baseline_id, root=artifacts_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except BaselineNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"baseline not found: {baseline_id}",
        ) from exc
    except BaselineCorruptError as exc:
        logger.exception("[BASELINES] corrupt artifact for baseline_id=%s", baseline_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"baseline artifact is corrupt: {exc}",
        ) from exc
    return BaselineResponse(config=config, result=result)


@router.get("", response_model=BaselineListResponse)
def list_baselines_endpoint(
    parent_run_id: str | None = Query(None, description="Filter by parent run"),
    method: BaselineMethodLiteral | None = Query(
        None, description="buy_and_hold | random_ema_windows"
    ),
    since_ms: int | None = Query(
        None, ge=0, description="Only return baselines created at or after this ms"
    ),
    limit: int | None = Query(None, ge=1, description="Newest-first cap"),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> BaselineListResponse:
    """List persisted baselines, optionally filtered, newest first."""
    items = list_baselines(
        root=artifacts_root,
        parent_run_id=parent_run_id,
        method=method,
        since_ms=since_ms,
        limit=limit,
    )
    return BaselineListResponse(baselines=items)
