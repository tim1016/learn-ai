"""``GET /api/data-lake/instrument-data``: one instrument's data for a window, read-only.

Transport only. The window is validated and the sessions read by the existing
``GET /api/data-lake/coverage`` (its typed 422s are this route's), and
``app.services.instrument_data_service`` reads that into the states a Fill
acts on. Opening this never fills anything: the Fill is the established backfill job
(``POST /api/jobs/data_lake_backfill``) over the ``fill_window`` this reports.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter

from app.data_lake import catalog_client
from app.data_lake.polygon_fetcher import polygon_history_floor
from app.data_lake.types import trading_date_at_ms
from app.routers.data_lake import get_coverage
from app.schemas.instrument_data import InstrumentDataResponse
from app.services.instrument_data_service import build_instrument_data, read_daily_rollup
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/data-lake", tags=["data-lake"])


@router.get("/instrument-data", response_model=InstrumentDataResponse)
async def get_instrument_data(
    symbol: str,
    start_trading_date_ms: int,
    end_trading_date_ms: int,
    *,
    price_adjustment_mode: Literal["raw", "polygon_split_adjusted"],
) -> InstrumentDataResponse:
    """Per scheduled session in the window: ready, or why not, and whether a Fill can close it.

    ``price_adjustment_mode`` has no default, for the reason ``/coverage``
    gives, and names only the trees the fetch pipeline writes. The window is
    two ``int64 ms UTC`` instants inside the first and last ET day wanted.
    A window the coverage read refuses (too wide, reversed, a symbol the lake
    cannot hold) is refused with its typed reason; it is never cut short.
    """
    await catalog_client.init_pool()
    now_ms = now_ms_utc()
    logger.info(
        "[STEP 1] /api/data-lake/instrument-data requested",
        extra={"symbol": symbol, "price_adjustment_mode": price_adjustment_mode},
    )
    coverage = await get_coverage(
        symbol,
        start_trading_date_ms,
        end_trading_date_ms,
        market="usa",
        data_type="trade",
        price_adjustment_mode=price_adjustment_mode,
    )
    floor = polygon_history_floor(trading_date_at_ms(now_ms))
    rollup = await read_daily_rollup(symbol=symbol, price_adjustment_mode=price_adjustment_mode, floor=floor)
    return build_instrument_data(
        coverage,
        start=trading_date_at_ms(start_trading_date_ms),
        end=trading_date_at_ms(end_trading_date_ms),
        now_ms=now_ms,
        floor=floor,
        rollup=rollup,
    )
