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

Line cap: IBKR's default user-level streaming-line allocation is 100
(shared by TWS and every API connection; see
:mod:`app.broker.ibkr.market_data` module docstring). The surface
fans more aggressively than the chain — N expiries × M strikes × 2
sides + 1 underlying — so we enforce a configurable hard cap up front
and refuse oversubscription rather than letting the gateway start
silently dropping lines.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.contracts import (
    build_chain_contracts,
    qualify_underlying,
)
from app.broker.ibkr.live.quotes import QuoteLease
from app.broker.ibkr.market_data import (
    _quote_to_model,
    _resolve_market_price,
)
from app.broker.ibkr.models import (
    IbkrOptionQuote,
    IbkrSurfaceExpiry,
    IbkrSurfaceSnapshot,
    OptionRight,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


DEFAULT_DEBOUNCE_S = 0.25
DEFAULT_MAX_LINES = 100


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
        max_lines: local hard cap on streaming market-data lines (default
            100, matching IBKR's default shared user allocation). Includes
            the underlying; operators must also account for lines open in
            TWS and sibling API clients.

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
    # dropping subscriptions past the default 100-line user allocation.
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

    quotes_by_expiry: dict[int, list[tuple[float, OptionRight, QuoteLease]]] = {}
    leases: list[QuoteLease] = []

    try:
        stock_lease = client.live_data.quote(stock)
        leases.append(stock_lease)
        for exp, contracts in qualified_by_expiry.items():
            qlist: list[tuple[float, OptionRight, QuoteLease]] = []
            for c in contracts:
                lease = client.live_data.quote(c)
                leases.append(lease)
                qlist.append((float(c.strike), c.right, lease))
            quotes_by_expiry[exp] = qlist

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
                quotes: list[IbkrOptionQuote] = []
                for strike, right, lease in quotes_by_expiry[exp]:
                    quotes.append(
                        _quote_to_model(lease.latest, symbol, exp, strike, right),
                    )
                expiry_groups.append(
                    IbkrSurfaceExpiry(expiry_ms=exp, quotes=quotes),
                )

            underlying_price = _resolve_market_price(stock_lease.latest)

            yield IbkrSurfaceSnapshot(
                symbol=symbol,
                underlying_price=underlying_price,
                expiries=expiry_groups,
                line_count=projected,
                as_of_ms=now_ms_utc(),
            )
    finally:
        for lease in leases:
            lease.release()
