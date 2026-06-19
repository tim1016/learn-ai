"""Tests for app.broker.ibkr.contracts.search_option_contracts (Slice 1F).

The wrapper is one step beyond ``build_option_contract``: it takes the
concrete (symbol, expiry_ms, strike, right) drill-down picks from the
cockpit and returns one ``OptionContractMatch`` per qualified contract.

Unlike ``build_option_contract``, this returns the rich DTO
(``con_id``, ``local_symbol``, ``trading_class``, ``multiplier``)
because the picker persists those fields with the leg.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.ibkr.client import NotConnectedError
from app.broker.ibkr.contracts import search_option_contracts


def _qualified_option(
    *,
    con_id: int,
    symbol: str = "SPY",
    strike: float = 650.0,
    right: str = "C",
    yyyymmdd: str = "20251219",
    local_symbol: str = "SPY   251219C00650000",
    trading_class: str = "SPY",
    exchange: str = "SMART",
    currency: str = "USD",
    multiplier: str = "100",
) -> SimpleNamespace:
    """Mirror the shape of an ib_async qualified ``Option`` contract."""
    return SimpleNamespace(
        conId=con_id,
        symbol=symbol,
        lastTradeDateOrContractMonth=yyyymmdd,
        strike=strike,
        right=right,
        localSymbol=local_symbol,
        tradingClass=trading_class,
        exchange=exchange,
        currency=currency,
        multiplier=multiplier,
    )


def _mock_client(qualified: list[SimpleNamespace | None]) -> SimpleNamespace:
    async def qualify(*_contracts):
        return qualified

    return SimpleNamespace(
        require_connected=lambda: None,
        ib=SimpleNamespace(qualifyContractsAsync=qualify),
    )


def _mock_disconnected() -> SimpleNamespace:
    def require_connected() -> None:
        raise NotConnectedError("not connected")

    return SimpleNamespace(require_connected=require_connected, ib=None)


async def test_search_option_contracts_returns_qualified_match() -> None:
    client = _mock_client([_qualified_option(con_id=42)])

    result = await search_option_contracts(
        client,
        symbol="SPY",
        expiry_ms=1_766_188_800_000,
        strike=650.0,
        right="C",
    )

    assert len(result) == 1
    assert result[0].con_id == 42
    assert result[0].symbol == "SPY"
    assert result[0].strike == 650.0
    assert result[0].right == "C"
    assert result[0].multiplier == 100
    assert result[0].local_symbol == "SPY   251219C00650000"


async def test_search_option_contracts_strips_none_placeholders() -> None:
    """Mirror the ``build_chain_contracts`` regression: ib_async returns
    ``None`` for contracts the gateway can't resolve. The wrapper drops
    them silently so the cockpit's response is a clean list, not a
    sentinel-laden one."""

    client = _mock_client([_qualified_option(con_id=1), None])

    result = await search_option_contracts(
        client,
        symbol="SPY",
        expiry_ms=1_766_188_800_000,
        strike=650.0,
        right="C",
    )

    assert len(result) == 1
    assert result[0].con_id == 1


async def test_search_option_contracts_empty_when_nothing_qualifies() -> None:
    client = _mock_client([])

    result = await search_option_contracts(
        client,
        symbol="SPY",
        expiry_ms=1_766_188_800_000,
        strike=650.0,
        right="C",
    )

    assert result == []


async def test_search_option_contracts_raises_when_disconnected() -> None:
    client = _mock_disconnected()

    with pytest.raises(NotConnectedError):
        await search_option_contracts(
            client,
            symbol="SPY",
            expiry_ms=1_766_188_800_000,
            strike=650.0,
            right="C",
        )
