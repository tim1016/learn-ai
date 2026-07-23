"""Consumer seam tests for the owned ``trade_updates`` websocket (S4).

The consumer is driven by an **injected frame source** (an async-iterator
factory) plus an injected clock and backoff, so every concern — capture,
parse, live_idempotent dedup, attribution, and the post-reconnect REST
gap-reconcile — is exercised with no network. A REAL ``CaptureJournal`` and a
REAL ``OrderJournal``-backed ``AlpacaClerk`` on tmp dirs assert the actual
on-disk records, not mocks.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import responses

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.trade_updates import TradeUpdatesConsumer, _stream_url
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerOrderRequest,
)

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
    "pattern_day_trader": False,
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
        account_status="ACTIVE",
        currency="USD",
        cash=1000.0,
        equity=1000.0,
        buying_power=2000.0,
        portfolio_value=1000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
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


class _FakeBroker:
    """Read+trade port double: submit records the owned order_ref in the journal."""

    broker_id = "alpaca"

    def __init__(self, *, account: BrokerAccountSnapshot | None = None) -> None:
        self._account = account or _account()
        self.orders: list[BrokerOrder] = []
        self.list_orders_calls: list[dict[str, Any]] = []

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


def _frame_source(frames: list[Any]):
    """Build a FrameSource that yields the given frames once, then ends."""

    async def _source() -> AsyncIterator[bytes | str]:
        for frame in frames:
            yield frame if isinstance(frame, (bytes, str)) else json.dumps(frame)

    return _source


async def _no_backoff(attempt: int) -> None:
    return None


async def _warm(clerk: AlpacaClerk, operator: str = "inkant") -> None:
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
            legs=[BrokerOrderLeg(symbol="AAPL", side="buy", quantity=10)],
        )
    )


@pytest.fixture(autouse=True)
def _clerk_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path / "clerk"))
    journal_module.reset_clerk_settings_for_testing()
    yield
    journal_module.reset_clerk_settings_for_testing()


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
    clerk: AlpacaClerk | None = None,
    max_reconnects: int = 0,
) -> tuple[TradeUpdatesConsumer, AlpacaClerk, CaptureJournal]:
    broker = broker or _FakeBroker()
    clerk = clerk or AlpacaClerk(read=broker, trade=broker)
    journal = _capture_journal(tmp_path)
    consumer = TradeUpdatesConsumer(
        clerk=clerk,
        read=broker,
        frame_source=_frame_source(frames),
        journal=journal,
        clock=lambda: _FIXED_MS,
        backoff=_no_backoff,
        max_reconnects=max_reconnects,
    )
    return consumer, clerk, journal


def _load_frames() -> list[dict[str, Any]]:
    path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "alpaca"
        / "trade_updates"
        / "trade_updates.json"
    )
    return json.loads(path.read_text())


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

    class _SpyClerk(AlpacaClerk):
        async def record_lifecycle_event(self, **kwargs: Any) -> ClerkEntryKind:  # type: ignore[override]
            order_of_calls.append("handle")
            return await super().record_lifecycle_event(**kwargs)

    broker = _FakeBroker()
    clerk = _SpyClerk(read=broker, trade=broker)
    frame = json.dumps(_load_frames()[0])
    journal = _SpyJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS)
    consumer = TradeUpdatesConsumer(
        clerk=clerk,
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


# ── (d) attribution: owned vs unexplained (NO hold — that is S6) ─────────────


async def test_owned_client_order_id_journals_order_event(tmp_path: Path) -> None:
    # Warm the clerk with a real submit so ``manual/inkant/v1`` is a known
    # namespace, then feed an event with that owned client_order_id.
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker)
    submit = await clerk.submit(
        BrokerOrderRequest(operator="inkant", legs=[BrokerOrderLeg(symbol="AAPL", side="buy", quantity=10)])
    )
    owned_ref = submit.results[0].order_ref
    frame = _load_frames()[0]
    frame["data"]["order"]["client_order_id"] = owned_ref

    consumer, _, _ = await _consumer(tmp_path, [frame], broker=broker, clerk=clerk)
    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    event = next(e for e in entries if e.kind is ClerkEntryKind.ORDER_EVENT)
    assert event.owned is True
    assert event.order_ref == owned_ref
    assert consumer.counters.unexplained == 0


async def test_foreign_client_order_id_journals_unexplained_and_counts(tmp_path: Path) -> None:
    frame = _load_frames()[0]
    frame["data"]["order"]["client_order_id"] = "someone-elses-order-id"
    consumer, clerk, _ = await _consumer(tmp_path, [frame])

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    unexplained = [e for e in entries if e.kind is ClerkEntryKind.UNEXPLAINED_ORDER]
    assert len(unexplained) == 1
    assert unexplained[0].owned is False
    assert unexplained[0].client_order_id == "someone-elses-order-id"
    # No fabricated identity.
    assert unexplained[0].order_ref == ""
    assert consumer.counters.unexplained == 1
    assert clerk.unexplained_order_count == 1
    # NO hold is wired in S4 — a subsequent submit is NOT blocked.
    result = await clerk.submit(
        BrokerOrderRequest(operator="inkant", legs=[BrokerOrderLeg(symbol="AAPL", side="buy", quantity=1)])
    )
    assert result.results[0].status == "acked"


async def test_absent_client_order_id_journals_unexplained(tmp_path: Path) -> None:
    frame = _load_frames()[0]
    frame["data"]["order"].pop("client_order_id", None)
    consumer, clerk, _ = await _consumer(tmp_path, [frame])

    await consumer.run()

    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    unexplained = [e for e in entries if e.kind is ClerkEntryKind.UNEXPLAINED_ORDER]
    assert len(unexplained) == 1
    assert unexplained[0].client_order_id == ""
    assert consumer.counters.unexplained == 1


async def test_authorization_frame_is_captured_not_attributed(tmp_path: Path) -> None:
    auth_ok = {"stream": "authorization", "data": {"status": "authorized", "action": "authenticate"}}
    consumer, clerk, _ = await _consumer(tmp_path, [auth_ok])

    await consumer.run()

    # Captured (verbatim) but nothing attributed.
    assert len(_capture_records(tmp_path)) == 1
    assert clerk._journal is None or clerk._journal.read_entries() == []  # type: ignore[union-attr]
    assert consumer.counters.events_applied == 0
    assert consumer.counters.unexplained == 0


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
    clerk = AlpacaClerk(read=broker, trade=broker)
    # Warm the clerk namespace so the reconciled order attributes as OWNED.
    await _warm(clerk)
    # Make the warmed order_ref match the reconciled client_order_id so the
    # namespace (manual/inkant/v1) is known; identity resolution keys on namespace.
    partial = _load_frames()[1]
    partial["data"]["order"]["client_order_id"] = _OWNED_COID

    journal = _capture_journal(tmp_path)
    consumer = TradeUpdatesConsumer(
        clerk=clerk,
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
async def test_gap_reconcile_dedups_already_seen_event(tmp_path: Path) -> None:
    # If the socket already delivered the fill, the gap-reconcile's re-observed
    # fill must dedup on the stable key — not journal a second fill.
    filled_order = {
        "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
        "client_order_id": _OWNED_COID,
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
        "status": "filled",
    }
    responses.add(responses.GET, f"{_BASE}/v2/account", json=_ACCOUNT_JSON, status=200)
    responses.add(responses.POST, f"{_BASE}/v2/orders", json=_ACCEPTED_ORDER_JSON, status=200)
    responses.add(responses.GET, f"{_BASE}/v2/orders", json=[filled_order], status=200)

    broker = _real_broker_with_responses([filled_order])
    clerk = AlpacaClerk(read=broker, trade=broker)
    await _warm(clerk)
    fill = _load_frames()[2]
    # The socket's fill has an execution_id key; the gap-reconcile synthesizes an
    # order-derived event WITHOUT an execution_id, so their keys differ. To prove
    # the reconcile is idempotent for the SAME event, feed a socket fill that
    # keys the same way the reconcile will: strip its execution_id so both key on
    # order_id|fill|timestamp.
    fill["data"].pop("execution_id", None)
    fill["data"]["order"]["client_order_id"] = _OWNED_COID
    fill["data"]["timestamp"] = "2021-03-16T18:38:02.123456Z"

    journal = _capture_journal(tmp_path)
    consumer = TradeUpdatesConsumer(
        clerk=clerk,
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
    # Delivered once on the socket, re-observed once on reconcile → journaled once.
    assert len(fills) == 1


@responses.activate
async def test_gap_reconcile_dedups_socket_fill_by_terminal_order(tmp_path: Path) -> None:
    # PRODUCTION shape: the socket delivered the fill WITH its execution_id (key
    # ``exec:...``); a reconnect re-pulls the now-terminal order via REST, which
    # has NO execution_id (key ``order_id|fill|ms``). The two keys differ, so
    # key-only dedup would double-journal the fill — the terminal-order guard
    # recognizes the re-pull as a stale re-observation and does not re-journal.
    filled_order = {
        "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
        "client_order_id": _OWNED_COID,
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
        "status": "filled",
    }
    responses.add(responses.GET, f"{_BASE}/v2/account", json=_ACCOUNT_JSON, status=200)
    responses.add(responses.POST, f"{_BASE}/v2/orders", json=_ACCEPTED_ORDER_JSON, status=200)
    responses.add(responses.GET, f"{_BASE}/v2/orders", json=[filled_order], status=200)

    broker = _real_broker_with_responses([filled_order])
    clerk = AlpacaClerk(read=broker, trade=broker)
    await _warm(clerk)
    # The socket fill keeps its execution_id — the real wire shape.
    fill = _load_frames()[2]
    assert fill["data"].get("execution_id")  # guards the test premise
    fill["data"]["order"]["client_order_id"] = _OWNED_COID

    journal = _capture_journal(tmp_path)
    consumer = TradeUpdatesConsumer(
        clerk=clerk,
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


# ── protocol helpers ─────────────────────────────────────────────────────────


def test_stream_url_is_paper_wss_stream() -> None:
    from app.broker.alpaca.config import AlpacaSettings

    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    assert _stream_url(settings) == "wss://paper-api.alpaca.markets/stream"
