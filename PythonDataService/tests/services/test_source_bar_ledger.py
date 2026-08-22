"""Durable source-bar and synthetic-price boundaries for Dry Run."""

from __future__ import annotations

import asyncio
import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.synthetic_broker import SyntheticBarBindingError, SyntheticBroker
from app.broker.contract.models import BrokerOrderLeg
from app.marketdata.feed import MarketDataBar
from app.services.source_bar_ledger import SourceBarConflictError, SourceBarLedger


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


def test_source_bar_ledger_uses_initialized_identity_index_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    original.append(_bar())
    restarted = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")

    def fail_full_replay() -> list:
        raise AssertionError("append must use the initialized identity index")

    monkeypatch.setattr(restarted._wal, "read_all", fail_full_replay)

    retained = restarted.append(_bar(start_ms=1_700_000_060_000))

    assert retained.seq == 2


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
