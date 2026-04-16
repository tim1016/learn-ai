"""Error handling utilities"""

import logging

from fastapi import Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def polygon_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler for the application"""
    logger.error(f"Unhandled exception: {exc!s}", exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"success": False, "error": str(exc), "detail": "An error occurred while processing your request"},
    )
