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


async def test_fill_matches_via_intent_wal_when_sidecar_empty(
    tmp_path: Path,
) -> None:
    """Regression for the normal durable-submit path: the engine writes
    a ``SUBMITTED`` event to ``intent_events.jsonl`` synchronously
    before ``placeOrder``, but the sidecar's ``submitted_orders`` map
    only catches up on the next artifact flush. A fill that arrives in
    between MUST still match its intent — otherwise every fresh fill
    would be authored as ``unmatched_execution`` with no engine
    overlay.
    """
    from app.engine.live.intent_events import IntentEventType
    from app.engine.live.intent_wal import IntentWal

    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    # Sidecar exists but submitted_orders is empty — the engine hasn't
    # flushed yet.
    _seed_envelope(artifacts, submitted_orders={})
    # Intent WAL carries the SUBMITTED event for this intent.
    wal = IntentWal(run_dir / "intent_events.jsonl")
    wal.append(
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=INTENT_ID,
        bot_order_namespace=NS,
        order_ref=ORDER_REF,
    )
    wal.append(
        event_type=IntentEventType.SUBMITTED,
        intent_id=INTENT_ID,
        bot_order_namespace=NS,
        order_ref=ORDER_REF,
        order_id=42,
        perm_id=999,
    )

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
            stable_broker_activity_wal_path(run_dir), want=1
        )
        # The fill matched the WAL-folded intent — NOT unmatched.
        assert rows[0].verdict == Verdict.EXPECTED
        assert rows[0].template_key == "normal_fill"
        assert rows[0].engine_overlay is not None
        assert rows[0].engine_overlay.intent_id == INTENT_ID
    finally:
        await publisher.stop()


async def test_event_for_foreign_namespace_is_silently_skipped(
    tmp_path: Path,
) -> None:
    """When multiple strategy instances share an IBKR account, every
    same-account trade is yielded by ``stream_order_events``. An event
    with a parseable ``order_ref`` whose namespace belongs to ANOTHER
    instance must be silently ignored — otherwise this instance's WAL
    fills up with one ``unmatched_execution`` row per other instance
    per fill.

    Truly foreign events (no parseable ``order_ref`` — e.g. a manual
    TWS click) still get authored as ``unmatched_execution``.
    """
    other_namespace_ref = "learn-ai/other-sid/v1:intent-x"
    publisher, run_dir, _ = _build_publisher(
        tmp_path,
        [
            # Other instance's fill — must be skipped.
            _fill_event(
                order_ref=other_namespace_ref,
                exec_id="other-instance-exec",
            ),
            # Our fill — must be authored.
            _fill_event(exec_id="our-exec"),
        ],
    )
    publisher.start()
    try:
        rows = await _wait_for_rows(
            stable_broker_activity_wal_path(run_dir), want=1
        )
        # Only our fill landed; the other namespace's event was dropped
        # before authoring.
        assert len(rows) == 1
        assert rows[0].exec_id == "our-exec"
    finally:
        await publisher.stop()


async def test_slow_subscriber_receives_sentinel_when_dropped(
    tmp_path: Path,
) -> None:
    """A full subscriber queue must receive a ``None`` sentinel before
    being discarded — otherwise the SSE handler is left blocked in
    ``queue.get()`` forever and silently misses all future rows. The
    sentinel lets the handler emit ``event: end`` and close the
    connection so the client knows to reconnect.
    """
    publisher, _, _ = _build_publisher(tmp_path, [])
    queue = publisher.subscribe()
    # Fill the queue to capacity with dummy rows.
    capacity = queue.maxsize
    for i in range(capacity):
        queue.put_nowait(
            BrokerActivityRow(
                seq=i + 1,
                ts_ms=1_700_000_000_000 + i,
                exec_id=f"prefill-{i}",
                symbol="SPY",
                side="BUY",
                quantity=1.0,
                order_type="MKT",
                verdict=Verdict.EXPECTED,
                template_key="normal_fill",
                template_version=1,
                headline="prefill",
                narrative="prefill",
            )
        )
    assert queue.full()
    # Author one more row — _broadcast must dedupe to make room for the
    # sentinel rather than silently dropping.
    overflow_row = BrokerActivityRow(
        seq=capacity + 1,
        ts_ms=1_700_000_000_999,
        exec_id="overflow",
        symbol="SPY",
        side="BUY",
        quantity=1.0,
        order_type="MKT",
        verdict=Verdict.EXPECTED,
        template_key="normal_fill",
        template_version=1,
        headline="overflow",
        narrative="overflow",
    )
    publisher._broadcast(overflow_row)
    # The queue should now hold the remaining stale rows AND a None
    # sentinel at the tail.
    drained: list[BrokerActivityRow | None] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert drained[-1] is None, f"expected None sentinel at tail, got {drained[-1]!r}"
    # The subscriber should have been discarded from the registry.
    assert queue not in publisher._subscribers
# ── Slice 3 — reconnect-recovery sweep ──────────────────────────────


def _recovery_factory(events: list[IbkrOrderEvent]):
    """Build a ``recovery_source_factory`` test double.

    The factory returns a coroutine that resolves to ``events`` — the
    same shape ``executions_for_reconnect_recovery`` produces in
    production. Each invocation returns a fresh copy so the publisher's
    iteration order is observable.
    """

    async def _fetch() -> list[IbkrOrderEvent]:
        return list(events)

    return _fetch


def _build_publisher_with_recovery(
    tmp_path: Path,
    *,
    live_events: list[IbkrOrderEvent] | None = None,
    recovery_events: list[IbkrOrderEvent] | None = None,
) -> tuple[BrokerActivityPublisher, Path, Path]:
    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    _seed_envelope(artifacts)
    publisher = BrokerActivityPublisher(
        strategy_instance_id=SID,
        bot_order_namespace=NS,
        run_dir=run_dir,
        artifacts_root=artifacts,
        timing_policy=ReconciliationTimingPolicy(),
        event_source_factory=_make_event_source(live_events or []),
        recovery_source_factory=_recovery_factory(recovery_events or []),
    )
    return publisher, run_dir, artifacts


async def test_reconnect_sweep_authors_missed_execs_as_caveats(
    tmp_path: Path,
) -> None:
    """After a reconnect, the sweep authors any execution not yet in the
    WAL with the ``reconnect_recovery`` template (verdict
    ``expected_with_caveat``)."""
    recovered = _fill_event(exec_id="recovered-1")
    publisher, run_dir, _ = _build_publisher_with_recovery(
        tmp_path, recovery_events=[recovered]
    )
    # No live events — purely test the sweep path.
    count = await publisher.sweep_reconnect_recovery()
    assert count == 1
    wal = BrokerActivityWal(stable_broker_activity_wal_path(run_dir))
    rows = wal.read_all()
    assert len(rows) == 1
    assert rows[0].exec_id == "recovered-1"
    assert rows[0].verdict == Verdict.EXPECTED_WITH_CAVEAT
    assert rows[0].template_key == "reconnect_recovery"
    # Truthfulness: the four template-required keys must render — the
    # narrative carries the symbol + price + order_type.
    assert "SPY" in rows[0].headline
    assert "450" in rows[0].headline


async def test_reconnect_sweep_dedupes_already_seen_execs(tmp_path: Path) -> None:
    """An exec_id already in the WAL (from a row authored before the
    drop) is not re-authored — the sweep's dedupe set is shared with
    the live event loop."""
    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    _seed_envelope(artifacts)
    wal = BrokerActivityWal(stable_broker_activity_wal_path(run_dir))
    # Pre-populate the WAL — this row predates the disconnect.
    pre = BrokerActivityRow(
        seq=1,
        ts_ms=1_700_000_000_000 - 1,
        exec_id="seen-1",
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
        event_source_factory=_make_event_source([]),
        recovery_source_factory=_recovery_factory(
            [
                _fill_event(exec_id="seen-1"),  # IBKR redelivered this on resume
                _fill_event(exec_id="missed-2"),  # genuinely missed during the drop
            ]
        ),
    )

    count = await publisher.sweep_reconnect_recovery()
    assert count == 1  # only the genuinely-missed exec was authored
    rows = wal.read_all()
    assert len(rows) == 2
    assert rows[0].exec_id == "seen-1"
    assert rows[0].headline == "pre-existing"  # untouched
    assert rows[1].exec_id == "missed-2"
    assert rows[1].template_key == "reconnect_recovery"


async def test_reconnect_sweep_skips_foreign_namespace(tmp_path: Path) -> None:
    """Executions with no namespace match (foreign account activity) are
    NOT authored by the recovery sweep — they are noise from other
    instances on the shared paper account. The live event loop still
    picks them up as foreign rows after reconnect."""
    foreign = _fill_event(
        order_ref="learn-ai/some-other-instance/v1:other-intent",
        exec_id="foreign-recover-1",
    )
    publisher, run_dir, _ = _build_publisher_with_recovery(
        tmp_path, recovery_events=[foreign]
    )
    count = await publisher.sweep_reconnect_recovery()
    assert count == 0
    wal = BrokerActivityWal(stable_broker_activity_wal_path(run_dir))
    assert wal.read_all() == []


async def test_excessive_lag_during_reconnect_window_renders_as_caveat_not_unexpected(
    tmp_path: Path,
) -> None:
    """An exec whose intent-to-exec lag exceeds ``excessive_lag_ms``
    would normally classify as UNEXPECTED with a TIMING_CAVEAT reason
    — but during a reconnect sweep, the reconciler's existing branch
    promotes the row to EXPECTED_WITH_CAVEAT under the reconnect
    template instead."""
    # Seed an envelope where the engine recorded an old intent_created_ms
    # so the lag from intent to exec is very large.
    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    envelope = LiveStateEnvelope(
        strategy_instance_id=SID,
        run_id="run-recovery-lag",
        bot_order_namespace=NS,
        ib_client_id=42,
        last_processed_bar_ms=1,
        last_artifact_flush_ms=1,
        submitted_orders={
            INTENT_ID: {
                "perm_id": 999,
                "order_id": 42,
                "status": "Submitted",
                "symbol": "SPY",
                "intent_created_ms": 1_700_000_000_000 - 60_000,  # 1 min before exec
                "dispatched_ms": 1_700_000_000_000 - 59_000,
                "acked_ms": 1_700_000_000_000 - 58_000,
                "requested_qty": 100.0,
            }
        },
    )
    repo = LiveStateSidecarRepo(stable_live_state_path(artifacts, SID))
    repo._path.parent.mkdir(parents=True, exist_ok=True)
    repo.write(envelope)

    publisher = BrokerActivityPublisher(
        strategy_instance_id=SID,
        bot_order_namespace=NS,
        run_dir=run_dir,
        artifacts_root=artifacts,
        timing_policy=ReconciliationTimingPolicy(
            caveat_lag_ms=2_000,
            excessive_lag_ms=10_000,
        ),
        event_source_factory=_make_event_source([]),
        recovery_source_factory=_recovery_factory(
            [_fill_event(exec_id="lag-recover-1")]
        ),
    )

    count = await publisher.sweep_reconnect_recovery()
    assert count == 1
    wal = BrokerActivityWal(stable_broker_activity_wal_path(run_dir))
    rows = wal.read_all()
    assert len(rows) == 1
    # The reconnect_recovery reason superseded TIMING_CAVEAT.
    assert rows[0].template_key == "reconnect_recovery"
    assert rows[0].verdict == Verdict.EXPECTED_WITH_CAVEAT


async def test_reconnect_sweep_sets_active_flag_during_sweep(
    tmp_path: Path,
) -> None:
    """While the sweep is in flight, ``is_reconnect_recovery_active`` is
    True so the registry surfaces it; the flag clears on completion (and
    on a factory raise, via the finally clause)."""
    seen_during_sweep: list[bool] = []

    async def _observing_factory() -> list[IbkrOrderEvent]:
        # Observe the flag inside the sweep — confirms it's set before
        # rows are authored, not just after.
        seen_during_sweep.append(publisher.is_reconnect_recovery_active)
        return [_fill_event(exec_id="observe-1")]

    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    _seed_envelope(artifacts)
    publisher = BrokerActivityPublisher(
        strategy_instance_id=SID,
        bot_order_namespace=NS,
        run_dir=run_dir,
        artifacts_root=artifacts,
        timing_policy=ReconciliationTimingPolicy(),
        event_source_factory=_make_event_source([]),
        recovery_source_factory=_observing_factory,
    )
    assert publisher.is_reconnect_recovery_active is False
    await publisher.sweep_reconnect_recovery()
    assert seen_during_sweep == [True]
    assert publisher.is_reconnect_recovery_active is False


async def test_reconnect_sweep_clears_flag_on_factory_raise(
    tmp_path: Path,
) -> None:
    """A crashing factory must lift the submission halt — otherwise a
    single bad sweep would pin the halt forever."""

    async def _bad_factory() -> list[IbkrOrderEvent]:
        raise RuntimeError("simulated reqExecutions failure")

    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    _seed_envelope(artifacts)
    publisher = BrokerActivityPublisher(
        strategy_instance_id=SID,
        bot_order_namespace=NS,
        run_dir=run_dir,
        artifacts_root=artifacts,
        timing_policy=ReconciliationTimingPolicy(),
        event_source_factory=_make_event_source([]),
        recovery_source_factory=_bad_factory,
    )
    with pytest.raises(RuntimeError, match="simulated reqExecutions failure"):
        await publisher.sweep_reconnect_recovery()
    assert publisher.is_reconnect_recovery_active is False


async def test_reconnect_sweep_no_op_without_factory(tmp_path: Path) -> None:
    """A publisher built without a ``recovery_source_factory`` (legacy
    callers / tests that don't exercise the sweep) returns 0 from
    ``sweep_reconnect_recovery`` without touching the WAL."""
    publisher, run_dir, _ = _build_publisher(tmp_path, [])
    assert publisher._recovery_source_factory is None  # sanity
    count = await publisher.sweep_reconnect_recovery()
    assert count == 0
    wal = BrokerActivityWal(stable_broker_activity_wal_path(run_dir))
    assert wal.read_all() == []


async def test_registry_any_recovery_active_reflects_publisher_state(
    tmp_path: Path,
) -> None:
    """The registry's ``any_recovery_active`` ORs the flag across every
    registered publisher — the gate ``place_paper_order`` reads."""
    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    _seed_envelope(artifacts)
    registry = BrokerActivityPublisherRegistry()
    assert registry.any_recovery_active() is False

    # Build a publisher whose factory blocks on an event so we can
    # observe the flag mid-sweep.
    block = asyncio.Event()
    release_observed = asyncio.Event()

    async def _slow_factory() -> list[IbkrOrderEvent]:
        release_observed.set()
        await block.wait()
        return []

    publisher = BrokerActivityPublisher(
        strategy_instance_id=SID,
        bot_order_namespace=NS,
        run_dir=run_dir,
        artifacts_root=artifacts,
        timing_policy=ReconciliationTimingPolicy(),
        event_source_factory=_make_event_source([]),
        recovery_source_factory=_slow_factory,
    )
    await registry.register(publisher, strategy_instance_id=SID)
    try:
        sweep_task = asyncio.create_task(publisher.sweep_reconnect_recovery())
        await asyncio.wait_for(release_observed.wait(), timeout=0.5)
        assert registry.any_recovery_active() is True
        block.set()
        await asyncio.wait_for(sweep_task, timeout=0.5)
        assert registry.any_recovery_active() is False
    finally:
        await registry.unregister(SID)


async def test_registry_sweep_all_isolates_per_publisher_failures(
    tmp_path: Path,
) -> None:
    """A raising sweep on one publisher must not abort the chain for
    others — the monitor's recovery flow shouldn't be hostage to one
    instance's bad broker state."""
    registry = BrokerActivityPublisherRegistry()
    artifacts_a = tmp_path / "a" / "artifacts"
    artifacts_b = tmp_path / "b" / "artifacts"
    run_dir_a = tmp_path / "a" / "run-dir"
    run_dir_b = tmp_path / "b" / "run-dir"

    sid_a = "sid-multi-a"
    ns_a = f"learn-ai/{sid_a}/v1"
    env_a = LiveStateEnvelope(
        strategy_instance_id=sid_a,
        run_id="run-a",
        bot_order_namespace=ns_a,
        ib_client_id=1,
        last_processed_bar_ms=1,
        last_artifact_flush_ms=1,
    )
    LiveStateSidecarRepo(stable_live_state_path(artifacts_a, sid_a))._path.parent.mkdir(
        parents=True, exist_ok=True
    )
    LiveStateSidecarRepo(stable_live_state_path(artifacts_a, sid_a)).write(env_a)

    sid_b = "sid-multi-b"
    ns_b = f"learn-ai/{sid_b}/v1"
    env_b = LiveStateEnvelope(
        strategy_instance_id=sid_b,
        run_id="run-b",
        bot_order_namespace=ns_b,
        ib_client_id=2,
        last_processed_bar_ms=1,
        last_artifact_flush_ms=1,
    )
    LiveStateSidecarRepo(stable_live_state_path(artifacts_b, sid_b))._path.parent.mkdir(
        parents=True, exist_ok=True
    )
    LiveStateSidecarRepo(stable_live_state_path(artifacts_b, sid_b)).write(env_b)

    async def _crashing() -> list[IbkrOrderEvent]:
        raise RuntimeError("publisher A's broker is upset")

    async def _good() -> list[IbkrOrderEvent]:
        return []

    pub_a = BrokerActivityPublisher(
        strategy_instance_id=sid_a,
        bot_order_namespace=ns_a,
        run_dir=run_dir_a,
        artifacts_root=artifacts_a,
        timing_policy=ReconciliationTimingPolicy(),
        event_source_factory=_make_event_source([]),
        recovery_source_factory=_crashing,
    )
    pub_b = BrokerActivityPublisher(
        strategy_instance_id=sid_b,
        bot_order_namespace=ns_b,
        run_dir=run_dir_b,
        artifacts_root=artifacts_b,
        timing_policy=ReconciliationTimingPolicy(),
        event_source_factory=_make_event_source([]),
        recovery_source_factory=_good,
    )
    await registry.register(pub_a, strategy_instance_id=sid_a)
    await registry.register(pub_b, strategy_instance_id=sid_b)
    try:
        results = await registry.sweep_all_for_recovery()
        # A's exception was logged-and-isolated; B's sweep still ran.
        assert results == {sid_a: 0, sid_b: 0}
    finally:
        await registry.stop_all()


# ── End slice 3 sweep tests ─────────────────────────────────────────


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
