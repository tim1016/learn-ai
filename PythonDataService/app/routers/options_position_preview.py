"""Read-only position previews, shared by Options Backtest and Golden Search."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.options_backtest.fees import FeeRateNotPublished
from app.options_backtest.past_chain import ChainRefusal
from app.options_backtest.position_preview import PreviewUnavailable
from app.options_backtest.run import SessionDataMissing
from app.schemas.http_errors import ErrorDetailResponse
from app.schemas.options_position_preview import PositionPreviewRequest, PositionPreviewResponse
from app.services.options_position_preview import PositionPreviewService, get_position_preview_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post(
    "/position-preview",
    response_model=PositionPreviewResponse,
    responses={409: {"model": ErrorDetailResponse}, 503: {"model": ErrorDetailResponse}},
)
async def position_preview(
    request: PositionPreviewRequest,
    service: PositionPreviewService = Depends(get_position_preview_service),
) -> PositionPreviewResponse:
    try:
        return await service.preview(request)
    except (PreviewUnavailable, ChainRefusal, SessionDataMissing, FeeRateNotPublished) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(
            "Position preview data could not be read", extra={"error_type": type(exc).__name__}, exc_info=True
        )
        raise HTTPException(
            status_code=503,
            detail="The preview's market data could not be read. Retry, or check the selected session in Past chain.",
        ) from exc
