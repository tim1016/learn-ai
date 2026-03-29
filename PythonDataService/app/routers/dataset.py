"""API endpoints for dataset generation: chunked OHLCV + dynamic indicator calculation + CSV export"""
from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
import io
import logging

from app.services.dataset_service import (
    list_available_indicators,
    get_indicator_configs,
    fetch_minute_bars_chunked,
    compute_warmup_start_date,
    estimate_max_lookback,
    preprocess_and_calculate,
    build_csv_bytes,
    build_metadata_json,
    build_metadata_csv,
)
from app.services.polygon_client import PolygonClientService
from app.models.requests import DatasetGenerationRequest

router = APIRouter()
logger = logging.getLogger(__name__)
polygon_client = PolygonClientService()


def _fetch_and_process(request: DatasetGenerationRequest):
    """Shared fetch + preprocess for all dataset endpoints.
    Returns (df, column_meta, raw_bar_count)."""

    # Warm-up: fetch extra bars before from_date so indicators converge
    fetch_from = request.from_date
    trim_from_ts = None
    if request.warmup and request.indicator_entries:
        max_lookback = estimate_max_lookback(request.indicator_entries)
        fetch_from = compute_warmup_start_date(request.from_date, max_lookback)
        trim_from_ts = int(datetime.strptime(request.from_date, "%Y-%m-%d").timestamp() * 1000)
        logger.info(
            f"[DATASET] Warm-up: fetching from {fetch_from} "
            f"(requested {request.from_date}, lookback={max_lookback})"
        )

    bars = fetch_minute_bars_chunked(
        polygon_client, request.ticker, fetch_from, request.to_date
    )
    if not bars:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No bars returned")

    raw_count = len(bars)

    df, column_meta = preprocess_and_calculate(
        bars=bars,
        indicator_entries=request.indicator_entries,
        session=request.session,
        forward_fill=request.forward_fill,
        trim_from_ts=trim_from_ts,
    )

    return df, column_meta, raw_count


@router.get("/available")
async def get_available_indicators():
    """List all pandas-ta indicators grouped by category with configurable params."""
    try:
        categories_raw = list_available_indicators()
        configs = get_indicator_configs()

        categories = {}
        for cat, items in categories_raw.items():
            cat_items = []
            for item in items:
                cat_items.append({
                    **item,
                    "configurable_params": configs.get(item["name"], []),
                })
            categories[cat] = cat_items

        total = sum(len(items) for items in categories.values())
        return {"success": True, "categories": categories, "total": total}
    except Exception as e:
        logger.error(f"Error listing indicators: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/generate-csv")
async def generate_dataset_csv(request: DatasetGenerationRequest):
    """Fetch minute OHLCV data in chunks, calculate selected indicators,
    and return a streaming CSV file."""
    try:
        logger.info(
            f"[DATASET] Generating CSV for {request.ticker}: "
            f"{request.from_date} to {request.to_date}, "
            f"indicators={[e.get('name') for e in request.indicator_entries]}"
        )

        df, column_meta, raw_count = _fetch_and_process(request)

        ohlcv_cols = ["open", "high", "low", "close", "volume"]
        extra_cols = [c for c in ["vwap", "transactions"] if c in df.columns]
        indicator_col_names = [m["column"] for m in column_meta]
        all_data_cols = ohlcv_cols + extra_cols + indicator_col_names

        csv_bytes = build_csv_bytes(df, all_data_cols)

        session_label = "rth" if request.session == "rth" else "ext"
        filename = f"{request.ticker}_minute_{session_label}_{request.from_date}_to_{request.to_date}.csv"
        logger.info(
            f"[DATASET] CSV ready: {raw_count} raw bars → {len(df)} processed, "
            f"{len(indicator_col_names)} indicator columns"
        )

        return StreamingResponse(
            io.BytesIO(csv_bytes),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DATASET] Error: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/generate-metadata")
async def generate_dataset_metadata(request: DatasetGenerationRequest):
    """Fetch minute OHLCV data, calculate indicators, and return metadata JSON."""
    try:
        df, column_meta, raw_count = _fetch_and_process(request)

        ohlcv_cols = ["open", "high", "low", "close", "volume"]
        extra_cols = [c for c in ["vwap", "transactions"] if c in df.columns]

        metadata_bytes = build_metadata_json(
            ticker=request.ticker,
            from_date=request.from_date,
            to_date=request.to_date,
            bar_count=raw_count,
            column_meta=column_meta,
            ohlcv_cols=ohlcv_cols + extra_cols,
            session=request.session,
            forward_fill=request.forward_fill,
            raw_bar_count=raw_count,
            filled_bar_count=len(df),
        )

        session_label = "rth" if request.session == "rth" else "ext"
        filename = f"{request.ticker}_minute_{session_label}_{request.from_date}_to_{request.to_date}_metadata.json"
        return StreamingResponse(
            io.BytesIO(metadata_bytes),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DATASET] Metadata error: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/generate-metadata-csv")
async def generate_dataset_metadata_csv(request: DatasetGenerationRequest):
    """Fetch minute OHLCV data, calculate indicators, and return column descriptions CSV."""
    try:
        df, column_meta, _ = _fetch_and_process(request)

        ohlcv_cols = ["open", "high", "low", "close", "volume"]
        extra_cols = [c for c in ["vwap", "transactions"] if c in df.columns]

        csv_bytes = build_metadata_csv(column_meta, ohlcv_cols + extra_cols)

        session_label = "rth" if request.session == "rth" else "ext"
        filename = f"{request.ticker}_minute_{session_label}_{request.from_date}_to_{request.to_date}_columns.csv"
        return StreamingResponse(
            io.BytesIO(csv_bytes),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DATASET] Metadata CSV error: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
