"""IBKR contract resolution helpers.

Phase 1 scope: equities and US equity options on SMART. The wrappers
take repo-native types (``int64`` ms expiry, ``str`` symbol, ``float``
strike, ``OptionRight`` literal) and return ``ib_async`` Contract
objects qualified by the connected gateway. Direct callers in this
package then pass them to ``reqMktData`` etc.

Why qualification matters: IBKR contracts must be uniquely identifiable
(``conId``) before any market-data request. ``qualifyContractsAsync``
fills that in by going to the wire once. We cache the result per
(symbol) for the underlying — the option-side cost is the per-strike
qualifier called inside the chain stream.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Literal

from app.broker.ibkr.client import IbkrClient
from app.options.expiry import option_expiry_anchor_ms, option_expiry_date

logger = logging.getLogger(__name__)


class OptionQualificationError(ValueError):
    """IBKR found no qualified underlying for a valid symbol request."""


# IBKR names an option expiry by its date in the exchange's calendar, as a
# ``YYYYMMDD`` string. Here an expiry is one ``int64 ms UTC`` instant, the
# date's 16:00 New York anchor (ADR 0022 (a)); the string exists at the wire only.


def expiry_ms_to_yyyymmdd(expiry_ms: int) -> str:
    """An option expiry → the ``YYYYMMDD`` string IBKR names it by. Refuses an instant that is not an expiry."""
    return option_expiry_date(expiry_ms).strftime("%Y%m%d")


def yyyymmdd_to_expiry_ms(yyyymmdd: str) -> int:
    """IBKR's ``YYYYMMDD`` expiry → that date's 16:00 New York anchor, ``int64 ms UTC``."""
    return option_expiry_anchor_ms(datetime.strptime(yyyymmdd, "%Y%m%d").date())


async def qualify_underlying(client: IbkrClient, symbol: str):
    """Return a qualified ``Stock`` contract for the underlying.

    Currently hard-codes ``SMART`` exchange and ``USD`` currency — the
    only combination Phase 1 uses. Generalisation is a router-line
    follow-up.
    """
    from ib_async import Stock

    client.require_connected()
    stock = Stock(symbol=symbol, exchange="SMART", currency="USD")
    qualified = await client.ib.qualifyContractsAsync(stock)
    if not qualified:
        raise OptionQualificationError(f"IBKR could not qualify underlying {symbol!r} (SMART/USD).")
    return qualified[0]


async def list_expirations(client: IbkrClient, symbol: str) -> list[int]:
    """Return all currently-listed option expiries for ``symbol``.

    Result is sorted ascending, each expiry as its 16:00 New York anchor
    in ``int64`` ms UTC.
    """
    client.require_connected()
    stock = await qualify_underlying(client, symbol)
    params = await client.ib.reqSecDefOptParamsAsync(
        underlyingSymbol=stock.symbol,
        futFopExchange="",
        underlyingSecType=stock.secType,
        underlyingConId=stock.conId,
    )
    if not params:
        return []
    # Multiple exchanges can list the same chain; deduplicate.
    expirations: set[str] = set()
    for p in params:
        expirations.update(p.expirations)
    return sorted(yyyymmdd_to_expiry_ms(e) for e in expirations)


async def list_strikes(
    client: IbkrClient,
    symbol: str,
    expiry_ms: int,
) -> list[float]:
    """All strikes IBKR lists for one (symbol, expiry).

    The ``reqSecDefOptParams`` payload reports strikes union-of-exchanges;
    we deduplicate before returning. This is intentionally cheap — Phase
    1 callers narrow to ATM ± window before subscribing tickers.
    """
    client.require_connected()
    stock = await qualify_underlying(client, symbol)
    params = await client.ib.reqSecDefOptParamsAsync(
        underlyingSymbol=stock.symbol,
        futFopExchange="",
        underlyingSecType=stock.secType,
        underlyingConId=stock.conId,
    )
    target = expiry_ms_to_yyyymmdd(expiry_ms)
    strikes: set[float] = set()
    for p in params:
        if target in p.expirations:
            strikes.update(p.strikes)
    return sorted(float(k) for k in strikes)


async def list_qualified_strikes(
    client: IbkrClient,
    symbol: str,
    expiry_ms: int,
) -> list[float]:
    """Return only strikes IBKR can actually qualify for one (symbol, expiry).

    ``reqSecDefOptParams`` reports strikes at (symbol, exchange) granularity:
    every $1 strike that exists on *any* expiry is included, even when the
    chosen expiry only lists $5 multiples. ``list_strikes`` filters that
    payload by expiry text but cannot tell which strikes are actually
    instantiated as contracts. We probe by qualifying both the call and
    put leg of each candidate and return the strikes whose **both** legs
    qualified — the chain stream subscribes to both sides per strike, so
    a one-sided strike would still trip the partial-qualification guard.
    """
    from ib_async import Option

    candidates = await list_strikes(client, symbol, expiry_ms)
    if not candidates:
        return []
    yyyymmdd = expiry_ms_to_yyyymmdd(expiry_ms)

    def _build(right: Literal["C", "P"]) -> list:
        return [
            Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=yyyymmdd,
                strike=float(k),
                right=right,
                exchange="SMART",
                currency="USD",
                multiplier="100",
            )
            for k in candidates
        ]

    qualified_calls, qualified_puts = await asyncio.gather(
        client.ib.qualifyContractsAsync(*_build("C")),
        client.ib.qualifyContractsAsync(*_build("P")),
    )
    call_strikes = {float(c.strike) for c in qualified_calls if c is not None}
    put_strikes = {float(c.strike) for c in qualified_puts if c is not None}
    return sorted(call_strikes & put_strikes)


async def build_chain_contracts(
    client: IbkrClient,
    symbol: str,
    expiry_ms: int,
    strikes: list[float],
) -> list:
    """Qualify both call and put for every requested strike at one expiry.

    Returns a flat list of qualified ``Option`` contracts. Order is
    ``[call_k0, put_k0, call_k1, put_k1, ...]`` — strike-major because
    that's the order the chain UI walks.
    """
    from ib_async import Option

    client.require_connected()
    yyyymmdd = expiry_ms_to_yyyymmdd(expiry_ms)
    raw: list = []
    for strike in strikes:
        for right in ("C", "P"):
            raw.append(
                Option(
                    symbol=symbol,
                    lastTradeDateOrContractMonth=yyyymmdd,
                    strike=float(strike),
                    right=right,
                    exchange="SMART",
                    currency="USD",
                    multiplier="100",
                )
            )
    # ib_async's qualifyContractsAsync can return a length-matching list
    # with None placeholders for contracts the gateway could not resolve.
    # Strip them so the caller's length guard sees the true qualified
    # count and fails fast with a clean error instead of crashing on
    # ``None.strike`` downstream.
    qualified = [c for c in await client.ib.qualifyContractsAsync(*raw) if c is not None]
    if len(qualified) != len(raw):
        logger.warning(
            "qualifyContractsAsync dropped %d/%d contracts for %s %s",
            len(raw) - len(qualified),
            len(raw),
            symbol,
            yyyymmdd,
        )
    return qualified
