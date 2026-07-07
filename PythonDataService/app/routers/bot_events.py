"""HTTP backfill surface for the authored bot-event stream."""

from __future__ import annotations

import logging
import re
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.broker.ibkr.config import get_settings
from app.schemas.bot_events import BotEventPage
from app.services.bot_event_stream_service import (
    BotEventStreamService,
    BotEventStreamUnavailableError,
    get_bot_event_stream_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bot-events"])

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,127}$")


def _validate_run_id_literal(run_id: str) -> str:
    if not run_id or run_id != run_id.strip():
        raise ValueError("run_id must be non-empty with no surrounding whitespace")
    if run_id in (".", ".."):
        raise ValueError("run_id must not be a path segment ('.' or '..')")
    if "\x00" in run_id or "/" in run_id or "\\" in run_id:
        raise ValueError("run_id must not contain path separators or NUL bytes")
    if FsPath(run_id).is_absolute():
        raise ValueError("run_id must not be an absolute path")
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(f"Invalid run_id format: {run_id!r}")
    return run_id


def _find_run_dir(root: FsPath, run_id: str) -> FsPath | None:
    safe = _validate_run_id_literal(run_id)
    try:
        root_resolved = root.resolve()
        for candidate in root_resolved.iterdir():
            if candidate.name == safe and candidate.is_dir():
                return candidate
    except OSError:
        return None
    return None


@router.get(
    "/{run_id}/bot-events",
    response_model=BotEventPage,
    summary="Paginated authored bot-event stream for a live run",
)
async def bot_event_backfill(
    run_id: Annotated[str, Path(min_length=1)],
    after_seq: Annotated[
        int,
        Query(
            ge=0,
            description=(
                "Return authored rows with ``seq > after_seq``. To paginate, "
                "pass the previous response's ``next_seq`` verbatim."
            ),
        ),
    ] = 0,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=500,
            description="Max authored rows per page.",
        ),
    ] = 100,
    service: BotEventStreamService = Depends(get_bot_event_stream_service),
) -> BotEventPage:
    root = FsPath(get_settings().live_runs_root)
    try:
        run_dir = _find_run_dir(root, run_id)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"Invalid run_id: {run_id!r}"
        ) from exc
    if run_dir is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found")

    try:
        page = service.backfill_run(
            run_dir=run_dir,
            after_seq=after_seq,
            limit=limit,
        )
    except BotEventStreamUnavailableError as exc:
        logger.warning(
            "Could not project bot-event stream",
            extra={"run_id": run_id, "run_dir": str(run_dir)},
            exc_info=True,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bot-event stream history cannot be projected",
        ) from exc
    return BotEventPage(rows=page.rows, next_seq=page.next_seq)
