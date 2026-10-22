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
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from eventkit import Event
from ib_async import Ticker
from ib_async.objects import TickData

from app.broker.ibkr.client import BrokerError
from app.broker.ibkr.surface import stream_option_surface
from app.options.expiry import option_expiry_anchor_ms
from tests._helpers.live_source import native_source


def _make_contract(*, conid: int, strike: float, right: str) -> SimpleNamespace:
    return SimpleNamespace(conId=conid, strike=strike, right=right)


class _FakeIb:
    """Just enough of ``ib_async.IB`` to drive the surface stream."""

    def __init__(self) -> None:
        self.cancel_calls: list = []
        self.errorEvent = Event()
        self.wrapper = SimpleNamespace(ticker2ReqId={"mktData": {}}, tickGeneric=lambda *args: None, marketDataType=lambda *args: None)
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

    def reqMktData(self, contract, _generic_ticks, _snapshot, _regulatory_snapshot):
        stock = not getattr(contract, "strike", 0)
        bid, ask = (420.50, 420.50) if stock else (1.20, 1.25)
        ticker = Ticker(contract=contract, marketDataType=1)
        ticker.bid, ticker.ask, ticker.last = bid, ask, bid
        ticker.bidSize = ticker.askSize = 10
        ticker.ticks = [TickData(None, 1, bid, 10), TickData(None, 2, ask, 10)]
        self.wrapper.ticker2ReqId["mktData"][ticker] = len(self.wrapper.ticker2ReqId["mktData"]) + 1
        asyncio.get_running_loop().call_soon(ticker.updateEvent.emit, ticker)
        return ticker

    def cancelMktData(self, contract) -> None:        self.cancel_calls.append(contract)


class _FakeClient:
    """Shim that satisfies the surface stream's ``IbkrClient`` contract."""

    data_loss_epoch = 0

    def __init__(self) -> None:
        self.connection_generation = 1
        self.connectivity_lost_count = self.last_event_ms = 0
        self.ib = _FakeIb()
        self.connection_state = "connected"
        self.connection_lost = False
        native_source(self)

    def require_connected(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_surface_emits_one_snapshot_per_debounce_window() -> None:
    client = _FakeClient()
    expiries = [1_800_046_800_000, 1_803_070_800_000]
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
    expiries = [option_expiry_anchor_ms(date(2027, 1, 15) + timedelta(days=day)) for day in range(10)]
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
async def test_surface_counts_lines_after_input_dedup() -> None:
    """A caller passing the same expiry / strike twice translates into
    one subscription downstream, so the cap check must dedupe up front —
    otherwise an over-cap projection would block a request that the
    backend would actually be able to handle."""
    client = _FakeClient()
    # Without dedup: 4 expiries × 12 strikes × 2 + 1 = 97 — fits under 100
    # only because dedupe collapses to 2 × 6.
    duplicated_expiries = [
        1_800_046_800_000,
        1_800_046_800_000,
        1_803_070_800_000,
        1_803_070_800_000,
    ]
    duplicated_strikes = [420.0, 420.0, 421.0, 421.0, 422.0, 422.0, 423.0, 423.0,
                          424.0, 424.0, 425.0, 425.0]

    agen = stream_option_surface(
        client,
        "SPY",
        duplicated_expiries,
        duplicated_strikes,
        debounce_seconds=0.0,
        max_lines=100,
    )
    snap = await asyncio.wait_for(agen.__anext__(), timeout=1.0)

    # Deduped: 2 expiries × 6 strikes × 2 + 1 = 25.
    assert snap.line_count == 1 + 2 * 6 * 2
    assert [e.expiry_ms for e in snap.expiries] == [1_800_046_800_000, 1_803_070_800_000]
    for group in snap.expiries:
        assert {q.strike for q in group.quotes} == {420.0, 421.0, 422.0, 423.0, 424.0, 425.0}

    await agen.aclose()


@pytest.mark.asyncio
async def test_surface_rejects_empty_strikes() -> None:
    client = _FakeClient()
    agen = stream_option_surface(
        client,
        "SPY",
        [1_800_046_800_000],
        [],
        debounce_seconds=0.0,
    )
    with pytest.raises(BrokerError, match="strikes must be non-empty"):
        await agen.__anext__()
