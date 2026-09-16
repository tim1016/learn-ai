"""IBKR status evidence must fail closed without an Alpaca data subscription."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.broker.contract.models import BrokerClockEvidence
from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
from app.services.market_liveness import MarketLivenessStore

NOW = 1_789_569_273_347


@pytest.fixture
def source(monkeypatch):
    ticker = SimpleNamespace(
        halted=float("nan"), marketDataType=1,
        rtTime=datetime.fromtimestamp(NOW / 1000, tz=UTC), contract=object(),
    )
    client = Mock(
        connection_generation=1, connectivity_lost_count=0,
        connection_lost=False, connection_state="connected", last_event_ms=NOW,
    )
    client.is_connected.return_value = True
    client.ib.reqMktData.return_value = ticker
    monkeypatch.setattr(
        "app.broker.ibkr.market_liveness.qualify_underlying", AsyncMock(return_value=ticker.contract),
    )
    return IbkrMarketStatusSource(symbols=lambda: ("SPY",), client=lambda: client, clock=lambda: NOW), client, ticker


async def test_fresh_live_trade_proves_activity_without_a_watchlist_only_zero_tick(source):
    status, client, ticker = source
    snapshot = await status()
    assert snapshot.source == "ibkr.market_data.status"
    assert snapshot.symbol_statuses[0].state == "TRADABLE"
    assert snapshot.symbol_statuses[0].source_timestamp_ms == NOW
    client.ib.reqMktData.assert_called_once_with(ticker.contract, "233", False, False)
    await status()
    client.ib.reqMktData.assert_called_once()
    await status.close()
    client.ib.cancelMktData.assert_called_once_with(ticker.contract)


@pytest.mark.parametrize("kind", ["delayed", "frozen", "missing", "stale", "future", "unavailable"])
async def test_unproven_or_non_live_data_never_implies_tradable(source, kind):
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
    snapshot = await status()
    assert snapshot.symbol_statuses[0].state == "UNKNOWN"


async def test_halt_survives_disconnect_and_fresh_trades_until_explicit_resume(source):
    status, client, ticker = source
    ticker.halted = 2
    assert (await status()).symbol_statuses[0].state == "HALTED"
    client.connection_lost = True
    client.connection_state = "soft_lost"
    disconnected = await status()
    assert not disconnected.connected
    assert disconnected.symbol_statuses[0].state == "HALTED"
    client.connection_lost = False
    client.connection_state = "connected"
    client.connectivity_lost_count += 1
    ticker.halted = float("nan")
    assert (await status()).symbol_statuses[0].state == "HALTED"
    ticker.halted = 0
    assert (await status()).symbol_statuses[0].state == "TRADABLE"


async def test_unrequested_symbol_and_expired_status_cannot_borrow_another_symbols_proof(source):
    status, _, _ = source
    store = MarketLivenessStore()
    store.observe_clock(BrokerClockEvidence(
        broker="alpaca", is_open=True, vendor_timestamp_ms=NOW,
        next_open_ms=None, next_close_ms=None, observed_at_ms=NOW,
    ))
    store.apply_status_snapshot(await status(), now_ms=NOW)
    assert store.fact("SPY", now_ms=NOW).state == "TRADABLE"
    assert store.fact("QQQ", now_ms=NOW).state == "UNKNOWN"
    # A fresh source connection cannot refresh an older, absent symbol record.
    store.apply_status_snapshot((await status()).model_copy(update={
        "observed_at_ms": NOW + 5001, "symbol_statuses": (),
    }), now_ms=NOW + 5001)
    store.observe_clock(BrokerClockEvidence(
        broker="alpaca", is_open=True, vendor_timestamp_ms=NOW + 5001,
        next_open_ms=None, next_close_ms=None, observed_at_ms=NOW + 5001,
    ))
    assert store.fact("SPY", now_ms=NOW + 5001).state == "UNKNOWN"


async def test_explicit_resume_orders_by_status_observation_not_an_older_trade(source):
    status, _, ticker = source
    store = MarketLivenessStore()
    ticker.halted = 1
    store.apply_status_snapshot(await status(), now_ms=NOW)
    ticker.halted = 0
    ticker.rtTime = datetime.fromtimestamp((NOW - 1000) / 1000, tz=UTC)
    store.apply_status_snapshot(await status(), now_ms=NOW)
    assert store.status_snapshot(now_ms=NOW).symbol_statuses[0].state == "TRADABLE"


@pytest.mark.parametrize("connection_state", ["degraded_data_farm", "subscriptions_stale"])
async def test_unhealthy_data_session_discards_cached_resume_until_resubscribed(source, connection_state):
    status, client, ticker = source
    ticker.halted = 0
    assert (await status()).symbol_statuses[0].state == "TRADABLE"

    # The API socket and order connection stay up during these data outages.
    client.connection_state = connection_state
    disconnected = await status()
    assert not disconnected.connected
    assert disconnected.symbol_statuses[0].state == "UNKNOWN"
    client.ib.cancelMktData.assert_called_once_with(ticker.contract)
    assert not (await status()).connected
    client.ib.reqMktData.assert_called_once()

    client.connection_state = "connected"
    recovered_ticker = SimpleNamespace(
        halted=float("nan"), marketDataType=1, rtTime=None, contract=ticker.contract,
    )
    client.ib.reqMktData.return_value = recovered_ticker
    recovered = await status()
    assert recovered.connected
    assert recovered.symbol_statuses[0].state == "UNKNOWN"
    assert client.ib.reqMktData.call_count == 2
    recovered_ticker.halted = 0
    assert (await status()).symbol_statuses[0].state == "TRADABLE"


@pytest.mark.parametrize("connection_state", ["degraded_data_farm", "subscriptions_stale"])
async def test_data_session_failure_during_qualification_cannot_publish_tradable(source, monkeypatch, connection_state):
    status, client, ticker = source
    ticker.halted = 0

    async def qualify(*_args):
        client.connection_state = connection_state
        return ticker.contract

    monkeypatch.setattr("app.broker.ibkr.market_liveness.qualify_underlying", qualify)
    snapshot = await status()
    assert not snapshot.connected
    assert snapshot.symbol_statuses[0].state == "UNKNOWN"
    client.ib.cancelMktData.assert_called_once_with(ticker.contract)


async def test_data_farm_recovery_between_polls_requires_new_status_subscription(source):
    status, client, ticker = source
    ticker.halted = 0
    assert (await status()).symbol_statuses[0].state == "TRADABLE"
    # Farm-down and farm-restored events can both occur between polls without
    # changing the socket generation or the connectivity-lost counter.
    client.last_event_ms = NOW + 1
    client.ib.reqMktData.return_value = SimpleNamespace(
        halted=float("nan"), marketDataType=1, rtTime=None, contract=object(),
    )
    assert (await status()).symbol_statuses[0].state == "UNKNOWN"
    client.ib.cancelMktData.assert_called_once_with(ticker.contract)
    assert client.ib.reqMktData.call_count == 2
