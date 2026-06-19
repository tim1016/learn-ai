"""Tests for app.broker.ibkr.symbol_search (Slice 1F, issue #605).

Thin wrapper around ib_async's ``reqMatchingSymbolsAsync`` — the only
boundary work is mapping IBKR's ``ContractDescription`` payload onto the
repo-native ``SymbolMatch`` DTO. Network-touching paths are out of
scope for unit tests; integration tests cover the live-Gateway flow.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.ibkr.client import NotConnectedError
from app.broker.ibkr.symbol_search import search_symbols


def _make_contract_desc(
    *,
    symbol: str,
    name: str,
    exchange: str,
    currency: str,
    sec_type: str,
    derivative_sec_types: list[str],
) -> SimpleNamespace:
    """Mirror the shape of ib_async ``ContractDescription``."""
    return SimpleNamespace(
        contract=SimpleNamespace(
            symbol=symbol,
            secType=sec_type,
            currency=currency,
            primaryExchange=exchange,
            description=name,
        ),
        derivativeSecTypes=list(derivative_sec_types),
    )


def _mock_client(matches: list[SimpleNamespace]) -> SimpleNamespace:
    async def req(_pattern: str) -> list[SimpleNamespace]:
        return matches

    return SimpleNamespace(
        require_connected=lambda: None,
        ib=SimpleNamespace(reqMatchingSymbolsAsync=req),
    )


def _mock_disconnected_client() -> SimpleNamespace:
    def require_connected() -> None:
        raise NotConnectedError("IBKR client is not connected.")

    return SimpleNamespace(require_connected=require_connected, ib=None)


async def test_search_symbols_maps_contract_description_to_dto() -> None:
    matches = [
        _make_contract_desc(
            symbol="SPY",
            name="SPDR S&P 500 ETF Trust",
            exchange="ARCA",
            currency="USD",
            sec_type="STK",
            derivative_sec_types=["OPT", "FOP", "BAG"],
        ),
    ]
    client = _mock_client(matches)

    result = await search_symbols(client, "SP")

    assert len(result) == 1
    assert result[0].symbol == "SPY"
    assert result[0].name == "SPDR S&P 500 ETF Trust"
    assert result[0].exchange == "ARCA"
    assert result[0].currency == "USD"
    assert result[0].sec_type == "STK"
    assert result[0].derivative_sec_types == ["OPT", "FOP", "BAG"]


async def test_search_symbols_empty_pattern_short_circuits() -> None:
    """A blank pattern would hit IBKR's "invalid request" wire error;
    short-circuit so the caller's UX layer gets an empty list, not a 422."""

    client = _mock_client([])

    result = await search_symbols(client, "")

    assert result == []


async def test_search_symbols_filters_by_sec_type_when_specified() -> None:
    """The router exposes ``sec_type=STK|OPT|<empty>``. When set, the
    wrapper drops rows whose underlying contract's secType doesn't match
    so the picker doesn't surface futures when the user is searching for
    a stock leg's underlying."""

    matches = [
        _make_contract_desc(
            symbol="SPY",
            name="SPDR",
            exchange="ARCA",
            currency="USD",
            sec_type="STK",
            derivative_sec_types=["OPT"],
        ),
        _make_contract_desc(
            symbol="ES",
            name="E-mini",
            exchange="CME",
            currency="USD",
            sec_type="FUT",
            derivative_sec_types=[],
        ),
    ]
    client = _mock_client(matches)

    result = await search_symbols(client, "S", sec_type="STK")

    assert [r.symbol for r in result] == ["SPY"]


async def test_search_symbols_unknown_sec_type_is_dropped_silently() -> None:
    """IBKR can emit secType strings outside our allowlist (e.g. ``WAR``,
    ``BILL``). The DTO's ``Literal`` would 500 on validation; drop the row
    instead so the picker stays usable for the rest of the matches."""

    matches = [
        _make_contract_desc(
            symbol="SPY",
            name="SPDR",
            exchange="ARCA",
            currency="USD",
            sec_type="STK",
            derivative_sec_types=[],
        ),
        _make_contract_desc(
            symbol="WAR123",
            name="Warrant",
            exchange="ARCA",
            currency="USD",
            sec_type="WAR",  # not in our Literal allowlist
            derivative_sec_types=[],
        ),
    ]
    client = _mock_client(matches)

    result = await search_symbols(client, "SP")

    assert [r.symbol for r in result] == ["SPY"]


async def test_search_symbols_raises_when_disconnected() -> None:
    """The router translates NotConnectedError → 503; the wrapper itself
    just lets it bubble so the disconnect surfaces fail-fast."""

    client = _mock_disconnected_client()

    with pytest.raises(NotConnectedError):
        await search_symbols(client, "SPY")
