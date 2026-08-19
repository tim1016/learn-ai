"""PR 3 — INTENT_DROPPED_BEFORE_SUBMIT WAL event tests.

Covers:
- IntentEvent model validator rejects mismatched drop_reason / event_type combos.
- WAL append round-trips drop_reason through model_dump_json / model_validate_json.
- Fold-side legacy classification (legacy_sizing_only_dropped) when
  SIZING_RESOLVED-only event is before the cutoff.
- Post-cutoff SIZING_RESOLVED-only is not classified (publisher handles it).

The former engine emission tests retired with LiveEngine in #1583; retained
tests cover durable historical-WAL parsing and fold semantics.
"""

from __future__ import annotations

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
from tests._helpers.legacy_ibkr_artifacts import (
    write_historical_intent_wal,
)

NS = build_bot_order_namespace("testbot")


# ─── helpers ────────────────────────────────────────────────────────────────


def _sizing_resolved_event(
    seq: int,
    ts_ms: int,
    appended_at_ms: int | None = None,
) -> IntentEvent:
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
        appended_at_ms=appended_at_ms,
    )


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


def test_historical_drop_event_round_trips(tmp_path: Path) -> None:
    """A retained drop event remains readable with its typed reason."""
    path = tmp_path / "intent_events.jsonl"
    iid = mint_intent_id()
    write_historical_intent_wal(
        path,
        [
            IntentEvent(
                seq=1,
                event_type=IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
                intent_id=iid,
                bot_order_namespace=NS,
                order_ref=build_order_ref(NS, iid),
                drop_reason="max_orders_per_day",
                ts_ms=1_700_000_000_000,
            )
        ],
    )
    events = IntentWal(path).read_tail()
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
    """SIZING_RESOLVED-only with appended_at_ms >= cutoff must NOT be classified."""
    cutoff_ms = 1_750_000_000_000
    # ts_ms is bar time — can be anything. What matters is appended_at_ms >= cutoff.
    event = _sizing_resolved_event(
        seq=1,
        ts_ms=cutoff_ms - 1_000,  # bar time before cutoff is irrelevant now
        appended_at_ms=cutoff_ms,  # appended at exactly the cutoff → NOT legacy
    )
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


# ─── reviewer finding tests ──────────────────────────────────────────────────


def test_fold_uses_appended_at_for_legacy_classification() -> None:
    """Finding 2: bar time before cutoff but appended_at >= cutoff → NOT legacy.

    ``ts_ms`` is the strategy bar close timestamp (set_holdings(..., time)).
    In delayed live feeds a current-run bar close can precede engine start.
    The fold must use ``appended_at_ms`` (process wall-clock at WAL write time)
    for the cutoff comparison, not ``ts_ms``.
    """
    cutoff_ms = 1_700_000_000_000
    event = _sizing_resolved_event(
        seq=1,
        ts_ms=cutoff_ms - 1_000,  # bar time before cutoff — irrelevant for classification
        appended_at_ms=cutoff_ms + 5_000,  # appended after cutoff → current-run intent
    )
    view = fold(LedgerProjection(), [event], legacy_sizing_only_cutoff_ms=cutoff_ms)
    sentinel = view.submitted_orders[event.intent_id]
    assert sentinel.classification is None, (
        "Current-run intent (appended_at_ms > cutoff) must NOT be classified as legacy, "
        "even when bar time is before the cutoff."
    )


def test_fold_classifies_event_with_no_appended_at_as_pre_cutoff() -> None:
    """Finding 2 backward-compat: events on disk without appended_at_ms are pre-cutoff.

    WAL entries written before this field was introduced parse with
    ``appended_at_ms=None``. The fold treats None as pre-cutoff (safe default:
    the event belongs to a prior engine process and will never receive a
    terminal event in this session).
    """
    cutoff_ms = 1_700_000_000_000
    event = _sizing_resolved_event(
        seq=1,
        ts_ms=cutoff_ms + 99_000,  # bar time well AFTER cutoff — should not matter
        appended_at_ms=None,  # simulates an on-disk event before the field existed
    )
    view = fold(LedgerProjection(), [event], legacy_sizing_only_cutoff_ms=cutoff_ms)
    sentinel = view.submitted_orders[event.intent_id]
    assert sentinel.classification == "legacy_sizing_only_dropped", (
        "Event with appended_at_ms=None must be treated as pre-cutoff for backward-compat."
    )
