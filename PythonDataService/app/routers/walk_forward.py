"""Walk-forward HTTP boundary.

Three endpoints under ``/api/research/strategy-runs/walk-forward``:

  * ``POST   /``                 — kick off a walk-forward analysis,
    persist the config + result, and return them. Synchronous: blocks
    until every fold finishes; for grids of >10 folds consider
    backgrounding via ``app/routers/jobs.py`` (deferred).
  * ``GET    /{wf_id}``          — load a previously-persisted WF.
  * ``GET    /``                 — list WFs, optionally filtered by
    ``parent_run_id`` / ``spec_hash`` / ``since_ms``.

Mounted *before* ``research_runs`` in ``app/main.py`` so the literal
``/walk-forward`` segment wins against the parameterised
``GET /{run_id}`` route on the parent router.

Like ``research_runs.py`` this is a sync handler — every operation is
blocking I/O and FastAPI's threadpool is the right place for it.
GraphQL passthrough deliberately deferred until a UI consumer needs it.
"""

from __future__ import annotations

import logging
import os
from datetime import date as Date
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.engine.strategy.spec import StrategySpec
from app.research.runs import RunCorruptError, RunNotFoundError, load_run
from app.research.walk_forward import (
    SplitPolicySpec,
    WalkForwardAlreadyExistsError,
    WalkForwardConfig,
    WalkForwardCorruptError,
    WalkForwardNotFoundError,
    WalkForwardRequest,
    WalkForwardResult,
    build_split_policy,
    list_walk_forwards,
    load_walk_forward,
    run_walk_forward,
    save_walk_forward,
)
from app.routers.research_runs import (
    get_artifacts_root,
    get_data_source_factory,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response shapes.
# ---------------------------------------------------------------------------
class WalkForwardHttpRequest(BaseModel):
    """Inputs for ``POST /api/research/strategy-runs/walk-forward``."""

    spec: StrategySpec = Field(..., description="Validated StrategySpec")
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    initial_cash: float = Field(100_000.0, ge=0)
    fill_mode: str = Field("signal_bar_close")
    commission_per_order: float = Field(0.0, ge=0)
    slippage_per_share: float = Field(0.0, ge=0)
    random_seed: int = 0
    parent_run_id: str | None = Field(
        None, description="Optional baseline run this WF is derived from"
    )
    split_policy: SplitPolicySpec = Field(
        ..., description="``kind`` discriminator + policy-specific fields"
    )


class WalkForwardResponse(BaseModel):
    config: WalkForwardConfig
    result: WalkForwardResult


class WalkForwardListResponse(BaseModel):
    walk_forwards: list[WalkForwardConfig]


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _parse_date(s: str, field: str) -> Date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} must be YYYY-MM-DD: {s!r}",
        ) from exc


def _validate_fill_mode(s: str) -> None:
    norm = s.lower().replace("-", "_")
    if norm not in {"signal_bar_close", "next_bar_open"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown fill_mode {s!r} — expected signal_bar_close or next_bar_open",
        )


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------
@router.post("", response_model=WalkForwardResponse)
def create_walk_forward(
    request: WalkForwardHttpRequest,
    data_source_factory=Depends(get_data_source_factory),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> WalkForwardResponse:
    """Run a walk-forward analysis, persist, and return ``(config, result)``."""
    start_d = _parse_date(request.start_date, "start_date")
    end_d = _parse_date(request.end_date, "end_date")
    _validate_fill_mode(request.fill_mode)
    if start_d >= end_d:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"start_date must be strictly before end_date "
                f"(got start={start_d.isoformat()}, end={end_d.isoformat()})"
            ),
        )

    # Build the typed split policy from the JSON discriminator.
    try:
        split_policy = build_split_policy(request.split_policy.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    wf_request = WalkForwardRequest(
        spec=request.spec,
        start_date=request.start_date,
        end_date=request.end_date,
        split_policy=split_policy,
        initial_cash=request.initial_cash,
        fill_mode=request.fill_mode,
        commission_per_order=request.commission_per_order,
        slippage_per_share=request.slippage_per_share,
        random_seed=request.random_seed,
        parent_run_id=request.parent_run_id,
    )
    parent_sharpe = _load_parent_sharpe(
        request.parent_run_id,
        artifacts_root=artifacts_root,
    )

    config, result = run_walk_forward(
        wf_request,
        data_source_factory=data_source_factory,
        artifacts_root=artifacts_root,
        parent_sharpe=parent_sharpe,
        # Resolve the data root revision via the same env-driven path
        # the runner uses by default — ``None`` triggers the default
        # ``resolve_data_root_revision()`` lookup inside
        # ``run_strategy_spec`` per fold.
        data_root_revision=os.environ.get("LEAN_DATA_ROOT_REVISION") or None,
    )

    try:
        save_walk_forward(config, result, root=artifacts_root)
    except WalkForwardAlreadyExistsError as exc:
        logger.exception(
            "[WF] walk_forward_id collision: %s", config.walk_forward_id
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"walk_forward_id collision: {config.walk_forward_id}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception(
            "[WF] failed to persist walk_forward_id=%s",
            config.walk_forward_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"walk-forward completed but persistence failed: {exc}",
        ) from exc

    return WalkForwardResponse(config=config, result=result)


@router.get("/{wf_id}", response_model=WalkForwardResponse)
def get_walk_forward(
    wf_id: str,
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> WalkForwardResponse:
    """Load a previously-persisted walk-forward."""
    try:
        config, result = load_walk_forward(wf_id, root=artifacts_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except WalkForwardNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"walk-forward not found: {wf_id}",
        ) from exc
    except WalkForwardCorruptError as exc:
        logger.exception("[WF] corrupt artifact for wf_id=%s", wf_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"walk-forward artifact is corrupt: {exc}",
        ) from exc
    return WalkForwardResponse(config=config, result=result)


@router.get("", response_model=WalkForwardListResponse)
def list_walk_forwards_endpoint(
    parent_run_id: str | None = Query(None, description="Filter by baseline run"),
    spec_hash: str | None = Query(None, description="Filter by ``strategy_spec_hash``"),
    since_ms: int | None = Query(
        None, ge=0, description="Only return WFs created at or after this ms-since-epoch"
    ),
    limit: int | None = Query(None, ge=1, description="Newest-first cap"),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> WalkForwardListResponse:
    """List persisted walk-forwards, optionally filtered, newest first."""
    items = list_walk_forwards(
        root=artifacts_root,
        parent_run_id=parent_run_id,
        spec_hash=spec_hash,
        since_ms=since_ms,
        limit=limit,
    )
    return WalkForwardListResponse(walk_forwards=items)


def _load_parent_sharpe(
    parent_run_id: str | None,
    *,
    artifacts_root: Path | None,
) -> float | None:
    if not parent_run_id:
        return None
    try:
        _, parent_result = load_run(parent_run_id, root=artifacts_root)
    except (ValueError, RunNotFoundError, RunCorruptError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"parent_run_id is not a readable strategy run: {parent_run_id}",
        ) from exc
    return parent_result.metrics.sharpe_ratio
