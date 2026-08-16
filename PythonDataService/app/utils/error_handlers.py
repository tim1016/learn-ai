"""Error handling utilities"""

import logging
import math
from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return a JSON-safe 422 even when rejected input is NaN/Infinity."""
    del request
    content = jsonable_encoder({"detail": exc.errors()})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_replace_non_finite(content),
    )


def _replace_non_finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _replace_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_non_finite(item) for item in value]
    return value


async def polygon_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler for the application"""
    logger.error(f"Unhandled exception: {exc!s}", exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"success": False, "error": str(exc), "detail": "An error occurred while processing your request"},
    )
