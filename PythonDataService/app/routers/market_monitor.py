"""API endpoints for market status and holiday calendar"""
from fastapi import APIRouter, HTTPException, status
import logging

from app.config import settings
from app.services.market_monitor import PolygonMarketMonitor
from app.models.responses import (
    ExchangeStatus,
    MarketStatusResponse,
    MarketHolidayEvent,
    MarketHolidaysResponse,
    MarketDashboardResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)

monitor = PolygonMarketMonitor(polygon_api_key=settings.POLYGON_API_KEY)


@router.get("/status", response_model=MarketStatusResponse)
async def get_market_status():
    """Get the current trading status of NYSE, NASDAQ, and overall market."""
    try:
        logger.info("[MarketMonitor] GET /status")
        state = monitor.get_current_market_state()

        exchanges_raw = state.get("exchanges", {})

        return MarketStatusResponse(
            success="error" not in state,
            market=state["market"],
            exchanges=ExchangeStatus(
                nyse=exchanges_raw.get("nyse"),
                nasdaq=exchanges_raw.get("nasdaq"),
                otc=exchanges_raw.get("otc"),
            ),
            early_hours=state["early_hours"],
            after_hours=state["after_hours"],
            server_time=state["server_time"],
            server_time_readable=state["server_time_readable"],
            error=state.get("error"),
        )

    except Exception as e:
        logger.error(f"[MarketMonitor] Error in get_market_status: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch market status: {e}",
        )


@router.get("/holidays", response_model=MarketHolidaysResponse)
async def get_market_holidays(limit: int = 5):
    """Get the next upcoming market holidays and early closes.

    - **limit**: Number of upcoming events to return (default 5)
    """
    try:
        logger.info(f"[MarketMonitor] GET /holidays?limit={limit}")
        events = monitor.get_upcoming_events(limit=limit)

        items = [MarketHolidayEvent(**ev) for ev in events]

        return MarketHolidaysResponse(
            success=True,
            events=items,
            count=len(items),
        )

    except Exception as e:
        logger.error(f"[MarketMonitor] Error in get_market_holidays: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch market holidays: {e}",
        )


@router.get("/dashboard", response_model=MarketDashboardResponse)
async def get_market_dashboard():
    """Get combined market status + upcoming holidays in a single call.

    Useful for the frontend dashboard calendar widget.
    """
    try:
        logger.info("[MarketMonitor] GET /dashboard")
        state = monitor.get_current_market_state()
        events = monitor.get_upcoming_events()

        exchanges_raw = state.get("exchanges", {})
        status_resp = MarketStatusResponse(
            success="error" not in state,
            market=state["market"],
            exchanges=ExchangeStatus(
                nyse=exchanges_raw.get("nyse"),
                nasdaq=exchanges_raw.get("nasdaq"),
                otc=exchanges_raw.get("otc"),
            ),
            early_hours=state["early_hours"],
            after_hours=state["after_hours"],
            server_time=state["server_time"],
            server_time_readable=state["server_time_readable"],
            error=state.get("error"),
        )

        holidays_resp = MarketHolidaysResponse(
            success=True,
            events=[MarketHolidayEvent(**ev) for ev in events],
            count=len(events),
        )

        return MarketDashboardResponse(
            success=True,
            status=status_resp,
            holidays=holidays_resp,
        )

    except Exception as e:
        logger.error(f"[MarketMonitor] Error in get_market_dashboard: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch market dashboard: {e}",
        )
