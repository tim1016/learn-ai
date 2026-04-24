"""API endpoints for dataset generation: chunked OHLCV + dynamic indicator calculation + CSV export"""

from __future__ import annotations

import io
import logging
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

from app.models.requests import DatasetGenerationRequest
from app.services.dataset_service import (
    build_csv_bytes,
    build_metadata_csv,
    build_metadata_json,
    build_zip_bytes,
    compute_warmup_start_date,
    estimate_max_lookback,
    fetch_bars_chunked,
    get_indicator_configs,
    list_available_indicators,
    preprocess_and_calculate,
)
from app.services.polygon_client import PolygonClientService

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
        fetch_from = compute_warmup_start_date(
            request.from_date,
            max_lookback,
            timespan=request.timespan,
            multiplier=request.multiplier,
        )
        trim_from_ts = int(datetime.strptime(request.from_date, "%Y-%m-%d").timestamp() * 1000)
        logger.info(
            f"[DATASET] Warm-up: fetching from {fetch_from} (requested {request.from_date}, lookback={max_lookback})"
        )

    bars = fetch_bars_chunked(
        polygon_client,
        request.ticker,
        fetch_from,
        request.to_date,
        timespan=request.timespan,
        multiplier=request.multiplier,
        adjusted=request.adjusted,
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
        from_date=request.from_date,
        to_date=request.to_date,
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
                cat_items.append(
                    {
                        **item,
                        "configurable_params": configs.get(item["name"], []),
                    }
                )
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
        extra_cols = [c for c in ["vwap", "transactions", "session"] if c in df.columns]
        indicator_col_names = [m["column"] for m in column_meta]
        all_data_cols = ohlcv_cols + extra_cols + indicator_col_names

        csv_bytes = build_csv_bytes(df, all_data_cols)

        session_label = "rth" if request.session == "rth" else "ext"
        ts_label = f"{request.multiplier}{request.timespan}" if request.multiplier > 1 else request.timespan
        filename = f"{request.ticker}_{ts_label}_{session_label}_{request.from_date}_to_{request.to_date}.csv"
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
        extra_cols = [c for c in ["vwap", "transactions", "session"] if c in df.columns]

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
        extra_cols = [c for c in ["vwap", "transactions", "session"] if c in df.columns]

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


@router.post("/generate-zip")
async def generate_dataset_zip(request: DatasetGenerationRequest):
    """Fetch OHLCV, calculate indicators, and return a ZIP.

    Always contains ``dataset.csv``, ``metadata.csv``, ``columns.csv``. Adds
    ``options_calls.csv`` / ``options_puts.csv`` when ``options_companion`` is
    enabled and ``quality_report.md`` when ``include_quality_report`` is true.
    """
    try:
        logger.info(
            f"[DATASET] Generating ZIP for {request.ticker}: "
            f"{request.from_date} to {request.to_date}, "
            f"indicators={[e.get('name') for e in request.indicator_entries]}, "
            f"options_companion={bool(request.options_companion and request.options_companion.enabled)}, "
            f"quality_report={request.include_quality_report}"
        )

        df, column_meta, raw_count = _fetch_and_process(request)

        ohlcv_cols = ["open", "high", "low", "close", "volume"]
        extra_cols = [c for c in ["vwap", "transactions", "session"] if c in df.columns]
        indicator_col_names = [m["column"] for m in column_meta]
        all_data_cols = ohlcv_cols + extra_cols + indicator_col_names

        # Options companion (optional)
        options_calls_bytes: bytes | None = None
        options_puts_bytes: bytes | None = None
        options_report: dict | None = None
        if request.options_companion and request.options_companion.enabled:
            from app.services.options_companion_service import build_options_companion_csvs

            options_calls_bytes, options_puts_bytes, options_report = build_options_companion_csvs(
                underlying_bars_df=df,
                ticker=request.ticker,
                from_date=request.from_date,
                to_date=request.to_date,
                config=request.options_companion,
                polygon=polygon_client,
                timespan=request.timespan,
                multiplier=request.multiplier,
            )

        # Quality report (optional)
        quality_report_bytes: bytes | None = None
        if request.include_quality_report:
            from app.services.data_quality_service import analyze as dq_analyze
            from app.services.data_quality_service import render_report_markdown

            dq_result = dq_analyze(
                polygon=polygon_client,
                ticker=request.ticker,
                from_date=request.from_date,
                to_date=request.to_date,
                volume_fix="round",
                recompute_indicators=False,
                indicator_entries=None,
            )
            if "error" not in dq_result:
                quality_report_bytes = render_report_markdown(dq_result)

        zip_bytes = build_zip_bytes(
            df=df,
            columns=all_data_cols,
            column_meta=column_meta,
            ohlcv_cols=ohlcv_cols + extra_cols,
            ticker=request.ticker,
            from_date=request.from_date,
            to_date=request.to_date,
            session=request.session,
            forward_fill=request.forward_fill,
            timespan=request.timespan,
            multiplier=request.multiplier,
            raw_bar_count=raw_count,
            filled_bar_count=len(df),
            options_calls_csv_bytes=options_calls_bytes,
            options_puts_csv_bytes=options_puts_bytes,
            options_companion_report=options_report,
            quality_report_md_bytes=quality_report_bytes,
        )

        session_label = "rth" if request.session == "rth" else "ext"
        ts_label = f"{request.multiplier}{request.timespan}" if request.multiplier > 1 else request.timespan
        filename = f"{request.ticker}_{ts_label}_{session_label}_{request.from_date}_to_{request.to_date}.zip"

        logger.info(
            f"[DATASET] ZIP ready: {raw_count} raw bars → {len(df)} processed, "
            f"{len(indicator_col_names)} indicator columns"
        )

        return StreamingResponse(
            io.BytesIO(zip_bytes),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DATASET] ZIP error: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/validation-report")
async def generate_validation_report(
    our_csv: UploadFile = File(..., description="pandas-ta generated CSV"),
    tv_csv: UploadFile = File(..., description="TradingView exported CSV"),
    ticker: str = Form("SPY"),
):
    """
    Compare a pandas-ta generated CSV against a TradingView CSV export.
    Returns a markdown validation report.
    """
    from app.services.validation_service import generate_validation_report as gen_report

    try:
        our_bytes = await our_csv.read()
        tv_bytes = await tv_csv.read()

        logger.info(f"[VALIDATION] Comparing {len(our_bytes)} bytes (ours) vs {len(tv_bytes)} bytes (TV) for {ticker}")

        report_md = gen_report(our_bytes, tv_bytes, ticker)

        return {"success": True, "report": report_md}

    except Exception as e:
        logger.error(f"[VALIDATION] Error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Validation report failed: {e!s}",
        )


@router.post("/validation-report-download")
async def download_validation_report(
    our_csv: UploadFile = File(..., description="pandas-ta generated CSV"),
    tv_csv: UploadFile = File(..., description="TradingView exported CSV"),
    ticker: str = Form("SPY"),
):
    """Same as validation-report but returns the markdown as a downloadable file."""
    from app.services.validation_service import generate_validation_report as gen_report

    try:
        our_bytes = await our_csv.read()
        tv_bytes = await tv_csv.read()

        report_md = gen_report(our_bytes, tv_bytes, ticker)
        report_bytes = report_md.encode("utf-8")

        filename = f"{ticker}_validation_report.md"
        return StreamingResponse(
            io.BytesIO(report_bytes),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as e:
        logger.error(f"[VALIDATION] Download error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
