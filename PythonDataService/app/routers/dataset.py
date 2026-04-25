"""API endpoints for dataset generation: chunked OHLCV + dynamic indicator calculation + CSV export"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
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
    RunCancelledError,
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
from app.services.run_session_service import RunSession, run_sessions

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
            logger.info(
                "[DATASET] adjust_for_dividends=True but no dividends in range — passthrough"
            )

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
    if request.options_companion and request.options_companion.enabled:
        if request.options_companion.include_calls:
            components.append("options_calls.csv")
        if request.options_companion.include_puts:
            components.append("options_puts.csv")
    if on_event is not None:
        on_event({"type": "bundle_start", "components": components})

    def _component_done(name: str) -> None:
        if on_event is not None:
            on_event({"type": "bundle_component_done", "name": name})

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
            on_event=on_event,
            cancel_check=cancel_check,
        )
        if request.options_companion.include_calls:
            _component_done("options_calls.csv")
        if request.options_companion.include_puts:
            _component_done("options_puts.csv")

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

    splits_bytes = None
    if request.include_splits:
        splits_bytes = build_splits_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("splits.csv")
    dividends_bytes = None
    if request.include_dividends:
        dividends_bytes = build_dividends_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("dividends.csv")
    overview_bytes = None
    if request.include_ticker_overview:
        overview_bytes = build_ticker_overview_json(polygon_client, request.ticker, request.to_date)
        _component_done("ticker_overview.json")
    news_bytes = None
    if request.include_news:
        news_bytes = build_news_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("news.csv")
    financials_bytes = None
    if request.include_financials:
        financials_bytes = build_financials_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("financials.csv")
    stock_trades_bytes = None
    if request.include_trades:
        stock_trades_bytes = build_trades_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("trades.csv")
    stock_quotes_bytes = None
    if request.include_quotes:
        stock_quotes_bytes = build_quotes_csv(polygon_client, request.ticker, request.from_date, request.to_date)
        _component_done("quotes.csv")

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
        _component_done("quality_report.md")

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
    ``options_calls.csv`` / ``options_puts.csv`` when ``options_companion`` is
    enabled and ``quality_report.md`` when ``include_quality_report`` is true.

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


# ──────────────────────────────────────────────────────────────────────
# Streaming variant for the unified Fetch & bundle flow
# ──────────────────────────────────────────────────────────────────────


def _run_zip_pipeline(
    request: DatasetGenerationRequest,
    session: RunSession,
    on_event: Callable[[dict[str, Any]], None],
) -> None:
    """Worker body — runs in a thread spawned from the SSE endpoint.

    Drives the synchronous fetch + bundle pipeline, emitting events through
    ``on_event``. On success: stows the ZIP bytes on ``session`` and emits
    ``complete``. On cancel or failure: emits ``error`` and stows the
    error message. Either way, the SSE generator sees a sentinel and
    closes the stream.
    """

    def _cancel_check() -> bool:
        return session.cancelled.is_set()

    try:
        df, column_meta, raw_count = _fetch_and_process(
            request,
            on_event=on_event,
            cancel_check=_cancel_check,
        )
        on_event({
            "type": "fetch_complete",
            "raw_bars": raw_count,
            "processed_bars": len(df),
            "indicator_columns": len([m["column"] for m in column_meta]),
        })
        zip_bytes, filename = _build_zip_with_events(
            request, df, column_meta, raw_count, on_event=on_event, cancel_check=_cancel_check,
        )
        session.zip_bytes = zip_bytes
        session.filename = filename
        on_event({
            "type": "complete",
            "session_id": session.id,
            "filename": filename,
            "size_bytes": len(zip_bytes),
            "file_count": len(zip_bytes) and len([m["column"] for m in column_meta]) + 3,
        })
    except RunCancelledError as exc:
        session.failed = True
        session.error_message = str(exc)
        on_event({"type": "error", "kind": "cancelled", "message": str(exc)})
    except HTTPException as exc:
        session.failed = True
        session.error_message = exc.detail
        on_event({"type": "error", "kind": "http", "message": exc.detail})
    except Exception as exc:
        logger.error("[DATASET] Stream pipeline error: %s", exc, exc_info=True)
        session.failed = True
        session.error_message = str(exc)
        on_event({"type": "error", "kind": "internal", "message": str(exc)})


@router.post("/generate-zip/stream")
async def generate_dataset_zip_stream(request: DatasetGenerationRequest):
    """Streaming variant of ``/generate-zip`` — emits Server-Sent Events
    so the frontend can render chunk-level progress (states B/C/D/E in
    the design brief). The final ``complete`` event carries a session id
    the client uses to retrieve the binary ZIP via
    ``GET /run/{id}/zip``. Cancel mid-fetch via
    ``DELETE /run/{id}``.
    """
    session = run_sessions.create()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    sentinel = object()

    def emit(event: dict[str, Any]) -> None:
        # Worker thread → event loop. ``call_soon_threadsafe`` is the only
        # safe way to schedule a coroutine-less queue put from a non-loop
        # thread. ``put_nowait`` doesn't block since the queue is unbounded.
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def worker() -> None:
        try:
            _run_zip_pipeline(request, session, emit)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)  # type: ignore[arg-type]

    threading.Thread(target=worker, name=f"run-{session.id}", daemon=True).start()
    # Surface the session id on event 0 so even a slow client sees it
    # before the first chunk_start.
    await queue.put({"type": "session_started", "session_id": session.id})

    async def event_stream():
        while True:
            event = await queue.get()
            if event is sentinel:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/run/{session_id}/zip")
async def get_run_zip(session_id: str):
    """Retrieve the bundled ZIP for a completed run. Single-shot —
    succeeding deletes the registry entry."""
    session = run_sessions.pop(session_id)
    if session is None or session.zip_bytes is None or session.filename is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="run not found, not complete, or already retrieved",
        )
    return StreamingResponse(
        io.BytesIO(session.zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{session.filename}"'},
    )


@router.delete("/run/{session_id}")
async def cancel_run(session_id: str):
    """Cooperative cancel — flips the session's cancel flag. The worker
    notices between chunks and emits a final ``error`` SSE event of kind
    ``cancelled``."""
    if not run_sessions.cancel(session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return {"cancelled": session_id}


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
