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
   chain snapshot, converts typed quote receipts → ``IbkrOptionQuote``, yields.

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
from collections.abc import AsyncIterator
from typing import Literal

from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.contracts import (
    build_chain_contracts,
    qualify_underlying,
)
from app.broker.ibkr.live.protocol import QuoteUpdate
from app.broker.ibkr.live.quotes import QuoteLease
from app.broker.ibkr.models import (
    IbkrChainSnapshot,
    IbkrOptionQuote,
    OptionRight,
    _coerce_iv,
    _coerce_optional_float,
    _coerce_quote,
    _coerce_size,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


DEFAULT_DEBOUNCE_S = 0.25


def _resolve_market_price(update: QuoteUpdate | None) -> float | None:
    return None if update is None else update.market_price


def _quote_to_model(
    update: QuoteUpdate | None,
    symbol: str,
    expiry_ms: int,
    strike: float,
    right: OptionRight,
) -> IbkrOptionQuote:
    """Convert one typed live receipt to the chain wire model.

    Greeks selection priority: ``modelGreeks`` → ``bidGreeks`` →
    ``askGreeks`` → ``lastGreeks`` → ``none``. Recorded in the
    ``greeks_source`` field so reconciliation against the engine can
    weight by source quality.
    """
    # Bid/ask/last are coerced via _coerce_quote (NaN OR negative ⇒ None)
    # so IBKR's "no quote available" sentinel ``-1.0`` is stripped at the
    # ingestion boundary instead of leaking through to mid-price math
    # and the UI table.
    bid = _coerce_quote(None if update is None else update.values.get("bid"))
    ask = _coerce_quote(None if update is None else update.values.get("ask"))
    last = _coerce_quote(None if update is None else update.values.get("last"))
    bid_size = _coerce_size(None if update is None else update.values.get("bidSize"))
    ask_size = _coerce_size(None if update is None else update.values.get("askSize"))

    greeks_source: Literal["model", "bid", "ask", "last", "none"] = "none"
    iv = delta = gamma = theta = vega = underlying = None
    for attr, label in (
        ("modelGreeks", "model"),
        ("bidGreeks", "bid"),
        ("askGreeks", "ask"),
        ("lastGreeks", "last"),
    ):
        block = None if update is None else update.greeks.get(attr)
        if block is None:
            continue
        candidate_iv = _coerce_iv(block.get("impliedVol"))
        if candidate_iv is None:
            continue
        iv = candidate_iv
        delta = _coerce_optional_float(block.get("delta"))
        gamma = _coerce_optional_float(block.get("gamma"))
        theta = _coerce_optional_float(block.get("theta"))
        vega = _coerce_optional_float(block.get("vega"))
        underlying = _coerce_optional_float(block.get("undPrice"))
        greeks_source = label  # type: ignore[assignment]
        break

    ts_ms = now_ms_utc() if update is None else update.received_at_ms

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

    quotes_by_contract: list[tuple[float, OptionRight, QuoteLease]] = []
    leases: list[QuoteLease] = []
    try:
        stock_lease = client.live_data.quote(stock)
        leases.append(stock_lease)
        for c in contracts:
            lease = client.live_data.quote(c)
            leases.append(lease)
            quotes_by_contract.append((float(c.strike), c.right, lease))

        logger.info(
            "Subscribed %d tickers for %s expiry=%s strikes=%s",
            len(quotes_by_contract),
            symbol,
            expiry_ms,
            strikes,
        )

        while True:
            await asyncio.sleep(debounce_seconds)
            quotes: list[IbkrOptionQuote] = []
            for strike, right, lease in quotes_by_contract:
                quotes.append(
                    _quote_to_model(lease.latest, symbol, expiry_ms, strike, right),
                )

            underlying_price = _resolve_market_price(stock_lease.latest)

            yield IbkrChainSnapshot(
                symbol=symbol,
                expiry_ms=expiry_ms,
                underlying_price=underlying_price,
                quotes=quotes,
                as_of_ms=now_ms_utc(),
            )
    finally:
        for lease in leases:
            lease.release()
