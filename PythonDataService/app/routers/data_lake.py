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

from fastapi import APIRouter, Depends, HTTPException

from app.data_lake import catalog_client
from app.data_lake.ensure_data import ensure_data, provider_for_data_type
from app.data_lake.types import (
    MAX_SYMBOL_LENGTH,
    MAX_TRADING_RANGE_DAYS,
    SYMBOL_RE,
    ArtifactDetail,
    CoverageDay,
    CoverageResponse,
    DataAvailabilityResult,
    DataRunSpec,
    PriceAdjustmentMode,
    StorageSummaryResponse,
    trading_range_span_days,
)
from app.lean_sidecar.trading_calendar import session_windows_ms_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/data-lake", tags=["data-lake"])


async def _ensure_catalog_pool() -> None:
    """FastAPI dependency: guarantee the asyncpg pool exists before a route
    touches the catalog.

    ensure_data() already calls catalog_client.init_pool() as its own first
    step, so POST /ensure-data has never needed this. The GET read routes
    never called it at all — in a fresh process (flag on, no prior POST),
    a GET hit connection()'s "asyncpg pool not initialized" RuntimeError as
    an unhandled 500. init_pool() is idempotent, so wiring it here is a
    no-op once ensure_data (or an earlier GET) has already initialized it.
    """
    await catalog_client.init_pool()


@router.post("/ensure-data", response_model=DataAvailabilityResult)
async def post_ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    logger.info(
        "[STEP 1] /api/data-lake/ensure-data received: request_id=%s, symbols=%s",
        spec.request_id,
        spec.symbols,
    )
    return await ensure_data(spec)


@router.get("/coverage", response_model=CoverageResponse, dependencies=[Depends(_ensure_catalog_pool)])
async def get_coverage(
    symbol: str,
    start_trading_date: date,
    end_trading_date: date,
    market: Literal["usa"] = "usa",
    data_type: Literal["trade", "quote"] = "trade",
    price_adjustment_mode: PriceAdjustmentMode = "raw",
) -> CoverageResponse:
    """Per-day artifact status for a symbol, keyed by the canonical NYSE calendar.

    Days are the calendar's own sessions in ``[start_trading_date,
    end_trading_date]`` — weekends and holidays are simply absent, never
    listed and never invented. A session with no matching catalog row is
    reported honestly as ``status="missing"`` rather than omitted.

    ``provider`` is not a caller-supplied parameter: it's derived from
    ``data_type`` via ``provider_for_data_type`` (the same rule
    ``expand_required_artifacts`` uses to write these rows) — trade bars are
    Polygon's, quote bars are ``learn_ai_derived``. A fixed ``provider="polygon"``
    parameter made quote coverage unfindable, since quote rows are never
    catalogued under that provider.
    """
    if not SYMBOL_RE.match(symbol) or len(symbol) > MAX_SYMBOL_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "invalid_symbol",
                "message": f"symbol must match {SYMBOL_RE.pattern} and be at most {MAX_SYMBOL_LENGTH} chars: {symbol!r}",
            },
        )
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
    provider = provider_for_data_type(data_type)
    # One schedule build for the whole range, not one per day: session_date
    # and its 09:30 ET open both come off the same SessionWindow, so no
    # per-day session_open_ms_utc() call re-queries pandas_market_calendars.
    # A per-day query pattern here measured ~10s at the 5-year cap.
    windows = session_windows_ms_utc(start_trading_date, end_trading_date)
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
    for window in windows:
        row = rows_by_date.get(window.session_date)
        days.append(
            CoverageDay(
                trading_date_ms=window.open_ms_utc,
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


@router.get("/artifacts/{artifact_id}", response_model=ArtifactDetail, dependencies=[Depends(_ensure_catalog_pool)])
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


@router.get("/storage-summary", response_model=StorageSummaryResponse, dependencies=[Depends(_ensure_catalog_pool)])
async def get_storage_summary(market: Literal["usa"] = "usa") -> StorageSummaryResponse:
    """Artifact counts/bytes by kind, plus each symbol's day-keyed coverage span."""
    logger.info("[STEP 1] /api/data-lake/storage-summary requested: market=%s", market)
    kinds, symbols = await asyncio.gather(
        catalog_client.select_storage_totals_by_kind(market),
        catalog_client.select_symbol_coverage_spans(market),
    )
    return StorageSummaryResponse(market=market, kinds=kinds, symbols=symbols)
