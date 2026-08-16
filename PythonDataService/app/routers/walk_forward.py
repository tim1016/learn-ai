"""Walk-forward HTTP boundary.

Three endpoints under ``/api/research/strategy-runs/walk-forward``:

  * ``POST   /``                 — kick off a walk-forward analysis,
    persist the config + result, and return them. Blocking engine and
    filesystem work is offloaded from the event loop. Large versioned
    protocols use the jobs boundary in ``app/routers/jobs.py`` instead.
  * ``GET    /{wf_id}``          — load a previously-persisted WF.
  * ``GET    /``                 — list WFs, optionally filtered by
    parent, spec hash, protocol identity, or creation time.

Mounted *before* ``research_runs`` in ``app/main.py`` so the literal
``/walk-forward`` segment wins against the parameterised
``GET /{run_id}`` route on the parent router.

GraphQL passthrough deliberately deferred until a UI consumer needs it.
"""

from __future__ import annotations

import logging
import os
from functools import partial
from pathlib import Path
from typing import Any

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.engine.strategy.spec import StrategySpec
from app.research.runs import (
    RunAlreadyExistsError,
    RunCorruptError,
    RunNotFoundError,
    load_run,
)
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

    model_config = ConfigDict(extra="forbid")

    spec: StrategySpec = Field(..., description="Validated StrategySpec")
    start_ms: int = Field(..., ge=0, strict=True, description="UTC epoch milliseconds")
    end_ms: int = Field(..., ge=0, strict=True, description="UTC epoch milliseconds")
    initial_cash: float = Field(
        100_000.0,
        ge=0,
        allow_inf_nan=False,
        strict=True,
    )
    fill_mode: str = Field("signal_bar_close")
    commission_per_order: float = Field(
        0.0,
        ge=0,
        allow_inf_nan=False,
        strict=True,
    )
    slippage_per_share: float = Field(
        0.0,
        ge=0,
        allow_inf_nan=False,
        strict=True,
    )
    random_seed: int = Field(0, strict=True)
    parent_run_id: str | None = Field(None, description="Optional baseline run this WF is derived from")
    split_policy: SplitPolicySpec = Field(..., description="``kind`` discriminator + policy-specific fields")


class WalkForwardResponse(BaseModel):
    config: WalkForwardConfig
    result: WalkForwardResult


class WalkForwardListResponse(BaseModel):
    walk_forwards: list[WalkForwardConfig]


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
async def create_walk_forward(
    request: WalkForwardHttpRequest,
    data_source_factory=Depends(get_data_source_factory),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> WalkForwardResponse:
    """Run a walk-forward analysis, persist, and return ``(config, result)``."""
    _validate_fill_mode(request.fill_mode)
    if request.start_ms >= request.end_ms:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"start_ms must be strictly before end_ms (got start={request.start_ms}, end={request.end_ms})"),
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
        start_ms=request.start_ms,
        end_ms=request.end_ms,
        split_policy=split_policy,
        initial_cash=request.initial_cash,
        fill_mode=request.fill_mode,
        commission_per_order=request.commission_per_order,
        slippage_per_share=request.slippage_per_share,
        random_seed=request.random_seed,
        parent_run_id=request.parent_run_id,
    )
    return await to_thread.run_sync(
        partial(
            _run_and_persist_walk_forward,
            wf_request,
            data_source_factory=data_source_factory,
            artifacts_root=artifacts_root,
            parent_run_id=request.parent_run_id,
        )
    )


@router.get("/{wf_id}", response_model=WalkForwardResponse)
async def get_walk_forward(
    wf_id: str,
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> WalkForwardResponse:
    """Load a previously-persisted walk-forward."""
    try:
        config, result = await to_thread.run_sync(partial(load_walk_forward, wf_id, root=artifacts_root))
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
async def list_walk_forwards_endpoint(
    parent_run_id: str | None = Query(None, description="Filter by baseline run"),
    spec_hash: str | None = Query(None, description="Filter by ``strategy_spec_hash``"),
    protocol_id: str | None = Query(None, description="Filter by exact protocol identity"),
    protocol_version: str | None = Query(None, description="Filter by exact protocol version"),
    since_ms: int | None = Query(None, ge=0, description="Only return WFs created at or after this ms-since-epoch"),
    limit: int | None = Query(None, ge=1, description="Newest-first cap"),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> WalkForwardListResponse:
    """List persisted walk-forwards, optionally filtered, newest first."""
    if (protocol_id is None) != (protocol_version is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="protocol_id and protocol_version filters must be provided together",
        )
    items = await to_thread.run_sync(
        partial(
            list_walk_forwards,
            root=artifacts_root,
            parent_run_id=parent_run_id,
            spec_hash=spec_hash,
            protocol_id=protocol_id,
            protocol_version=protocol_version,
            since_ms=since_ms,
            limit=limit,
        )
    )
    return WalkForwardListResponse(walk_forwards=items)


def _run_and_persist_walk_forward(
    request: WalkForwardRequest,
    *,
    data_source_factory: Any,
    artifacts_root: Path | None,
    parent_run_id: str | None,
) -> WalkForwardResponse:
    """Execute blocking engine and artifact work inside a worker thread."""
    parent_sharpe = _load_parent_sharpe(
        parent_run_id,
        artifacts_root=artifacts_root,
    )
    config, result = run_walk_forward(
        request,
        data_source_factory=data_source_factory,
        artifacts_root=artifacts_root,
        parent_sharpe=parent_sharpe,
        data_root_revision=os.environ.get("LEAN_DATA_ROOT_REVISION") or None,
    )

    try:
        save_walk_forward(config, result, root=artifacts_root)
    except (RunAlreadyExistsError, WalkForwardAlreadyExistsError) as exc:
        logger.exception("[WF] walk_forward_id collision: %s", config.walk_forward_id)
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
