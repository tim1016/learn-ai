"""Read-only HTTP boundary for persisted Exhaustive Run evidence."""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import Path as ApiPath

from app.research.exhaustive_run.errors import (
    ExhaustiveRunCorruptError,
    ExhaustiveRunNotFoundError,
)
from app.research.exhaustive_run.storage import (
    load_exhaustive_run,
    load_latest_for_walk_forward,
)
from app.routers.research_runs import get_artifacts_root
from app.schemas.exhaustive_run import ExhaustiveRunResponse

router = APIRouter()
logger = logging.getLogger(__name__)
_ID_PATTERN = r"^[0-9a-f]{32}$"


@router.get(
    "/by-walk-forward/{walk_forward_id}",
    response_model=ExhaustiveRunResponse,
)
async def get_latest_exhaustive_run_for_walk_forward(
    walk_forward_id: str = ApiPath(pattern=_ID_PATTERN),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> ExhaustiveRunResponse:
    """Load the newest Exhaustive Run derived from one walk-forward receipt."""
    try:
        config, result = await to_thread.run_sync(
            partial(
                load_latest_for_walk_forward,
                walk_forward_id,
                root=artifacts_root,
            )
        )
    except ExhaustiveRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exhaustive Run not found for walk-forward: {walk_forward_id}",
        ) from exc
    except ExhaustiveRunCorruptError as exc:
        logger.exception(
            "[EXHAUSTIVE] corrupt artifact for source walk-forward=%s",
            walk_forward_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Exhaustive Run artifact is corrupt: {exc}",
        ) from exc
    return ExhaustiveRunResponse(config=config, result=result)


@router.get("/{exhaustive_run_id}", response_model=ExhaustiveRunResponse)
async def get_exhaustive_run(
    exhaustive_run_id: str = ApiPath(pattern=_ID_PATTERN),
    artifacts_root: Path | None = Depends(get_artifacts_root),
) -> ExhaustiveRunResponse:
    """Load one Exhaustive Run by its immutable artifact id."""
    try:
        config, result = await to_thread.run_sync(
            partial(load_exhaustive_run, exhaustive_run_id, root=artifacts_root)
        )
    except ExhaustiveRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exhaustive Run not found: {exhaustive_run_id}",
        ) from exc
    except ExhaustiveRunCorruptError as exc:
        logger.exception(
            "[EXHAUSTIVE] corrupt artifact id=%s",
            exhaustive_run_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Exhaustive Run artifact is corrupt: {exc}",
        ) from exc
    return ExhaustiveRunResponse(config=config, result=result)
