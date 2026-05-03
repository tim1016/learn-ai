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

import logging
from datetime import UTC, datetime

from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.models import OptionRight

logger = logging.getLogger(__name__)


# IBKR encodes option expiry as ``YYYYMMDD`` strings tied to the
# exchange's local calendar, not UTC. For US equity options that's
# America/New_York. We accept and emit ``int64`` ms UTC at the boundary
# and translate at the wire only.

_NY_OFFSET = UTC  # placeholder; we use date-only conversion below


def expiry_ms_to_yyyymmdd(expiry_ms: int) -> str:
    """``int64 ms UTC`` → ``YYYYMMDD`` string for IBKR.

    Uses the **UTC date** of the expiry timestamp. Callers are expected
    to pass timestamps that already represent the exchange-local
    expiration date midnight-UTC-equivalent — the convention used by the
    rest of the engine for option expiry.
    """
    dt = datetime.fromtimestamp(expiry_ms / 1000.0, tz=UTC)
    return dt.strftime("%Y%m%d")


def yyyymmdd_to_expiry_ms(yyyymmdd: str) -> int:
    """``YYYYMMDD`` → midnight-UTC ``int64 ms``.

    Companion to ``expiry_ms_to_yyyymmdd`` — round-trips cleanly through
    the date floor. We do NOT try to encode the 4pm ET close because
    Phase 1 only uses the date for symbol matching, not for time
    arithmetic.
    """
    dt = datetime.strptime(yyyymmdd, "%Y%m%d").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


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
        raise ValueError(f"IBKR could not qualify underlying {symbol!r} (SMART/USD).")
    return qualified[0]


async def list_expirations(client: IbkrClient, symbol: str) -> list[int]:
    """Return all currently-listed option expiries for ``symbol``.

    Result is sorted ascending and converted to ``int64`` ms UTC at the
    boundary so consumers stay timezone-safe per repo timestamp rigor.
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


async def build_option_contract(
    client: IbkrClient,
    symbol: str,
    expiry_ms: int,
    strike: float,
    right: OptionRight,
):
    """Construct + qualify a single option contract."""
    from ib_async import Option

    client.require_connected()
    contract = Option(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry_ms_to_yyyymmdd(expiry_ms),
        strike=float(strike),
        right=right,
        exchange="SMART",
        currency="USD",
        multiplier="100",
    )
    qualified = await client.ib.qualifyContractsAsync(contract)
    if not qualified:
        raise ValueError(
            f"IBKR could not qualify option "
            f"{symbol} {expiry_ms_to_yyyymmdd(expiry_ms)} {strike:g}{right}"
        )
    return qualified[0]


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
    qualified = await client.ib.qualifyContractsAsync(*raw)
    if len(qualified) != len(raw):
        logger.warning(
            "qualifyContractsAsync dropped %d/%d contracts for %s %s",
            len(raw) - len(qualified),
            len(raw),
            symbol,
            yyyymmdd,
        )
    return qualified
