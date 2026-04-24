"""API endpoints for data quality analysis: cleanup pipeline with before/after reporting.

CSV downloads (raw / clean) were removed — the data-lab component is the single
authority for generating dataset CSVs. This router now only produces the
before/after quality *report*, which the frontend renders and serializes to
markdown client-side for download.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services.data_quality_service import analyze, get_pipeline_docs
from app.services.polygon_client import PolygonClientService

router = APIRouter()
logger = logging.getLogger(__name__)
polygon_client = PolygonClientService()


class DataQualityRequest(BaseModel):
    """Request schema for data quality analysis"""

    ticker: str = Field(..., min_length=1, max_length=20, description="Ticker symbol")
    from_date: str = Field(..., description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., description="End date (YYYY-MM-DD)")
    volume_fix: str = Field("round", description="How to fix fractional volume: 'round', 'drop', or 'nullify'")
    recompute_indicators: bool = Field(True, description="Whether to recompute indicators from scratch")
    indicator_entries: list[dict[str, Any]] = Field(
        default=[],
        description="List of indicator entries, each with 'name' and optional 'params' dict",
    )


@router.post("/analyze")
async def analyze_data_quality(request: DataQualityRequest):
    """Run the full cleanup pipeline and return a before/after report.

    No CSV is cached or downloadable from this endpoint — the data-lab ZIP
    bundles the quality report alongside the dataset, and the frontend can
    serialize this response to markdown for standalone download.
    """
    try:
        logger.info(
            f"[DQ] Analyze request: {request.ticker} {request.from_date} to {request.to_date}, "
            f"volume_fix={request.volume_fix}, recompute={request.recompute_indicators}"
        )

        if request.volume_fix not in ("round", "drop", "nullify"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="volume_fix must be 'round', 'drop', or 'nullify'",
            )

        result = analyze(
            polygon=polygon_client,
            ticker=request.ticker,
            from_date=request.from_date,
            to_date=request.to_date,
            volume_fix=request.volume_fix,
            recompute_indicators=request.recompute_indicators,
            indicator_entries=request.indicator_entries or None,
        )

        if "error" in result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=result["error"],
            )

        return {"success": True, **result}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DQ] Analysis error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.get("/docs")
async def get_docs():
    """Return documentation for each cleanup step."""
    return {"success": True, "steps": get_pipeline_docs()}
