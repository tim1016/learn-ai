"""``GET /api/data-lake/instrument-data``: one instrument's data for a window, read-only.

Transport only. ``app.services.instrument_data_service.read_instrument_data`` validates the window as
``GET /api/data-lake/coverage`` does (its typed 422 is this route's), reads the coverage and the daily file, and
folds them into the states a Fill acts on. Opening this never fills anything: the Fill is the established backfill
job (``POST /api/jobs/data_lake_backfill``) over the ``fill_window`` this reports.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException

from app.data_lake.coverage import CoverageRequestError
from app.schemas.instrument_data import InstrumentDataResponse
from app.services.instrument_data_service import read_instrument_data

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
    logger.info(
        "[STEP 1] /api/data-lake/instrument-data requested",
        extra={"symbol": symbol, "price_adjustment_mode": price_adjustment_mode},
    )
    try:
        return await read_instrument_data(
            symbol=symbol,
            start_trading_date_ms=start_trading_date_ms,
            end_trading_date_ms=end_trading_date_ms,
            price_adjustment_mode=price_adjustment_mode,
        )
    except CoverageRequestError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
