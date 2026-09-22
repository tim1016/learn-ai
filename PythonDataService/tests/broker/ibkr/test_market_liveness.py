"""IBKR status evidence must fail closed without an Alpaca data subscription."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from eventkit import Event
from ib_async import Stock, Ticker
from ib_async.objects import TickData

from app.broker.contract.models import BrokerClockEvidence
from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
from app.schemas.market_liveness import MarketLivenessFact, MarketStatusSnapshot
from app.services.market_liveness import MarketLivenessStore
from app.utils.timestamps import now_ms_utc

SourceFixture = tuple[IbkrMarketStatusSource, Mock, Ticker]

NOW = 1_789_569_273_347


async def poll(status: IbkrMarketStatusSource) -> MarketStatusSnapshot:
    """Drain the nonblocking supervisor's in-memory qualification/callbacks."""
    await status()
    for _ in range(12):
        await asyncio.sleep(0)
    return await status()


def emit(ticker: Ticker, at: int = NOW) -> None:
    ticker.time = datetime.fromtimestamp(at / 1000, tz=UTC)
    ticker.ticks = [TickData(ticker.time, 48, 512.0, 1)]
    if ticker.halted in (-1, 0, 1, 2):
        ticker.ticks.append(TickData(ticker.time, 49, ticker.halted, 0))
    if ticker.bid > 0:
        ticker.ticks.append(TickData(ticker.time, 1, ticker.bid, ticker.bidSize))
    if ticker.ask > 0:
        ticker.ticks.append(TickData(ticker.time, 2, ticker.ask, ticker.askSize))
    ticker.updateEvent.emit(ticker)


def fact(snapshot: MarketStatusSnapshot, at: int = NOW) -> MarketLivenessFact:
    store = MarketLivenessStore()
    store.observe_clock(BrokerClockEvidence(
        broker="alpaca", is_open=True, vendor_timestamp_ms=at,
        next_open_ms=None, next_close_ms=None, observed_at_ms=at,
    ))
    store.apply_status_snapshot(snapshot, now_ms=at)
    return store.fact("SPY", now_ms=at)


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> SourceFixture:
    ticker = Ticker(
        halted=float("nan"), marketDataType=1,
        rtTime=datetime.fromtimestamp(NOW / 1000, tz=UTC), contract=Stock("SPY", "SMART", "USD"),
    )
    client = Mock(
        connection_generation=1, connectivity_lost_count=0,
        connection_lost=False, connection_state="connected", last_event_ms=NOW,
        last_ibkr_code=None,
    )
    client.is_connected.return_value = True
    client.ib.errorEvent = Event()
    client.ib.wrapper.ticker2ReqId = {"mktData": Mock(get=Mock(return_value=7))}
    client.ib.reqMktData.return_value = ticker

    def request(contract: Any, *_args: Any) -> Ticker:
        returned = client.ib.reqMktData.return_value
        returned.contract = contract
        asyncio.get_running_loop().call_soon(emit, returned)
        return returned

    client.ib.reqMktData.side_effect = request
    monkeypatch.setattr(
        "app.broker.ibkr.market_liveness.qualify_underlying", AsyncMock(return_value=ticker.contract),
    )
    return IbkrMarketStatusSource(symbols=lambda: ("SPY",), client=lambda: client, clock=lambda: NOW), client, ticker


async def test_fresh_live_trade_proves_activity_without_a_watchlist_only_zero_tick(source: SourceFixture) -> None:
    status, client, ticker = source
    snapshot = await poll(status)
    assert snapshot.source == "ibkr.market_data.status"
    assert fact(snapshot).state == "TRADABLE"
    assert snapshot.subscriptions[0].trade_timestamp_ms == NOW
    client.ib.reqMktData.assert_called_once_with(ticker.contract, "233", False, False)
    await poll(status)
    client.ib.reqMktData.assert_called_once()
    await status.close()
    client.ib.cancelMktData.assert_called_once_with(ticker.contract)


@pytest.mark.parametrize("kind", ["delayed", "frozen", "missing", "stale", "future", "unavailable"])
async def test_unproven_or_non_live_data_never_implies_tradable(source: SourceFixture, kind: str) -> None:
    status, _, ticker = source
    if kind in ("delayed", "frozen"):
        ticker.marketDataType = 3 if kind == "delayed" else 2
    elif kind == "missing":
        ticker.rtTime = None
    elif kind == "unavailable":
        ticker.halted = -1
    else:
        offset = -5001 if kind == "stale" else 1
        ticker.rtTime = datetime.fromtimestamp((NOW + offset) / 1000, tz=UTC)
    snapshot = await poll(status)
    assert snapshot.symbol_statuses[0].state == "UNKNOWN"


async def test_halt_survives_disconnect_and_fresh_trades_until_explicit_resume(source: SourceFixture) -> None:
    status, client, ticker = source
    ticker.halted = 2
    assert fact(await poll(status)).state == "HALTED"
    client.connection_lost = True
    client.connection_state = "soft_lost"
    disconnected = await poll(status)
    assert not disconnected.connected
    assert disconnected.symbol_statuses[0].state == "HALTED"
    client.connection_lost = False
    client.connection_state = "connected"
    client.connectivity_lost_count += 1
    ticker.halted = float("nan")
    assert fact(await poll(status)).state == "HALTED"
    ticker.halted = 0
    emit(ticker)
    assert fact(await poll(status)).state == "TRADABLE"


async def test_unrequested_symbol_and_expired_status_cannot_borrow_another_symbols_proof(source: SourceFixture) -> None:
    status, _, _ = source
    store = MarketLivenessStore()
    store.observe_clock(BrokerClockEvidence(
        broker="alpaca", is_open=True, vendor_timestamp_ms=NOW,
        next_open_ms=None, next_close_ms=None, observed_at_ms=NOW,
    ))
    store.apply_status_snapshot(await poll(status), now_ms=NOW)
    assert store.fact("SPY", now_ms=NOW).state == "TRADABLE"
    assert store.fact("QQQ", now_ms=NOW).state == "UNKNOWN"
    # A fresh source connection cannot refresh an older, absent symbol record.
    store.apply_status_snapshot((await poll(status)).model_copy(update={
        "observed_at_ms": NOW + 5001, "symbol_statuses": (),
    }), now_ms=NOW + 5001)
    store.observe_clock(BrokerClockEvidence(
        broker="alpaca", is_open=True, vendor_timestamp_ms=NOW + 5001,
        next_open_ms=None, next_close_ms=None, observed_at_ms=NOW + 5001,
    ))
    assert store.fact("SPY", now_ms=NOW + 5001).state == "UNKNOWN"


async def test_explicit_resume_orders_by_status_observation_not_an_older_trade(source: SourceFixture) -> None:
    status, _, ticker = source
    store = MarketLivenessStore()
    ticker.halted = 1
    store.apply_status_snapshot(await poll(status), now_ms=NOW)
    ticker.halted = 0
    ticker.rtTime = datetime.fromtimestamp((NOW - 1000) / 1000, tz=UTC)
    emit(ticker)
    store.apply_status_snapshot(await poll(status), now_ms=NOW)
    assert store.status_snapshot(now_ms=NOW).symbol_statuses[0].state == "TRADABLE"


@pytest.mark.parametrize("connection_state", ["degraded_data_farm", "subscriptions_stale"])
async def test_unhealthy_data_session_discards_cached_resume_until_resubscribed(source: SourceFixture, connection_state: str) -> None:
    status, client, ticker = source
    ticker.halted = 0
    emit(ticker)
    assert fact(await poll(status)).state == "TRADABLE"

    # The API socket and order connection stay up during these data outages.
    client.connection_state = connection_state
    disconnected = await poll(status)
    assert not disconnected.connected
    assert disconnected.symbol_statuses[0].state == "UNKNOWN"
    client.ib.cancelMktData.assert_called_once_with(ticker.contract)
    assert not (await poll(status)).connected
    client.ib.reqMktData.assert_called_once()

    client.connection_state = "connected"
    recovered_ticker = Ticker(
        halted=float("nan"), marketDataType=1, rtTime=None, contract=ticker.contract,
    )
    client.ib.reqMktData.return_value = recovered_ticker
    recovered = await poll(status)
    assert recovered.connected
    assert recovered.symbol_statuses[0].state == "UNKNOWN"
    assert client.ib.reqMktData.call_count == 2
    recovered_ticker.halted = 0
    recovered_ticker.rtTime = datetime.fromtimestamp(NOW / 1000, tz=UTC)
    emit(recovered_ticker)
    assert fact(await poll(status)).state == "TRADABLE"


@pytest.mark.parametrize("connection_state", ["degraded_data_farm", "subscriptions_stale"])
async def test_data_session_failure_during_qualification_cannot_publish_tradable(source: SourceFixture, monkeypatch: pytest.MonkeyPatch, connection_state: str) -> None:
    status, client, ticker = source
    ticker.halted = 0

    async def qualify(*_args: Any) -> Any:
        client.connection_state = connection_state
        return ticker.contract

    monkeypatch.setattr("app.broker.ibkr.market_liveness.qualify_underlying", qualify)
    snapshot = await poll(status)
    assert not snapshot.connected
    assert snapshot.symbol_statuses[0].state == "UNKNOWN"
    client.ib.reqMktData.assert_not_called()


async def test_data_farm_recovery_between_polls_requires_new_status_subscription(source: SourceFixture) -> None:
    status, client, ticker = source
    ticker.halted = 0
    emit(ticker)
    assert fact(await poll(status)).state == "TRADABLE"
    # Farm-down and farm-restored events can both occur between polls without
    # changing the socket generation or the connectivity-lost counter.
    client.last_event_ms = NOW + 1
    client.ib.reqMktData.return_value = Ticker(
        halted=float("nan"), marketDataType=1, rtTime=None, contract=object(),
    )
    assert fact(await poll(status)).state == "UNKNOWN"
    client.ib.cancelMktData.assert_called_once_with(ticker.contract)
    assert client.ib.reqMktData.call_count == 2


async def test_live_top_of_book_rides_the_status_snapshot(source: SourceFixture) -> None:
    # #2007: an extended-hours safe flatten is priced from IBKR's live bid/ask,
    # read from the same demand-driven subscription that proves halts.
    status, _, ticker = source
    ticker.bid, ticker.ask, ticker.bidSize, ticker.askSize = 512.31, 512.36, 300.0, float("nan")

    snapshot = await poll(status)

    (quote,) = snapshot.quotes
    assert (quote.symbol, quote.bid, quote.ask) == ("SPY", 512.31, 512.36)
    assert (quote.bid_size, quote.ask_size) == (300, None)
    assert quote.observed_at_ms == NOW
    assert quote.source == "ibkr.market_data.status"


@pytest.mark.parametrize("kind", ["delayed", "no_bid", "sentinel_ask", "zero_bid", "absent"])
async def test_an_unusable_book_publishes_no_quote(source: SourceFixture, kind: str) -> None:
    status, _, ticker = source
    ticker.bid, ticker.ask = 512.31, 512.36
    if kind == "delayed":
        ticker.marketDataType = 3
    elif kind == "no_bid":
        ticker.bid = float("nan")
    elif kind == "sentinel_ask":
        ticker.ask = -1.0
    elif kind == "zero_bid":
        ticker.bid = 0.0
    else:
        ticker.bid, ticker.ask = float("nan"), float("nan")

    assert (await poll(status)).quotes == ()


async def test_a_disconnected_source_publishes_no_quote(source: SourceFixture) -> None:
    status, client, ticker = source
    ticker.bid, ticker.ask = 512.31, 512.36
    client.connection_state = "degraded_data_farm"

    assert (await poll(status)).quotes == ()


async def test_store_reads_quotes_and_accepts_explicit_preparation_demand(source: SourceFixture) -> None:
    status, _, ticker = source
    ticker.bid, ticker.ask = 512.31, 512.36
    store = MarketLivenessStore()
    store.apply_status_snapshot(await poll(status), now_ms=NOW)

    quote = store.top_of_book("spy", now_ms=NOW + 1_000)

    assert quote is not None and (quote.bid, quote.ask) == (512.31, 512.36)
    # A quote the source has not refreshed within the status freshness bound is
    # not a price anyone may confirm a limit against.
    assert store.top_of_book("SPY", now_ms=NOW + 5_001) is None
    # Only explicit preparation requests create demand; reads stay pure.
    store.request_symbol("QQQ", now_ms=now_ms_utc())
    assert store.top_of_book("QQQ", now_ms=now_ms_utc()) is None
    assert "QQQ" in store.requested_symbols()


async def test_a_snapshot_that_omits_a_symbol_drops_its_quote(source: SourceFixture) -> None:
    """Codex review of #2007: a dropped subscription, a disconnect, or a
    reconnect before the book arrives leaves the symbol out of the snapshot.
    Keeping the last one would let a remembered price answer as live while it
    is still inside the freshness window."""
    status, client, ticker = source
    ticker.bid, ticker.ask = 512.31, 512.36
    store = MarketLivenessStore()
    store.apply_status_snapshot(await poll(status), now_ms=NOW)
    assert store.top_of_book("SPY", now_ms=NOW + 1_000) is not None

    client.connection_state = "degraded_data_farm"
    store.apply_status_snapshot(await poll(status), now_ms=NOW)
    client.connection_state = "connected"
    ticker.bid, ticker.ask = float("nan"), float("nan")
    store.apply_status_snapshot(await poll(status), now_ms=NOW)

    assert store.top_of_book("SPY", now_ms=NOW + 1_000) is None


async def test_live_quotes_keep_a_quiet_trade_stream_ready(source: SourceFixture) -> None:
    """A last-trade timestamp is not the heartbeat of an active quote stream."""
    status, _, ticker = source
    ticker.bid, ticker.ask = 512.31, 512.36
    await poll(status)
    status._clock = lambda: NOW + 5_001
    ticker.time = datetime.fromtimestamp((NOW + 5_001) / 1000, tz=UTC)
    ticker.ticks = [SimpleNamespace(tickType=1), SimpleNamespace(tickType=2)]
    ticker.updateEvent.emit(ticker)

    snapshot = await poll(status)
    store = MarketLivenessStore()
    store.observe_clock(BrokerClockEvidence(
        broker="alpaca", is_open=True, vendor_timestamp_ms=NOW + 5_001,
        next_open_ms=None, next_close_ms=None, observed_at_ms=NOW + 5_001,
    ))
    store.apply_status_snapshot(snapshot, now_ms=NOW + 5_001)

    assert store.fact("SPY", now_ms=NOW + 5_001).state == "TRADABLE"


async def test_reading_a_quote_never_refreshes_its_receipt_timestamp(source: SourceFixture) -> None:
    status, _, ticker = source
    ticker.bid, ticker.ask = 512.31, 512.36
    first = await poll(status)
    status._clock = lambda: NOW + 4_000

    second = await poll(status)

    assert second.quotes[0].observed_at_ms == first.quotes[0].observed_at_ms


def test_fact_reads_do_not_create_subscription_demand() -> None:
    store = MarketLivenessStore()
    for _ in range(100):
        store.fact("SPY", now_ms=now_ms_utc())

    assert store.requested_symbols() == ()


async def test_callback_publishes_a_halt_without_waiting_for_a_poll(source: SourceFixture) -> None:
    status, _, ticker = source
    published = []
    status._publish_snapshot = published.append
    await poll(status)
    ticker.halted = 1
    emit(ticker)
    assert published[-1].symbol_statuses[0].state == "HALTED"
    assert fact(published[-1]).state == "HALTED"


async def test_halt_survives_source_restart_and_only_explicit_resume_clears_it(source: SourceFixture, tmp_path: Path) -> None:
    status, client, ticker = source
    status._halt_path = tmp_path / "halts.json"
    ticker.halted = 2
    assert fact(await poll(status)).state == "HALTED"
    await status.close()
    ticker.halted = float("nan")
    restarted = IbkrMarketStatusSource(
        symbols=lambda: ("SPY",), client=lambda: client, clock=lambda: NOW,
        halt_path=tmp_path / "halts.json",
    )
    assert fact(await poll(restarted)).state == "HALTED"
    ticker.halted = 0
    emit(ticker)
    assert fact(await poll(restarted)).state == "TRADABLE"
    await restarted.close()


async def test_data_maintained_reconnect_reuses_request_but_requires_new_receipts(source: SourceFixture) -> None:
    status, client, ticker = source
    original = await poll(status)
    client.connection_state = "soft_lost"
    client.connectivity_lost_count += 1
    assert fact(await poll(status)).state == "UNKNOWN"
    client.connection_state = "connected"
    client.last_ibkr_code = 1102
    client.last_event_ms += 1
    restored = await poll(status)
    assert restored.subscriptions[0].generation != original.subscriptions[0].generation
    assert fact(restored).state == "UNKNOWN"
    client.ib.reqMktData.assert_called_once()
    client.ib.cancelMktData.assert_not_called()
    emit(ticker)
    assert fact(await poll(status)).state == "TRADABLE"
    await status.close()


async def test_stall_blocks_then_repairs_without_reusing_old_callbacks(source: SourceFixture) -> None:
    status, client, ticker = source
    original = await poll(status)
    old_callback = status._subscriptions["SPY"].callback
    status._clock = lambda: NOW + 5_001
    assert fact(await poll(status), NOW + 5_001).state == "UNKNOWN"
    status._clock = lambda: NOW + 30_001
    assert fact(await poll(status), NOW + 30_001).state == "UNKNOWN"
    client.ib.cancelMktData.assert_called_once()
    replacement = Ticker(rtTime=None, contract=ticker.contract)
    client.ib.reqMktData.return_value = replacement
    status._clock = lambda: NOW + 31_001
    restarted = await poll(status)
    assert restarted.subscriptions[0].generation != original.subscriptions[0].generation
    old_callback(ticker)
    assert fact(await poll(status), NOW + 31_001).state == "UNKNOWN"
    replacement.rtTime = datetime.fromtimestamp((NOW + 31_001) / 1000, tz=UTC)
    emit(replacement, NOW + 31_001)
    assert fact(await poll(status), NOW + 31_001).state == "TRADABLE"
    await status.close()


async def test_slow_new_symbol_does_not_block_an_existing_subscription(source: SourceFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    status, client, ticker = source
    await poll(status)
    release = asyncio.Event()

    async def slow_qualification(*_args: Any) -> Any:
        await release.wait()
        return ticker.contract

    monkeypatch.setattr("app.broker.ibkr.market_liveness.qualify_underlying", slow_qualification)
    status._symbols = lambda: ("SPY", "QQQ")
    snapshot = await poll(status)
    assert fact(snapshot).state == "TRADABLE"
    assert snapshot.subscriptions[1].state == "STARTING"
    client.ib.reqMktData.assert_called_once()
    await status.close()


async def test_unavailable_status_blocks_even_when_quotes_are_fresh(source: SourceFixture) -> None:
    status, _, ticker = source
    ticker.bid, ticker.ask, ticker.halted = 512.31, 512.36, -1
    snapshot = await poll(status)
    assert snapshot.subscriptions[0].state == "READY"
    assert fact(snapshot).state == "UNKNOWN"
    assert fact(snapshot).reason_code == "SYMBOL_STATUS_UNKNOWN"


async def test_clear_status_alone_cannot_authorize_a_dead_price_stream(source: SourceFixture) -> None:
    status, _, ticker = source
    ticker.halted = 0
    await poll(status)
    status._clock = lambda: NOW + 5_001
    snapshot = await poll(status)
    assert snapshot.symbol_statuses[0].state == "TRADABLE"
    assert fact(snapshot, NOW + 5_001).state == "UNKNOWN"


async def test_request_rejection_is_published_immediately(source: SourceFixture) -> None:
    status, client, _ = source
    published = []
    status._publish_snapshot = published.append
    await poll(status)
    client.ib.errorEvent.emit(7, 354, "test entitlement refusal", None)
    assert published[-1].subscriptions[0].state == "UNAVAILABLE"
    assert fact(published[-1]).state == "UNKNOWN"


async def test_panel_poll_frequency_has_no_effect_on_deadline_or_subscription(source: SourceFixture) -> None:
    status, client, _ = source
    snapshot = await poll(status)
    store = MarketLivenessStore()
    store.apply_status_snapshot(snapshot, now_ms=NOW)
    store.observe_clock(BrokerClockEvidence(
        broker="alpaca", is_open=True, vendor_timestamp_ms=NOW,
        next_open_ms=None, next_close_ms=None, observed_at_ms=NOW,
    ))
    for offset in range(0, 5_001, 50):
        assert store.fact("SPY", now_ms=NOW + offset).state == "TRADABLE"
    assert store.fact("SPY", now_ms=NOW + 5_001).state == "UNKNOWN"
    assert store.requested_symbols() == ()
    client.ib.reqMktData.assert_called_once()
    client.ib.cancelMktData.assert_not_called()


@pytest.mark.parametrize("halted", [-1, 0, 1, 2])
def test_ingestion_preserves_the_vendor_halt_value(halted: int) -> None:
    from ib_async import IB

    from app.broker.ibkr.market_subscription import install_market_data_callbacks

    ib = IB()
    install_market_data_callbacks(ib.wrapper)
    ticker = Ticker(contract=Stock("SPY", "SMART", "USD"))
    ib.wrapper.reqId2Ticker[7] = ticker
    ib.wrapper.tickGeneric(7, 49, halted)
    assert ticker.halted == halted
    assert ticker.ticks[-1].price == halted


async def test_data_type_change_invalidates_prices_until_new_live_receipts(source: SourceFixture) -> None:
    status, _, ticker = source
    assert fact(await poll(status)).state == "TRADABLE"
    ticker.ticks = []
    ticker.marketDataType = 2
    ticker.updateEvent.emit(ticker)
    assert fact(await poll(status)).state == "UNKNOWN"
    ticker.marketDataType = 1
    ticker.updateEvent.emit(ticker)
    assert fact(await poll(status)).state == "UNKNOWN"
    emit(ticker)
    assert fact(await poll(status)).state == "TRADABLE"


def test_data_type_decoder_publishes_the_mode_transition() -> None:
    from ib_async import IB

    from app.broker.ibkr.market_subscription import install_market_data_callbacks

    ib = IB()
    install_market_data_callbacks(ib.wrapper)
    ticker = Ticker(contract=Stock("SPY", "SMART", "USD"))
    ib.wrapper.reqId2Ticker[7] = ticker
    ib.wrapper.marketDataType(7, 2)
    assert ticker.marketDataType == 2
    assert ticker in ib.wrapper.pendingTickers


async def test_non_live_halt_is_retained_but_non_live_clear_cannot_release_it(source: SourceFixture) -> None:
    status, _, ticker = source
    await poll(status)
    ticker.marketDataType, ticker.halted = 2, 1
    emit(ticker)
    assert fact(await poll(status)).state == "HALTED"
    ticker.halted = 0
    emit(ticker)
    assert fact(await poll(status)).state == "HALTED"
    ticker.marketDataType = 1
    ticker.ticks = [TickData(ticker.time, 48, 512.0, 1)]
    ticker.updateEvent.emit(ticker)
    assert fact(await poll(status)).state == "HALTED"
    emit(ticker)
    assert fact(await poll(status)).state == "TRADABLE"


async def test_failed_halt_persistence_cannot_clear_a_known_halt(
    source: SourceFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, _, ticker = source
    status._halt_path = tmp_path / "halts.json"
    published: list[MarketStatusSnapshot] = []
    status._publish_snapshot = published.append
    await poll(status)
    ticker.halted = 1
    emit(ticker)

    def fail_write(*_args: Any) -> None:
        raise OSError("test disk unavailable")

    monkeypatch.setattr("app.broker.ibkr.market_liveness.atomic_write_bytes", fail_write)
    ticker.halted = 0
    emit(ticker)
    assert fact(published[-1]).state == "HALTED"
    assert published[-1].subscriptions[0].state == "UNAVAILABLE"
    assert "storage" in published[-1].subscriptions[0].reason
    restored = IbkrMarketStatusSource(
        symbols=lambda: ("SPY",), client=lambda: None, clock=lambda: NOW,
        halt_path=status._halt_path,
    )
    assert (await restored()).symbol_statuses[0].state == "HALTED"
