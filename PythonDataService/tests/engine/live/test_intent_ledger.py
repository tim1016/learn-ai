"""Module B (intent_ledger fold) unit tests — ADR-0008 §2 / PRD #446 test plan B.

A representative event stream folds to the expected view; an unflushed tail
folds over a stale snapshot applying each event exactly once.
"""

from __future__ import annotations

from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.engine.live.intent_ledger import (
    LedgerProjection,
    SubmittedOrderView,
    fold,
    projection_from_envelope,
)
from app.engine.live.live_state_sidecar import LiveStateEnvelope
from app.engine.live.order_identity import build_bot_order_namespace, build_order_ref

NS = build_bot_order_namespace("foo")
IID = "AAAAAAAAAAAAAAAAAAAAAA"  # 22-char placeholder intent_id


def _event(seq: int, event_type: IntentEventType, **kw: object) -> IntentEvent:
    intent_id = str(kw.pop("intent_id", IID))
    return IntentEvent(
        seq=seq,
        event_type=event_type,
        intent_id=intent_id,
        bot_order_namespace=NS,
        order_ref=build_order_ref(NS, intent_id),
        **kw,  # type: ignore[arg-type]
    )


def test_representative_stream_folds_to_expected_view() -> None:
    events = [
        _event(1, IntentEventType.PENDING_INTENT),
        _event(2, IntentEventType.SUBMITTED, order_id=11, perm_id=900),
        _event(3, IntentEventType.SUBMITTED, perm_id=900, exec_id="e1"),
        _event(4, IntentEventType.SUBMITTED, perm_id=900, exec_id="e2"),
    ]
    view = fold(LedgerProjection(), events)

    order = view.submitted_orders[IID]
    assert order.status is IntentEventType.SUBMITTED
    assert order.order_id == 11
    assert order.perm_id == 900
    assert order.exec_ids == ("e1", "e2")
    assert view.known_perm_ids == frozenset({900})
    assert view.known_exec_ids == frozenset({"e1", "e2"})
    assert view.last_seq == 4
    assert view.unresolved_intent_ids == frozenset()


def test_lone_pending_then_uncertain_are_unresolved() -> None:
    pending = fold(LedgerProjection(), [_event(1, IntentEventType.PENDING_INTENT)])
    assert pending.unresolved_intent_ids == frozenset({IID})

    uncertain = fold(
        LedgerProjection(), [_event(1, IntentEventType.ACK_FAILED_UNCERTAIN)]
    )
    assert uncertain.unresolved_intent_ids == frozenset({IID})

    resolved = fold(
        LedgerProjection(),
        [
            _event(1, IntentEventType.PENDING_INTENT),
            _event(2, IntentEventType.SUBMITTED, perm_id=1),
        ],
    )
    assert resolved.unresolved_intent_ids == frozenset()


def test_unflushed_tail_over_stale_snapshot_applies_each_event_once() -> None:
    # Snapshot already reflects seq 1-2 (PENDING then SUBMITTED w/ perm 900).
    snapshot = LedgerProjection(
        submitted_orders={
            IID: SubmittedOrderView(
                intent_id=IID,
                bot_order_namespace=NS,
                order_ref=build_order_ref(NS, IID),
                status=IntentEventType.SUBMITTED,
                order_id=11,
                perm_id=900,
                exec_ids=("e1",),
            )
        },
        known_perm_ids=frozenset({900}),
        known_exec_ids=frozenset({"e1"}),
        last_intent_wal_seq=2,
    )
    # The full tail re-includes seq 1-2 plus a new seq 3 fill.
    tail = [
        _event(1, IntentEventType.PENDING_INTENT),
        _event(2, IntentEventType.SUBMITTED, order_id=11, perm_id=900, exec_id="e1"),
        _event(3, IntentEventType.SUBMITTED, perm_id=900, exec_id="e2"),
    ]
    view = fold(snapshot, tail)

    order = view.submitted_orders[IID]
    # seq 1-2 skipped (already folded); only seq 3 applied — e1 not duplicated.
    assert order.exec_ids == ("e1", "e2")
    assert view.known_exec_ids == frozenset({"e1", "e2"})
    assert view.last_seq == 3


def test_fold_is_idempotent_when_reapplied() -> None:
    events = [
        _event(1, IntentEventType.PENDING_INTENT),
        _event(2, IntentEventType.SUBMITTED, perm_id=5, exec_id="x"),
    ]
    once = fold(LedgerProjection(), events)
    # Re-folding the same events over the produced view's cursor is a no-op.
    twice = fold(
        LedgerProjection(
            submitted_orders=once.submitted_orders,
            known_perm_ids=once.known_perm_ids,
            known_exec_ids=once.known_exec_ids,
            last_intent_wal_seq=once.last_seq,
        ),
        events,
    )
    assert twice.submitted_orders[IID].exec_ids == ("x",)
    assert twice.last_seq == 2


def test_projection_from_envelope_round_trips() -> None:
    envelope = LiveStateEnvelope(
        strategy_instance_id="foo",
        run_id="r1",
        bot_order_namespace=NS,
        ib_client_id=7,
        submitted_orders={
            IID: {
                "bot_order_namespace": NS,
                "order_ref": build_order_ref(NS, IID),
                "status": "SUBMITTED",
                "order_id": 11,
                "perm_id": 900,
                "exec_ids": ["e1"],
            }
        },
        known_perm_ids=[900],
        known_exec_ids=["e1"],
        last_processed_bar_ms=1,
        last_artifact_flush_ms=1,
    )
    proj = projection_from_envelope(envelope)
    assert proj.last_intent_wal_seq == 0
    assert proj.known_perm_ids == frozenset({900})
    order = proj.submitted_orders[IID]
    assert order.status is IntentEventType.SUBMITTED
    assert order.perm_id == 900
    assert order.exec_ids == ("e1",)
