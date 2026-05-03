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

Pacing: IBKR's documented limits cap us at ~50 messages/sec and a
hardcoded 100 streaming-line quota per client. Callers are expected to
pre-narrow strikes to the ATM band; this module does not prevent
oversubscription on its own.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal

from app.broker.ibkr.client import IbkrClient
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
)

logger = logging.getLogger(__name__)


GENERIC_TICK_LIST = "100,101,106"  # bid/ask sizes + historical IV
DEFAULT_DEBOUNCE_S = 0.25


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _greeks_block(ticker, attr: Literal["modelGreeks", "bidGreeks", "askGreeks", "lastGreeks"]):
    """Return the named Greeks block from ``ticker`` or None."""
    return getattr(ticker, attr, None)


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
    bid = _coerce_optional_float(getattr(ticker, "bid", None))
    ask = _coerce_optional_float(getattr(ticker, "ask", None))
    last = _coerce_optional_float(getattr(ticker, "last", None))
    bid_size = getattr(ticker, "bidSize", None)
    ask_size = getattr(ticker, "askSize", None)
    if bid_size is not None and (isinstance(bid_size, float) and math.isnan(bid_size)):
        bid_size = None
    if ask_size is not None and (isinstance(ask_size, float) and math.isnan(ask_size)):
        ask_size = None

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

    # ib_async stamps Ticker.time when ticks land. Fall back to wall
    # clock so the snapshot always carries a timestamp.
    t = getattr(ticker, "time", None)
    if isinstance(t, datetime):
        ts_ms = int(t.timestamp() * 1000)
    else:
        ts_ms = _now_ms()

    return IbkrOptionQuote(
        symbol=symbol,
        expiry_ms=expiry_ms,
        strike=float(strike),
        right=right,
        bid=bid,
        ask=ask,
        last=last,
        bid_size=int(bid_size) if bid_size is not None else None,
        ask_size=int(ask_size) if ask_size is not None else None,
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

    # Underlying so we can populate ``underlying_price`` even when no
    # option modelGreeks block has computed yet.
    stock = await qualify_underlying(client, symbol)
    stock_ticker = client.ib.reqMktData(stock, "", False, False)

    contracts = await build_chain_contracts(client, symbol, expiry_ms, strikes)

    # Index contracts so the tick handler can recover (strike, right).
    by_conid: dict[int, tuple[float, OptionRight]] = {}
    tickers: list = []
    for c in contracts:
        by_conid[int(c.conId)] = (float(c.strike), c.right)
        tickers.append(client.ib.reqMktData(c, GENERIC_TICK_LIST, False, False))

    logger.info(
        "Subscribed %d tickers for %s expiry=%s strikes=%s",
        len(tickers),
        symbol,
        expiry_ms,
        strikes,
    )

    try:
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

            underlying_price = _coerce_optional_float(
                getattr(stock_ticker, "marketPrice", None)
            )
            if underlying_price is None:
                # ib_async ``marketPrice()`` is a method, not a field, on
                # some Ticker variants — tolerate either shape.
                mp = getattr(stock_ticker, "marketPrice", None)
                if callable(mp):
                    try:
                        underlying_price = _coerce_optional_float(mp())
                    except Exception:
                        underlying_price = None

            yield IbkrChainSnapshot(
                symbol=symbol,
                expiry_ms=expiry_ms,
                underlying_price=underlying_price,
                quotes=quotes,
                as_of_ms=_now_ms(),
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
