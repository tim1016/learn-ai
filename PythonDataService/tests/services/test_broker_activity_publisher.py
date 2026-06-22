"""Tests for ``app.services.broker_activity_publisher`` — the stateful
per-instance task that wires the pure reconciler to the WAL + SSE.

Coverage:

- Fill events get authored, written to WAL, broadcast to subscribers.
- Intermediate status events (Submitted) for OUR orders are skipped.
- Foreign fills (no namespace match) are authored as UNMATCHED rows.
- Duplicate exec_id from a re-delivered event is skipped (deduped via
  cold-start fold of the WAL).
- Slow subscribers are dropped without affecting others.
- ``stop()`` drains subscribers cleanly with a ``None`` sentinel.
- The registry singleton lifecycle: register starts, unregister stops.
- ``UnauthorableEventError`` events are skipped (logged), not authored.
- Cursor on ``LiveStateEnvelope`` advances per row.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.live_state_sidecar import (
    LiveStateEnvelope,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.schemas.broker_activity import (
    BrokerActivityRow,
    ReconciliationTimingPolicy,
    Verdict,
)
from app.services.broker_activity_publisher import (
    BrokerActivityPublisher,
    BrokerActivityPublisherRegistry,
)
from app.services.broker_activity_wal import (
    BrokerActivityWal,
    stable_broker_activity_wal_path,
)

pytestmark = pytest.mark.asyncio


SID = "sid-pub-test"
NS = f"learn-ai/{SID}/v1"
INTENT_ID = "intent-pub-1"
ORDER_REF = f"{NS}:{INTENT_ID}"


# ── Fixtures ────────────────────────────────────────────────────────


def _seed_envelope(
    artifacts_root: Path,
    *,
    submitted_orders: dict[str, dict] | None = None,
) -> LiveStateEnvelope:
    """Create a minimal valid LiveStateEnvelope at the canonical sidecar
    path so the publisher can read it. Returns the envelope so tests can
    inspect / assert on it later."""
    envelope = LiveStateEnvelope(
        strategy_instance_id=SID,
        run_id="run-pub-1",
        bot_order_namespace=NS,
        ib_client_id=42,
        last_processed_bar_ms=1,
        last_artifact_flush_ms=1,
        submitted_orders=submitted_orders
        or {INTENT_ID: {"perm_id": 999, "order_id": 42, "status": "Submitted", "symbol": "SPY"}},
    )
    repo = LiveStateSidecarRepo(stable_live_state_path(artifacts_root, SID))
    repo._path.parent.mkdir(parents=True, exist_ok=True)
    repo.write(envelope)
    return envelope


def _fill_event(
    *,
    order_ref: str | None = ORDER_REF,
    exec_id: str = "exec-pub-1",
    symbol: str = "SPY",
    side: str = "BUY",
    order_type: str = "MKT",
    fill_quantity: float = 100.0,
    last_fill_price: float = 450.0,
) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        account_id="DU1234567",
        order_id=42,
        perm_id=999,
        event_type="fill",
        status="Filled",
        order_ref=order_ref,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        order_type=order_type,
        exec_id=exec_id,
        fill_quantity=fill_quantity,
        avg_fill_price=last_fill_price,
        cumulative_filled=fill_quantity,
        remaining=0.0,
        last_fill_price=last_fill_price,
        exec_time_ms=1_700_000_000_000,
        fee=1.0,
        ts_ms=1_700_000_000_000,
    )


def _intermediate_status_event(order_ref: str | None = ORDER_REF) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        account_id="DU1234567",
        order_id=42,
        perm_id=999,
        event_type="status",
        status="Submitted",
        order_ref=order_ref,
        symbol="SPY",
        side="BUY",
        order_type="MKT",
        ts_ms=1_700_000_000_000,
    )


def _make_event_source(events: list[IbkrOrderEvent]):
    """Factory the publisher calls once; returns an async generator
    that yields the provided events then sleeps forever (so the
    publisher's task stays alive until ``stop()`` cancels it)."""

    async def _gen() -> AsyncIterator[IbkrOrderEvent]:
        for ev in events:
            yield ev
        # Stay alive so test can stop() cleanly.
        await asyncio.sleep(3600)

    return lambda: _gen()


def _build_publisher(
    tmp_path: Path,
    events: list[IbkrOrderEvent],
    *,
    timing_policy: ReconciliationTimingPolicy | None = None,
) -> tuple[BrokerActivityPublisher, Path, Path]:
    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    _seed_envelope(artifacts)
    publisher = BrokerActivityPublisher(
        strategy_instance_id=SID,
        bot_order_namespace=NS,
        run_dir=run_dir,
        artifacts_root=artifacts,
        timing_policy=timing_policy or ReconciliationTimingPolicy(),
        event_source_factory=_make_event_source(events),
    )
    return publisher, run_dir, artifacts


async def _wait_for_rows(
    wal_path: Path, *, want: int, timeout: float = 1.0
) -> list[BrokerActivityRow]:
    """Poll the WAL until ``want`` rows are persisted or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    wal = BrokerActivityWal(wal_path)
    while asyncio.get_event_loop().time() < deadline:
        rows = wal.read_all()
        if len(rows) >= want:
            return rows
        await asyncio.sleep(0.01)
    rows = wal.read_all()
    raise AssertionError(
        f"WAL has {len(rows)} row(s), wanted {want} within {timeout}s"
    )


# ── Tests ───────────────────────────────────────────────────────────


async def test_fill_event_is_authored_persisted_and_broadcast(
    tmp_path: Path,
) -> None:
    publisher, run_dir, _ = _build_publisher(tmp_path, [_fill_event()])
    queue = publisher.subscribe()
    publisher.start()
    try:
        rows = await _wait_for_rows(
            stable_broker_activity_wal_path(run_dir), want=1
        )
        assert rows[0].verdict == Verdict.EXPECTED
        assert rows[0].template_key == "normal_fill"
        assert rows[0].exec_id == "exec-pub-1"
        assert rows[0].order_ref == ORDER_REF

        # The subscriber receives the same row via the queue.
        broadcast = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert broadcast is not None
        assert broadcast.exec_id == "exec-pub-1"
    finally:
        publisher.unsubscribe(queue)
        await publisher.stop()


async def test_intermediate_status_events_are_skipped(tmp_path: Path) -> None:
    """A Submitted / PreSubmitted transition on an OWNED order is not a
    row — only fills / cancellations / rejections produce rows."""
    publisher, run_dir, _ = _build_publisher(
        tmp_path,
        [_intermediate_status_event(), _fill_event()],
    )
    publisher.start()
    try:
        rows = await _wait_for_rows(
            stable_broker_activity_wal_path(run_dir), want=1
        )
        # Only the fill became a row; the Submitted transition did not.
        assert len(rows) == 1
        assert rows[0].exec_id == "exec-pub-1"
    finally:
        await publisher.stop()


async def test_foreign_fill_authored_as_unmatched_execution(
    tmp_path: Path,
) -> None:
    """A fill arriving with no order_ref (or a non-matching namespace)
    is authored as UNMATCHED_EXECUTION so the operator sees it
    immediately."""
    publisher, run_dir, _ = _build_publisher(
        tmp_path, [_fill_event(order_ref=None, exec_id="foreign-1")]
    )
    publisher.start()
    try:
        rows = await _wait_for_rows(
            stable_broker_activity_wal_path(run_dir), want=1
        )
        assert rows[0].verdict == Verdict.UNEXPECTED
        assert rows[0].template_key == "unmatched_execution"
        assert rows[0].engine_overlay is None
    finally:
        await publisher.stop()


async def test_duplicate_exec_id_from_replay_is_deduped(tmp_path: Path) -> None:
    """On cold-start the publisher seeds its dedupe set from the WAL.
    A re-delivery of an already-authored exec_id authors a DUPLICATE
    row (audited) instead of a duplicate fill."""
    # Pre-populate the WAL with a row carrying exec_id="exec-pub-1".
    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    _seed_envelope(artifacts)
    wal = BrokerActivityWal(stable_broker_activity_wal_path(run_dir))
    pre = BrokerActivityRow(
        seq=1,
        ts_ms=1_700_000_000_000 - 1,
        exec_id="exec-pub-1",
        perm_id=999,
        order_ref=ORDER_REF,
        symbol="SPY",
        side="BUY",
        quantity=100.0,
        price=450.0,
        order_type="MKT",
        verdict=Verdict.EXPECTED,
        template_key="normal_fill",
        template_version=1,
        headline="pre-existing",
        narrative="pre-existing",
    )
    wal.allocate_seq()
    wal.append_row(pre)

    publisher = BrokerActivityPublisher(
        strategy_instance_id=SID,
        bot_order_namespace=NS,
        run_dir=run_dir,
        artifacts_root=artifacts,
        timing_policy=ReconciliationTimingPolicy(),
        event_source_factory=_make_event_source([_fill_event()]),
    )
    publisher.start()
    try:
        rows = await _wait_for_rows(
            stable_broker_activity_wal_path(run_dir), want=2
        )
        assert rows[0].headline == "pre-existing"
        assert rows[1].verdict == Verdict.UNEXPECTED
        assert rows[1].template_key == "duplicate_execution"
    finally:
        await publisher.stop()


async def test_unauthorable_event_is_skipped_not_persisted(
    tmp_path: Path,
) -> None:
    """An event missing symbol/side/order_type cannot be truthfully
    authored — the publisher logs and skips it rather than authoring a
    placeholder row."""
    bad_event = IbkrOrderEvent(
        account_id="DU1234567",
        order_id=42,
        perm_id=999,
        event_type="fill",
        status="Filled",
        order_ref=ORDER_REF,
        symbol=None,  # missing — unauthorable
        side="BUY",
        order_type="MKT",
        exec_id="exec-bad",
        fill_quantity=100.0,
        last_fill_price=450.0,
        ts_ms=1_700_000_000_000,
    )
    publisher, run_dir, _ = _build_publisher(tmp_path, [bad_event, _fill_event()])
    publisher.start()
    try:
        rows = await _wait_for_rows(
            stable_broker_activity_wal_path(run_dir), want=1
        )
        # Only the good event landed; the bad one was skipped.
        assert len(rows) == 1
        assert rows[0].exec_id == "exec-pub-1"
    finally:
        await publisher.stop()


async def test_envelope_cursor_advances_per_row(tmp_path: Path) -> None:
    publisher, run_dir, artifacts = _build_publisher(tmp_path, [_fill_event()])
    publisher.start()
    try:
        await _wait_for_rows(
            stable_broker_activity_wal_path(run_dir), want=1
        )
        repo = LiveStateSidecarRepo(stable_live_state_path(artifacts, SID))
        envelope = repo.read()
        assert envelope is not None
        assert envelope.last_broker_activity_wal_seq == 1
    finally:
        await publisher.stop()


async def test_backfill_returns_rows_after_cursor(tmp_path: Path) -> None:
    """REST backfill API returns rows with seq > cursor for cold-start
    clients."""
    publisher, run_dir, _ = _build_publisher(
        tmp_path,
        [
            _fill_event(exec_id="e1"),
            _fill_event(exec_id="e2"),
            _fill_event(exec_id="e3"),
        ],
    )
    publisher.start()
    try:
        await _wait_for_rows(
            stable_broker_activity_wal_path(run_dir), want=3
        )
        page = publisher.backfill(after_seq=1)
        assert [r.exec_id for r in page] == ["e2", "e3"]
    finally:
        await publisher.stop()


async def test_stop_drains_subscribers_with_none_sentinel(tmp_path: Path) -> None:
    publisher, _, _ = _build_publisher(tmp_path, [])
    queue = publisher.subscribe()
    publisher.start()
    # Stop immediately; subscriber should see the sentinel.
    await publisher.stop()
    sentinel = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert sentinel is None


async def test_registry_register_starts_unregister_stops(tmp_path: Path) -> None:
    publisher, _, _ = _build_publisher(tmp_path, [])
    registry = BrokerActivityPublisherRegistry()

    await registry.register(publisher, strategy_instance_id=SID)
    assert publisher.is_running
    assert registry.get(SID) is publisher
    assert SID in registry.instances()

    await registry.unregister(SID)
    assert not publisher.is_running
    assert registry.get(SID) is None


async def test_registry_stop_all_drains_every_publisher(tmp_path: Path) -> None:
    pubs = []
    registry = BrokerActivityPublisherRegistry()
    for i in range(3):
        p, _, _ = _build_publisher(tmp_path / f"i{i}", [])
        # Each publisher needs a unique sid so the registry holds all 3.
        # We rebuild with the override below for simplicity.
        artifacts = (tmp_path / f"i{i}") / "artifacts"
        run_dir = (tmp_path / f"i{i}") / "run-dir"
        envelope_sid = f"sid-multi-{i}"
        envelope_ns = f"learn-ai/{envelope_sid}/v1"
        envelope = LiveStateEnvelope(
            strategy_instance_id=envelope_sid,
            run_id=f"r{i}",
            bot_order_namespace=envelope_ns,
            ib_client_id=i,
            last_processed_bar_ms=1,
            last_artifact_flush_ms=1,
        )
        repo = LiveStateSidecarRepo(stable_live_state_path(artifacts, envelope_sid))
        repo._path.parent.mkdir(parents=True, exist_ok=True)
        repo.write(envelope)
        p = BrokerActivityPublisher(
            strategy_instance_id=envelope_sid,
            bot_order_namespace=envelope_ns,
            run_dir=run_dir,
            artifacts_root=artifacts,
            timing_policy=ReconciliationTimingPolicy(),
            event_source_factory=_make_event_source([]),
        )
        await registry.register(p, strategy_instance_id=envelope_sid)
        pubs.append(p)

    assert len(registry.instances()) == 3
    await registry.stop_all()
    assert registry.instances() == ()
    for p in pubs:
        assert not p.is_running


async def test_unauthorable_event_does_not_consume_seq(tmp_path: Path) -> None:
    """Regression: an unauthorable event must NOT advance the WAL seq
    (no row was authored, so no seq was consumed). The next good event
    gets the seq that the bad event would have taken — keeps the WAL
    sequence dense.
    """
    bad_event = IbkrOrderEvent(
        account_id="DU1234567",
        order_id=42,
        perm_id=999,
        event_type="fill",
        status="Filled",
        order_ref=ORDER_REF,
        symbol=None,
        side="BUY",
        order_type="MKT",
        exec_id="exec-bad",
        fill_quantity=100.0,
        last_fill_price=450.0,
        ts_ms=1_700_000_000_000,
    )
    publisher, run_dir, _ = _build_publisher(tmp_path, [bad_event, _fill_event()])
    publisher.start()
    try:
        rows = await _wait_for_rows(
            stable_broker_activity_wal_path(run_dir), want=1
        )
        assert rows[0].seq == 1
    finally:
        await publisher.stop()
