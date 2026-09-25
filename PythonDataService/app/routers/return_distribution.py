"""Return-distribution study API router.

POST /api/research/return-distribution — daily-returns histogram with open
edge bins, session-segmented per-day drill-down, and summary statistics,
computed from lake minute bars (see app/research/return_distribution.py
for the math and its provenance). The router is transport only: it maps
the service's typed errors onto HTTP codes and the engine result onto the
wire model. Only ``StudyRequestError`` (unusable study parameters) maps to
400 — internal ``ValueError``s (a malformed factor file, a corrupt lake
zip) propagate as operational 500s instead of masquerading as caller
errors.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.models.return_distribution_models import (
    AdjustmentNotCoveredResponse,
    CaptureReceiptModel,
    CoverageInfo,
    DayCandleBarModel,
    DayCandlesNotCapturedResponse,
    DayCandlesRequest,
    DayCandlesResponse,
    ReturnDistributionInsufficientCoverageResponse,
    ReturnDistributionMeta,
    ReturnDistributionNotCapturedResponse,
    ReturnDistributionRequest,
    ReturnDistributionResponse,
)
from app.research.return_distribution import StudyRequestError
from app.services.return_distribution_service import (
    AdjustmentNotCoveredError,
    DayNotCapturedError,
    InsufficientCoverageError,
    SymbolNotCapturedError,
    compute_day_candles,
    compute_return_distribution,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post(
    "/return-distribution",
    response_model=ReturnDistributionResponse,
    responses={
        400: {
            "model": ReturnDistributionInsufficientCoverageResponse,
            "description": "The window holds fewer usable sessions than the statistics floor, "
            "or the study geometry is unusable.",
        },
        404: {
            "model": ReturnDistributionNotCapturedResponse,
            "description": "The symbol is not lake-addressable, or the on-demand capture "
            "could not populate the lake for it.",
        },
        409: {
            "model": AdjustmentNotCoveredResponse,
            "description": "The split and dividend adjustment does not cover the window, even "
            "after the on-demand capture rebuilt it; the study is refused rather than labelled "
            "adjusted.",
        },
    },
)
async def run_return_distribution(
    request: ReturnDistributionRequest,
) -> ReturnDistributionResponse:
    """Compute the daily-return distribution for one symbol and window."""
    try:
        outcome = await compute_return_distribution(
            symbol=request.symbol,
            from_ms_utc=request.from_ms_utc,
            to_ms_utc=request.to_ms_utc,
            bin_width_pct=request.bin_width_pct,
            span_pct=request.span_pct,
        )
    except SymbolNotCapturedError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "NOT_CAPTURED",
                "message": (
                    "this symbol can never be held by the data lake (not lake-addressable)"
                    if not e.lake_addressable
                    else "the on-demand capture could not populate the data lake for this "
                    "symbol and window"
                ),
                "capture_note": e.capture_note,
                "captured_symbols": e.captured_symbols,
            },
        ) from e
    except InsufficientCoverageError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INSUFFICIENT_COVERAGE",
                "message": str(e),
                "requested_sessions": e.requested,
                "available_sessions": e.available,
            },
        ) from e
    except AdjustmentNotCoveredError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "ADJUSTMENT_NOT_COVERED",
                "message": str(e),
                "capture_note": e.capture_note,
            },
        ) from e
    except StudyRequestError as e:
        # Geometry/empty-series failures from the pure module (e.g. span not
        # a multiple of bin width) — caller errors, not server errors. Other
        # ValueErrors from this path are internal data corruption and must
        # surface as 500s, not 400s.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    days = outcome.result.days
    coverage = CoverageInfo(
        requested_sessions=outcome.requested_sessions,
        returned_sessions=len(days),
        missing_sessions=outcome.missing_sessions,
        excluded_sessions=outcome.excluded_sessions,
        first_session_open_ms_utc=days[0].session_open_ms_utc if days else None,
        last_session_open_ms_utc=days[-1].session_open_ms_utc if days else None,
    )
    meta = ReturnDistributionMeta(
        symbol=request.symbol.upper(),
        from_ms_utc=request.from_ms_utc,
        to_ms_utc=request.to_ms_utc,
        bin_width_pct=request.bin_width_pct,
        span_pct=request.span_pct,
        adjustment=outcome.result.adjustment,
        capture=CaptureReceiptModel(
            status=outcome.capture.status,
            fetched_artifact_count=outcome.capture.fetched_artifact_count,
            detail=outcome.capture.detail,
        ),
        warnings=outcome.warnings,
    )
    return ReturnDistributionResponse.from_engine_result(
        meta=meta,
        coverage=coverage,
        result=outcome.result,
    )


@router.post(
    "/return-distribution/day-candles",
    response_model=DayCandlesResponse,
    responses={
        404: {
            "model": DayCandlesNotCapturedResponse,
            "description": "The symbol is not lake-addressable, or the lake holds no bars "
            "for that trading date.",
        },
        409: {
            "model": AdjustmentNotCoveredResponse,
            "description": "The split and dividend adjustment does not cover that trading date.",
        },
    },
)
async def run_day_candles(request: DayCandlesRequest) -> DayCandlesResponse:
    """One captured trading day's minute candles on the study's price basis."""
    try:
        outcome = await compute_day_candles(
            symbol=request.symbol,
            session_open_ms_utc=request.session_open_ms_utc,
        )
    except SymbolNotCapturedError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "NOT_CAPTURED",
                "message": (
                    "this symbol can never be held by the data lake (not lake-addressable)"
                    if not e.lake_addressable
                    else "the data lake holds no minute bars for this symbol"
                ),
                "trading_date": None,
            },
        ) from e
    except DayNotCapturedError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "DAY_NOT_CAPTURED",
                "message": str(e),
                "trading_date": e.trading_date.isoformat(),
            },
        ) from e
    except AdjustmentNotCoveredError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "ADJUSTMENT_NOT_COVERED",
                "message": str(e),
                "capture_note": e.capture_note,
            },
        ) from e
    return DayCandlesResponse(
        symbol=request.symbol.upper(),
        session_open_ms_utc=request.session_open_ms_utc,
        adjustment=outcome.adjustment,
        bars=[
            DayCandleBarModel(
                t=b.start_ms,
                o=float(b.open),
                h=float(b.high),
                l=float(b.low),
                c=float(b.close),
                v=float(b.volume),
            )
            for b in outcome.bars
        ],
    )
