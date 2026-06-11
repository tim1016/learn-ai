"""Tests for app.broker.ibkr.surface — multi-expiry option-surface stream.

These tests stand up a tiny ``ib_async``-shaped fake: a stub ``ib`` with
``qualifyContractsAsync`` / ``reqMktData`` / ``cancelMktData`` and an
:class:`IbkrClient` shim that satisfies ``require_connected`` and
``is_connected``. The goal is to exercise the fan-out, line-cap, and
teardown logic — actual tick conversion is covered in
``test_market_data.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.broker.ibkr.client import BrokerError
from app.broker.ibkr.surface import stream_option_surface


def _greeks(*, iv: float, delta: float) -> SimpleNamespace:
    return SimpleNamespace(
        impliedVol=iv,
        delta=delta,
        gamma=0.04,
        theta=-0.05,
        vega=0.10,
        undPrice=420.0,
    )


def _make_ticker(contract, *, bid: float, ask: float) -> SimpleNamespace:
    return SimpleNamespace(
        contract=contract,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        bidSize=10,
        askSize=10,
        modelGreeks=_greeks(iv=0.20, delta=0.55 if contract.right == "C" else -0.45),
        bidGreeks=None,
        askGreeks=None,
        lastGreeks=None,
        time=datetime(2026, 5, 2, 14, 30, tzinfo=UTC),
    )


def _make_contract(*, conid: int, strike: float, right: str) -> SimpleNamespace:
    return SimpleNamespace(conId=conid, strike=strike, right=right)


class _FakeIb:
    """Just enough of ``ib_async.IB`` to drive the surface stream."""

    def __init__(self) -> None:
        self.cancel_calls: list = []
        self._next_conid = 100
        self._qualify_calls = 0

    async def qualifyContractsAsync(self, *contracts):
        self._qualify_calls += 1
        out = []
        for c in contracts:
            # Underlying (Stock) qualification: no ``strike`` attr.
            if not hasattr(c, "strike") or c.strike == 0 or c.strike is None:
                c.conId = 1
                out.append(c)
                continue
            c.conId = self._next_conid
            self._next_conid += 1
            out.append(c)
        return out

    def reqMktData(self, contract, _generic_ticks, _snapshot, _regulatory_snapshot):        # Underlying gets a "spot" ticker with a marketPrice attribute.
        if not hasattr(contract, "strike") or contract.strike == 0 or contract.strike is None:
            return SimpleNamespace(contract=contract, marketPrice=420.50, time=None)
        return _make_ticker(contract, bid=1.20, ask=1.25)

    def cancelMktData(self, contract) -> None:        self.cancel_calls.append(contract)


class _FakeClient:
    """Shim that satisfies the surface stream's ``IbkrClient`` contract."""

    def __init__(self) -> None:
        self.ib = _FakeIb()

    def require_connected(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_surface_emits_one_snapshot_per_debounce_window() -> None:
    client = _FakeClient()
    expiries = [1_800_000_000_000, 1_802_592_000_000]
    strikes = [420.0, 425.0]

    agen = stream_option_surface(
        client,
        "SPY",
        expiries,
        strikes,
        debounce_seconds=0.0,
        max_lines=100,
    )

    snap = await asyncio.wait_for(agen.__anext__(), timeout=1.0)

    assert snap.symbol == "SPY"
    assert snap.line_count == 1 + 2 * 2 * 2
    assert snap.underlying_price == 420.50
    assert [e.expiry_ms for e in snap.expiries] == sorted(expiries)
    # Each expiry must carry one quote per (strike × right) = 4.
    for group in snap.expiries:
        assert len(group.quotes) == 4
        assert {q.right for q in group.quotes} == {"C", "P"}
        assert {q.strike for q in group.quotes} == {420.0, 425.0}

    await agen.aclose()
    # Teardown should cancel both option lines and the underlying.
    assert len(client.ib.cancel_calls) == 1 + 2 * 2 * 2


@pytest.mark.asyncio
async def test_surface_rejects_oversubscription() -> None:
    """N expiries × M strikes × 2 + 1 above the cap must fail fast."""
    client = _FakeClient()
    # 10 expiries × 6 strikes × 2 + 1 underlying = 121 lines > 100.
    expiries = list(range(1_800_000_000_000, 1_800_000_000_000 + 10))
    strikes = [400.0, 405.0, 410.0, 415.0, 420.0, 425.0]

    agen = stream_option_surface(
        client,
        "SPY",
        expiries,
        strikes,
        debounce_seconds=0.0,
        max_lines=100,
    )
    with pytest.raises(BrokerError, match="exceeds cap"):
        await agen.__anext__()


@pytest.mark.asyncio
async def test_surface_rejects_empty_expiries() -> None:
    client = _FakeClient()
    agen = stream_option_surface(
        client,
        "SPY",
        [],
        [420.0],
        debounce_seconds=0.0,
    )
    with pytest.raises(BrokerError, match="expiry_ms_list must be non-empty"):
        await agen.__anext__()


@pytest.mark.asyncio
async def test_surface_rejects_empty_strikes() -> None:
    client = _FakeClient()
    agen = stream_option_surface(
        client,
        "SPY",
        [1_800_000_000_000],
        [],
        debounce_seconds=0.0,
    )
    with pytest.raises(BrokerError, match="strikes must be non-empty"):
        await agen.__anext__()
