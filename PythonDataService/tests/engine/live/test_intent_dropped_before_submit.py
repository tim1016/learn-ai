"""PR 3 — INTENT_DROPPED_BEFORE_SUBMIT WAL event tests.

Covers:
- IntentEvent model validator rejects mismatched drop_reason / event_type combos.
- WAL append round-trips drop_reason through model_dump_json / model_validate_json.
- Fold-side legacy classification (legacy_sizing_only_dropped) when
  SIZING_RESOLVED-only event is before the cutoff.
- Post-cutoff SIZING_RESOLVED-only is not classified (publisher handles it).
- Engine bar-loop emits INTENT_DROPPED_BEFORE_SUBMIT at the four gates:
    operator_paused, control_plane_lease_lost, max_orders_per_day,
    broker_safety_halt.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.engine.live.intent_ledger import LedgerProjection, fold
from app.engine.live.intent_wal import IntentWal
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_order_ref,
    mint_intent_id,
)

NS = build_bot_order_namespace("testbot")


# ─── helpers ────────────────────────────────────────────────────────────────


def _sizing_resolved_event(seq: int, ts_ms: int) -> IntentEvent:
    iid = mint_intent_id()
    return IntentEvent(
        seq=seq,
        event_type=IntentEventType.SIZING_RESOLVED,
        intent_id=iid,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, iid),
        policy_kind="percent",
        policy_value="0.5",
        intended_qty=10,
        reference_price="500.00",
        sizing_provenance_at_resolve_time="test",
        sized_via="policy_set_holdings",
        ts_ms=ts_ms,
    )


def _bar(minute: int) -> object:
    from app.engine.data.trade_bar import TradeBar

    start = datetime(2026, 6, 23, 14, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=100,
    )


async def _iter_bars(bars):
    for bar in bars:
        yield bar


# ─── model validator tests ───────────────────────────────────────────────────


def test_intent_dropped_before_submit_requires_drop_reason() -> None:
    """INTENT_DROPPED_BEFORE_SUBMIT with drop_reason=None must be rejected."""
    from pydantic import ValidationError

    iid = mint_intent_id()
    with pytest.raises(ValidationError, match="drop_reason"):
        IntentEvent(
            seq=1,
            event_type=IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
            intent_id=iid,
            bot_order_namespace=NS,
            order_ref=build_order_ref(NS, iid),
            drop_reason=None,
        )


def test_non_drop_event_rejects_drop_reason() -> None:
    """PENDING_INTENT carrying a drop_reason must be rejected."""
    from pydantic import ValidationError

    iid = mint_intent_id()
    with pytest.raises(ValidationError, match="drop_reason"):
        IntentEvent(
            seq=1,
            event_type=IntentEventType.PENDING_INTENT,
            intent_id=iid,
            bot_order_namespace=NS,
            order_ref=build_order_ref(NS, iid),
            drop_reason="operator_paused",
        )


def test_intent_dropped_accepts_valid_drop_reason() -> None:
    """INTENT_DROPPED_BEFORE_SUBMIT with a valid drop_reason round-trips."""
    iid = mint_intent_id()
    event = IntentEvent(
        seq=1,
        event_type=IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
        intent_id=iid,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, iid),
        drop_reason="operator_paused",
    )
    assert event.drop_reason == "operator_paused"
    # Round-trip through JSON (WAL serialization path)
    json_str = event.model_dump_json()
    restored = IntentEvent.model_validate_json(json_str)
    assert restored.drop_reason == "operator_paused"
    assert restored.event_type is IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT


# ─── WAL append tests ────────────────────────────────────────────────────────


def test_wal_append_drop_event_round_trips(tmp_path: Path) -> None:
    """WAL.append with drop_reason writes and reads back correctly."""
    wal = IntentWal(tmp_path / "intent_events.jsonl")
    iid = mint_intent_id()
    wal.append(
        event_type=IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
        intent_id=iid,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, iid),
        drop_reason="max_orders_per_day",
        ts_ms=1_700_000_000_000,
    )
    events = wal.read_tail()
    assert len(events) == 1
    assert events[0].event_type is IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT
    assert events[0].drop_reason == "max_orders_per_day"


# ─── fold-side legacy classification tests ──────────────────────────────────


def test_fold_classifies_legacy_sizing_only_dropped() -> None:
    """SIZING_RESOLVED-only event before cutoff gets legacy_sizing_only_dropped."""
    cutoff_ms = 1_750_000_000_000
    event = _sizing_resolved_event(seq=1, ts_ms=cutoff_ms - 1000)
    view = fold(LedgerProjection(), [event], legacy_sizing_only_cutoff_ms=cutoff_ms)
    iid = event.intent_id
    sentinel = view.submitted_orders[iid]
    assert sentinel.classification == "legacy_sizing_only_dropped"


def test_fold_does_not_classify_post_cutoff() -> None:
    """SIZING_RESOLVED-only at or after cutoff must NOT be classified (publisher handles)."""
    cutoff_ms = 1_750_000_000_000
    event = _sizing_resolved_event(seq=1, ts_ms=cutoff_ms)
    view = fold(LedgerProjection(), [event], legacy_sizing_only_cutoff_ms=cutoff_ms)
    iid = event.intent_id
    sentinel = view.submitted_orders[iid]
    assert sentinel.classification is None


def test_fold_legacy_classification_absent_when_no_cutoff() -> None:
    """Existing callers that don't pass cutoff get None classification (default)."""
    event = _sizing_resolved_event(seq=1, ts_ms=1_000_000_000)
    view = fold(LedgerProjection(), [event])
    iid = event.intent_id
    sentinel = view.submitted_orders[iid]
    assert sentinel.classification is None


def test_fold_does_not_classify_when_pending_intent_follows() -> None:
    """SIZING_RESOLVED followed by PENDING_INTENT is a normal lifecycle — not legacy."""
    cutoff_ms = 1_750_000_000_000
    iid = mint_intent_id()
    events = [
        IntentEvent(
            seq=1,
            event_type=IntentEventType.SIZING_RESOLVED,
            intent_id=iid,
            bot_order_namespace=NS,
            order_ref=build_order_ref(NS, iid),
            policy_kind="percent",
            policy_value="0.5",
            intended_qty=10,
            reference_price="500.00",
            sizing_provenance_at_resolve_time="test",
            sized_via="policy_set_holdings",
            ts_ms=cutoff_ms - 1000,
        ),
        IntentEvent(
            seq=2,
            event_type=IntentEventType.PENDING_INTENT,
            intent_id=iid,
            bot_order_namespace=NS,
            order_ref=build_order_ref(NS, iid),
        ),
    ]
    view = fold(LedgerProjection(), events, legacy_sizing_only_cutoff_ms=cutoff_ms)
    # PENDING_INTENT overwrites status — classification is not legacy since a
    # lifecycle event followed.
    assert view.submitted_orders[iid].classification is None


# ─── engine bar-loop gate tests ─────────────────────────────────────────────


class _OrderingStrategy:
    """Strategy that queues one buy order on every minute bar.

    Deliberately emits on every on_minute_bar call (not just on consolidated
    bars) so the first bar always populates pending_orders before the submit
    gates fire — this exercises the drop paths without waiting 15 bars for
    a consolidator to fire.
    """

    def __init__(self) -> None:
        self.ctx = None
        # Mirrors Strategy.__init__ so the engine can call on_minute_bar.
        self.start_date = None
        self.end_date = None
        self.initial_cash = Decimal("100000")
        self.last_decision_snapshot = None

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")

    def on_minute_bar(self, bar: object) -> None:
        # Emit a set_holdings on every bar so pending_orders is always
        # populated when the submit gates fire.
        assert self.ctx is not None
        self.ctx.portfolio.set_holdings("SPY", Decimal("0.5"), bar.end_time)

    def on_end_of_algorithm(self) -> None:
        pass


def _read_drop_events(wal_path: Path) -> list[IntentEvent]:
    """Return all INTENT_DROPPED_BEFORE_SUBMIT events from a WAL file."""
    wal = IntentWal(wal_path)
    return [
        e
        for e in wal.read_tail()
        if e.event_type is IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT
    ]


@pytest.mark.asyncio
async def test_paused_drop_writes_wal_event(tmp_path: Path) -> None:
    """operator_paused gate emits INTENT_DROPPED_BEFORE_SUBMIT for pending orders."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    wal_path = tmp_path / "intent_events.jsonl"
    strategy = _OrderingStrategy()

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        intent_wal_path=wal_path,
        strategy_instance_id="paused-test",
        start_paused=True,
    )

    bars = [_bar(i) for i in range(30, 65)]
    await asyncio.wait_for(engine.run(strategy, _iter_bars(bars)), timeout=10.0)

    drops = _read_drop_events(wal_path)
    assert len(drops) >= 1, "Expected at least one drop event while paused"
    assert all(e.drop_reason == "operator_paused" for e in drops)


@pytest.mark.asyncio
async def test_lease_lost_drop_writes_wal_event(tmp_path: Path) -> None:
    """control_plane_lease_lost (submissions_blocked) gate emits drop events."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    wal_path = tmp_path / "intent_events.jsonl"
    strategy = _OrderingStrategy()

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        intent_wal_path=wal_path,
        strategy_instance_id="lease-test",
    )
    # Force submissions_blocked state before run.
    engine._submissions_blocked = True

    bars = [_bar(i) for i in range(30, 65)]
    await asyncio.wait_for(engine.run(strategy, _iter_bars(bars)), timeout=10.0)

    drops = _read_drop_events(wal_path)
    assert len(drops) >= 1
    assert all(e.drop_reason == "control_plane_lease_lost" for e in drops)


@pytest.mark.asyncio
async def test_max_orders_drop_writes_wal_event(tmp_path: Path) -> None:
    """max_orders_per_day gate emits INTENT_DROPPED_BEFORE_SUBMIT then raises."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine, MaxOrdersPerDayExceeded
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    wal_path = tmp_path / "intent_events.jsonl"
    strategy = _OrderingStrategy()

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        intent_wal_path=wal_path,
        strategy_instance_id="maxorders-test",
        max_orders_per_day=0,  # cap at 0: the very first pending batch triggers the gate
    )

    bars = [_bar(i) for i in range(30, 65)]
    with pytest.raises(MaxOrdersPerDayExceeded):
        await asyncio.wait_for(engine.run(strategy, _iter_bars(bars)), timeout=10.0)

    drops = _read_drop_events(wal_path)
    assert len(drops) >= 1
    assert all(e.drop_reason == "max_orders_per_day" for e in drops)


@pytest.mark.asyncio
async def test_broker_safety_halt_drop_writes_wal_event(tmp_path: Path) -> None:
    """broker_safety_halt gate in submit_pending_orders emits drop events."""
    from app.engine.live.config import LiveConfig
    from app.engine.live.live_engine import LiveEngine
    from tests.engine.live.fixtures.fake_broker import FakeBroker

    wal_path = tmp_path / "intent_events.jsonl"
    strategy = _OrderingStrategy()

    # Verdict provider that immediately returns unsafe.
    def _unsafe_verdict() -> str:
        return "unsafe"

    broker = FakeBroker()
    # Wire verdict_provider on the FakeBroker via monkey-patch — FakeBroker
    # doesn't set requires_durable_submit so portfolio will use the non-durable
    # path. We attach it to the portfolio after construction via the engine's
    # run() setup, which is not directly accessible. Instead we rely on the
    # LivePortfolio.verdict_provider being set at portfolio construction by
    # the engine's run() method.
    # The live_engine passes verdict_provider to both the portfolio and the
    # engine-level check. We'll pass a verdict_provider to the LivePortfolio
    # through the engine's verdict_provider kwarg.
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        intent_wal_path=wal_path,
        strategy_instance_id="verdict-test",
        verdict_provider=_unsafe_verdict,
    )

    bars = [_bar(i) for i in range(30, 65)]
    # The engine-level _check_verdict_transition_halt runs before the submit,
    # clears pending_orders, and raises. The drop events are emitted there.
    with pytest.raises(Exception):
        await asyncio.wait_for(engine.run(strategy, _iter_bars(bars)), timeout=10.0)

    drops = _read_drop_events(wal_path)
    # The verdict transition halt clears pending_orders and emits drops.
    assert len(drops) >= 1
    assert all(e.drop_reason in ("broker_safety_halt",) for e in drops)
