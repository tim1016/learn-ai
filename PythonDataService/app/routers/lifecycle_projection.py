"""Read-only HTTP routes for the lifecycle Postgres projection."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.schemas.lifecycle_projection import (
    LifecycleSafetyTriageResponse,
    LifecycleTimelineResponse,
)
from app.services.lifecycle_projection_store import (
    LifecycleProjectionStore,
    LifecycleProjectionUnavailable,
    get_lifecycle_projection_store,
)

router = APIRouter(prefix="/api/lifecycle-projection", tags=["lifecycle-projection"])


@router.get("/timeline", response_model=LifecycleTimelineResponse)
async def get_lifecycle_timeline(
    account_id: str | None = Query(default=None, min_length=1, max_length=64),
    strategy_instance_id: str | None = Query(default=None, min_length=1, max_length=128),
    run_id: str | None = Query(default=None, min_length=1, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
    store: LifecycleProjectionStore = Depends(get_lifecycle_projection_store),
) -> LifecycleTimelineResponse:
    """Return a bounded timeline from the rebuildable projection."""

    try:
        rows = await store.select_timeline(
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            limit=limit,
        )
    except LifecycleProjectionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="lifecycle projection unavailable; use canonical file-backed status",
        ) from exc
    return LifecycleTimelineResponse(
        projection_available=True,
        canonical_fallback_required=False,
        rows=rows,
    )


@router.get("/safety-triage", response_model=LifecycleSafetyTriageResponse)
async def get_lifecycle_safety_triage(
    account_id: str | None = Query(default=None, min_length=1, max_length=64),
    status_filter: str | None = Query(default=None, alias="status", min_length=1, max_length=40),
    limit: int = Query(default=100, ge=1, le=500),
    store: LifecycleProjectionStore = Depends(get_lifecycle_projection_store),
) -> LifecycleSafetyTriageResponse:
    """Return warning/critical projection rows for fleet triage."""

    try:
        rows = await store.select_safety_triage(
            account_id=account_id,
            status=status_filter,
            limit=limit,
        )
    except LifecycleProjectionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="lifecycle projection unavailable; use canonical file-backed status",
        ) from exc
    return LifecycleSafetyTriageResponse(
        projection_available=True,
        canonical_fallback_required=False,
        rows=rows,
    )
