"""The Research History over REST: ``GET /api/research/history``.

One page of the typed index over every research store (``app.research.history``). Transport only: the filters are
validated here and handed to the merge, which pages the union exactly.

A store that fails is named in ``sources`` and flagged ``partial``; when every store asked fails the answer is a 503, never
an empty page. Reading never runs a study: every source only reads what its store already holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.research.history.contract import HistoryQuery, HistorySource
from app.research.history.merge import HistoryUnavailableError, read_history
from app.research.history.registry import default_history_sources
from app.schemas.research_history import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE,
    MAX_PAGE_SIZE,
    HistoryStatus,
    HistoryType,
    ResearchHistoryPage,
    ResearchHistoryRefusal,
    ResearchHistoryRefusalBody,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS

router = APIRouter()

SYMBOL_MAX_LENGTH = 50


def get_history_sources() -> Sequence[HistorySource]:
    """The registered stores. Tests override this to stand in for them."""
    return default_history_sources()


@router.get(
    "",
    response_model=ResearchHistoryPage,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ResearchHistoryRefusalBody}},
)
async def list_research_history(
    sources: Annotated[Sequence[HistorySource], Depends(get_history_sources)],
    record_type: Annotated[HistoryType | None, Query(alias="type", description="Only this kind of record.")] = None,
    symbol: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=SYMBOL_MAX_LENGTH,
            pattern=r"^\S+$",
            description="Only records that covered this symbol; case does not matter.",
        ),
    ] = None,
    record_status: Annotated[HistoryStatus | None, Query(alias="status", description="Only records that ended this way.")] = None,
    created_from_ms: Annotated[
        int | None, Query(ge=0, le=MAX_TIMESTAMP_MS, description="Only records created at or after this instant.")
    ] = None,
    created_before_ms: Annotated[
        int | None, Query(ge=0, le=MAX_TIMESTAMP_MS, description="Only records created before this instant.")
    ] = None,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ResearchHistoryPage:
    """One page of the merged history, newest first, with the count the pages add up to."""
    if created_from_ms is not None and created_before_ms is not None and created_from_ms >= created_before_ms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="created_from_ms must be before created_before_ms",
        )
    query = HistoryQuery(
        symbol=None if symbol is None else symbol.upper(),
        status=record_status,
        created_from_ms=created_from_ms,
        created_before_ms=created_before_ms,
    )
    try:
        return await read_history(sources, query, type_filter=record_type, page=page, page_size=page_size)
    except HistoryUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ResearchHistoryRefusal(
                code="RESEARCH_HISTORY_UNAVAILABLE",
                message="Every research store that was asked failed, so there is no history to show. Try again once they are back.",
                sources=exc.sources,
            ).model_dump(),
        ) from exc
