"""Real-time option-surface stream (multi-expiry fan-out).

Sibling of :mod:`app.broker.ibkr.market_data` — that one streams one
``(symbol, expiry, strikes)`` chain; this one streams the same strike
band across **multiple** expiries so the 3D surface UI can render the
whole call/put landscape with one SSE connection.

Public surface: :func:`stream_option_surface` — yields
:class:`IbkrSurfaceSnapshot` per debounce window. The producer:

1. Resolves the underlying spot via a single qualified Stock contract.
2. Builds qualified Option contracts for ``strikes × {C,P}`` at every
   requested expiry.
3. Subscribes to streaming market data with the same generic-tick set
   (``"100,101,106"``) as the chain stream.
4. Coalesces tick events on a debounce timer into a per-expiry quote
   group, yields one :class:`IbkrSurfaceSnapshot` carrying every group.

Cancellation: cancels every outstanding ``reqMktData`` on iterator
exit so we don't leak server-side market-data lines.

Line cap: IBKR's documented per-client streaming-line quota is 100
(see :mod:`app.broker.ibkr.market_data` module docstring). The surface
fans more aggressively than the chain — N expiries × M strikes × 2
sides + 1 underlying — so we enforce a configurable hard cap up front
and refuse oversubscription rather than letting the gateway start
silently dropping lines.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.contracts import (
    build_chain_contracts,
    qualify_underlying,
)
from app.broker.ibkr.market_data import (
    GENERIC_TICK_LIST,
    _resolve_market_price,
    _ticker_to_quote,
)
from app.broker.ibkr.models import (
    IbkrOptionQuote,
    IbkrSurfaceExpiry,
    IbkrSurfaceSnapshot,
    OptionRight,
)

logger = logging.getLogger(__name__)


DEFAULT_DEBOUNCE_S = 0.25
DEFAULT_MAX_LINES = 100


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


async def stream_option_surface(
    client: IbkrClient,
    symbol: str,
    expiry_ms_list: list[int],
    strikes: list[float],
    *,
    debounce_seconds: float = DEFAULT_DEBOUNCE_S,
    max_lines: int = DEFAULT_MAX_LINES,
) -> AsyncIterator[IbkrSurfaceSnapshot]:
    """Yield successive snapshots of a multi-expiry option surface.

    Args:
        client: connected ``IbkrClient``.
        symbol: e.g. ``"SPY"``.
        expiry_ms_list: expirations to fan over, ``int64`` ms UTC each.
        strikes: strike band applied at every expiry (caller-narrowed).
        debounce_seconds: minimum gap between yielded snapshots.
        max_lines: hard cap on streaming market-data lines (default 100,
            matching IBKR's per-client quota). Includes the underlying.

    Yields:
        :class:`IbkrSurfaceSnapshot` per debounce window, with one
        :class:`IbkrSurfaceExpiry` group per requested expiry.

    Raises:
        BrokerError: if the projected line count exceeds ``max_lines``
            or if any expiry's contract qualification drops a leg.

    Cancels every market-data subscription on iterator exit.
    """
    client.require_connected()

    if not expiry_ms_list:
        raise BrokerError("stream_option_surface: expiry_ms_list must be non-empty.")
    if not strikes:
        raise BrokerError("stream_option_surface: strikes must be non-empty.")

    # Normalise inputs up front: the same expiry or strike passed twice
    # would only translate into one subscription downstream, so the cap
    # check has to count what we will actually subscribe, not raw caller
    # arity.
    sorted_expiries = sorted(set(int(e) for e in expiry_ms_list))
    unique_strikes = sorted(set(float(k) for k in strikes))

    # Project the line budget: 1 underlying + N expiries × M strikes × 2 sides.
    # Reject up front rather than letting IBKR's gateway start silently
    # dropping subscriptions past its 100-line per-client quota.
    projected = 1 + len(sorted_expiries) * len(unique_strikes) * 2
    if projected > max_lines:
        raise BrokerError(
            f"stream_option_surface: projected {projected} market-data lines "
            f"exceeds cap of {max_lines}. Narrow the strike band or expiry "
            f"window (N={len(sorted_expiries)} expiries × M={len(unique_strikes)} "
            f"strikes × 2 sides + 1 underlying)."
        )

    # Resolve underlying once — every expiry shares the same spot ticker.
    stock = await qualify_underlying(client, symbol)

    # Qualify every expiry's contract block before any market-data
    # subscription opens. If qualification drops a leg on any expiry, fail
    # fast so we don't half-subscribe a surface and have to cancel midway.
    qualified_by_expiry: dict[int, list] = {}
    for exp in sorted_expiries:
        contracts = await build_chain_contracts(client, symbol, exp, unique_strikes)
        expected = len(unique_strikes) * 2
        if len(contracts) != expected:
            raise BrokerError(
                f"Contract qualification dropped {expected - len(contracts)} of "
                f"{expected} contracts for {symbol} expiry={exp}. Refusing to "
                f"stream a partial surface."
            )
        qualified_by_expiry[exp] = contracts

    # Index each ticker by (expiry, conId) → (strike, right) so the
    # debounce loop can rebuild per-expiry quote groups in O(N) per cycle
    # without re-walking the qualified-contracts dict.
    by_expiry_conid: dict[int, dict[int, tuple[float, OptionRight]]] = {}
    tickers_by_expiry: dict[int, list] = {}
    stock_ticker = None
    all_contracts: list = []

    try:
        stock_ticker = client.ib.reqMktData(stock, "", False, False)
        for exp, contracts in qualified_by_expiry.items():
            conid_map: dict[int, tuple[float, OptionRight]] = {}
            tlist: list = []
            for c in contracts:
                conid_map[int(c.conId)] = (float(c.strike), c.right)
                tlist.append(client.ib.reqMktData(c, GENERIC_TICK_LIST, False, False))
                all_contracts.append(c)
            by_expiry_conid[exp] = conid_map
            tickers_by_expiry[exp] = tlist

        logger.info(
            "Subscribed surface: symbol=%s expiries=%d strikes=%d lines=%d",
            symbol,
            len(sorted_expiries),
            len(unique_strikes),
            projected,
        )

        while True:
            await asyncio.sleep(debounce_seconds)
            expiry_groups: list[IbkrSurfaceExpiry] = []
            for exp in sorted_expiries:
                conid_map = by_expiry_conid[exp]
                quotes: list[IbkrOptionQuote] = []
                for t in tickers_by_expiry[exp]:
                    conid = int(t.contract.conId)
                    if conid not in conid_map:
                        continue
                    strike, right = conid_map[conid]
                    quotes.append(
                        _ticker_to_quote(t, symbol, exp, strike, right),
                    )
                expiry_groups.append(
                    IbkrSurfaceExpiry(expiry_ms=exp, quotes=quotes),
                )

            underlying_price = _resolve_market_price(stock_ticker)

            yield IbkrSurfaceSnapshot(
                symbol=symbol,
                underlying_price=underlying_price,
                expiries=expiry_groups,
                line_count=projected,
                as_of_ms=_now_ms(),
            )
    finally:
        # Cancel every subscription so we don't leak market-data lines.
        for c in all_contracts:
            try:
                client.ib.cancelMktData(c)
            except Exception as exc:
                logger.debug("cancelMktData(%s) raised on shutdown: %s", c, exc)
        try:
            client.ib.cancelMktData(stock)
        except Exception as exc:
            logger.debug("cancelMktData(underlying) raised on shutdown: %s", exc)
