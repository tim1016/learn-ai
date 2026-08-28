"""HTTP routes for the data lake.

POST /api/data-lake/ensure-data — invokes the in-process ensure_data() function.
GET /api/data-lake/coverage, /artifacts/{id}, /storage-summary — read-only
projections of the catalog for the Observatory UI (issue #1835). Thin: no new
state, no writes.

Behind the DATA_LAKE_ENABLED feature flag; routes return 404 when the flag is off
(via main.py wiring, not this module).

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.3
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException

from app.data_lake import catalog_client
from app.data_lake.catalog_client import session_open_ms_or_none
from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import (
    MAX_TRADING_RANGE_DAYS,
    ArtifactDetail,
    CoverageDay,
    CoverageResponse,
    DataAvailabilityResult,
    DataRunSpec,
    PriceAdjustmentMode,
    StorageSummaryResponse,
    trading_range_span_days,
)
from app.lean_sidecar.trading_calendar import expected_sessions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/data-lake", tags=["data-lake"])


@router.post("/ensure-data", response_model=DataAvailabilityResult)
async def post_ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    logger.info(
        "[STEP 1] /api/data-lake/ensure-data received: request_id=%s, symbols=%s",
        spec.request_id,
        spec.symbols,
    )
    return await ensure_data(spec)


@router.get("/coverage", response_model=CoverageResponse)
async def get_coverage(
    symbol: str,
    start_trading_date: date,
    end_trading_date: date,
    market: Literal["usa"] = "usa",
    data_type: Literal["trade", "quote"] = "trade",
    provider: Literal["polygon"] = "polygon",
    price_adjustment_mode: PriceAdjustmentMode = "raw",
) -> CoverageResponse:
    """Per-day artifact status for a symbol, keyed by the canonical NYSE calendar.

    Days are the calendar's own sessions in ``[start_trading_date,
    end_trading_date]`` — weekends and holidays are simply absent, never
    listed and never invented. A session with no matching catalog row is
    reported honestly as ``status="missing"`` rather than omitted.
    """
    if start_trading_date > end_trading_date:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "invalid_range",
                "message": f"start_trading_date {start_trading_date} is after end_trading_date {end_trading_date}",
            },
        )
    # Same computation DataRunSpec's validator uses (types.py) — one shared
    # cap, one shared formula, so a write-window accepted by POST
    # /ensure-data can never be a read-window GET /coverage rejects.
    span_days = trading_range_span_days(start_trading_date, end_trading_date)
    if span_days > MAX_TRADING_RANGE_DAYS:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "range_too_large",
                "message": f"range is {span_days} days; max is {MAX_TRADING_RANGE_DAYS}",
            },
        )

    logger.info(
        "[STEP 1] /api/data-lake/coverage requested: symbol=%s, start=%s, end=%s",
        symbol,
        start_trading_date,
        end_trading_date,
    )
    sessions = expected_sessions(start_trading_date, end_trading_date)
    rows_by_date = {
        row.trading_date: row
        for row in await catalog_client.select_artifact_coverage(
            market=market,
            symbol=symbol,
            data_type=data_type,
            provider=provider,
            price_adjustment_mode=price_adjustment_mode,
            start_trading_date=start_trading_date,
            end_trading_date=end_trading_date,
        )
    }
    days = []
    for session_date in sessions:
        row = rows_by_date.get(session_date)
        days.append(
            CoverageDay(
                trading_date_ms=session_open_ms_or_none(session_date),
                status=row.status if row is not None else "missing",
                artifact_id=row.artifact_id if row is not None else None,
            )
        )
    return CoverageResponse(
        market=market,
        symbol=symbol,
        data_type=data_type,
        resolution="minute",
        provider=provider,
        price_adjustment_mode=price_adjustment_mode,
        days=days,
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactDetail)
async def get_artifact_detail(artifact_id: int) -> ArtifactDetail:
    """Return the full receipt for one catalog row: hashes, size, provider params."""
    logger.info("[STEP 1] /api/data-lake/artifacts/%s requested", artifact_id)
    detail = await catalog_client.select_artifact_by_id(artifact_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": "artifact_not_found",
                "message": f"artifact {artifact_id} not found",
            },
        )
    return detail


@router.get("/storage-summary", response_model=StorageSummaryResponse)
async def get_storage_summary(market: Literal["usa"] = "usa") -> StorageSummaryResponse:
    """Artifact counts/bytes by kind, plus each symbol's day-keyed coverage span."""
    logger.info("[STEP 1] /api/data-lake/storage-summary requested: market=%s", market)
    kinds, symbols = await asyncio.gather(
        catalog_client.select_storage_totals_by_kind(market),
        catalog_client.select_symbol_coverage_spans(market),
    )
    return StorageSummaryResponse(market=market, kinds=kinds, symbols=symbols)
