"""Polygon reference-endpoint companion files for the Data Lab ZIP.

Each helper pulls from one Polygon endpoint and returns either a CSV byte
payload (for list-shaped data) or a JSON byte payload (for the ticker
overview single-object endpoint). Every helper is tolerant of Polygon
failures — if the call raises, the companion is omitted with a warning
rather than failing the whole ZIP build.

Endpoint map:
  splits            → GET /stocks/v1/splits
  dividends         → GET /stocks/v1/dividends
  ticker_overview   → GET /v3/reference/tickers/{ticker}
  news              → GET /v2/reference/news
  financials        → GET /vX/reference/financials
  trades (tick)     → GET /v3/trades/{ticker}
  quotes (tick)     → GET /v3/quotes/{ticker}

Tick-level endpoints return millions of rows per day; they are gated by an
explicit ``enabled`` toggle in the config and capped server-side to avoid
blowing memory on an accidental click.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from typing import Any

from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)


def _write_csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else str(row.get(c)) for c in columns])
    return buf.getvalue().encode("utf-8")


def _iso_to_ns(iso_date: str, end_of_day: bool = False) -> int:
    """Convert a ``YYYY-MM-DD`` string to a nanosecond epoch timestamp (UTC).

    Polygon's ``/v3/trades`` and ``/v3/quotes`` endpoints accept nanoseconds
    for the ``timestamp`` filter. End-of-day uses 23:59:59.999 UTC.
    """
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59, microsecond=999_000)
    return int(d.timestamp() * 1_000_000_000)


def build_splits_csv(polygon: PolygonClientService, ticker: str, from_date: str, to_date: str) -> bytes | None:
    try:
        rows = polygon.list_splits(ticker=ticker, execution_date_gte=from_date, execution_date_lte=to_date)
        if not rows:
            return None
        return _write_csv(rows, ["ticker", "execution_date", "split_from", "split_to", "id"])
    except Exception as exc:
        logger.warning(f"[REF] splits fetch failed for {ticker}: {exc}")
        return None


def build_dividends_csv(polygon: PolygonClientService, ticker: str, from_date: str, to_date: str) -> bytes | None:
    try:
        rows = polygon.list_dividends(ticker=ticker, ex_dividend_date_gte=from_date, ex_dividend_date_lte=to_date)
        if not rows:
            return None
        return _write_csv(
            rows,
            [
                "ticker",
                "ex_dividend_date",
                "declaration_date",
                "record_date",
                "pay_date",
                "cash_amount",
                "currency",
                "dividend_type",
                "frequency",
            ],
        )
    except Exception as exc:
        logger.warning(f"[REF] dividends fetch failed for {ticker}: {exc}")
        return None


def build_ticker_overview_json(polygon: PolygonClientService, ticker: str, to_date: str) -> bytes | None:
    try:
        details = polygon.get_ticker_overview(ticker=ticker, as_of_date=to_date)
        if not details:
            return None
        return json.dumps(details, indent=2, default=str).encode("utf-8")
    except Exception as exc:
        logger.warning(f"[REF] ticker overview fetch failed for {ticker}: {exc}")
        return None


def build_news_csv(
    polygon: PolygonClientService,
    ticker: str,
    from_date: str,
    to_date: str,
    limit: int = 1000,
) -> bytes | None:
    try:
        rows = polygon.list_news(
            ticker=ticker,
            published_utc_gte=f"{from_date}T00:00:00Z",
            published_utc_lte=f"{to_date}T23:59:59Z",
            limit=limit,
        )
        if not rows:
            return None
        return _write_csv(
            rows,
            [
                "id",
                "publisher",
                "title",
                "author",
                "published_utc",
                "article_url",
                "tickers",
                "description",
                "keywords",
            ],
        )
    except Exception as exc:
        logger.warning(f"[REF] news fetch failed for {ticker}: {exc}")
        return None


def build_financials_csv(
    polygon: PolygonClientService,
    ticker: str,
    from_date: str,
    to_date: str,
    timeframe: str = "quarterly",
    limit: int = 100,
) -> bytes | None:
    try:
        rows = polygon.list_financials(
            ticker=ticker,
            timeframe=timeframe,
            filing_date_gte=from_date,
            filing_date_lte=to_date,
            limit=limit,
        )
        if not rows:
            return None
        return _write_csv(
            rows,
            [
                "ticker",
                "start_date",
                "end_date",
                "filing_date",
                "fiscal_period",
                "fiscal_year",
                "timeframe",
                "revenues",
                "gross_profit",
                "operating_income_loss",
                "net_income_loss",
                "basic_earnings_per_share",
                "diluted_earnings_per_share",
                "assets",
                "liabilities",
                "equity",
                "net_cash_flow_from_operating_activities",
                "net_cash_flow_from_investing_activities",
                "net_cash_flow_from_financing_activities",
            ],
        )
    except Exception as exc:
        logger.warning(f"[REF] financials fetch failed for {ticker}: {exc}")
        return None


def build_trades_csv(
    polygon: PolygonClientService,
    ticker: str,
    from_date: str,
    to_date: str,
    cap: int = 500_000,
) -> bytes | None:
    try:
        rows = polygon.list_stock_trades(
            ticker=ticker,
            timestamp_gte_ns=_iso_to_ns(from_date),
            timestamp_lte_ns=_iso_to_ns(to_date, end_of_day=True),
            cap=cap,
        )
        if not rows:
            return None
        return _write_csv(
            rows,
            [
                "sip_timestamp_ns",
                "price",
                "size",
                "exchange",
                "conditions",
                "trade_id",
                "tape",
                "sequence_number",
            ],
        )
    except Exception as exc:
        logger.warning(f"[REF] trades fetch failed for {ticker}: {exc}")
        return None


def build_quotes_csv(
    polygon: PolygonClientService,
    ticker: str,
    from_date: str,
    to_date: str,
    cap: int = 500_000,
) -> bytes | None:
    try:
        rows = polygon.list_stock_quotes(
            ticker=ticker,
            timestamp_gte_ns=_iso_to_ns(from_date),
            timestamp_lte_ns=_iso_to_ns(to_date, end_of_day=True),
            cap=cap,
        )
        if not rows:
            return None
        return _write_csv(
            rows,
            [
                "sip_timestamp_ns",
                "bid_price",
                "bid_size",
                "bid_exchange",
                "ask_price",
                "ask_size",
                "ask_exchange",
                "conditions",
            ],
        )
    except Exception as exc:
        logger.warning(f"[REF] quotes fetch failed for {ticker}: {exc}")
        return None
