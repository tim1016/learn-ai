"""Consumer seam tests for the owned ``trade_updates`` websocket (S4).

The consumer is driven by an **injected frame source** (an async-iterator
factory) plus an injected clock and backoff, so every concern — capture,
parse, live_idempotent dedup, attribution, and the post-reconnect REST
gap-reconcile — is exercised with no network. A REAL ``CaptureJournal`` and a
an injected authority sink assert durable-dispatch ordering independently of
the SQLite repository's own fold tests.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import responses

from app.broker.alpaca import adapter as adapter_module
from app.broker.alpaca.adapter import from_alpaca_order
from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.clerk.stream_health import build_default_stream_health_gate
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.trade_updates import (
    _MIN_HELD_CONNECTION_MS,
    TradeUpdatesConsumer,
    _broker_order_fingerprint_fields,
    _inject_frame_faults,
    _recovery_event,
    _stream_url,
    alpaca_socket_frames,
    reset_trade_updates_consumer_for_testing,
    set_trade_updates_consumer,
)
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerOrderRequest,
)
from tests.broker.alpaca.conftest import load_alpaca_fixture_file

_FIXED_MS = 1_700_000_000_000
_BASE = "https://paper-api.alpaca.markets"
_OWNED_COID = "manual/inkant/v1:aWQ1234567890abcdefgh"

# Complete raw-Alpaca payloads for the real-broker (``responses``-mocked) tests:
# ``from_alpaca_account`` / ``from_alpaca_order`` require the full field set, so
# the warming submit's ``_ensure_journal`` (GET /v2/account) and the POST
# /v2/orders round-trip both succeed against the mock.
_ACCOUNT_JSON: dict[str, Any] = {
    "account_number": "PA-TEST",
    "status": "ACTIVE",
    "currency": "USD",
    "cash": "1000.00",
    "equity": "1000.00",
    "buying_power": "2000.00",
    "portfolio_value": "1000.00",
    "long_market_value": "0.00",
    "short_market_value": "0.00",
    "trading_blocked": False,
    "account_blocked": False,
    "created_at": "2020-09-13T12:26:40Z",
}
_ACCEPTED_ORDER_JSON: dict[str, Any] = {
    "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
    "client_order_id": _OWNED_COID,
    "created_at": "2021-03-16T18:38:01.937734Z",
    "updated_at": "2021-03-16T18:38:01.937734Z",
    "submitted_at": "2021-03-16T18:38:01.937734Z",
    "filled_at": None,
    "symbol": "AAPL",
    "asset_class": "us_equity",
    "qty": "10",
    "filled_qty": "0",
    "filled_avg_price": None,
    "order_type": "market",
    "type": "market",
    "side": "buy",
    "time_in_force": "day",
    "limit_price": None,
    "stop_price": None,
    "status": "accepted",
}


# ── Test doubles / fixtures ──────────────────────────────────────────────────


def _account(account_id: str = "PA-TEST") -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id=account_id,
        account_mode="paper",
        account_status="ACTIVE",
        currency="USD",
        cash=1000.0,
        equity=1000.0,
        buying_power=2000.0,
        portfolio_value=1000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=1_600_000_000_000,
        observed_at_ms=_FIXED_MS,
    )


def _accepted_order(client_order_id: str, *, symbol: str = "AAPL") -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id="61e69015-8549-4bfd-b9c3-01e75843f47d",
        client_order_id=client_order_id,
        symbol=symbol,
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=10.0,
        filled_quantity=0.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status="accepted",
        submitted_at_ms=_FIXED_MS,
        created_at_ms=_FIXED_MS,
        updated_at_ms=_FIXED_MS,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=_FIXED_MS,
    )


def _filled_broker_order(order_id: str, client_order_id: str) -> BrokerOrder:
    """A terminal (filled) ``BrokerOrder`` for the gap-reconcile re-pull path."""
    return _accepted_order(client_order_id).model_copy(
        update={
            "order_id": order_id,
            "status": "filled",
            "filled_quantity": 10.0,
            "filled_avg_price": 135.80,
            "filled_at_ms": _FIXED_MS,
            "updated_at_ms": _FIXED_MS,
        }
    )


def test_gap_reconciled_fill_carries_execution_qty_and_price() -> None:
    # A gap-reconciled fill must carry the order's cumulative qty/price on the
    # BrokerOrderEvent. If it carried none, the per-instance exposure/FIFO fold
    # would silently drop the recovered fill while the account-level sweep
    # stayed clean.
    order = _filled_broker_order(
        order_id="61e69015-8549-4bfd-b9c3-01e75843f47d",
        client_order_id=_OWNED_COID,
    )

    event = _recovery_event(order)

    assert event is not None
    assert event.event_type == "fill"
    assert event.execution_id is None  # a recovered aggregate never fabricates an execution
    assert event.quantity is not None
    assert math.isclose(event.quantity, 10.0, abs_tol=1e-9, rel_tol=0.0)
    assert event.price is not None
    assert math.isclose(event.price, 135.80, abs_tol=1e-9, rel_tol=0.0)


def test_gap_reconcile_never_feeds_an_order_with_unreadable_values() -> None:
    """A filled order whose fill count would not parse is not re-fed as a zero fill (#2648 review).

    The adapter keeps the broker's own status on such a row, so the status
    alone no longer keeps it out of the REST recovery, which would
    turn its absent fill count into a readable ``qty: 0.0``.
    """
    order = _filled_broker_order("unreadable-fill", _OWNED_COID).model_copy(
        update={"filled_quantity": 0.0, "unreadable_fields": ("filled_qty",)}
    )

    assert _recovery_event(order) is None


def test_an_unreadable_row_is_not_an_exact_redelivery_of_its_readable_twin() -> None:
    """A degraded row and a readable one whose value really is zero are different evidence (#2648 review).

    Readable orders keep exactly the fingerprint shape they always had, so
    no variant digest a running consumer computes changes.
    """
    readable = _accepted_order(_OWNED_COID)
    degraded = readable.model_copy(update={"unreadable_fields": ("filled_qty",)})

    assert sorted(_broker_order_fingerprint_fields(readable)) == [
        "canceled_at_ms", "client_order_id", "created_at_ms", "expired_at_ms",
        "filled_at_ms", "filled_avg_price", "filled_quantity", "limit_price",
        "order_id", "order_type", "quantity", "side", "status", "stop_price",
        "submitted_at_ms", "symbol", "time_in_force", "updated_at_ms",
    ]
    assert _broker_order_fingerprint_fields(degraded) != _broker_order_fingerprint_fields(readable)


class _FakeBroker:
    """Read+trade port double: submit records the owned order_ref in the journal."""

    broker_id = "alpaca"

    def __init__(self, *, account: BrokerAccountSnapshot | None = None) -> None:
        self._account = account or _account()
        self.orders: list[BrokerOrder] = []
        self.activities: list[BrokerActivity] = []
        self.list_orders_calls: list[dict[str, Any]] = []
        self.list_activities_calls: list[dict[str, Any]] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        return self._account

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        return _accepted_order(client_order_id, symbol=leg.symbol)

    async def cancel(self, order_id: str) -> None:  # pragma: no cover - unused here
        return None

    async def list_orders(
        self, *, status: str | None = None, limit: int | None = None, after_ms: int | None = None
    ) -> list[BrokerOrder]:
        self.list_orders_calls.append({"status": status, "limit": limit, "after_ms": after_ms})
        return list(self.orders)

    async def list_activities(self, *, after_ms: int | None = None, limit: int = 100) -> list[BrokerActivity]:
        self.list_activities_calls.append({"after_ms": after_ms, "limit": limit})
        return list(self.activities)


class _EvidenceSink:
    """Small active-authority sink double for transport-focused tests."""

    def __init__(self, **_: object) -> None:
        self.entries: list[SimpleNamespace] = []
        self.owned_refs = {_OWNED_COID}
        self._journal = self

    def guard_reconnect_read(self, read: object):
        return read

    def read_entries(self) -> list[SimpleNamespace]:
        return list(self.entries)

    async def record_lifecycle_event(
        self,
        *,
        client_order_id: str | None,
        event: object,
        event_key: str,
        order: BrokerOrder | None,
        **_: object,
    ) -> ClerkEntryKind:
        owned = client_order_id in self.owned_refs
        kind = ClerkEntryKind.ORDER_EVENT if owned else ClerkEntryKind.UNEXPLAINED_ORDER
        self.entries.append(
            SimpleNamespace(
                kind=kind,
                owned=owned,
                event=event,
                event_key=event_key,
                broker_order=order,
            )
        )
        return kind

    async def reconcile_gap(self) -> None:
        return None

    async def submit(self, request: BrokerOrderRequest) -> SimpleNamespace:
        order_ref = _OWNED_COID
        self.owned_refs.add(order_ref)
        return SimpleNamespace(results=[SimpleNamespace(order_ref=order_ref)])


def _frame_source(frames: list[Any]):
    """Build a FrameSource that yields the given frames once, then ends."""

    async def _source() -> AsyncIterator[bytes | str]:
        for frame in frames:
            yield frame if isinstance(frame, (bytes, str)) else json.dumps(frame)

    return _source


class _OpenFrameSource:
    """A connected source that stays open while tests inspect live health."""

    def __init__(self) -> None:
        self._frames: asyncio.Queue[bytes | str | None] = asyncio.Queue()

    async def __call__(self) -> AsyncIterator[bytes | str]:
        while (frame := await self._frames.get()) is not None:
            yield frame

    async def send(self, frame: bytes | str | dict[str, Any]) -> None:
        await self._frames.put(
            frame if isinstance(frame, (bytes, str)) else json.dumps(frame)
        )


async def _wait_for_execution_health(*, healthy: bool) -> None:
    gate = build_default_stream_health_gate()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while loop.time() < deadline:
        if gate.execution().healthy is healthy:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"execution health did not become healthy={healthy}")


@asynccontextmanager
async def _running_consumer(
    tmp_path: Path,
    *,
    journal: CaptureJournal | None = None,
) -> AsyncIterator[tuple[_OpenFrameSource, TradeUpdatesConsumer]]:
    """Run a singleton-wired consumer on an open, authorized socket cycle."""
    source = _OpenFrameSource()
    broker = _FakeBroker()
    clerk = _EvidenceSink(read=broker, trade=broker)
    await _warm(clerk)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=source,
        journal=journal or _capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=0,
    )
    set_trade_updates_consumer(consumer)
    consumer.start()
    try:
        await source.send(
            {"stream": "authorization", "data": {"status": "authorized"}}
        )
        await _wait_for_execution_health(healthy=True)
        yield source, consumer
    finally:
        await consumer.stop()
        reset_trade_updates_consumer_for_testing()


async def _no_backoff(attempt: int) -> None:
    return None


async def _warm(clerk: _EvidenceSink, operator: str = "inkant") -> None:
    """Warm the clerk's namespace allowlist so ``manual/{operator}/v1`` events
    attribute as OWNED.

    Attribution is an allowlist of namespaces this clerk has actually minted
    (rebuilt from the journal), not a bare pattern match — so an owned event is
    only recognized after the clerk has recorded a submit under that namespace.
    The fixture frames all carry ``manual/inkant/v1`` client_order_ids.
    """
    await clerk.submit(
        BrokerOrderRequest(
            operator=operator,
            expected_account_id="PA-TEST",
            legs=[BrokerOrderLeg(symbol="AAPL", side="buy", quantity=10)],
        )
    )


def _capture_journal(tmp_path: Path) -> CaptureJournal:
    return CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS)


def _capture_records(tmp_path: Path) -> list[dict[str, Any]]:
    root = tmp_path / "capture" / "alpaca" / "stream"
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text().splitlines())
    return records


async def _consumer(
    tmp_path: Path,
    frames: list[Any],
    *,
    broker: _FakeBroker | None = None,
    clerk: _EvidenceSink | None = None,
    max_reconnects: int = 0,
) -> tuple[TradeUpdatesConsumer, _EvidenceSink, CaptureJournal]:
    broker = broker or _FakeBroker()
    clerk = clerk or _EvidenceSink(read=broker, trade=broker)
    journal = _capture_journal(tmp_path)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source(frames),
        journal=journal,
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=max_reconnects,
    )
    return consumer, clerk, journal


def _load_frames() -> list[dict[str, Any]]:
    """Return trade_updates event frames for consumer integration tests.

    Filters the full real-capture fixture to only the event kinds this test
    suite exercises, in backward-compatible index order:
      [0]=new, [1]=partial_fill, [2]=fill, [3]=canceled, [4]=rejected.

    Auth/subscribe/pending_new frames are excluded — they're validated by
    test_adapter_trade_updates.py and test_schema_drift.py.  COIDs are
    normalised to _OWNED_COID so the clerk's namespace allowlist (seeded by
    _warm()) treats every frame as OWNED.
    """
    all_frames = load_alpaca_fixture_file("trade_updates", "trade_updates.json")
    _ORDER = {"new": 0, "partial_fill": 1, "fill": 2, "canceled": 3, "rejected": 4}
    trade = [f for f in all_frames if f["stream"] == "trade_updates" and f["data"].get("event") in _ORDER]
    present_events = {str(frame["data"]["event"]) for frame in trade}
    missing_events = set(_ORDER) - present_events
    assert not missing_events, f"trade_updates fixture missing documented events: {missing_events}"
    assert len(trade) == len(_ORDER), "trade_updates fixture contains duplicate documented events"
    trade = sorted(copy.deepcopy(trade), key=lambda f: _ORDER[f["data"]["event"]])
    for f in trade:
        if "order" in f["data"]:
            f["data"]["order"]["client_order_id"] = _OWNED_COID
    return trade


# ── (a) capture-before-parse ─────────────────────────────────────────────────


async def test_each_frame_captured_verbatim_before_handler_runs(tmp_path: Path) -> None:
    # A capturing journal double asserts the raw bytes are recorded BEFORE the
    # clerk (the parse/handler consumer) is ever invoked.
    order_of_calls: list[str] = []
    captured_bytes: list[bytes] = []

    class _SpyJournal(CaptureJournal):
        def record(self, *, raw_body: bytes, **kwargs: Any) -> bool:  # type: ignore[override]
            order_of_calls.append("capture")
            captured_bytes.append(raw_body)
            return super().record(raw_body=raw_body, **kwargs)

    class _SpyClerk(_EvidenceSink):
        async def record_lifecycle_event(self, **kwargs: Any) -> ClerkEntryKind:  # type: ignore[override]
            order_of_calls.append("handle")
            return await super().record_lifecycle_event(**kwargs)

    broker = _FakeBroker()
    clerk = _SpyClerk(read=broker, trade=broker)
    frame = json.dumps(_load_frames()[0])
    journal = _SpyJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source([frame]),
        journal=journal,
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=0,
    )

    await consumer.run()

    # Capture happened, and it happened before the first handler call.
    assert "capture" in order_of_calls
    assert order_of_calls.index("capture") < order_of_calls.index("handle")
    # The captured bytes are the verbatim frame.
    assert captured_bytes[0] == frame.encode("utf-8")
    # And it landed on disk under the STREAM family.
    records = _capture_records(tmp_path)
    assert records[0]["raw_body"] == frame
    assert records[0]["endpoint"] == "stream"


# ── (b) parse → BrokerOrderEvent for each event kind ─────────────────────────


async def test_each_event_kind_is_journaled_as_order_event(tmp_path: Path) -> None:
    frames = _load_frames()
    consumer, clerk, _ = await _consumer(tmp_path, frames)
    await _warm(clerk)

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    events = [e for e in entries if e.kind is ClerkEntryKind.ORDER_EVENT]
    # All five owned frames journaled as ORDER_EVENT with the parsed event.
    assert [e.event.event_type for e in events] == [  # type: ignore[union-attr]
        "new",
        "partial_fill",
        "fill",
        "canceled",
        "rejected",
    ]
    # The parsed event carries the mapped instant and the owned identity.
    for entry in events:
        assert entry.owned is True
        assert entry.event is not None
        assert entry.event.occurred_at_ms > 0
        assert entry.event_key is not None


# ── (c) live_idempotent: exact redelivery + stale terminal ───────────────────


async def test_exact_redelivery_is_skipped_and_counted(tmp_path: Path) -> None:
    # The partial_fill (carries an execution_id) delivered twice: the second is
    # an exact redelivery — skipped, counted, NOT journaled a second time.
    partial = _load_frames()[1]
    consumer, clerk, _ = await _consumer(tmp_path, [partial, partial])
    await _warm(clerk)

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    order_events = [e for e in entries if e.kind is ClerkEntryKind.ORDER_EVENT]
    assert len(order_events) == 1  # journaled once despite two deliveries
    assert consumer.counters.skipped_duplicate == 1
    assert consumer.counters.events_applied == 1


async def test_changed_payload_with_reused_event_key_is_journaled_as_a_variant(
    tmp_path: Path,
) -> None:
    first = _load_frames()[1]
    corrected = _load_frames()[1]
    # Alpaca can correct a fill while retaining its execution id. The key is
    # therefore identical, but the event's ledger meaning is not.
    corrected["data"]["price"] = "136.25"
    consumer, clerk, _ = await _consumer(tmp_path, [first, corrected])
    await _warm(clerk)

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    events = [entry for entry in entries if entry.kind is ClerkEntryKind.ORDER_EVENT]
    assert len(events) == 2
    assert events[0].event_key != events[1].event_key
    assert consumer.counters.event_key_collisions == 1
    assert consumer.counters.skipped_duplicate == 0


async def test_redelivery_of_terminal_order_surfaces_as_stale(tmp_path: Path) -> None:
    # The fill (order status=filled == terminal) delivered twice: the second is
    # a stale redelivery of a finalized order — surfaced + counted per policy,
    # not silently dropped, and never double-journaled.
    fill = _load_frames()[2]
    consumer, clerk, _ = await _consumer(tmp_path, [fill, fill])
    await _warm(clerk)

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    order_events = [e for e in entries if e.kind is ClerkEntryKind.ORDER_EVENT]
    assert len(order_events) == 1
    assert consumer.counters.stale_terminal == 1
    assert consumer.counters.skipped_duplicate == 0


async def test_unparseable_frame_is_captured_counted_not_fatal(tmp_path: Path) -> None:
    good = json.dumps(_load_frames()[0])
    consumer, clerk, _ = await _consumer(tmp_path, [b"{ not json", good])
    await _warm(clerk)

    await consumer.run()

    # The bad frame was captured (verbatim) and counted; the good one applied.
    records = _capture_records(tmp_path)
    assert len(records) == 2
    assert consumer.counters.parse_errors == 1
    assert consumer.counters.events_applied == 1


async def test_unparseable_frame_degrades_gate_health_until_valid_frame(
    tmp_path: Path,
) -> None:
    async with _running_consumer(tmp_path) as (source, consumer):
        await source.send(b"{ not json")
        await _wait_for_execution_health(healthy=False)
        assert consumer.connected is True

        await source.send(_load_frames()[0])
        await _wait_for_execution_health(healthy=True)
        assert consumer.connected is True


async def test_quiet_connected_stream_without_trade_updates_stays_healthy(
    tmp_path: Path,
) -> None:
    # Authorization in the helper establishes a real connected socket cycle,
    # but it is a control frame rather than custody evidence. No update follows.
    async with _running_consumer(tmp_path) as (_source, consumer):
        assert consumer.connected is True
        assert build_default_stream_health_gate().execution().healthy is True


async def test_capture_failure_refuses_to_derive_lifecycle_state(tmp_path: Path) -> None:
    class _FailingCaptureJournal(CaptureJournal):
        def record(self, **_: Any) -> bool:  # type: ignore[override]
            return False

    broker = _FakeBroker()
    clerk = _EvidenceSink(read=broker, trade=broker)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source([_load_frames()[0]]),
        journal=_FailingCaptureJournal(capture_dir=tmp_path / "capture"),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=0,
    )
    await _warm(clerk)

    await consumer.run()

    assert consumer.counters.capture_failures == 1
    assert consumer.counters.events_applied == 0
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    assert not [entry for entry in entries if entry.kind is ClerkEntryKind.ORDER_EVENT]


async def test_capture_failure_degrades_gate_health_until_valid_frame(
    tmp_path: Path,
) -> None:
    rejected_frame = json.dumps(_load_frames()[0])

    class _FailingOnceCaptureJournal(CaptureJournal):
        def record(self, *, raw_body: bytes, **kwargs: Any) -> bool:  # type: ignore[override]
            if raw_body == rejected_frame.encode("utf-8"):
                return False
            return super().record(raw_body=raw_body, **kwargs)

    journal = _FailingOnceCaptureJournal(capture_dir=tmp_path / "capture")
    async with _running_consumer(tmp_path, journal=journal) as (source, consumer):
        await source.send(rejected_frame)
        await _wait_for_execution_health(healthy=False)
        assert consumer.connected is True

        await source.send(_load_frames()[1])
        await _wait_for_execution_health(healthy=True)
        assert consumer.connected is True


async def test_clerk_append_failure_leaves_event_retryable(tmp_path: Path) -> None:
    class _FailingOnceClerk(_EvidenceSink):
        calls = 0

        async def record_lifecycle_event(self, **kwargs: Any) -> ClerkEntryKind:  # type: ignore[override]
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary journal failure")
            return await super().record_lifecycle_event(**kwargs)

    broker = _FakeBroker()
    clerk = _FailingOnceClerk(read=broker, trade=broker)
    await _warm(clerk)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source([_load_frames()[1]]),
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=1,
    )

    await consumer.run()

    assert clerk.calls == 2
    assert consumer.counters.events_applied == 1


async def test_valid_frame_restores_health_only_after_durable_clerk_append(
    tmp_path: Path,
) -> None:
    class _BlockingClerk(_EvidenceSink):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.append_started = asyncio.Event()
            self.allow_append = asyncio.Event()

        async def record_lifecycle_event(self, **kwargs: Any) -> ClerkEntryKind:  # type: ignore[override]
            self.append_started.set()
            await self.allow_append.wait()
            return await super().record_lifecycle_event(**kwargs)

    broker = _FakeBroker()
    clerk = _BlockingClerk(read=broker, trade=broker)
    await _warm(clerk)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source([]),
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=0,
    )
    consumer._mark_evidence_health(False)

    handling = asyncio.create_task(
        consumer._handle_trade_update(_load_frames()[0]["data"])
    )
    await clerk.append_started.wait()
    assert consumer.evidence_health.healthy is False

    clerk.allow_append.set()
    await handling
    assert consumer.evidence_health.healthy is True


async def test_already_durable_duplicate_restores_degraded_evidence_health(
    tmp_path: Path,
) -> None:
    frame = _load_frames()[0]
    consumer, clerk, _ = await _consumer(tmp_path, [])
    await _warm(clerk)
    await consumer._handle_trade_update(frame["data"])
    consumer._mark_evidence_health(False)

    await consumer._handle_trade_update(frame["data"])

    assert consumer.evidence_health.healthy is True
    assert consumer.counters.skipped_duplicate == 1
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    order_events = [entry for entry in entries if entry.kind is ClerkEntryKind.ORDER_EVENT]
    assert len(order_events) == 1


async def test_malformed_embedded_order_is_parse_error_not_stream_abort(tmp_path: Path) -> None:
    # A frame whose event/timestamp map cleanly but whose embedded ``order`` is
    # not an object must be a parse error — captured, counted, ``_seen``
    # unpoisoned — and must NOT abort the drain of the frames that follow it.
    # (Regression: the order was once mapped outside the parse guard and after
    # ``_seen`` was set, so a bad order silently lost the event and truncated
    # the stream.) Missing text maps blank and is contained per order (#2643);
    # an unreadable value maps degraded and is contained the same way (#2648).
    bad = _load_frames()[0]
    bad["data"]["order"] = "not-an-order"
    good = _load_frames()[0]  # a valid owned frame delivered AFTER the bad one
    consumer, clerk, _ = await _consumer(tmp_path, [bad, good])
    await _warm(clerk)

    await consumer.run()

    # Both frames captured verbatim — a parse failure never loses the audit trail.
    assert len(_capture_records(tmp_path)) == 2
    assert consumer.counters.parse_errors == 1
    # The good frame after the bad one still applied — the drain was not aborted.
    assert consumer.counters.events_applied == 1
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    events = [e for e in entries if e.kind is ClerkEntryKind.ORDER_EVENT]
    assert len(events) == 1


async def test_a_multi_leg_parents_fill_credits_each_leg_its_own_exact_execution(tmp_path: Path) -> None:
    """#3103: Alpaca sends no frame of its own for a leg; the parent's fill frame names each leg's execution.

    Without it the legs hold only the cumulative the REST lookup credits, which names no execution:
    the account's fee population stays incomplete and every entry on the account is refused.
    """
    frame = _load_frames()[2]
    parent = frame["data"]["order"]
    parent["order_class"] = "mleg"
    parent["symbol"] = ""
    parent["legs"] = [
        {**parent, "id": f"leg-order-{n}", "client_order_id": f"{_OWNED_COID}~{n}", "symbol": symbol, "side": side,
         "order_class": "simple", "legs": None, "qty": "1", "filled_qty": "1", "status": "filled"}
        for n, (symbol, side) in enumerate((("SPY261016C00771000", "buy"), ("SPY261016C00776000", "sell")), start=1)
    ]
    frame["data"].pop("execution_id", None)
    frame["data"]["legs"] = [
        {"execution_id": "exec-buy-leg", "qty": "1", "price": "3.31", "order_id": "leg-order-1",
         "symbol": "SPY261016C00771000", "timestamp": "2026-10-14T15:59:03.721579799Z"},
        {"execution_id": "exec-sell-leg", "qty": "1", "price": "1.18", "order_id": "leg-order-2",
         "symbol": "SPY261016C00776000", "timestamp": "2026-10-14T15:59:03.721579799Z"},
    ]
    consumer, clerk, _ = await _consumer(tmp_path, [frame])
    await _warm(clerk)

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    exact = [
        (e.event.execution_id, e.event.price, e.event.quantity, e.broker_order.symbol)
        for e in entries
        if getattr(e.event, "execution_id", None) is not None
    ]
    assert exact == [("exec-buy-leg", 3.31, 1.0, "SPY261016C00771000"), ("exec-sell-leg", 1.18, 1.0, "SPY261016C00776000")]
    assert consumer.counters.parse_errors == 0
    assert consumer.evidence_health.healthy is True


async def test_a_leg_execution_that_names_no_leg_order_is_counted_and_credits_nothing(tmp_path: Path) -> None:
    frame = _load_frames()[2]
    frame["data"].pop("execution_id", None)
    frame["data"]["order"]["order_class"] = "mleg"
    frame["data"]["order"]["symbol"] = ""
    frame["data"]["order"]["legs"] = []
    frame["data"]["legs"] = [{"execution_id": "orphan", "qty": "1", "price": "1", "order_id": "no-such-leg"}]
    consumer, clerk, _ = await _consumer(tmp_path, [frame])
    await _warm(clerk)

    await consumer.run()

    assert consumer.counters.leg_executions_unmapped == 1
    assert consumer.counters.parse_errors == 0
    # The parent was applied; an orphan leg must not hold every reduction on the account.
    assert consumer.evidence_health.healthy is True
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    assert [e for e in entries if getattr(getattr(e, "event", None), "execution_id", None) is not None] == []


async def test_unmappable_frame_degrades_gate_health_until_valid_frame(
    tmp_path: Path,
) -> None:
    malformed = _load_frames()[0]
    # Not an object at all; missing text and unreadable values are contained
    # per order instead (#2643, #2648).
    malformed["data"]["order"] = "not-an-order"
    async with _running_consumer(tmp_path) as (source, consumer):
        await source.send(malformed)
        await _wait_for_execution_health(healthy=False)
        assert consumer.connected is True

        await source.send(_load_frames()[0])
        await _wait_for_execution_health(healthy=True)
        assert consumer.connected is True


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda data: data.update(price=True), id="boolean-execution-price"),
        pytest.param(lambda data: data.update(qty=False), id="boolean-execution-qty"),
    ],
)
async def test_a_boolean_numeric_is_a_parse_error_not_a_reconnect(
    tmp_path: Path,
    corrupt: Callable[[dict[str, Any]], None],
) -> None:
    # ``float(True)`` is refused with a ``TypeError``; a frame carrying one in
    # its execution slice is a frame that would not map — counted, health
    # degraded, socket kept — not a crash that tears the connection down and
    # reconnects (#2606). A boolean in the embedded ORDER is no longer this
    # path: the row maps degraded and is contained per order (#2648).
    fill = _load_frames()[2]
    corrupt(fill["data"])
    async with _running_consumer(tmp_path) as (source, consumer):
        await source.send(fill)
        await _wait_for_execution_health(healthy=False)
        assert consumer.connected is True
        assert consumer.counters.parse_errors == 1

        await source.send(_load_frames()[0])
        await _wait_for_execution_health(healthy=True)


@pytest.mark.parametrize("data", [[{"event": "fill"}], "fill", 7], ids=["list", "string", "number"])
async def test_a_frame_whose_data_is_not_an_object_is_a_parse_error_not_a_reconnect(
    tmp_path: Path, data: object
) -> None:
    async with _running_consumer(tmp_path) as (source, consumer):
        await source.send({"stream": "trade_updates", "data": data})
        await _wait_for_execution_health(healthy=False)
        assert consumer.connected is True
        assert consumer.counters.parse_errors == 1

        await source.send(_load_frames()[0])
        await _wait_for_execution_health(healthy=True)
        assert consumer.connected is True


# ── (d) attribution: owned vs unexplained (NO hold — that is S6) ─────────────


async def test_foreign_client_order_id_journals_unexplained_and_counts(tmp_path: Path) -> None:
    frame = _load_frames()[0]
    frame["data"]["order"]["client_order_id"] = "someone-elses-order-id"
    consumer, clerk, _ = await _consumer(tmp_path, [frame])

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    unexplained = [e for e in entries if e.kind is ClerkEntryKind.UNEXPLAINED_ORDER]
    assert len(unexplained) == 1
    assert consumer.counters.unexplained == 1


async def test_done_for_day_does_not_hide_a_later_terminal_transition(tmp_path: Path) -> None:
    done_for_day = _load_frames()[0]
    done_for_day["data"]["event"] = "done_for_day"
    done_for_day["data"]["order"]["status"] = "done_for_day"
    later_fill = _load_frames()[2]
    consumer, clerk, _ = await _consumer(tmp_path, [done_for_day, later_fill])
    await _warm(clerk)

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    events = [entry.event.event_type for entry in entries if entry.kind is ClerkEntryKind.ORDER_EVENT]
    assert events == ["done_for_day", "fill"]
    assert consumer.counters.stale_terminal == 0


async def test_authorization_frame_is_captured_not_attributed(tmp_path: Path) -> None:
    auth_ok = {"stream": "authorization", "data": {"status": "authorized", "action": "authenticate"}}
    consumer, clerk, _ = await _consumer(tmp_path, [auth_ok])

    await consumer.run()

    # Captured (verbatim) but nothing attributed.
    assert len(_capture_records(tmp_path)) == 1
    assert clerk._journal is None or clerk._journal.read_entries() == []  # type: ignore[union-attr]
    assert consumer.counters.events_applied == 0
    assert consumer.counters.unexplained == 0


@pytest.mark.parametrize("data", [["authorized"], "authorized", 7], ids=["list", "string", "number"])
async def test_an_authorization_frame_whose_data_is_not_an_object_does_not_abort_the_cycle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, data: object
) -> None:
    consumer, clerk, _ = await _consumer(
        tmp_path, [{"stream": "authorization", "data": data}, _load_frames()[0]]
    )
    await _warm(clerk)

    with caplog.at_level(logging.WARNING, logger="app.broker.alpaca.trade_updates"):
        await consumer.run()

    # The frame after it was still applied: the cycle was not torn down for a control frame.
    assert consumer.counters.events_applied == 1
    assert [r.status for r in caplog.records if getattr(r, "action", None) == "trade_updates_auth_denied"] == [""]


def _parent_fill_naming_two_legs(first_leg_filled_qty: str = "1", *, unreadable_timestamp_on: int | None = None) -> dict[str, Any]:
    """A several-leg parent's fill frame: no execution of its own, one execution per leg (#3103)."""
    frame = _load_frames()[2]
    parent = frame["data"]["order"]
    parent["order_class"] = "mleg"
    parent["symbol"] = ""
    parent["legs"] = [
        {**parent, "id": f"leg-order-{n}", "client_order_id": f"{_OWNED_COID}~{n}", "symbol": symbol, "side": side,
         "order_class": "simple", "legs": None, "qty": "1", "filled_qty": filled_qty, "status": "filled"}
        for n, (symbol, side, filled_qty) in enumerate(
            (("SPY261016C00771000", "buy", first_leg_filled_qty), ("SPY261016C00776000", "sell", "1")), start=1
        )
    ]
    frame["data"].pop("execution_id", None)
    frame["data"]["legs"] = [
        {"execution_id": "exec-buy-leg", "qty": "1", "price": "3.31", "order_id": "leg-order-1",
         "symbol": "SPY261016C00771000", "timestamp": "2026-10-14T15:59:03.721579799Z"},
        {"execution_id": "exec-sell-leg", "qty": "1", "price": "1.18", "order_id": "leg-order-2",
         "symbol": "SPY261016C00776000", "timestamp": "2026-10-14T15:59:03.721579799Z"},
    ]
    if unreadable_timestamp_on is not None:
        frame["data"]["legs"][unreadable_timestamp_on]["timestamp"] = "not-a-timestamp"
    return frame


@pytest.mark.parametrize("bad_leg", [0, 1], ids=["first-leg", "last-leg"])
async def test_a_leg_that_fails_to_map_leaves_health_degraded_whatever_its_place_in_the_frame(
    tmp_path: Path, bad_leg: int
) -> None:
    # The leg's own frame is mapped by the shared handler, which counts the failure and degrades
    # health -- and the next leg's mapping would restore it. A frame with an unusable leg is not
    # a healthy frame whichever leg came first.
    consumer, clerk, _ = await _consumer(tmp_path, [_parent_fill_naming_two_legs(unreadable_timestamp_on=bad_leg)])
    await _warm(clerk)

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    exact = [e.event.execution_id for e in entries if getattr(e.event, "execution_id", None) is not None]
    assert exact == ["exec-sell-leg" if bad_leg == 0 else "exec-buy-leg"]
    assert consumer.counters.parse_errors == 1
    assert consumer.evidence_health.healthy is False


@pytest.mark.parametrize("raises_on_non_finite", [False, True], ids=["opt_float-reads-nan", "opt_float-refuses-nan"])
@pytest.mark.parametrize("filled_qty", ["nan", "inf", "five"])
async def test_a_leg_with_an_unreadable_quantity_is_a_counted_parse_error_not_a_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filled_qty: str, raises_on_non_finite: bool
) -> None:
    # The leg's own quantities are read to say fill or partial fill, outside the
    # order mapper's guard. A leg Alpaca states unreadably is one that would not
    # map: counted, health degraded, the rest of the frame's legs still credited
    # -- whether the numeric reader returns a non-finite number or refuses it.
    if raises_on_non_finite:
        read_float = adapter_module.opt_float

        def _refusing(value: object) -> float | None:
            parsed = read_float(value)
            if parsed is not None and not math.isfinite(parsed):
                raise ValueError("Alpaca numeric is not finite")
            return parsed

        monkeypatch.setattr(adapter_module, "opt_float", _refusing)
    consumer, clerk, _ = await _consumer(tmp_path, [_parent_fill_naming_two_legs(filled_qty)])
    await _warm(clerk)

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    exact = [e.event.execution_id for e in entries if getattr(e.event, "execution_id", None) is not None]
    assert exact == ["exec-sell-leg"]
    assert consumer.counters.parse_errors == 1
    assert consumer.evidence_health.healthy is False


# ── (e) reconnect → REST gap-reconcile pulls missed orders and dedups ────────


def _real_broker_with_responses(orders_body: list[dict[str, Any]]) -> AlpacaBroker:
    """A real AlpacaBroker whose HTTP is mocked by ``responses``.

    The SDK drives ``requests``; ``responses`` intercepts ``GET /v2/orders`` and
    ``GET /v2/account`` so the gap-reconcile exercises the real client + adapter
    path with no network.
    """
    from alpaca.trading.client import TradingClient

    def _factory() -> Any:
        return TradingClient(api_key="k", secret_key="s", paper=True, raw_data=True)

    return AlpacaBroker(client=AlpacaTradingClient(client_factory=_factory))


@responses.activate
async def test_reconnect_gap_reconcile_pulls_missed_orders(tmp_path: Path) -> None:
    # The socket ends with a partial_fill (never delivers the fill), then the
    # consumer reconnects and REST-reconciles — the GET /v2/orders returns the
    # now-filled order, which the gap-fill feeds through as a synthetic fill.
    filled_order = {
        "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
        "client_order_id": _OWNED_COID,
        "created_at": "2021-03-16T18:38:01.937734Z",
        "updated_at": "2021-03-16T18:38:02.123456Z",
        "submitted_at": "2021-03-16T18:38:01.937734Z",
        "filled_at": "2021-03-16T18:38:02.123456Z",
        "symbol": "AAPL",
        "asset_class": "us_equity",
        "qty": "10",
        "filled_qty": "10",
        "filled_avg_price": "135.80",
        "order_type": "market",
        "type": "market",
        "side": "buy",
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "status": "filled",
    }
    responses.add(responses.GET, f"{_BASE}/v2/account", json=_ACCOUNT_JSON, status=200)
    responses.add(responses.POST, f"{_BASE}/v2/orders", json=_ACCEPTED_ORDER_JSON, status=200)
    responses.add(responses.GET, f"{_BASE}/v2/orders", json=[filled_order], status=200)

    broker = _real_broker_with_responses([filled_order])
    clerk = _EvidenceSink(read=broker, trade=broker)
    # Warm the clerk namespace so the reconciled order attributes as OWNED.
    await _warm(clerk)
    # Make the warmed order_ref match the reconciled client_order_id so the
    # namespace (manual/inkant/v1) is known; identity resolution keys on namespace.
    partial = _load_frames()[1]
    partial["data"]["order"]["client_order_id"] = _OWNED_COID

    journal = _capture_journal(tmp_path)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source([partial]),
        journal=journal,
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=1,
    )

    await consumer.run()

    # The gap-reconcile pulled the filled order and applied it as a fill.
    assert consumer.counters.gap_reconciled >= 1
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    fills = [
        e
        for e in entries
        if e.kind is ClerkEntryKind.ORDER_EVENT and e.event is not None and e.event.event_type == "fill"
    ]
    assert len(fills) == 1


@responses.activate
async def test_gap_reconcile_dedups_socket_fill_by_terminal_order(tmp_path: Path) -> None:
    # PRODUCTION shape: the socket delivered the fill WITH its execution_id (key
    # ``exec:...``); a reconnect re-pulls the now-terminal order via REST, which
    # has NO execution_id (key ``order_id|fill|ms``). The two keys differ, so
    # key-only dedup would double-journal the fill — the terminal-order guard
    # recognizes the re-pull as a stale re-observation and does not re-journal.
    # Load the fill first so we can derive the order_id for the REST mock.
    fill = _load_frames()[2]
    assert fill["data"].get("execution_id")  # guards the test premise
    order_id = fill["data"]["order"]["id"]  # must match REST response for guard to fire
    # The terminal fingerprint includes filled_at_ms, so the REST response must
    # use the same filled_at as the socket fill's embedded order to match.
    filled_at = fill["data"]["order"]["filled_at"]
    filled_order = {
        "id": order_id,
        "client_order_id": _OWNED_COID,
        "updated_at": filled_at,
        "submitted_at": fill["data"]["order"]["submitted_at"],
        "filled_at": filled_at,
        "symbol": "SPY",
        "asset_class": "us_equity",
        "qty": "1",
        "filled_qty": "1",
        "filled_avg_price": "737.91",
        "order_type": "market",
        "type": "market",
        "side": "buy",
        "time_in_force": "day",
        "status": "filled",
    }
    responses.add(responses.GET, f"{_BASE}/v2/account", json=_ACCOUNT_JSON, status=200)
    responses.add(responses.POST, f"{_BASE}/v2/orders", json=_ACCEPTED_ORDER_JSON, status=200)
    responses.add(responses.GET, f"{_BASE}/v2/orders", json=[filled_order], status=200)

    broker = _real_broker_with_responses([filled_order])
    clerk = _EvidenceSink(read=broker, trade=broker)
    await _warm(clerk)

    journal = _capture_journal(tmp_path)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source([fill]),
        journal=journal,
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=1,
    )

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    fills = [
        e
        for e in entries
        if e.kind is ClerkEntryKind.ORDER_EVENT and e.event is not None and e.event.event_type == "fill"
    ]
    # Socket (exec-keyed) once + reconcile (order-keyed) re-pull → journaled once.
    assert len(fills) == 1
    assert consumer.counters.stale_terminal >= 1
    assert consumer.counters.gap_reconciled >= 1


async def test_gap_reconcile_dedups_a_socket_cancel_against_its_rest_repull(tmp_path: Path) -> None:
    # A non-fill terminal event carries no execution id: the socket and the REST
    # re-pull both key it on ``order_id|canceled|instant``. The socket delivered
    # the cancel; the reconnect's REST pull returns the same closed order. The
    # Clerk must see the cancel once, and the re-pull is counted as stale.
    canceled = _load_frames()[3]
    assert canceled["data"]["event"] == "canceled"
    authorization = {"stream": "authorization", "data": {"status": "authorized"}}
    connections = iter([[canceled], [authorization]])
    broker = _FakeBroker()
    broker.orders = [from_alpaca_order(canceled["data"]["order"], observed_at_ms=_FIXED_MS)]
    clerk = _EvidenceSink(read=broker, trade=broker)
    await _warm(clerk)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=lambda: _frame_source(next(connections))(),
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=1,
    )

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    cancels = [
        e
        for e in entries
        if e.kind is ClerkEntryKind.ORDER_EVENT and e.event is not None and e.event.event_type == "canceled"
    ]
    assert len(cancels) == 1
    assert consumer.counters.gap_reconciled == 1
    assert consumer.counters.stale_terminal == 1
    assert consumer.counters.events_applied == 1
    assert consumer.counters.parse_errors == 0


async def test_gap_reconcile_pulls_closed_orders_no_submission_time_filter(
    tmp_path: Path,
) -> None:
    # A terminal transition missed while disconnected is recovered by re-pulling
    # CLOSED orders — NOT by Alpaca's ``after`` filter, which keys on submission
    # time and would exclude an order submitted before its last-seen event. The
    # socket delivers only ``new`` (order still open at disconnect); the fill
    # lands while down and is visible only via the REST re-pull. (Regression:
    # the reconcile once passed last_event_ms as ``after`` and queried "all",
    # silently missing exactly this order.)
    broker = _FakeBroker()
    clerk = _EvidenceSink(read=broker, trade=broker)
    await _warm(clerk)
    new_frame = _load_frames()[0]  # event=new, owned coid
    order_id = new_frame["data"]["order"]["id"]
    coid = new_frame["data"]["order"]["client_order_id"]
    broker.orders = [_filled_broker_order(order_id, coid)]

    journal = _capture_journal(tmp_path)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source([new_frame]),
        journal=journal,
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=1,
    )

    await consumer.run()

    # The gap-reconcile queried CLOSED orders with no submission-time filter.
    assert broker.list_orders_calls
    last_call = broker.list_orders_calls[-1]
    assert last_call["status"] == "closed"
    assert last_call["after_ms"] is None
    # The missed fill was recovered exactly once.
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    fills = [
        e
        for e in entries
        if e.kind is ClerkEntryKind.ORDER_EVENT and e.event is not None and e.event.event_type == "fill"
    ]
    assert len(fills) == 1
    assert consumer.counters.gap_reconciled >= 1


_AUTHORIZED = {"stream": "authorization", "data": {"status": "authorized"}}


async def test_a_failed_closed_order_pull_is_counted_and_the_stream_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The authoritative reconciliation held; only the re-feed of closed orders'
    # events failed. Admission opens (the sweep covers the window), and the
    # failure is counted and says so, not left to a reconnect that may be days off.
    class _ClosedOrdersUnavailable(_FakeBroker):
        async def list_orders(self, **_: Any) -> list[BrokerOrder]:
            raise RuntimeError("alpaca closed orders unavailable")

    broker = _ClosedOrdersUnavailable()
    clerk = _EvidenceSink(read=broker, trade=broker)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source([_AUTHORIZED]),
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=1,
    )

    with caplog.at_level(logging.WARNING, logger="app.broker.alpaca.trade_updates"):
        await consumer.run()

    assert consumer.counters.gap_reconcile_failures == 1
    assert consumer.counters.connects == 2
    assert consumer.counters.gap_reconciled == 0
    [record] = [r for r in caplog.records if getattr(r, "action", None) == "trade_updates_gap_reconcile_error"]
    assert record.failures == 1 and record.error == "alpaca closed orders unavailable"
    assert record.exc_info is not None


async def test_a_failed_authoritative_reconcile_keeps_the_connection_closed(tmp_path: Path) -> None:
    # SQLite's own reconciliation is what closes the gap. When it fails the
    # reconnect is not admitted, and no closed-order pull is made past it.
    class _ReconcileRefused(_EvidenceSink):
        async def reconcile_gap(self) -> None:
            raise RuntimeError("the clerk could not reconcile the trade-update gap")

    broker = _FakeBroker()
    clerk = _ReconcileRefused(read=broker, trade=broker)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=_frame_source([_AUTHORIZED]),
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=1,
    )

    await consumer.run()

    assert consumer.counters.connects == 1  # the first connection has no gap to close
    assert consumer.connected is False
    assert consumer.counters.gap_reconcile_failures == 0
    assert broker.list_orders_calls == []


async def test_reconnect_opens_the_next_source_before_gap_reconcile(tmp_path: Path) -> None:
    order: list[str] = []
    authorization = {"stream": "authorization", "data": {"status": "authorized"}}

    def frame_source() -> AsyncIterator[bytes | str]:
        order.append("source_opened")
        return _frame_source([authorization])()

    broker = _FakeBroker()
    clerk = _EvidenceSink(read=broker, trade=broker)
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=frame_source,
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=1,
    )

    async def _record_gap_reconcile() -> None:
        order.append("gap_reconcile")

    consumer._gap_reconcile = _record_gap_reconcile  # type: ignore[method-assign]
    await consumer.run()

    assert order == ["source_opened", "source_opened", "gap_reconcile"]


async def test_gap_reconcile_uses_the_lifecycle_timestamp_for_fills() -> None:
    filled_at_ms = _FIXED_MS + 5_000
    order = _filled_broker_order("filled-order", _OWNED_COID).model_copy(
        update={"updated_at_ms": _FIXED_MS + 60_000, "filled_at_ms": filled_at_ms}
    )

    event = _recovery_event(order)

    assert event is not None
    assert event.occurred_at_ms == filled_at_ms
    assert event.price == 135.8
    assert event.quantity == 10.0


async def test_a_gap_reconciled_order_reaches_the_clerk_as_the_broker_returned_it(tmp_path: Path) -> None:
    """The REST re-pull hands the Clerk the order it read: nothing is re-shaped on the way (#2656).

    A ``replaced`` order keeps the link naming its replacement -- without it
    the Clerk cannot follow the chain -- and the order keeps every other
    value it was read with, ``extended_hours`` and the read instant included.
    """
    replaced = _accepted_order(_OWNED_COID).model_copy(
        update={
            "order_id": "00000000-0000-0000-0000-00000000000a",
            "status": "replaced",
            "replaced_by": "00000000-0000-0000-0000-00000000000b",
            "extended_hours": True,
            "updated_at_ms": _FIXED_MS,
            "observed_at_ms": _FIXED_MS - 7_000,
        }
    )
    broker = _FakeBroker()
    broker.orders = [replaced]
    consumer, clerk, _ = await _consumer(tmp_path, [_AUTHORIZED], broker=broker, max_reconnects=1)

    await consumer.run()

    [entry] = [e for e in clerk._journal.read_entries() if e.kind is ClerkEntryKind.ORDER_EVENT]  # type: ignore[union-attr]
    assert entry.event.event_type == "replaced"
    assert entry.event.execution_id is None
    assert entry.broker_order == replaced
    assert consumer.counters.gap_reconciled == 1


async def test_a_gap_reconciled_order_with_a_negative_instant_is_counted_not_fed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Corruption is refused the way a frame with a negative ``timestamp_ms`` is:
    # counted, logged with its order and why, the rest of the pull still fed, no reconnect.
    corrupt = _filled_broker_order("corrupt-instant", _OWNED_COID).model_copy(update={"filled_at_ms": -5_000})
    healthy = _filled_broker_order("healthy-fill", _OWNED_COID)
    broker = _FakeBroker()
    broker.orders = [corrupt, healthy]
    consumer, clerk, _ = await _consumer(tmp_path, [_AUTHORIZED], broker=broker, max_reconnects=1)

    with caplog.at_level(logging.WARNING, logger="app.broker.alpaca.trade_updates"):
        await consumer.run()

    fed = [e.broker_order.order_id for e in clerk._journal.read_entries()]  # type: ignore[union-attr]
    assert fed == ["healthy-fill"]
    assert consumer.counters.parse_errors == 1
    assert consumer.counters.connects == 2
    [record] = [r for r in caplog.records if getattr(r, "action", None) == "trade_updates_map_error"]
    assert record.order_id == "corrupt-instant"
    assert "negative lifecycle instant" in record.error


async def test_backoff_caps_before_exponentiation(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def _record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("app.broker.alpaca.trade_updates.asyncio.sleep", _record_sleep)

    from app.broker.alpaca.trade_updates import _default_backoff

    await _default_backoff(2_000)

    assert delays == [30.0]


# ── protocol helpers ─────────────────────────────────────────────────────────


async def test_socket_authorizes_before_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    class _Socket:
        async def send(self, message: str) -> None:
            sent.append(json.loads(message))

        async def recv(self) -> str:
            return json.dumps({"stream": "authorization", "data": {"status": "authorized"}})

        def __aiter__(self) -> AsyncIterator[str]:
            async def _empty() -> AsyncIterator[str]:
                if False:
                    yield ""

            return _empty()

    class _Connection:
        socket = _Socket()

        async def __aenter__(self) -> _Socket:
            return self.socket

        async def __aexit__(self, *_: object) -> None:
            return None

    connection = _Connection()
    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=lambda _, **__: connection))
    source = alpaca_socket_frames(AlpacaSettings(api_key_id="key", api_secret_key="secret", mode="paper"))

    authorization = await anext(source)

    assert json.loads(authorization)["data"]["status"] == "authorized"
    assert [message["action"] for message in sent] == ["authenticate", "listen"]
    await source.aclose()


async def test_a_server_that_never_answers_authentication_ends_the_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A server that completes the handshake and stays silent still answers the
    # library's pings: without its own bound the cycle waits for ever, with
    # ``connected`` False and nothing logged.
    monkeypatch.setattr("app.broker.alpaca.trade_updates._AUTHORIZATION_TIMEOUT_S", 0.05)
    closed: list[bool] = []

    class _SilentSocket:
        async def send(self, message: str) -> None:
            del message

        async def recv(self) -> str:
            await asyncio.Event().wait()
            raise AssertionError("a silent server never answers")  # pragma: no cover

    class _Connection:
        async def __aenter__(self) -> _SilentSocket:
            return _SilentSocket()

        async def __aexit__(self, *_: object) -> None:
            closed.append(True)

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=lambda _, **__: _Connection()))
    settings = AlpacaSettings(api_key_id="key", api_secret_key="secret", mode="paper")
    broker = _FakeBroker()
    consumer = TradeUpdatesConsumer(
        evidence_sink=_EvidenceSink(read=broker, trade=broker),
        read=broker,
        frame_source=lambda: alpaca_socket_frames(settings),
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=0,
    )

    await asyncio.wait_for(consumer.run(), timeout=3)

    assert closed == [True]
    assert consumer.counters.connects == 0
    assert consumer.counters.reconnects == 1


async def test_the_frame_source_is_closed_when_a_cycle_ends_on_an_error(tmp_path: Path) -> None:
    # A cycle that ends on a raised error leaves its generator suspended inside
    # the socket's ``async with``; the socket must be released with the cycle,
    # not whenever the generator is collected, or the reconnect opens a second
    # connection against the account's connection limit.
    closed: list[bool] = []

    async def source() -> AsyncIterator[bytes | str]:
        try:
            yield json.dumps(_load_frames()[0])
            yield json.dumps(_load_frames()[0])
        finally:
            closed.append(True)

    class _ClerkDown(_EvidenceSink):
        async def record_lifecycle_event(self, **_: object) -> ClerkEntryKind:  # type: ignore[override]
            raise RuntimeError("the clerk is down")

    broker = _FakeBroker()
    consumer = TradeUpdatesConsumer(
        evidence_sink=_ClerkDown(read=broker, trade=broker),
        read=broker,
        frame_source=source,
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=0,
    )

    await consumer.run()

    assert closed == [True]


async def test_the_authenticate_frame_never_reaches_a_debug_log(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``websockets`` logs every frame it sends at DEBUG, and the authenticate
    # frame carries the secret key. The library elides the middle of a long
    # frame, so it is the key's tail that reaches the log, not the whole of it.
    # Driven through a real loopback server with the root logger at DEBUG; the
    # server's own logger is held quiet so the assertion reads the client alone.
    import websockets

    secret = "s" * 29 + "TAILMARKER7"
    received: list[dict[str, Any]] = []

    async def server(connection: Any) -> None:
        received.append(json.loads(await connection.recv()))
        await connection.send(json.dumps(_AUTHORIZED))
        await connection.recv()

    server_logger = logging.getLogger("tests.loopback.trade_updates_server")
    server_logger.setLevel(logging.INFO)
    async with websockets.serve(server, "127.0.0.1", 0, logger=server_logger) as listening:
        port = listening.sockets[0].getsockname()[1]
        monkeypatch.setattr("app.broker.alpaca.trade_updates._stream_url", lambda _: f"ws://127.0.0.1:{port}/stream")
        caplog.set_level(logging.DEBUG)
        source = alpaca_socket_frames(AlpacaSettings(api_key_id="key-id", api_secret_key=secret, mode="paper"))
        await anext(source)
        await source.aclose()

    assert received[0]["data"]["secret_key"] == secret  # it did cross the wire
    assert "TAILMARKER7" not in caplog.text


def test_stream_url_is_paper_wss_stream() -> None:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    assert _stream_url(settings) == "wss://paper-api.alpaca.markets/stream"


async def test_connection_watermark_flips_during_a_cycle_and_clears_after(
    tmp_path: Path,
) -> None:
    """S4 (#1262): the connection watermark feeds the dual-health gate.

    A consumer starts disconnected; a consumed cycle marks it connected from
    the first accepted frame and disconnected again when the source ends, with
    the change time stamped for P7 age.
    """
    observed: list[bool] = []
    consumer, _clerk, _journal = await _consumer(tmp_path, _load_frames()[:1])
    original = consumer._handle_frame

    async def _spy(frame):  # records the watermark while a frame is in flight
        observed.append(consumer.connected)
        await original(frame)

    consumer._handle_frame = _spy  # type: ignore[method-assign]
    assert consumer.connected is False

    await consumer.run()

    assert observed == [True]  # connected while the cycle was live
    assert consumer.connected is False  # source exhausted -> disconnected
    assert consumer.connection_changed_at_ms == _FIXED_MS


# ── Dev-only fault-injection seam: frame-source hook (PRD #1354) ───────────────


@pytest.fixture
def permit_frame_injection(monkeypatch: pytest.MonkeyPatch):
    """Permit the seam (flag on + paper) and reset the registry around the test."""
    from types import SimpleNamespace

    from app.broker.alpaca import fault_injection as fi

    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", True)
    monkeypatch.setattr(fi, "resolved_alpaca_settings", lambda: SimpleNamespace(is_paper=True))
    fi.reset_fault_injection_for_testing()
    yield fi
    fi.reset_fault_injection_for_testing()


async def _collect(agen) -> list[str]:
    return [frame async for frame in agen]


async def test_inject_frame_faults_interleaves_after_real_frame(permit_frame_injection) -> None:
    from app.broker.alpaca.fault_injection import FrameFaultKind

    real = json.dumps({"stream": "trade_updates", "data": {"event": "new", "order": {"client_order_id": "coid"}}})
    permit_frame_injection.get_fault_injection_registry().arm(
        FrameFaultKind.HALT_SUSPENDED, target="coid", params={"symbol": "AAPL"}
    )

    out = await _collect(_inject_frame_faults(_frame_source([real])()))

    assert len(out) == 2
    assert out[0] == real
    injected = json.loads(out[1])
    assert injected["stream"] == "trade_updates"
    assert injected["data"]["event"] == "suspended"
    assert injected["data"]["order"]["client_order_id"] == "coid"


async def test_inject_frame_faults_redelivers_last_trade_update(permit_frame_injection) -> None:
    from app.broker.alpaca.fault_injection import FrameFaultKind

    fill = json.dumps({"stream": "trade_updates", "data": {"event": "fill", "order": {"client_order_id": "coid"}}})
    permit_frame_injection.get_fault_injection_registry().arm(FrameFaultKind.REDELIVER_LAST)

    out = await _collect(_inject_frame_faults(_frame_source([fill])()))

    # The fill is redelivered verbatim — the dedup path must absorb the copy.
    assert out == [fill, fill]


async def test_injected_disconnect_enters_reconnect_path(permit_frame_injection) -> None:
    from app.broker.alpaca.fault_injection import FrameFaultKind

    real = json.dumps({"stream": "authorization", "data": {"status": "authorized"}})
    permit_frame_injection.get_fault_injection_registry().arm(FrameFaultKind.DISCONNECT)
    source = _inject_frame_faults(_frame_source([real])())

    assert await anext(source) == real
    with pytest.raises(ConnectionError, match="injected trade_updates disconnect"):
        await anext(source)


async def test_injected_disconnect_preserves_prior_fault_frames(
    permit_frame_injection,
) -> None:
    from app.broker.alpaca.fault_injection import FrameFaultKind

    real = json.dumps({"stream": "trade_updates", "data": {"event": "new", "order": {"client_order_id": "coid"}}})
    registry = permit_frame_injection.get_fault_injection_registry()
    registry.arm(
        FrameFaultKind.PARTIAL_FILL,
        target="coid",
        params={"broker_order_id": "broker-1", "qty": "1", "order_qty": "2"},
    )
    registry.arm(FrameFaultKind.DISCONNECT)
    source = _inject_frame_faults(_frame_source([real])())

    assert await anext(source) == real
    injected = json.loads(await anext(source))
    assert injected["data"]["event"] == "partial_fill"
    with pytest.raises(ConnectionError, match="injected trade_updates disconnect"):
        await anext(source)


async def test_inject_frame_faults_redeliver_with_no_prior_frame_is_skipped(
    permit_frame_injection,
) -> None:
    from app.broker.alpaca.fault_injection import FrameFaultKind

    # A non-trade_updates frame does not become the redelivery target.
    keepalive = json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}})
    permit_frame_injection.get_fault_injection_registry().arm(FrameFaultKind.REDELIVER_LAST)

    out = await _collect(_inject_frame_faults(_frame_source([keepalive])()))

    assert out == [keepalive]


async def test_inject_frame_faults_passthrough_when_not_permitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.broker.alpaca import fault_injection as fi

    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", False)
    fi.reset_fault_injection_for_testing()
    real = json.dumps({"stream": "trade_updates", "data": {"event": "new"}})

    out = await _collect(_inject_frame_faults(_frame_source([real])()))

    assert out == [real]


async def test_injected_frame_threads_through_the_real_consumer(tmp_path: Path, permit_frame_injection) -> None:
    # The injected frame must reach the SAME consumer path — verbatim capture
    # is step 1 of _handle_frame, so a captured injected frame proves it threaded
    # through the real consumer, not a stubbed shortcut.
    from app.broker.alpaca.fault_injection import FrameFaultKind

    permit_frame_injection.get_fault_injection_registry().arm(FrameFaultKind.HALT_SUSPENDED, target=_OWNED_COID)
    broker = _FakeBroker()
    clerk = _EvidenceSink(read=broker, trade=broker)
    await _warm(clerk)
    journal = _capture_journal(tmp_path)
    real = json.dumps(_load_frames()[0])  # a real 'new' event for _OWNED_COID
    consumer = TradeUpdatesConsumer(
        evidence_sink=clerk,
        read=broker,
        frame_source=lambda: _inject_frame_faults(_frame_source([real])()),
        journal=journal,
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=0,
    )

    await consumer.run()

    captured = _capture_records(tmp_path)
    # Both the real frame AND the injected suspended frame were captured.
    assert len(captured) == 2
    bodies = "".join(json.dumps(record) for record in captured)
    assert "suspended" in bodies


class _ReconcileRecordingSink(_EvidenceSink):
    """An _EvidenceSink that records which loop cycle asked for a gap-fill."""

    def __init__(self, cycles: dict[str, int]) -> None:
        super().__init__()
        self._cycles = cycles
        self.reconciled_on_cycle: list[int] = []

    async def reconcile_gap(self) -> None:
        # ``_consume_once`` reconciles after ``anext`` has already advanced the
        # counter, so the cycle being reconciled is one behind it.
        self.reconciled_on_cycle.append(self._cycles["n"] - 1)


async def test_reconnect_backoff_resets_after_a_connected_cycle_and_still_gap_reconciles(
    tmp_path: Path,
) -> None:
    """A blip hours ago must not pin a healthy trade_updates stream at 30s.

    ``attempt`` fed ``_default_backoff`` *and* answered "is this a reconnect?".
    Set to zero once before the loop and only ever incremented, it pinned the
    backoff at its 30s ceiling after roughly six lifetime errors — and on this
    stream every second of backoff is a second the clerk is not receiving
    fills and order lifecycle events.

    Both halves of the split are pinned here. The backoff resets after a cycle
    that actually connected AND held the socket at least the backoff ceiling,
    AND the connect that follows the reset still runs the REST gap-reconcile:
    a single-counter reset that lands after the increment makes the next
    connect look like a first connect, skips the gap-fill, and silently drops
    the fills missed while down. A fifth, connect-then-raise-mid-stream cycle
    pins that the held-duration reset applies on the error path too, not just
    a clean end of stream.
    """
    cycles = {"n": 0}
    now = {"ms": _FIXED_MS}
    backoff_attempts: list[int] = []
    broker = _FakeBroker()
    sink = _ReconcileRecordingSink(cycles)

    async def frame_source() -> AsyncIterator[bytes | str]:
        index = cycles["n"]
        cycles["n"] += 1
        if index >= 5:
            # End the loop deterministically; run() re-raises CancelledError
            # rather than swallowing it in its except-Exception.
            raise asyncio.CancelledError
        if index in (2, 3):
            # The connected cycles: each delivers a frame, holds the socket
            # for the backoff ceiling, then ends cleanly.
            yield json.dumps({"stream": "authorization", "data": {"status": "authorized"}})
            now["ms"] += _MIN_HELD_CONNECTION_MS
            return
        if index == 4:
            # Connect, hold past the ceiling, then die mid-stream (not a
            # clean end of stream) — the reset must still apply here.
            yield json.dumps({"stream": "authorization", "data": {"status": "authorized"}})
            now["ms"] += _MIN_HELD_CONNECTION_MS
            raise RuntimeError("simulated mid-stream disconnect")
        raise RuntimeError("simulated disconnect")

    async def _record_backoff(attempt: int) -> None:
        backoff_attempts.append(attempt)

    consumer = TradeUpdatesConsumer(
        evidence_sink=sink,
        read=broker,
        frame_source=frame_source,
        journal=_capture_journal(tmp_path),
        clock=lambda: now["ms"],
        backoff=_record_backoff,
    )

    with pytest.raises(asyncio.CancelledError):
        await consumer.run()

    assert backoff_attempts == [1, 2, 1, 1, 1], (
        "A cycle that connected AND held the socket at least the backoff "
        "ceiling must reset the reconnect backoff. Without the reset this "
        "reads [1, 2, 3, 4, 5] and keeps climbing to the 30s ceiling, where "
        "every later blip costs half a minute of unseen fills."
    )
    assert sink.reconciled_on_cycle == [2, 3, 4], (
        "Every connect after the first cycle must REST gap-reconcile, "
        "including the one immediately after a backoff reset and the one "
        "that dies mid-stream after being held — that is the fill-losing "
        "regression a single-counter reset introduces."
    )


async def test_reconnect_backoff_keeps_escalating_when_the_socket_flaps(
    tmp_path: Path,
) -> None:
    """A connect-then-drop flap must not hot-loop the reconnect at the 1s floor.

    A socket the server evicts seconds after the auth ack reaches the
    connection watermark every cycle — resetting on bare connect would pin
    the backoff at 1s forever, which on this stream is 60 reconnects/min and
    60 REST gap-reconciles/min (500-order ``list_orders`` each) against a
    live broker. The held-duration conjunct is what stops that.
    """
    cycles = {"n": 0}
    backoff_attempts: list[int] = []
    broker = _FakeBroker()
    sink = _ReconcileRecordingSink(cycles)

    async def frame_source() -> AsyncIterator[bytes | str]:
        index = cycles["n"]
        cycles["n"] += 1
        if index >= 4:
            # End the loop deterministically; run() re-raises CancelledError
            # rather than swallowing it in its except-Exception.
            raise asyncio.CancelledError
        # The flap: connects every cycle (reaches the watermark) but the
        # clock never advances, so the connection is never "held".
        yield json.dumps({"stream": "authorization", "data": {"status": "authorized"}})
        return

    async def _record_backoff(attempt: int) -> None:
        backoff_attempts.append(attempt)

    consumer = TradeUpdatesConsumer(
        evidence_sink=sink,
        read=broker,
        frame_source=frame_source,
        journal=_capture_journal(tmp_path),
        clock=lambda: _FIXED_MS,
        backoff=_record_backoff,
    )

    with pytest.raises(asyncio.CancelledError):
        await consumer.run()

    assert backoff_attempts == [1, 2, 3, 4], (
        "A socket evicted right after the auth ack is a flap; resetting on "
        "bare connect hot-loops the reconnect and the 500-order REST "
        "gap-reconcile at the 1s floor."
    )
    assert consumer.counters.connects == 4, (
        "The flap DID reach the watermark every cycle — that is what makes "
        "the held-duration conjunct load-bearing."
    )
