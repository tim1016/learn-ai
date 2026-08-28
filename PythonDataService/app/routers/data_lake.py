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

import logging
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException

from app.data_lake import catalog_client
from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import (
    ArtifactDetail,
    CoverageDay,
    CoverageResponse,
    DataAvailabilityResult,
    DataRunSpec,
    StorageKindTotal,
    StorageSummaryResponse,
    SymbolCoverageSpan,
)
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/data-lake", tags=["data-lake"])

# Mirrors DataRunSpec's _MAX_RANGE_YEARS cap (types.py) — bounds the coverage
# endpoint's per-day calendar walk against an accidental multi-decade query.
_MAX_COVERAGE_RANGE_DAYS = 5 * 366


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
    price_adjustment_mode: Literal["raw", "polygon_split_adjusted", "lean_adjusted"] = "raw",
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
    span_days = (end_trading_date - start_trading_date).days + 1
    if span_days > _MAX_COVERAGE_RANGE_DAYS:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "range_too_large",
                "message": f"range is {span_days} days; max is {_MAX_COVERAGE_RANGE_DAYS}",
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
    days = [
        CoverageDay(
            trading_date_ms=session_open_ms_utc(session_date),
            status=rows_by_date[session_date].status if session_date in rows_by_date else "missing",
            artifact_id=rows_by_date[session_date].artifact_id if session_date in rows_by_date else None,
        )
        for session_date in sessions
    ]
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
    row = await catalog_client.select_artifact_by_id(artifact_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": "artifact_not_found",
                "message": f"artifact {artifact_id} not found",
            },
        )
    return ArtifactDetail(
        id=row.id,
        artifact_kind=row.artifact_kind,
        market=row.market,
        symbol=row.symbol,
        trading_date_ms=session_open_ms_utc(row.trading_date) if row.trading_date is not None else None,
        resolution=row.resolution,
        data_type=row.data_type,
        provider=row.provider,
        provider_params=row.provider_params,
        price_adjustment_mode=row.price_adjustment_mode,
        data_contract_hash=row.data_contract_hash,
        content_hash=row.content_hash,
        file_path=row.file_path,
        file_size_bytes=row.file_size_bytes,
        status=row.status,
        row_count=row.row_count,
        first_bar_start_ms=row.first_bar_start_ms,
        last_bar_start_ms=row.last_bar_start_ms,
        fetched_at_ms=row.fetched_at_ms,
        completed_at_ms=row.completed_at_ms,
    )


@router.get("/storage-summary", response_model=StorageSummaryResponse)
async def get_storage_summary(market: Literal["usa"] = "usa") -> StorageSummaryResponse:
    """Artifact counts/bytes by kind, plus each symbol's day-keyed coverage span."""
    logger.info("[STEP 1] /api/data-lake/storage-summary requested: market=%s", market)
    kinds = [
        StorageKindTotal(
            artifact_kind=row.artifact_kind,
            resolution=row.resolution,
            artifact_count=row.artifact_count,
            total_bytes=row.total_bytes,
        )
        for row in await catalog_client.select_storage_totals_by_kind(market)
    ]
    symbols = [
        SymbolCoverageSpan(
            symbol=row.symbol,
            first_trading_date_ms=session_open_ms_utc(row.first_trading_date) if row.first_trading_date else None,
            last_trading_date_ms=session_open_ms_utc(row.last_trading_date) if row.last_trading_date else None,
            artifact_count=row.artifact_count,
        )
        for row in await catalog_client.select_symbol_coverage_spans(market)
    ]
    return StorageSummaryResponse(market=market, kinds=kinds, symbols=symbols)
