"""Real-time option-chain stream.

Phase 1 surface: ``stream_option_chain`` — an ``AsyncIterator`` of
``IbkrChainSnapshot`` for one (symbol, expiry, strike-window). The
producer:

1. Resolves the underlying spot via a qualified Stock contract.
2. Builds qualified Option contracts for the requested strikes ×
   {call, put}.
3. Subscribes to streaming market data for every contract via
   ``reqMktData`` with **generic tick types** ``"100,101,106"`` so we
   get bid/ask sizes and historical/implied volatility on top of the
   default top-of-book + Greeks.
4. Coalesces tick events on a debounce timer (default 250 ms) into a
   chain snapshot, converts ``Ticker`` → ``IbkrOptionQuote``, yields.

Cancellation: when the consumer breaks out of the iterator, the
``finally`` clause cancels every outstanding ``reqMktData`` so we don't
leak server-side market-data lines.

Pacing: at the default 100-line allocation IBKR permits 50 requests/sec
per client connection; ``IbkrClient`` conservatively pins the transport to
45. Active lines are a user-level allocation shared by TWS and every API
client, not 100 fresh lines per connection. Callers are expected to pre-narrow
strikes to the ATM band; this module does not know the username's remaining
shared capacity.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal

from app.broker.ibkr.api_evidence import (
    evidence_request,
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.contracts import (
    build_chain_contracts,
    qualify_underlying,
)
from app.broker.ibkr.models import (
    IbkrChainSnapshot,
    IbkrOptionQuote,
    OptionRight,
    _coerce_iv,
    _coerce_optional_float,
    _coerce_quote,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


GENERIC_TICK_LIST = "100,101,106"  # bid/ask sizes + historical IV
DEFAULT_DEBOUNCE_S = 0.25


def _coerce_size(value) -> int | None:
    """Coerce an IBKR bid/ask size to ``int`` or ``None``.

    IBKR uses NaN and a negative sentinel (``-1``) for "no size available" on
    the L1 top-of-book line, the same way it uses ``-1.0`` for "no quote" on
    the price fields (see ``models._coerce_quote``). Sizes were previously only
    NaN-checked, so a ``-1`` leaked through to the wire as a negative depth.
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if value < 0:
        return None
    return int(value)


def _greeks_block(ticker, attr: Literal["modelGreeks", "bidGreeks", "askGreeks", "lastGreeks"]):
    """Return the named Greeks block from ``ticker`` or None."""
    return getattr(ticker, attr, None)


def _resolve_market_price(ticker) -> float | None:
    """Read ``ticker.marketPrice`` whether it's a method or a plain attribute.

    ``ib_async``'s ``Ticker.marketPrice`` is a method that derives the best
    mark from last / bid / ask. Test shims and earlier versions sometimes
    expose it as a plain attribute. Passing the bound method directly to
    ``_coerce_optional_float`` raises ``TypeError`` because ``float()``
    cannot accept a method, so callers route through this helper.
    """
    mp_attr = getattr(ticker, "marketPrice", None)
    if callable(mp_attr):
        try:
            return _coerce_optional_float(mp_attr())
        except Exception:
            return None
    return _coerce_optional_float(mp_attr)


def _ticker_to_quote(
    ticker,
    symbol: str,
    expiry_ms: int,
    strike: float,
    right: OptionRight,
) -> IbkrOptionQuote:
    """Convert one ``ib_async.Ticker`` snapshot to our wire model.

    Greeks selection priority: ``modelGreeks`` → ``bidGreeks`` →
    ``askGreeks`` → ``lastGreeks`` → ``none``. Recorded in the
    ``greeks_source`` field so reconciliation against the engine can
    weight by source quality.
    """
    # Bid/ask/last are coerced via _coerce_quote (NaN OR negative ⇒ None)
    # so IBKR's "no quote available" sentinel ``-1.0`` is stripped at the
    # ingestion boundary instead of leaking through to mid-price math
    # and the UI table.
    bid = _coerce_quote(getattr(ticker, "bid", None))
    ask = _coerce_quote(getattr(ticker, "ask", None))
    last = _coerce_quote(getattr(ticker, "last", None))
    bid_size = _coerce_size(getattr(ticker, "bidSize", None))
    ask_size = _coerce_size(getattr(ticker, "askSize", None))

    greeks_source: Literal["model", "bid", "ask", "last", "none"] = "none"
    iv = delta = gamma = theta = vega = underlying = None
    for attr, label in (
        ("modelGreeks", "model"),
        ("bidGreeks", "bid"),
        ("askGreeks", "ask"),
        ("lastGreeks", "last"),
    ):
        block = _greeks_block(ticker, attr)
        if block is None:
            continue
        candidate_iv = _coerce_iv(getattr(block, "impliedVol", None))
        if candidate_iv is None:
            continue
        iv = candidate_iv
        delta = _coerce_optional_float(getattr(block, "delta", None))
        gamma = _coerce_optional_float(getattr(block, "gamma", None))
        theta = _coerce_optional_float(getattr(block, "theta", None))
        vega = _coerce_optional_float(getattr(block, "vega", None))
        underlying = _coerce_optional_float(getattr(block, "undPrice", None))
        greeks_source = label  # type: ignore[assignment]
        break

    # ib_async stamps Ticker.time when ticks land. Only a tz-aware datetime is
    # trustworthy: .timestamp() on a naive datetime interprets it as process-
    # local time, yielding a ts_ms off by the UTC offset (timestamp-rigor ban,
    # and inconsistent with bars._to_utc_ms which rejects naive outright).
    # Convert tz-aware → UTC ms; otherwise fall back to wall clock.
    t = getattr(ticker, "time", None)
    if isinstance(t, datetime) and t.tzinfo is not None:
        ts_ms = int(t.astimezone(UTC).timestamp() * 1000)
    else:
        if isinstance(t, datetime):
            logger.warning(
                "Ignoring naive Ticker.time for %s; using wall clock",
                symbol,
                extra={"action": "naive_ticker_time"},
            )
        ts_ms = now_ms_utc()

    return IbkrOptionQuote(
        symbol=symbol,
        expiry_ms=expiry_ms,
        strike=float(strike),
        right=right,
        bid=bid,
        ask=ask,
        last=last,
        bid_size=bid_size,
        ask_size=ask_size,
        iv=iv,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        underlying_price=underlying,
        greeks_source=greeks_source,
        ts_ms=ts_ms,
    )


async def stream_option_chain(
    client: IbkrClient,
    symbol: str,
    expiry_ms: int,
    strikes: list[float],
    *,
    debounce_seconds: float = DEFAULT_DEBOUNCE_S,
) -> AsyncIterator[IbkrChainSnapshot]:
    """Yield successive snapshots of one (symbol, expiry) chain.

    Args:
        client: connected ``IbkrClient``.
        symbol: e.g. ``"SPY"``.
        expiry_ms: expiration timestamp, ``int64`` ms UTC.
        strikes: which strikes to subscribe (caller-narrowed).
        debounce_seconds: minimum gap between yielded snapshots.

    Yields:
        ``IbkrChainSnapshot`` per debounce window.

    Cancels every market-data subscription on iterator exit, and the
    underlying ticker subscription is request-scoped — there is no
    leakage of server-side lines once the consumer disconnects.
    """
    client.require_connected()

    # Resolve and qualify everything before opening any market-data
    # subscriptions. A leaked ``reqMktData`` from a setup that fails
    # partway through consumes one of the username's shared market-data
    # lines until the owning client reconnects.
    stock = await qualify_underlying(client, symbol)
    contracts = await build_chain_contracts(client, symbol, expiry_ms, strikes)

    # ``build_chain_contracts`` qualifies call+put for every requested
    # strike, so a complete chain has ``2 * len(strikes)`` contracts.
    # Anything less means qualification silently dropped one or more
    # contracts; rather than stream a half-shaped chain that the UI will
    # render with phantom holes, fail fast so the caller can re-narrow.
    expected = len(strikes) * 2
    if len(contracts) != expected:
        raise BrokerError(
            f"Contract qualification dropped {expected - len(contracts)} of {expected} "
            f"contracts for {symbol} expiry={expiry_ms}. Refusing to stream a partial chain."
        )

    # Index contracts so the tick handler can recover (strike, right).
    by_conid: dict[int, tuple[float, OptionRight]] = {}
    tickers: list = []
    stock_ticker = None
    try:
        stock_ticker = client.ib.reqMktData(stock, "", False, False)
        recorder = get_ibkr_api_evidence_recorder()
        recorder.record(
            source="market_data.stream_option_chain.underlying",
            symbol=symbol,
            request=evidence_request(
                "reqMktData",
                contract={"conId": int(stock.conId), "symbol": stock.symbol, "secType": stock.secType},
                genericTickList="",
                snapshot=False,
                regulatorySnapshot=False,
                mktDataOptions=[],
            ),
            response=evidence_response("tickSnapshot", objects=[stock_ticker]),
        )
        for c in contracts:
            by_conid[int(c.conId)] = (float(c.strike), c.right)
            ticker = client.ib.reqMktData(c, GENERIC_TICK_LIST, False, False)
            tickers.append(ticker)
            recorder.record(
                source="market_data.stream_option_chain.option",
                symbol=symbol,
                request=evidence_request(
                    "reqMktData",
                    contract={
                        "conId": int(c.conId),
                        "symbol": c.symbol,
                        "secType": c.secType,
                        "lastTradeDateOrContractMonth": c.lastTradeDateOrContractMonth,
                        "strike": float(c.strike),
                        "right": c.right,
                        "exchange": c.exchange,
                    },
                    genericTickList=GENERIC_TICK_LIST,
                    snapshot=False,
                    regulatorySnapshot=False,
                    mktDataOptions=[],
                ),
                response=evidence_response("tickSnapshot", objects=[ticker]),
            )

        logger.info(
            "Subscribed %d tickers for %s expiry=%s strikes=%s",
            len(tickers),
            symbol,
            expiry_ms,
            strikes,
        )

        while True:
            await asyncio.sleep(debounce_seconds)
            quotes: list[IbkrOptionQuote] = []
            for t in tickers:
                conid = int(t.contract.conId)
                if conid not in by_conid:
                    continue
                strike, right = by_conid[conid]
                quotes.append(
                    _ticker_to_quote(t, symbol, expiry_ms, strike, right),
                )

            underlying_price = _resolve_market_price(stock_ticker)
            recorder.record(
                source="market_data.stream_option_chain.tick",
                symbol=symbol,
                request=evidence_request(
                    "reqMktData",
                    contract_count=len(tickers) + 1,
                    genericTickList=GENERIC_TICK_LIST,
                    snapshot=False,
                    regulatorySnapshot=False,
                ),
                response=evidence_response(
                    "tickSnapshot",
                    fields={"ticker_count": len(tickers) + 1},
                    objects=[stock_ticker, *tickers],
                ),
            )

            yield IbkrChainSnapshot(
                symbol=symbol,
                expiry_ms=expiry_ms,
                underlying_price=underlying_price,
                quotes=quotes,
                as_of_ms=now_ms_utc(),
            )
    finally:
        # Cancel every subscription so we don't leak market-data lines.
        for c in contracts:
            try:
                client.ib.cancelMktData(c)
            except Exception as exc:
                logger.debug("cancelMktData(%s) raised on shutdown: %s", c, exc)
        try:
            client.ib.cancelMktData(stock)
        except Exception as exc:
            logger.debug("cancelMktData(underlying) raised on shutdown: %s", exc)
