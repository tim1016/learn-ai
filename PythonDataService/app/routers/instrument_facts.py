"""GET /api/markets/instrument-facts: the header facts of a bounded batch of instruments (#3502).

One read answers an instrument's header or one page of the Markets list. It opens no live subscription and makes at
most one vendor request however many symbols it names (``MAX_FACT_SYMBOLS``).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.lean_sidecar.trading_calendar import NoSessionError
from app.schemas.instrument_facts import MAX_FACT_SYMBOLS, InstrumentFactsResponse
from app.services.instrument_facts_service import (
    CLOSES_ADJUSTMENT,
    InstrumentFactsService,
    InvalidSymbolsError,
    parse_symbols,
)
from app.services.instrument_facts_sources import (
    AggregatorLiveQuoteSource,
    LakeCloseSource,
    PolygonSnapshotSource,
    ServiceArchiveSource,
)
from app.services.polygon_client import PolygonClientService

router = APIRouter()


@lru_cache(maxsize=1)
def _polygon_client() -> PolygonClientService:
    return PolygonClientService()


def get_instrument_facts_service(request: Request) -> InstrumentFactsService:
    """The service over the process's real sources, as a dependency so a transport test can replace it."""
    # Imported here: the aggregator is a module-level singleton, and a process that never asks for facts need not build it.
    from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

    return InstrumentFactsService(
        live=AggregatorLiveQuoteSource(LIVE_BAR_AGGREGATOR),
        snapshots=PolygonSnapshotSource(_polygon_client()),
        closes=LakeCloseSource(CLOSES_ADJUSTMENT),
        archive=ServiceArchiveSource(getattr(request.app.state, "option_archive_service", None)),
    )


FactsServiceDep = Annotated[InstrumentFactsService, Depends(get_instrument_facts_service)]


@router.get("/instrument-facts", response_model=InstrumentFactsResponse)
async def get_instrument_facts(
    service: FactsServiceDep,
    symbols: Annotated[
        str,
        Query(
            min_length=1,
            max_length=MAX_FACT_SYMBOLS * 10,
            description=f"Comma-separated distinct US-equity symbols, at most {MAX_FACT_SYMBOLS}.",
        ),
    ],
) -> InstrumentFactsResponse:
    """Each symbol's latest price with its basis and as-of time, its day move and its options-archive status.

    A price is the freshest source that answers (live, delayed snapshot, last completed close) and names which. A
    source that failed leaves only its own fields ``unavailable``; the response is still 200 and every other
    instrument and fact in it stands.
    """
    try:
        wanted = parse_symbols(symbols)
    except InvalidSymbolsError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    try:
        return await service.facts(wanted)
    except NoSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The trading calendar could not name the last completed session, so no close can be stated.",
        ) from exc
