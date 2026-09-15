"""Return-distribution study API router.

POST /api/research/return-distribution — daily-returns histogram with open
edge bins, session-segmented per-day drill-down, and summary statistics,
computed from lake minute bars (see app/research/return_distribution.py
for the math and its provenance). The router is transport only: it maps
the service's typed errors onto HTTP codes and the engine result onto the
wire model.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.models.return_distribution_models import (
    CoverageInfo,
    ReturnDistributionMeta,
    ReturnDistributionRequest,
    ReturnDistributionResponse,
)
from app.services.return_distribution_service import (
    InsufficientCoverageError,
    SymbolNotCapturedError,
    compute_return_distribution,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/return-distribution", response_model=ReturnDistributionResponse)
async def run_return_distribution(
    request: ReturnDistributionRequest,
) -> ReturnDistributionResponse:
    """Compute the daily-return distribution for one symbol and window."""
    try:
        outcome = await compute_return_distribution(
            symbol=request.symbol,
            from_date=request.from_date,
            to_date=request.to_date,
            bin_width_pct=request.bin_width_pct,
            span_pct=request.span_pct,
        )
    except SymbolNotCapturedError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "NOT_CAPTURED",
                "message": (
                    "the data lake holds no minute bars for this symbol in the "
                    "requested window — capture it via the Data Lab first"
                ),
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
    except ValueError as e:
        # Geometry/validation failures from the pure module (e.g. span not a
        # multiple of bin width) — caller errors, not server errors.
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
        from_date=request.from_date,
        to_date=request.to_date,
        bin_width_pct=request.bin_width_pct,
        span_pct=request.span_pct,
        adjustment=outcome.result.adjustment,
        warnings=outcome.warnings,
    )
    return ReturnDistributionResponse.from_engine_result(
        meta=meta,
        coverage=coverage,
        result=outcome.result,
    )
