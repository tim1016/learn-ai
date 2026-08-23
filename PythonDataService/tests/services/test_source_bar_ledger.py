"""Durable source-bar and synthetic-price boundaries for Dry Run."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.synthetic_broker import SyntheticBarBindingError, SyntheticBroker
from app.broker.contract.models import BrokerOrderLeg
from app.marketdata.feed import MarketDataBar
from app.services import source_bar_ledger
from app.services.bot_trade_strategy import _RetainedSourceBarFeed
from app.services.source_bar_ledger import (
    SourceBarConflictError,
    SourceBarLedger,
    SourceBarRetentionLimitError,
)


def _bar(
    *,
    close: str = "501.25",
    start_ms: int = 1_700_000_000_000,
    fetched_at_ms: int = 1_700_000_061_000,
) -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("500"),
        high=Decimal("502"),
        low=Decimal("499"),
        close=Decimal(close),
        volume=42,
        fetched_at_ms=fetched_at_ms,
        feed_id="polygon-minute",
        session_phase="RTH",
    )


def test_source_bar_ledger_is_idempotent_but_refuses_revised_identity(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")

    first = ledger.append(_bar())
    replay = ledger.append(_bar())

    assert replay == first
    assert len(ledger.bars(provider="polygon-minute", symbol="SPY")) == 1
    with pytest.raises(SourceBarConflictError, match="SOURCE_BAR_IDENTITY_CONFLICT"):
        ledger.append(_bar(close="502.25"))


def test_source_bar_ledger_treats_later_fetch_time_as_exact_redelivery(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")

    retained = ledger.append(_bar())
    replay = ledger.append(_bar(fetched_at_ms=1_700_000_099_000))

    assert replay == retained
    assert ledger.find_by_market_bar(_bar()) == retained


def test_source_bar_ledger_uses_sqlite_identity_index_after_restart(tmp_path: Path) -> None:
    original = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    original.append(_bar())
    restarted = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")

    retained = restarted.append(_bar(start_ms=1_700_000_060_000))

    assert retained.seq == 2
    assert restarted.path.name == "source_bars.sqlite3"


def test_source_bar_ledger_rejects_non_monotonic_live_delivery(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    ledger.append(_bar(start_ms=1_700_000_060_000))

    with pytest.raises(SourceBarConflictError, match="SOURCE_BAR_NON_MONOTONIC_LIVE"):
        ledger.append(_bar())


def test_source_bar_ledger_rejects_history_after_live_delivery_begins(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    ledger.append_history(_bar())
    ledger.append(_bar(start_ms=1_700_000_060_000))

    with pytest.raises(SourceBarConflictError, match="SOURCE_BAR_HISTORY_AFTER_LIVE"):
        ledger.append_history(_bar(start_ms=1_700_000_120_000))


def test_source_bar_ledger_fails_closed_at_its_explicit_retention_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(source_bar_ledger, "SOURCE_BAR_STREAM_CAPACITY", 1)
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    ledger.append(_bar())

    with pytest.raises(SourceBarRetentionLimitError, match="SOURCE_BAR_RETENTION_LIMIT"):
        ledger.append(_bar(start_ms=1_700_000_060_000))


@pytest.mark.asyncio
async def test_retained_feed_persists_unfiltered_stream_before_applying_rth_policy(tmp_path: Path) -> None:
    class _Source:
        feed_id = "polygon-minute"

        def __init__(self) -> None:
            self.use_rth_calls: list[bool] = []

        async def stream_bars(self, _symbol: str, *, use_rth: bool = True):
            self.use_rth_calls.append(use_rth)
            yield _bar().model_copy(update={"session_phase": "PRE"})
            yield _bar(start_ms=1_700_000_060_000).model_copy(update={"session_phase": "RTH"})

    source = _Source()
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = _RetainedSourceBarFeed(source, ledger)

    yielded = [bar async for bar in retained.stream_bars("SPY", use_rth=True)]

    assert source.use_rth_calls == [False]
    assert [bar.session_phase for bar in yielded] == ["RTH"]
    assert [bar.session_phase for bar in ledger.bars(provider="polygon-minute", symbol="SPY")] == [
        "PRE",
        "RTH",
    ]


@pytest.mark.asyncio
async def test_synthetic_port_fills_only_from_the_retained_decision_bar_and_recovers(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = ledger.append(_bar())
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)

    order = await broker.submit(
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
        client_order_id="bot:ema:enter",
    )

    assert order.status == "filled"
    assert order.filled_avg_price == float(retained.close)
    assert order.filled_at_ms == retained.end_ms
    restarted = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    recovered = await restarted.get_order_by_client_order_id("bot:ema:enter")
    assert recovered == order
    assert (await restarted.list_positions())[0].quantity == 2


@pytest.mark.asyncio
async def test_synthetic_port_consumes_order_scoped_exact_bar_binding(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    evaluated = ledger.append(_bar(close="501.25"))
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    broker.bind_evaluated_bar("bot:ema:enter", evaluated)
    ledger.append(_bar(close="502.25", start_ms=1_700_000_060_000))

    order = await broker.submit(
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
        client_order_id="bot:ema:enter",
    )

    assert order.filled_avg_price == float(evaluated.close)
    assert order.filled_at_ms == evaluated.end_ms


@pytest.mark.asyncio
async def test_synthetic_position_projection_preserves_average_cost_through_reduce_and_flip(
    tmp_path: Path,
) -> None:
    """Average-cost fold parity: buy, reduce, add, then cross through flat."""
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)

    async def submit(order_id: str, side: str, quantity: int, close: str, offset: int) -> None:
        retained = ledger.append(_bar(close=close, start_ms=1_700_000_000_000 + offset))
        broker.bind_evaluated_bar(order_id, retained)
        await broker.submit(
            BrokerOrderLeg(symbol="SPY", side=side, quantity=quantity),
            client_order_id=order_id,
        )

    await submit("buy-1", "buy", 10, "100", 0)
    await submit("sell-reduce", "sell", 4, "120", 60_000)
    await submit("buy-add", "buy", 2, "110", 120_000)

    [long_position] = await broker.list_positions()
    assert long_position.quantity == 8
    assert long_position.average_entry_price == 102.5
    assert long_position.cost_basis == 820

    await submit("sell-flip", "sell", 10, "130", 180_000)

    [short_position] = await broker.list_positions()
    assert short_position.quantity == -2
    assert short_position.side == "short"
    assert short_position.average_entry_price == 130
    assert short_position.cost_basis == 260


def test_synthetic_port_rejects_binding_from_another_account_authority(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    retained = ledger.append(_bar())
    broker = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)

    with pytest.raises(SyntheticBarBindingError, match="different account authority"):
        broker.bind_evaluated_bar(
            "bot:ema:enter",
            retained.model_copy(update={"account_id": "sim:other"}),
        )


def _submit_in_thread(
    broker: SyntheticBroker,
    barrier: threading.Barrier,
    client_order_id: str,
) -> None:
    barrier.wait(timeout=10)
    asyncio.run(
        broker.submit(
            BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
            client_order_id=client_order_id,
        )
    )


def test_synthetic_port_serializes_concurrent_order_id_and_sequence_writes(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    ledger.append(_bar())
    first = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    second = SyntheticBroker(account_id="sim:ema-1", source_bars=ledger)
    barrier = threading.Barrier(2)
    duplicate = [
        threading.Thread(target=_submit_in_thread, args=(broker, barrier, "bot:ema:enter"))
        for broker in (first, second)
    ]
    for worker in duplicate:
        worker.start()
    for worker in duplicate:
        worker.join(timeout=10)
        assert not worker.is_alive()

    order_path = ledger.path.with_name("simulated_orders.jsonl")
    duplicate_rows = [json.loads(line) for line in order_path.read_text(encoding="utf-8").splitlines()]
    assert [row["seq"] for row in duplicate_rows] == [1]

    sequence_barrier = threading.Barrier(2)
    distinct = [
        threading.Thread(target=_submit_in_thread, args=(broker, sequence_barrier, client_order_id))
        for broker, client_order_id in ((first, "bot:ema:exit"), (second, "bot:ema:stop"))
    ]
    for worker in distinct:
        worker.start()
    for worker in distinct:
        worker.join(timeout=10)
        assert not worker.is_alive()

    rows = [json.loads(line) for line in order_path.read_text(encoding="utf-8").splitlines()]
    assert [row["seq"] for row in rows] == [1, 2, 3]


def test_close_checkpoints_and_truncates_the_wal(tmp_path: Path) -> None:
    """Controlled shutdown leaves no un-checkpointed WAL behind (#1740).

    A second connection is held open across ``close`` on purpose: SQLite only
    checkpoints-and-removes the WAL by itself when the LAST connection
    closes, so this proves the ledger's own explicit TRUNCATE checkpoint in
    ``close`` -- the one its ``checkpoint_wal`` docstring promises for
    shutdown -- rather than SQLite's last-connection behaviour.
    """
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="PA-WAL")
    for index in range(3):
        ledger.append_history(_bar(start_ms=1_800_000_000_000 + index * 60_000))
    wal_path = ledger.path.with_name(f"{ledger.path.name}-wal")
    assert wal_path.exists() and wal_path.stat().st_size > 0, "setup: writes must land in the WAL first"
    bystander = sqlite3.connect(ledger.path)
    try:
        # A statement is what actually opens the file; an unused connection
        # would leave the ledger's handle as the last one after all.
        assert bystander.execute("SELECT COUNT(*) FROM source_bars").fetchone()[0] == 3
        ledger.close()
        assert wal_path.exists() and wal_path.stat().st_size == 0, (
            "close() must checkpoint and truncate the WAL itself while another connection is open"
        )
    finally:
        bystander.close()
