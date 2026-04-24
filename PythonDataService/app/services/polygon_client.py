"""Wrapper around Polygon.io REST client with error handling.

Proactive rate limiting
-----------------------
On the Polygon **Starter** plan every account gets **5 requests per minute**.
Exceeding that returns HTTP 429 and gets counted against a daily "bad-citizen"
quota that slows *all* subsequent traffic. Instead of reacting to 429s after
they happen, this client paces requests on the way out:

    Before sending any request, we check the timestamps of the last N
    requests. If N is already at the plan's per-minute ceiling, we sleep
    until the oldest one falls out of the 60-second window.

This adds deterministic latency that the UI surfaces as "Your Polygon Starter
plan allows 5 requests/minute — waiting X seconds for the next slot." Users
see why the app is slow rather than guessing.

See ``docs/references/polygon-throttle.md`` for the layman explanation.
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from polygon import RESTClient

from app.config import settings

logger = logging.getLogger(__name__)


# A progress callback the throttle invokes when it has to sleep — lets the
# SSE pipeline surface "next request paced for 9s" to the user.
ThrottleEvent = Callable[[dict[str, Any]], None]


class _PolygonThrottle:
    """Thread-safe sliding-window rate limiter for Polygon.

    Tracks the monotonic timestamps of the most recent requests and
    sleeps the caller just long enough to stay under ``max_per_min``.
    Zero disables the throttle (handy for local dev, or for Advanced-plan
    accounts with no per-minute cap). Uses a lock so concurrent
    FastAPI workers coordinate on the same budget.

    When ``on_event`` is supplied to :meth:`acquire`, the throttle emits a
    ``{"type": "chunk_paced", "wait_seconds": float, "label": str}`` event
    each time it sleeps. Callers wire this through to a per-request event
    queue so the UI can show a live "paced for 9s" readout.
    """

    def __init__(self, max_per_min: int):
        self._max = max_per_min
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(
        self,
        label: str = "polygon",
        on_event: ThrottleEvent | None = None,
    ) -> None:
        if self._max <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                # Evict any hits older than 60s from the left.
                while self._hits and now - self._hits[0] >= 60.0:
                    self._hits.popleft()
                if len(self._hits) < self._max:
                    self._hits.append(now)
                    return
                wait = 60.0 - (now - self._hits[0])
            if wait > 0:
                logger.info(
                    "[THROTTLE] Paused %.1fs on %s — your Polygon plan allows %d requests/min.",
                    wait,
                    label,
                    self._max,
                )
                if on_event is not None:
                    on_event({"type": "chunk_paced", "wait_seconds": wait, "label": label})
                time.sleep(wait)


class PolygonClientService:
    """Wrapper around Polygon.io REST client with error handling"""

    def __init__(self):
        self.client = RESTClient(api_key=settings.POLYGON_API_KEY)
        self._throttle = _PolygonThrottle(settings.POLYGON_RATE_LIMIT_PER_MIN)

    def fetch_aggregates(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_date: str,
        to_date: str,
        limit: int = 50000,
        adjusted: bool = True,
        sort: str = "asc",
        on_event: ThrottleEvent | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch aggregate bars (OHLCV) from Polygon.

        All Polygon ``/v2/aggs/ticker`` query parameters are passthrough:
        ``adjusted``, ``sort``, and ``limit`` map directly to the upstream
        request. ``timespan`` accepts any Polygon-supported value (``second``,
        ``minute``, ``hour``, ``day``, ``week``, ``month``, ``quarter``,
        ``year``); validation happens upstream in the request model.
        """
        try:
            logger.info(
                f"Fetching aggregates for {ticker}: {from_date} to {to_date} "
                f"(timespan={timespan}x{multiplier}, adjusted={adjusted}, sort={sort}, limit={limit})"
            )

            # Proactive pacing — one list_aggs call is one Polygon request
            # (pagination happens inside the SDK call; the SDK-level retry
            # is what eats the 429s when we run over). Acquiring before
            # .list_aggs keeps us under the per-minute ceiling.
            self._throttle.acquire(label=f"aggs:{ticker}", on_event=on_event)

            aggs = []
            for agg in self.client.list_aggs(
                ticker=ticker,
                multiplier=multiplier,
                timespan=timespan,
                from_=from_date,
                to=to_date,
                limit=limit,
                adjusted=adjusted,
                sort=sort,
            ):
                # Convert to dict for serialization
                aggs.append(
                    {
                        "timestamp": agg.timestamp,
                        "open": agg.open,
                        "high": agg.high,
                        "low": agg.low,
                        "close": agg.close,
                        "volume": agg.volume,
                        "vwap": agg.vwap if hasattr(agg, "vwap") else None,
                        "transactions": agg.transactions if hasattr(agg, "transactions") else None,
                    }
                )

            logger.info(f"Fetched {len(aggs)} aggregates for {ticker}")
            return aggs

        except Exception as e:
            logger.error(f"Error fetching aggregates for {ticker}: {e!s}")
            raise

    def fetch_trades(self, ticker: str, timestamp: str | None = None, limit: int = 50000) -> list[dict[str, Any]]:
        """Fetch real-time trades from Polygon"""
        try:
            logger.info(f"Fetching trades for {ticker}")

            trades = []
            for trade in self.client.list_trades(ticker=ticker, timestamp=timestamp, limit=limit):
                trades.append(
                    {
                        "timestamp": trade.sip_timestamp if hasattr(trade, "sip_timestamp") else trade.timestamp,
                        "price": trade.price,
                        "size": trade.size,
                        "exchange": trade.exchange if hasattr(trade, "exchange") else None,
                        "conditions": trade.conditions if hasattr(trade, "conditions") else None,
                        "sequence_number": trade.sequence_number if hasattr(trade, "sequence_number") else None,
                        "trade_id": trade.id if hasattr(trade, "id") else None,
                    }
                )

            logger.info(f"Fetched {len(trades)} trades for {ticker}")
            return trades

        except Exception as e:
            logger.error(f"Error fetching trades for {ticker}: {e!s}")
            raise

    # ------------------------------------------------------------------
    # Stock reference endpoints — exposed as Data Lab companion files
    # ------------------------------------------------------------------

    def list_splits(
        self,
        ticker: str,
        execution_date_gte: str | None = None,
        execution_date_lte: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """List historical splits for a ticker. Endpoint: GET /stocks/v1/splits"""
        try:
            out: list[dict[str, Any]] = []
            for s in self.client.list_splits(
                ticker=ticker,
                execution_date_gte=execution_date_gte,
                execution_date_lte=execution_date_lte,
                limit=min(limit, 1000),
            ):
                out.append(
                    {
                        "ticker": getattr(s, "ticker", ticker),
                        "execution_date": getattr(s, "execution_date", None),
                        "split_from": getattr(s, "split_from", None),
                        "split_to": getattr(s, "split_to", None),
                        "id": getattr(s, "id", None),
                    }
                )
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:
            logger.error(f"Error listing splits for {ticker}: {exc}")
            raise

    def list_dividends(
        self,
        ticker: str,
        ex_dividend_date_gte: str | None = None,
        ex_dividend_date_lte: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """List historical dividends for a ticker. Endpoint: GET /stocks/v1/dividends"""
        try:
            out: list[dict[str, Any]] = []
            for d in self.client.list_dividends(
                ticker=ticker,
                ex_dividend_date_gte=ex_dividend_date_gte,
                ex_dividend_date_lte=ex_dividend_date_lte,
                limit=min(limit, 1000),
            ):
                out.append(
                    {
                        "ticker": getattr(d, "ticker", ticker),
                        "ex_dividend_date": getattr(d, "ex_dividend_date", None),
                        "declaration_date": getattr(d, "declaration_date", None),
                        "record_date": getattr(d, "record_date", None),
                        "pay_date": getattr(d, "pay_date", None),
                        "cash_amount": getattr(d, "cash_amount", None),
                        "currency": getattr(d, "currency", None),
                        "dividend_type": getattr(d, "dividend_type", None),
                        "frequency": getattr(d, "frequency", None),
                    }
                )
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:
            logger.error(f"Error listing dividends for {ticker}: {exc}")
            raise

    def get_ticker_overview(self, ticker: str, as_of_date: str | None = None) -> dict[str, Any]:
        """Ticker details / company overview. Endpoint: GET /v3/reference/tickers/{ticker}"""
        try:
            details = self.client.get_ticker_details(ticker, date=as_of_date)
            # The SDK returns a dataclass-like object; shallow-flatten the common fields.
            fields = [
                "ticker",
                "name",
                "market",
                "locale",
                "primary_exchange",
                "type",
                "active",
                "currency_name",
                "cik",
                "composite_figi",
                "share_class_figi",
                "market_cap",
                "phone_number",
                "address",
                "description",
                "sic_code",
                "sic_description",
                "ticker_root",
                "homepage_url",
                "total_employees",
                "list_date",
                "share_class_shares_outstanding",
                "weighted_shares_outstanding",
            ]
            result: dict[str, Any] = {}
            for f in fields:
                result[f] = getattr(details, f, None)
            # address is a nested object
            if result["address"] is not None and not isinstance(result["address"], dict):
                result["address"] = {
                    k: getattr(result["address"], k, None) for k in ("address1", "city", "state", "postal_code")
                }
            return result
        except Exception as exc:
            logger.error(f"Error fetching ticker overview for {ticker}: {exc}")
            raise

    def list_news(
        self,
        ticker: str,
        published_utc_gte: str | None = None,
        published_utc_lte: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Ticker news. Endpoint: GET /v2/reference/news"""
        try:
            out: list[dict[str, Any]] = []
            for n in self.client.list_ticker_news(
                ticker=ticker,
                published_utc_gte=published_utc_gte,
                published_utc_lte=published_utc_lte,
                limit=min(limit, 1000),
            ):
                out.append(
                    {
                        "id": getattr(n, "id", None),
                        "publisher": getattr(n.publisher, "name", None) if getattr(n, "publisher", None) else None,
                        "title": getattr(n, "title", None),
                        "author": getattr(n, "author", None),
                        "published_utc": getattr(n, "published_utc", None),
                        "article_url": getattr(n, "article_url", None),
                        "tickers": ",".join(getattr(n, "tickers", []) or []),
                        "description": getattr(n, "description", None),
                        "keywords": ",".join(getattr(n, "keywords", []) or []),
                    }
                )
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:
            logger.error(f"Error listing news for {ticker}: {exc}")
            raise

    def list_financials(
        self,
        ticker: str,
        timeframe: str = "quarterly",
        filing_date_gte: str | None = None,
        filing_date_lte: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fundamental financials (balance sheet + income + cash flow).

        Endpoint: GET /vX/reference/financials

        ``timeframe`` accepts ``quarterly`` or ``annual``. Fields are
        flattened to one row per filing period.
        """
        try:
            out: list[dict[str, Any]] = []
            for f in self.client.vx.list_stock_financials(
                ticker=ticker,
                timeframe=timeframe,
                filing_date_gte=filing_date_gte,
                filing_date_lte=filing_date_lte,
                limit=min(limit, 100),
            ):

                def _get_leaf(stmt: Any, field: str) -> Any:
                    v = getattr(stmt, field, None) if stmt else None
                    return getattr(v, "value", None) if v is not None else None

                income = getattr(getattr(f, "financials", None), "income_statement", None)
                balance = getattr(getattr(f, "financials", None), "balance_sheet", None)
                cash = getattr(getattr(f, "financials", None), "cash_flow_statement", None)

                out.append(
                    {
                        "ticker": ticker,
                        "start_date": getattr(f, "start_date", None),
                        "end_date": getattr(f, "end_date", None),
                        "filing_date": getattr(f, "filing_date", None),
                        "fiscal_period": getattr(f, "fiscal_period", None),
                        "fiscal_year": getattr(f, "fiscal_year", None),
                        "timeframe": getattr(f, "timeframe", None),
                        # Income statement highlights
                        "revenues": _get_leaf(income, "revenues"),
                        "gross_profit": _get_leaf(income, "gross_profit"),
                        "operating_income_loss": _get_leaf(income, "operating_income_loss"),
                        "net_income_loss": _get_leaf(income, "net_income_loss"),
                        "basic_earnings_per_share": _get_leaf(income, "basic_earnings_per_share"),
                        "diluted_earnings_per_share": _get_leaf(income, "diluted_earnings_per_share"),
                        # Balance sheet highlights
                        "assets": _get_leaf(balance, "assets"),
                        "liabilities": _get_leaf(balance, "liabilities"),
                        "equity": _get_leaf(balance, "equity"),
                        # Cash flow highlights
                        "net_cash_flow_from_operating_activities": _get_leaf(
                            cash, "net_cash_flow_from_operating_activities"
                        ),
                        "net_cash_flow_from_investing_activities": _get_leaf(
                            cash, "net_cash_flow_from_investing_activities"
                        ),
                        "net_cash_flow_from_financing_activities": _get_leaf(
                            cash, "net_cash_flow_from_financing_activities"
                        ),
                    }
                )
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:
            logger.error(f"Error listing financials for {ticker}: {exc}")
            raise

    def list_stock_quotes(
        self,
        ticker: str,
        timestamp_gte_ns: int | None = None,
        timestamp_lte_ns: int | None = None,
        limit: int = 50000,
        cap: int = 500_000,
    ) -> list[dict[str, Any]]:
        """Historical NBBO quotes. Endpoint: GET /v3/quotes/{stockTicker}

        Tick-level data — can return millions of rows. ``cap`` protects the
        server from OOM; callers should set a reasonable window.
        """
        try:
            out: list[dict[str, Any]] = []
            for q in self.client.list_quotes(
                ticker=ticker,
                timestamp_gte=timestamp_gte_ns,
                timestamp_lte=timestamp_lte_ns,
                limit=min(limit, 50000),
            ):
                out.append(
                    {
                        "sip_timestamp_ns": getattr(q, "sip_timestamp", None),
                        "bid_price": getattr(q, "bid_price", None),
                        "bid_size": getattr(q, "bid_size", None),
                        "bid_exchange": getattr(q, "bid_exchange", None),
                        "ask_price": getattr(q, "ask_price", None),
                        "ask_size": getattr(q, "ask_size", None),
                        "ask_exchange": getattr(q, "ask_exchange", None),
                        "conditions": ",".join(str(c) for c in (getattr(q, "conditions", None) or [])),
                    }
                )
                if len(out) >= cap:
                    logger.warning(f"list_stock_quotes: hit cap={cap} for {ticker}")
                    break
            return out
        except Exception as exc:
            logger.error(f"Error listing quotes for {ticker}: {exc}")
            raise

    def list_stock_trades(
        self,
        ticker: str,
        timestamp_gte_ns: int | None = None,
        timestamp_lte_ns: int | None = None,
        limit: int = 50000,
        cap: int = 500_000,
    ) -> list[dict[str, Any]]:
        """Historical tick-level trades. Endpoint: GET /v3/trades/{stockTicker}"""
        try:
            out: list[dict[str, Any]] = []
            for t in self.client.list_trades(
                ticker=ticker,
                timestamp_gte=timestamp_gte_ns,
                timestamp_lte=timestamp_lte_ns,
                limit=min(limit, 50000),
            ):
                out.append(
                    {
                        "sip_timestamp_ns": getattr(t, "sip_timestamp", None),
                        "price": getattr(t, "price", None),
                        "size": getattr(t, "size", None),
                        "exchange": getattr(t, "exchange", None),
                        "conditions": ",".join(str(c) for c in (getattr(t, "conditions", None) or [])),
                        "trade_id": getattr(t, "id", None),
                        "tape": getattr(t, "tape", None),
                        "sequence_number": getattr(t, "sequence_number", None),
                    }
                )
                if len(out) >= cap:
                    logger.warning(f"list_stock_trades: hit cap={cap} for {ticker}")
                    break
            return out
        except Exception as exc:
            logger.error(f"Error listing trades for {ticker}: {exc}")
            raise

    def list_options_contracts(
        self,
        underlying_ticker: str,
        as_of_date: str | None = None,
        contract_type: str | None = None,
        strike_price_gte: float | None = None,
        strike_price_lte: float | None = None,
        expiration_date: str | None = None,
        expiration_date_gte: str | None = None,
        expiration_date_lte: str | None = None,
        expired: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List options contracts from Polygon for a given underlying ticker"""
        try:
            logger.info(f"Listing options contracts for {underlying_ticker}, as_of={as_of_date}")

            contracts = []
            # SDK limit is per-page; cap total results to avoid exhausting all pages
            page_size = min(limit, 250)
            for c in self.client.list_options_contracts(
                underlying_ticker=underlying_ticker,
                as_of=as_of_date,
                contract_type=contract_type,
                strike_price_gte=strike_price_gte,
                strike_price_lte=strike_price_lte,
                expiration_date=expiration_date,
                expiration_date_gte=expiration_date_gte,
                expiration_date_lte=expiration_date_lte,
                expired=expired,
                limit=page_size,
            ):
                contracts.append(
                    {
                        "ticker": c.ticker,
                        "underlying_ticker": c.underlying_ticker,
                        "contract_type": c.contract_type,
                        "strike_price": c.strike_price,
                        "expiration_date": c.expiration_date,
                        "exercise_style": getattr(c, "exercise_style", None),
                        "shares_per_contract": getattr(c, "shares_per_contract", None),
                        "primary_exchange": getattr(c, "primary_exchange", None),
                    }
                )
                if len(contracts) >= limit:
                    logger.info(f"Reached max {limit} contracts for {underlying_ticker}, stopping pagination")
                    break

            logger.info(f"Found {len(contracts)} options contracts for {underlying_ticker}")
            return contracts

        except Exception as e:
            logger.error(f"Error listing options contracts for {underlying_ticker}: {e!s}")
            raise

    def list_options_expirations(
        self,
        underlying_ticker: str,
        contract_type: str | None = None,
        expiration_date_gte: str | None = None,
        expiration_date_lte: str | None = None,
    ) -> list[str]:
        """Fetch unique expiration dates for an underlying ticker.

        Breaks the date range into ~30-day windows and fires all window
        requests **concurrently** via ThreadPoolExecutor (bypassing the
        auto-paginating client).  Each window retries up to 3 times with
        exponential backoff on transient errors (502/503/504, timeouts).
        A permanently-failing window is skipped so the remaining windows
        still return data.
        """
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime, timedelta

        import requests as _requests

        MAX_RETRIES = 3
        BASE_DELAY = 2.0  # seconds

        def _fetch_window(w_start_str: str, w_end_str: str, effective_type: str) -> set[str]:
            """Fetch expiration dates for a single 30-day window with retries."""
            window_dates: set[str] = set()
            for attempt in range(MAX_RETRIES):
                try:
                    params: dict[str, Any] = {
                        "underlying_ticker": underlying_ticker,
                        "contract_type": effective_type,
                        "expiration_date.gte": w_start_str,
                        "expiration_date.lte": w_end_str,
                        "limit": 1000,
                        "apiKey": settings.POLYGON_API_KEY,
                    }
                    resp = _requests.get(
                        "https://api.polygon.io/v3/reference/options/contracts",
                        params=params,
                        timeout=30,
                    )
                    if resp.status_code in (502, 503, 504):
                        delay = BASE_DELAY * (2**attempt)
                        logger.warning(
                            f"[Expirations] Window {w_start_str}..{w_end_str}: "
                            f"HTTP {resp.status_code}, retry {attempt + 1}/{MAX_RETRIES} "
                            f"in {delay:.1f}s"
                        )
                        time.sleep(delay)
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    for r in data.get("results", []):
                        exp = r.get("expiration_date")
                        if exp:
                            window_dates.add(exp)
                    return window_dates

                except (_requests.exceptions.ReadTimeout, _requests.exceptions.ConnectionError) as exc:
                    delay = BASE_DELAY * (2**attempt)
                    logger.warning(
                        f"[Expirations] Window {w_start_str}..{w_end_str}: "
                        f"{type(exc).__name__}, retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s"
                    )
                    time.sleep(delay)

            logger.error(
                f"[Expirations] Window {w_start_str}..{w_end_str}: failed after {MAX_RETRIES} retries, skipping"
            )
            return window_dates

        try:
            logger.info(
                f"Listing expirations for {underlying_ticker}, range=[{expiration_date_gte}, {expiration_date_lte}]"
            )

            today = datetime.now().strftime("%Y-%m-%d")
            start = datetime.strptime(expiration_date_gte or today, "%Y-%m-%d")
            end = datetime.strptime(
                expiration_date_lte or (datetime.now() + timedelta(days=180)).strftime("%Y-%m-%d"),
                "%Y-%m-%d",
            )

            effective_type = contract_type or "call"

            # Build list of (window_start, window_end) pairs
            windows: list[tuple[str, str]] = []
            window_days = 30
            window_start = start
            while window_start <= end:
                window_end = min(window_start + timedelta(days=window_days - 1), end)
                windows.append((window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")))
                window_start = window_end + timedelta(days=1)

            logger.info(f"[Expirations] Firing {len(windows)} window requests concurrently")

            # Fire all windows in parallel
            dates: set[str] = set()
            with ThreadPoolExecutor(max_workers=len(windows)) as pool:
                futures = {pool.submit(_fetch_window, ws, we, effective_type): (ws, we) for ws, we in windows}
                for future in as_completed(futures):
                    dates.update(future.result())

            result = sorted(dates)
            logger.info(f"Found {len(result)} unique expirations for {underlying_ticker} ({len(windows)} windows)")
            return result

        except Exception as e:
            err_msg = str(e).replace(settings.POLYGON_API_KEY, "***")
            logger.error(f"Error listing expirations for {underlying_ticker}: {err_msg}")
            raise

    def list_snapshot_options_chain(
        self,
        underlying_asset: str,
        expiration_date: str | None = None,
    ) -> dict[str, Any]:
        """Fetch snapshot of options chain for an underlying asset.

        Args:
            underlying_asset: Ticker symbol (e.g., AAPL)
            expiration_date: Filter to only this expiration date (YYYY-MM-DD).
                             Defaults to today if not specified.
        """
        try:
            # Default to today's date to avoid fetching thousands of contracts
            if expiration_date is None:
                expiration_date = datetime.now().strftime("%Y-%m-%d")

            logger.info(f"Fetching options chain snapshot for {underlying_asset}, expiration={expiration_date}")

            contracts = []
            underlying_info = None

            params: dict[str, Any] = {}
            if expiration_date:
                params["expiration_date"] = expiration_date

            for snapshot in self.client.list_snapshot_options_chain(
                underlying_asset=underlying_asset,
                params=params if params else None,
            ):
                # Capture underlying asset info from first result
                if underlying_info is None and hasattr(snapshot, "underlying_asset"):
                    ua = snapshot.underlying_asset
                    underlying_info = {
                        "ticker": getattr(ua, "ticker", None) or underlying_asset,
                        "price": getattr(ua, "price", None) or 0,
                        "change": getattr(ua, "change_to_break_even", None) or 0,
                        "change_percent": getattr(ua, "change_to_break_even", None) or 0,
                    }

                greeks = getattr(snapshot, "greeks", None)
                day = getattr(snapshot, "day", None)
                details = getattr(snapshot, "details", None)
                last_trade = getattr(snapshot, "last_trade", None)
                last_quote = getattr(snapshot, "last_quote", None)

                contract = {
                    "ticker": getattr(details, "ticker", None) if details else None,
                    "contract_type": getattr(details, "contract_type", None) if details else None,
                    "strike_price": getattr(details, "strike_price", None) if details else None,
                    "expiration_date": getattr(details, "expiration_date", None) if details else None,
                    "break_even_price": getattr(snapshot, "break_even_price", None),
                    "implied_volatility": getattr(snapshot, "implied_volatility", None),
                    "open_interest": getattr(snapshot, "open_interest", None),
                    "greeks": {
                        "delta": getattr(greeks, "delta", None),
                        "gamma": getattr(greeks, "gamma", None),
                        "theta": getattr(greeks, "theta", None),
                        "vega": getattr(greeks, "vega", None),
                    }
                    if greeks
                    else None,
                    "day": {
                        "open": getattr(day, "open", None),
                        "high": getattr(day, "high", None),
                        "low": getattr(day, "low", None),
                        "close": getattr(day, "close", None),
                        "volume": getattr(day, "volume", None),
                        "vwap": getattr(day, "vwap", None),
                    }
                    if day
                    else None,
                    "last_trade": {
                        "price": getattr(last_trade, "price", None),
                        "size": getattr(last_trade, "size", None),
                        "exchange": getattr(last_trade, "exchange", None),
                        "conditions": getattr(last_trade, "conditions", None),
                        "sip_timestamp": getattr(last_trade, "sip_timestamp", None),
                        "timeframe": getattr(last_trade, "timeframe", None),
                    }
                    if last_trade
                    else None,
                    "last_quote": {
                        "bid": getattr(last_quote, "bid", None),
                        "ask": getattr(last_quote, "ask", None),
                        "bid_size": getattr(last_quote, "bid_size", None),
                        "ask_size": getattr(last_quote, "ask_size", None),
                        "midpoint": getattr(last_quote, "midpoint", None),
                        "timeframe": getattr(last_quote, "timeframe", None),
                        "last_updated": getattr(last_quote, "last_updated", None),
                    }
                    if last_quote
                    else None,
                }
                contracts.append(contract)

            if underlying_info is None:
                underlying_info = {"ticker": underlying_asset, "price": 0, "change": 0, "change_percent": 0}

            # Fallback: if underlying price is 0/None, fetch from stock snapshot
            if not underlying_info.get("price"):
                try:
                    stock_snap = self.get_stock_snapshot(underlying_asset)
                    day = stock_snap.get("day", {})
                    prev_day = stock_snap.get("prev_day", {})
                    price = day.get("close") or prev_day.get("close") or 0
                    underlying_info["price"] = price
                    if price:
                        logger.info(f"Enriched underlying price from stock snapshot: {price}")
                except Exception as enrich_err:
                    logger.warning(f"Could not enrich underlying price: {enrich_err}")

            logger.info(f"Fetched {len(contracts)} options chain snapshots for {underlying_asset}")
            return {
                "underlying": underlying_info,
                "contracts": contracts,
            }

        except Exception as e:
            logger.error(f"Error fetching options chain snapshot for {underlying_asset}: {e!s}")
            raise

    def get_stock_snapshot(self, ticker: str) -> dict[str, Any]:
        """Fetch snapshot for a single stock ticker (v2 API)."""
        try:
            logger.info(f"Fetching stock snapshot for {ticker}")

            snapshot = self.client.get_snapshot_ticker("stocks", ticker)

            result = self._serialize_ticker_snapshot(snapshot)
            logger.info(f"Fetched snapshot for {ticker}")
            return result

        except Exception as e:
            logger.error(f"Error fetching stock snapshot for {ticker}: {e!s}")
            raise

    def get_stock_snapshots(
        self,
        tickers: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch snapshots for multiple stock tickers (v2 API).

        Args:
            tickers: List of ticker symbols. If None, returns all tickers.
        """
        try:
            ticker_str = ",".join(tickers) if tickers else None
            logger.info(f"Fetching stock snapshots for {ticker_str or 'all tickers'}")

            snapshots = self.client.get_snapshot_all("stocks", tickers=ticker_str)

            results = [self._serialize_ticker_snapshot(s) for s in snapshots]
            logger.info(f"Fetched {len(results)} stock snapshots")
            return results

        except Exception as e:
            logger.error(f"Error fetching stock snapshots: {e!s}")
            raise

    def get_market_movers(self, direction: str) -> list[dict[str, Any]]:
        """Fetch top market movers — gainers or losers (v2 API).

        Args:
            direction: "gainers" or "losers"
        """
        try:
            logger.info(f"Fetching market movers: {direction}")

            snapshots = self.client.get_snapshot_direction("stocks", direction)

            results = [self._serialize_ticker_snapshot(s) for s in snapshots]
            logger.info(f"Fetched {len(results)} {direction}")
            return results

        except Exception as e:
            logger.error(f"Error fetching market movers ({direction}): {e!s}")
            raise

    def get_unified_snapshots(
        self,
        tickers: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch unified snapshots via the v3 API.

        Args:
            tickers: Optional list of ticker symbols to filter.
            limit: Max results per page (default 10, max 250).
        """
        try:
            logger.info(f"Fetching unified snapshots: tickers={tickers}, limit={limit}")

            results = []
            for snapshot in self.client.list_universal_snapshots(
                ticker_any_of=tickers,
                limit=limit,
            ):
                session = getattr(snapshot, "session", None)
                results.append(
                    {
                        "ticker": getattr(snapshot, "ticker", None),
                        "type": getattr(snapshot, "type", None),
                        "market_status": getattr(snapshot, "market_status", None),
                        "name": getattr(snapshot, "name", None),
                        "session": {
                            "price": getattr(session, "price", None),
                            "change": getattr(session, "change", None),
                            "change_percent": getattr(session, "change_percent", None),
                            "open": getattr(session, "open", None),
                            "close": getattr(session, "close", None),
                            "high": getattr(session, "high", None),
                            "low": getattr(session, "low", None),
                            "previous_close": getattr(session, "previous_close", None),
                            "volume": getattr(session, "volume", None),
                        }
                        if session
                        else None,
                    }
                )

            logger.info(f"Fetched {len(results)} unified snapshots")
            return results

        except Exception as e:
            logger.error(f"Error fetching unified snapshots: {e!s}")
            raise

    @staticmethod
    def _serialize_bar(bar: Any) -> dict[str, Any] | None:
        """Serialize an Agg or MinuteSnapshot bar to a dict."""
        if bar is None:
            return None
        return {
            "open": getattr(bar, "open", None),
            "high": getattr(bar, "high", None),
            "low": getattr(bar, "low", None),
            "close": getattr(bar, "close", None),
            "volume": getattr(bar, "volume", None),
            "vwap": getattr(bar, "vwap", None),
        }

    @staticmethod
    def _serialize_minute_bar(bar: Any) -> dict[str, Any] | None:
        """Serialize a MinuteSnapshot bar with accumulated volume and timestamp."""
        if bar is None:
            return None
        return {
            "open": getattr(bar, "open", None),
            "high": getattr(bar, "high", None),
            "low": getattr(bar, "low", None),
            "close": getattr(bar, "close", None),
            "volume": getattr(bar, "volume", None),
            "vwap": getattr(bar, "vwap", None),
            "accumulated_volume": getattr(bar, "accumulated_volume", None),
            "timestamp": getattr(bar, "timestamp", None),
        }

    def _serialize_ticker_snapshot(self, snapshot: Any) -> dict[str, Any]:
        """Convert a TickerSnapshot to a serializable dict."""
        return {
            "ticker": getattr(snapshot, "ticker", None),
            "day": self._serialize_bar(getattr(snapshot, "day", None)),
            "prev_day": self._serialize_bar(getattr(snapshot, "prev_day", None)),
            "min": self._serialize_minute_bar(getattr(snapshot, "min", None)),
            "todays_change": getattr(snapshot, "todays_change", None),
            "todays_change_percent": getattr(snapshot, "todays_change_percent", None),
            "updated": getattr(snapshot, "updated", None),
        }

    def list_tickers(self, tickers: list[str]) -> list[dict[str, Any]]:
        """Fetch basic info for a list of stock tickers from Polygon reference API.

        Uses GET /v3/reference/tickers with limit=1000, then filters to requested tickers.
        """
        try:
            ticker_set = {t.upper() for t in tickers}
            logger.info(f"[Tickers] Fetching basic info for {len(ticker_set)} tickers")

            results = []
            for t in self.client.list_tickers(market="stocks", active=True, limit=1000):
                symbol = getattr(t, "ticker", None)
                if symbol and symbol in ticker_set:
                    results.append(
                        {
                            "ticker": symbol,
                            "name": getattr(t, "name", None) or "",
                            "market": getattr(t, "market", None) or "",
                            "type": getattr(t, "type", None) or "",
                            "active": getattr(t, "active", True),
                            "primary_exchange": getattr(t, "primary_exchange", None),
                            "currency_name": getattr(t, "currency_name", None),
                        }
                    )

            logger.info(f"[Tickers] Found {len(results)}/{len(ticker_set)} tickers")
            return results

        except Exception as e:
            logger.error(f"[Tickers] Error fetching ticker list: {e!s}")
            raise

    def get_ticker_details(self, ticker: str) -> dict[str, Any]:
        """Fetch detailed overview for a single ticker from Polygon.

        Uses GET /v3/reference/tickers/{ticker}.
        """
        try:
            logger.info(f"[Tickers] Fetching details for {ticker}")

            details = self.client.get_ticker_details(ticker)

            address = getattr(details, "address", None)
            result = {
                "ticker": getattr(details, "ticker", ticker),
                "name": getattr(details, "name", None) or "",
                "description": getattr(details, "description", None),
                "market_cap": getattr(details, "market_cap", None),
                "homepage_url": getattr(details, "homepage_url", None),
                "total_employees": getattr(details, "total_employees", None),
                "list_date": getattr(details, "list_date", None),
                "sic_description": getattr(details, "sic_description", None),
                "primary_exchange": getattr(details, "primary_exchange", None),
                "type": getattr(details, "type", None),
                "weighted_shares_outstanding": getattr(details, "weighted_shares_outstanding", None),
                "address": {
                    "address1": getattr(address, "address1", None),
                    "city": getattr(address, "city", None),
                    "state": getattr(address, "state", None),
                    "postal_code": getattr(address, "postal_code", None),
                }
                if address
                else None,
            }

            logger.info(f"[Tickers] Fetched details for {ticker}")
            return result

        except Exception as e:
            logger.error(f"[Tickers] Error fetching details for {ticker}: {e!s}")
            raise

    def get_related_companies(self, ticker: str) -> list[str]:
        """Fetch related company tickers from Polygon.

        Uses GET /v1/related-companies/{ticker}.
        """
        try:
            logger.info(f"[Tickers] Fetching related companies for {ticker}")

            response = self.client.get_related_companies(ticker)
            related = [getattr(r, "ticker", None) for r in (response or []) if getattr(r, "ticker", None)]

            logger.info(f"[Tickers] Found {len(related)} related companies for {ticker}")
            return related

        except Exception as e:
            logger.error(f"[Tickers] Error fetching related companies for {ticker}: {e!s}")
            raise

    def fetch_technical_indicator(
        self,
        ticker: str,
        indicator_type: str,  # sma, ema, rsi, macd
        timestamp: str | None = None,
        timespan: str = "day",
        window: int = 50,
        **kwargs,
    ) -> dict[str, Any]:
        """Fetch technical indicators from Polygon"""
        try:
            logger.info(f"Fetching {indicator_type.upper()} for {ticker}")

            # Map indicator types to client methods
            indicator_methods = {
                "sma": self.client.get_sma,
                "ema": self.client.get_ema,
                "rsi": self.client.get_rsi,
                "macd": self.client.get_macd,
            }

            if indicator_type.lower() not in indicator_methods:
                raise ValueError(f"Unsupported indicator type: {indicator_type}")

            method = indicator_methods[indicator_type.lower()]

            # Call appropriate method
            result = method(ticker=ticker, timestamp=timestamp, timespan=timespan, window=window, **kwargs)

            # Convert to serializable format
            return {
                "ticker": ticker,
                "indicator_type": indicator_type,
                "timestamp": result.timestamp if hasattr(result, "timestamp") else None,
                "values": result.values if hasattr(result, "values") else None,
                "metadata": {
                    "timespan": timespan,
                    "window": window,
                },
            }

        except Exception as e:
            logger.error(f"Error fetching {indicator_type} for {ticker}: {e!s}")
            raise
