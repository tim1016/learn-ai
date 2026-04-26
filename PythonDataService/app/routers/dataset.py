"""API endpoints for dataset generation: chunked OHLCV + dynamic indicator calculation + CSV export.

Streaming bundle pipeline
-------------------------
The async fetch+bundle flow used to live here as
``POST /generate-zip/stream`` with a custom ``RunSession`` registry. It
was migrated to the unified job framework: the public surface is now
``POST /api/jobs/dataset-zip`` (.NET layer), which dispatches to
``POST /api/jobs-internal/dataset-zip`` (this service) — see
``app/routers/jobs.py``. The synchronous one-shot ``POST /generate-zip``
endpoint below is unchanged.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

from app.models.requests import DatasetGenerationRequest
from app.research.divergence.ingest import (
    apply_dividend_adjustment,
    dividends_from_polygon_payload,
)
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


def _fetch_and_process(
    request: DatasetGenerationRequest,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
):
    """Shared fetch + preprocess for all dataset endpoints.

    When ``on_event`` is supplied, the chunker emits chunk-level progress;
    we also emit a ``dividend_adjust`` event when applicable. When
    ``cancel_check`` is supplied and returns True, the chunker raises
    ``RunCancelledError`` and the caller should propagate it.

    Returns (df, column_meta, raw_bar_count).
    """

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
        sort=request.sort,
        limit=request.limit,
        on_event=on_event,
        cancel_check=cancel_check,
    )
    if not bars:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No bars returned")

    raw_count = len(bars)

    # Dividend adjustment (TV-style). Polygon's adjusted=True only does splits;
    # when the user opts in, we fetch the dividend reference file and subtract
    # each dividend from bars dated before its ex-date. See
    # docs/tv-polygon-validation-gotchas.md §1 for the reason this matters.
    if request.adjust_for_dividends:
        import pandas as pd

        div_rows = polygon_client.list_dividends(
            ticker=request.ticker,
            ex_dividend_date_gte=fetch_from,
            ex_dividend_date_lte=request.to_date,
        )
        events = dividends_from_polygon_payload(div_rows, ticker=request.ticker)
        if events:
            df_bars = pd.DataFrame(bars)
            df_bars = apply_dividend_adjustment(df_bars, events)
            bars = df_bars.to_dict("records")
            logger.info(
                "[DATASET] Applied dividend adjustment: %d events over %d bars",
                len(events),
                raw_count,
            )
            if on_event is not None:
                on_event({"type": "dividend_adjusted", "events": len(events), "bars": raw_count})
        else:
            logger.info("[DATASET] adjust_for_dividends=True but no dividends in range — passthrough")

    # Indicator computation can take several seconds when many indicators
    # are configured (each pandas-ta call is a separate vectorized pass).
    # Surface it as a phase so the run-card doesn't appear stuck between
    # the last chunk and bundle_start.
    if on_event is not None:
        on_event({
            "type": "processing_indicators",
            "indicator_count": len(request.indicator_entries),
            "bar_count": len(bars),
        })

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


def _build_zip_with_events(
    request: DatasetGenerationRequest,
    df,
    column_meta,
    raw_count: int,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[bytes, str]:
    """Bundle the dataset + companions + quality report into a ZIP.

    When ``on_event`` is supplied, emits ``bundle_start`` listing the
    components that will be produced, ``bundle_progress`` events from
    the options-companion loop (one per contract), and
    ``bundle_component_done`` after each component finishes. When
    ``cancel_check`` is supplied and returns True between components or
    between contracts, the bundler raises ``RunCancelledError``.
    Returns ``(zip_bytes, filename)``.
    """
    ohlcv_cols = ["open", "high", "low", "close", "volume"]
    extra_cols = [c for c in ["vwap", "transactions", "session"] if c in df.columns]
    indicator_col_names = [m["column"] for m in column_meta]
    all_data_cols = ohlcv_cols + extra_cols + indicator_col_names

    components: list[str] = ["dataset.csv", "metadata.csv", "columns.csv"]
    if request.include_quality_report:
        components.append("quality_report.md")
    if request.include_splits:
        components.append("splits.csv")
    if request.include_dividends:
        components.append("dividends.csv")
    if request.include_ticker_overview:
        components.append("ticker_overview.json")
    if request.include_news:
        components.append("news.csv")
    if request.include_financials:
        components.append("financials.csv")
    if request.include_trades:
        components.append("trades.csv")
    if request.include_quotes:
        components.append("quotes.csv")
    # Per-slot companion files are emitted by side+slot (e.g. calls/atm-03.csv).
    # The exact slot list isn't known until contracts are fetched, so we
    # advertise the side-level placeholders up front and emit per-file
    # ``bundle_component_done`` events as the slot files materialize.
    if request.options_companion and request.options_companion.enabled:
        if request.options_companion.include_calls:
            components.append("calls/")
        if request.options_companion.include_puts:
            components.append("puts/")
    if on_event is not None:
        on_event({"type": "bundle_start", "components": components})

    def _component_start(name: str) -> None:
        if on_event is not None:
            on_event({"type": "bundle_component_start", "name": name})

    def _component_done(name: str) -> None:
        if on_event is not None:
            on_event({"type": "bundle_component_done", "name": name})

    # Options companion (optional)
    options_slot_files: dict[str, bytes] = {}
    options_report: dict | None = None
    if request.options_companion and request.options_companion.enabled:
        from app.services.options_companion_service import build_options_companion_csvs

        options_slot_files, options_report = build_options_companion_csvs(
            underlying_bars_df=df,
            ticker=request.ticker,
            from_date=request.from_date,
            to_date=request.to_date,
            config=request.options_companion,
            polygon=polygon_client,
            timespan=request.timespan,
            multiplier=request.multiplier,
            on_event=on_event,
            cancel_check=cancel_check,
        )
        for path in sorted(options_slot_files.keys()):
            _component_done(path)

    # Reference companions (optional, each independent)
    from app.services.reference_companion_service import (
        build_dividends_csv,
        build_financials_csv,
        build_news_csv,
        build_quotes_csv,
        build_splits_csv,
        build_ticker_overview_json,
        build_trades_csv,
    )

    # Reference companions are independent Polygon REST calls (each can take
    # several seconds for large date ranges); emit start+done so the run-card
    # surfaces a "fetching X.csv" indicator instead of jumping queued→done.
    splits_bytes = None
    if request.include_splits:
        _component_start("splits.csv")
        splits_bytes = build_splits_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("splits.csv")
    dividends_bytes = None
    if request.include_dividends:
        _component_start("dividends.csv")
        dividends_bytes = build_dividends_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("dividends.csv")
    overview_bytes = None
    if request.include_ticker_overview:
        _component_start("ticker_overview.json")
        overview_bytes = build_ticker_overview_json(polygon_client, request.ticker, request.to_date)
        _component_done("ticker_overview.json")
    news_bytes = None
    if request.include_news:
        _component_start("news.csv")
        news_bytes = build_news_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("news.csv")
    financials_bytes = None
    if request.include_financials:
        _component_start("financials.csv")
        financials_bytes = build_financials_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("financials.csv")
    stock_trades_bytes = None
    if request.include_trades:
        _component_start("trades.csv")
        stock_trades_bytes = build_trades_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("trades.csv")
    stock_quotes_bytes = None
    if request.include_quotes:
        _component_start("quotes.csv")
        stock_quotes_bytes = build_quotes_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("quotes.csv")

    # Quality report (optional)
    quality_report_bytes: bytes | None = None
    if request.include_quality_report:
        _component_start("quality_report.md")
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
        _component_done("quality_report.md")

    # The core components (dataset/metadata/columns) are written together
    # inside build_zip_bytes. Emit start events for all three first so the
    # run-card shows "▸ packaging" while the writer runs, then done events
    # below once it returns.
    _component_start("dataset.csv")
    _component_start("metadata.csv")
    _component_start("columns.csv")
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
        options_slot_files=options_slot_files,
        options_companion_report=options_report,
        quality_report_md_bytes=quality_report_bytes,
        splits_csv_bytes=splits_bytes,
        dividends_csv_bytes=dividends_bytes,
        ticker_overview_json_bytes=overview_bytes,
        news_csv_bytes=news_bytes,
        financials_csv_bytes=financials_bytes,
        stock_trades_csv_bytes=stock_trades_bytes,
        stock_quotes_csv_bytes=stock_quotes_bytes,
    )
    # The "core" components (dataset/metadata/columns) are produced by
    # build_zip_bytes itself; emit their done events post-hoc so the UI
    # gets a complete checklist.
    _component_done("dataset.csv")
    _component_done("metadata.csv")
    _component_done("columns.csv")

    session_label = "rth" if request.session == "rth" else "ext"
    ts_label = f"{request.multiplier}{request.timespan}" if request.multiplier > 1 else request.timespan
    filename = f"{request.ticker}_{ts_label}_{session_label}_{request.from_date}_to_{request.to_date}.zip"
    return zip_bytes, filename


@router.post("/generate-zip")
async def generate_dataset_zip(request: DatasetGenerationRequest):
    """Fetch OHLCV, calculate indicators, and return a ZIP.

    Always contains ``dataset.csv``, ``metadata.csv``, ``columns.csv``. Adds
    per-slot CSVs under ``calls/`` and ``puts/`` subfolders when
    ``options_companion`` is enabled, and ``quality_report.md`` when
    ``include_quality_report`` is true.

    Synchronous variant — single response with the binary ZIP. The
    streaming counterpart at ``/generate-zip/stream`` emits SSE events for
    chunk-level UI progress; use that one for unified-flow Fetch.
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
        zip_bytes, filename = _build_zip_with_events(request, df, column_meta, raw_count)

        logger.info(
            f"[DATASET] ZIP ready: {raw_count} raw bars → {len(df)} processed, "
            f"{len([m['column'] for m in column_meta])} indicator columns"
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
