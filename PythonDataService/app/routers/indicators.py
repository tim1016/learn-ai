"""API endpoints for technical indicator calculation"""
from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, HTTPException, status
import logging
import pandas as pd
import pandas_ta as ta

from app.services.ta_service import TechnicalAnalysisService
from app.services.polygon_client import PolygonClientService
from app.services.dataset_service import (
    compute_warmup_start_date,
    estimate_max_lookback,
    indicator_table_params_to_entries,
    preprocess_and_calculate,
    rename_to_indicator_table_columns,
)
from app.models.requests import CalculateIndicatorsRequest, IndicatorTableRequest
from app.models.responses import CalculateIndicatorsResponse, IndicatorTableResponse

router = APIRouter()
logger = logging.getLogger(__name__)

ta_service = TechnicalAnalysisService()
polygon_client = PolygonClientService()


@router.post("/calculate", response_model=CalculateIndicatorsResponse)
async def calculate_indicators(request: CalculateIndicatorsRequest):
    """Calculate technical indicators from OHLCV data."""
    try:
        logger.info(
            f"[TA] Calculating {len(request.indicators)} indicators "
            f"for {request.ticker} ({len(request.bars)} bars)"
        )

        bars_dicts = [bar.model_dump() for bar in request.bars]
        indicator_dicts = [ind.model_dump() for ind in request.indicators]

        results = ta_service.calculate_indicators(bars_dicts, indicator_dicts)

        return CalculateIndicatorsResponse(
            success=True,
            ticker=request.ticker,
            indicators=results
        )

    except Exception as e:
        logger.error(f"[TA] Error calculating indicators: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to calculate indicators: {str(e)}"
        )


@router.post("/generate-table", response_model=IndicatorTableResponse)
async def generate_indicator_table(request: IndicatorTableRequest):
    """
    Fetch bars from Polygon and generate a full indicator table
    with EMAs, Bollinger Bands, Supertrend, RSI, MACD, and ADX.

    Uses the shared preprocessing pipeline (session filter, forward-fill,
    warm-up buffer, indicator calculation).
    """
    try:
        # Convert fixed params to dynamic entries
        indicator_entries = indicator_table_params_to_entries(
            ema_periods=request.ema_periods,
            bb_length=request.bb_length,
            bb_std=request.bb_std,
            supertrend_length=request.supertrend_length,
            supertrend_multiplier=request.supertrend_multiplier,
            rsi_length=request.rsi_length,
            macd_fast=request.macd_fast,
            macd_slow=request.macd_slow,
            macd_signal=request.macd_signal,
            adx_length=request.adx_length,
        )

        # Compute warm-up: account for ADX double smoothing and RSI+MA chain
        max_lookback = estimate_max_lookback(indicator_entries)
        max_lookback = max(max_lookback, request.adx_length * 2)
        max_lookback = max(max_lookback, request.rsi_length + request.rsi_ma_length)

        warmup_start = compute_warmup_start_date(
            request.from_date, max_lookback, request.timespan, request.multiplier,
        )

        logger.info(
            f"[STEP 1] Fetching {request.timespan} bars for {request.ticker} "
            f"from {warmup_start} (warmup) to {request.to_date} "
            f"(requested from {request.from_date})"
        )
        bars = polygon_client.fetch_aggregates(
            ticker=request.ticker,
            multiplier=request.multiplier,
            timespan=request.timespan,
            from_date=warmup_start,
            to_date=request.to_date,
            adjusted=request.adjusted,
        )

        if not bars:
            return IndicatorTableResponse(
                success=False,
                ticker=request.ticker,
                error="No bars returned from Polygon",
            )

        # Trim timestamp for warm-up removal
        from_ts = int(datetime.strptime(request.from_date, "%Y-%m-%d").timestamp() * 1000)

        logger.info(f"[STEP 2] Processing {len(bars)} bars through shared pipeline")
        df, column_meta = preprocess_and_calculate(
            bars=bars,
            indicator_entries=indicator_entries,
            session=request.session,
            forward_fill=request.forward_fill,
            trim_from_ts=from_ts,
        )

        # Post-processing: RSI MA (SMA of the RSI column — not a standard single-call indicator)
        rsi_cols = [m["column"] for m in column_meta if m["indicator"] == "rsi"]
        if rsi_cols and rsi_cols[0] in df.columns:
            rsi_ma = ta.sma(df[rsi_cols[0]], length=request.rsi_ma_length)
            df["rsi_ma"] = rsi_ma

        # Rename to backward-compatible column names
        df = rename_to_indicator_table_columns(df, column_meta)

        # Build result: rename timestamp → time for API contract
        df = df.rename(columns={"timestamp": "time"})

        # Convert to list of dicts, NaN → None
        raw_rows = df.to_dict(orient="records")
        rows = [
            {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.items()}
            for row in raw_rows
        ]

        columns = list(rows[0].keys()) if rows else []

        logger.info(f"[STEP 3] Done: {len(rows)} rows, columns={columns}")

        return IndicatorTableResponse(
            success=True,
            ticker=request.ticker,
            row_count=len(rows),
            columns=columns,
            rows=rows,
        )

    except Exception as e:
        logger.error(f"[TA] Error generating indicator table: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate indicator table: {str(e)}"
        )
