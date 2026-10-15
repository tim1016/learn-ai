"""HTTP answer for a research-store connection that cannot serve a request."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

import asyncpg
from fastapi import HTTPException, Request, Response, status
from fastapi.routing import APIRoute

from app.data_lake.catalog_client import CatalogUnavailableError

logger = logging.getLogger(__name__)

_STORE_UNREACHABLE = (CatalogUnavailableError, asyncpg.PostgresConnectionError, asyncpg.InterfaceError, ConnectionError)


class StoreUnreachableIsA503(APIRoute):
    """Answer research-store connection failures without exposing driver text."""

    unavailable_detail: dict[str, Any] = {
        "code": "RESEARCH_STORE_UNAVAILABLE",
        "message": "The research store could not be reached. Try again once it is back.",
    }

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def answered(request: Request) -> Response:
            try:
                return await handle(request)
            except _STORE_UNREACHABLE as exc:
                logger.warning(
                    "research store unreachable; answered 503",
                    extra={"action": "research_store_unreachable", "error_type": type(exc).__name__, "path": request.url.path},
                    exc_info=True,
                )
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=self.unavailable_detail) from exc

        return answered
