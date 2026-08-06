"""Authority-boundary tests for Alpaca trade-update evidence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.trade_updates import TradeUpdatesConsumer
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort


class _ReadWithoutLegacyActivities:
    def __init__(self) -> None:
        self.activity_reads = 0

    async def list_activities(self, **_kwargs: Any) -> list:
        self.activity_reads += 1
        raise AssertionError("activated SQLite must not write legacy activity evidence")


class _NoBrokerMutation:
    async def submit(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("evidence recovery must not submit")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("evidence recovery must not cancel")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        return None


class _EvidenceSink:
    def __init__(self) -> None:
        self.consumer: TradeUpdatesConsumer | None = None
        self.gap_connection_states: list[bool] = []
        self.activity_recoveries = 0

    async def record_lifecycle_event(self, **_kwargs: Any) -> ClerkEntryKind:
        return ClerkEntryKind.ORDER_EVENT

    async def recover_activity_window(self, **_kwargs: Any) -> int:
        self.activity_recoveries += 1
        return 0

    async def reconcile_gap(self) -> None:
        assert self.consumer is not None
        self.gap_connection_states.append(self.consumer.connected)


class _ClosedOrderRead:
    async def list_orders(self, **_kwargs: Any) -> list:
        return []


class _Capture:
    def record(self, **_kwargs: Any) -> bool:
        return True


def _authorization_source() -> AsyncIterator[bytes | str]:
    async def _frames() -> AsyncIterator[bytes | str]:
        yield '{"stream":"authorization","data":{"status":"authorized"}}'

    return _frames()


async def test_sqlite_activity_recovery_never_reads_or_writes_legacy_window(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    read = _ReadWithoutLegacyActivities()
    read_port = cast(BrokerReadPort, read)
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo,
        read=read_port,
        trade=cast(BrokerTradePort, _NoBrokerMutation()),
        intake=ReentrantAsyncLock(),
    )

    recorded = await sink.recover_activity_window(read=read_port, limit=100)

    assert recorded == 0
    assert read.activity_reads == 0
    repo.close()


async def test_reconnect_reconciles_selected_sink_before_connection_reopens() -> None:
    sink = _EvidenceSink()
    consumer = TradeUpdatesConsumer(
        evidence_sink=sink,
        read=cast(BrokerReadPort, _ClosedOrderRead()),
        frame_source=_authorization_source,
        journal=cast(CaptureJournal, _Capture()),
        backoff=lambda _attempt: _no_backoff(),
        max_reconnects=1,
    )
    sink.consumer = consumer

    await consumer.run()

    assert sink.gap_connection_states == [False]
    assert sink.activity_recoveries == 1


async def _no_backoff() -> None:
    return
