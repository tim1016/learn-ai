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
from datetime import UTC, datetime
from typing import Literal

from app.broker.ibkr.api_evidence import (
    evidence_request,
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
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
    get_ibkr_api_evidence_recorder().record(
        source="contracts.qualify_underlying",
        symbol=symbol,
        request=evidence_request(
            "qualifyContractsAsync",
            contract={"symbol": symbol, "secType": "STK", "exchange": "SMART", "currency": "USD"},
        ),
        response=evidence_response(
            "contractDetails",
            fields={"contract_count": len(qualified)},
            objects=qualified,
        ),
    )
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
    get_ibkr_api_evidence_recorder().record(
        source="contracts.list_expirations",
        symbol=symbol,
        request=evidence_request(
            "reqSecDefOptParamsAsync",
            underlyingSymbol=stock.symbol,
            futFopExchange="",
            underlyingSecType=stock.secType,
            underlyingConId=int(stock.conId),
        ),
        response=evidence_response(
            "securityDefinitionOptionParameter",
            fields={"row_count": len(params)},
            objects=params,
        ),
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
    get_ibkr_api_evidence_recorder().record(
        source="contracts.list_strikes",
        symbol=symbol,
        request=evidence_request(
            "reqSecDefOptParamsAsync",
            underlyingSymbol=stock.symbol,
            futFopExchange="",
            underlyingSecType=stock.secType,
            underlyingConId=int(stock.conId),
            expiry=expiry_ms_to_yyyymmdd(expiry_ms),
        ),
        response=evidence_response(
            "securityDefinitionOptionParameter",
            fields={"row_count": len(params)},
            objects=params,
        ),
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
    get_ibkr_api_evidence_recorder().record(
        source="contracts.build_option_contract",
        symbol=symbol,
        request=evidence_request(
            "qualifyContractsAsync",
            contract={
                "symbol": symbol,
                "secType": "OPT",
                "lastTradeDateOrContractMonth": expiry_ms_to_yyyymmdd(expiry_ms),
                "strike": float(strike),
                "right": right,
                "exchange": "SMART",
                "currency": "USD",
                "multiplier": "100",
            },
        ),
        response=evidence_response(
            "contractDetails",
            fields={"contract_count": len(qualified)},
            objects=qualified,
        ),
    )
    if not qualified:
        raise ValueError(
            f"IBKR could not qualify option "
            f"{symbol} {expiry_ms_to_yyyymmdd(expiry_ms)} {strike:g}{right}"
        )
    return qualified[0]


async def search_option_contracts(
    client: IbkrClient,
    *,
    symbol: str,
    expiry_ms: int,
    strike: float,
    right: OptionRight,
) -> list:
    """Qualify one (symbol, expiry, strike, right) option drill-down pick
    and return the rich ``OptionContractMatch`` rows (Slice 1F, #605).

    Mirrors ``build_option_contract`` but returns repo-native DTOs
    carrying ``con_id`` + ``local_symbol`` + ``trading_class`` +
    ``multiplier`` because the cockpit action-plan picker persists those
    alongside the leg. Returns ``[]`` when IBKR cannot qualify the
    contract — the picker shows the empty result inline rather than
    raising, which is consistent with the broker's "no such contract"
    response.
    """
    from ib_async import Option

    from app.schemas.broker_search import OptionContractMatch

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
    raw = await client.ib.qualifyContractsAsync(contract)
    get_ibkr_api_evidence_recorder().record(
        source="contracts.search_option_contracts",
        symbol=symbol,
        request=evidence_request(
            "qualifyContractsAsync",
            contract={
                "symbol": symbol,
                "secType": "OPT",
                "lastTradeDateOrContractMonth": expiry_ms_to_yyyymmdd(expiry_ms),
                "strike": float(strike),
                "right": right,
                "exchange": "SMART",
                "currency": "USD",
                "multiplier": "100",
            },
        ),
        response=evidence_response(
            "contractDetails",
            fields={"contract_count": len(raw)},
            objects=raw,
        ),
    )
    out: list[OptionContractMatch] = []
    for c in raw:
        if c is None:
            continue
        out.append(
            OptionContractMatch(
                con_id=int(c.conId),
                symbol=c.symbol,
                local_symbol=c.localSymbol,
                trading_class=c.tradingClass,
                exchange=c.exchange,
                currency=c.currency,
                expiry_ms=yyyymmdd_to_expiry_ms(c.lastTradeDateOrContractMonth),
                strike=float(c.strike),
                right=c.right,
                multiplier=int(c.multiplier),
            )
        )
    return out


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
