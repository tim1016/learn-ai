"""Ticker-news HTTP boundary — GET /api/news.

One read endpoint that passes through every query parameter Polygon's
``/v2/reference/news`` accepts and returns every documented response field.

The upstream client is synchronous *and* self-throttling (the plan ceiling is
5 requests/minute, so ``acquire`` can sleep for seconds), which is why the call
is offloaded with ``to_thread`` rather than awaited inline — blocking the event
loop here would stall every other request in the service.

Sentiment carried in ``insights`` is vendor-asserted; see ``app/schemas/news.py``
for why that label is on the wire.

**Deliberate deviation from ``.claude/rules/temporal-rigor.md``** (CLAUDE.md
philosophy #4 requires this be stated rather than left silent): the five
``published_utc*`` *filter* parameters are vendor-format strings, not
``int64 ms UTC``. Polygon accepts a bare ``YYYY-MM-DD`` with whole-day
semantics that an instant in milliseconds cannot express, so canonicalizing
them would narrow what a caller can ask for with no gain. The rule binds
unchanged in the direction that carries data: every ``published_utc`` *value*
returned is canonicalized to ``published_utc_ms`` at the ingestion boundary in
``PolygonClientService.list_news``, and the vendor string is not kept.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Literal

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.news import NewsArticle, NewsResponse
from app.services.polygon_client import PolygonClientService
from app.utils.timestamps import now_ms_utc

router = APIRouter()
logger = logging.getLogger(__name__)

polygon_client = PolygonClientService()


@router.get("", response_model=NewsResponse)
async def list_news(
    ticker: str | None = Query(default=None, description="Exact ticker, case-sensitive (e.g. SPY)."),
    ticker_gte: str | None = Query(default=None, description="Ticker range: greater than or equal."),
    ticker_gt: str | None = Query(default=None, description="Ticker range: strictly greater than."),
    ticker_lte: str | None = Query(default=None, description="Ticker range: less than or equal."),
    ticker_lt: str | None = Query(default=None, description="Ticker range: strictly less than."),
    published_utc: str | None = Query(default=None, description="Exact publication date (YYYY-MM-DD or RFC3339)."),
    published_utc_gte: str | None = Query(default=None, description="Published on or after."),
    published_utc_gt: str | None = Query(default=None, description="Published strictly after."),
    published_utc_lte: str | None = Query(default=None, description="Published on or before."),
    published_utc_lt: str | None = Query(default=None, description="Published strictly before."),
    sort: str | None = Query(default=None, description="Field to order by; upstream supports published_utc."),
    order: Literal["asc", "desc"] | None = Query(default=None, description="Sort direction."),
    limit: int = Query(default=50, ge=1, le=1000, description="Max articles; upstream ceiling is 1000."),
) -> NewsResponse:
    """Fetch ticker news with vendor sentiment insights."""
    try:
        rows = await to_thread.run_sync(
            partial(
                polygon_client.list_news,
                ticker=ticker,
                ticker_gte=ticker_gte,
                ticker_gt=ticker_gt,
                ticker_lte=ticker_lte,
                ticker_lt=ticker_lt,
                published_utc=published_utc,
                published_utc_gte=published_utc_gte,
                published_utc_gt=published_utc_gt,
                published_utc_lte=published_utc_lte,
                published_utc_lt=published_utc_lt,
                sort=sort,
                order=order,
                limit=limit,
            )
        )
    except Exception as exc:
        logger.error("[News] Fetch failed", extra={"ticker": ticker, "error": str(exc)}, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream news fetch failed: {exc!s}",
        ) from exc

    articles = [NewsArticle.model_validate(row) for row in rows]
    logger.info("[News] Returning articles", extra={"ticker": ticker, "count": len(articles)})
    return NewsResponse(articles=articles, count=len(articles), fetched_at_ms=now_ms_utc())
