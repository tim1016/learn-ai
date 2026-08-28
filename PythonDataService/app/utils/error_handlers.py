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

async def clerk_sqlite_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer an unusable Clerk authority in the Clerk's own vocabulary.

    ``ClerkSqliteError`` means the SQLite authority cannot serve this request --
    absent, wrong identity, wrong schema, lease held elsewhere. That is a
    *state of the system*, not an unexpected fault, so it must not fall through
    to the catch-all 500: an operator staring at a post-reset account was being
    shown ``sqlite3.OperationalError: unable to open database file`` on the very
    endpoint that exists to explain the situation. 503 with a typed
    ``reason_code`` says "this authority is not currently usable", which is both
    true and actionable.
    """
    logger.warning(
        "Clerk SQLite authority unusable for this request",
        extra={"action": "clerk_authority_unusable", "error_type": type(exc).__name__},
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "success": False,
            "detail": {
                "reason_code": "clerk_authority_unusable",
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
        },
    )
