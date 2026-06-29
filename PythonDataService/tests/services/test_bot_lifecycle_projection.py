from __future__ import annotations

from app.engine.live.intent_events import DropReason, IntentEvent, IntentEventType
from app.schemas.live_runs import BotLifecycleEvent
from app.services.bot_lifecycle_projection import (
    SOURCE_RANKS,
    account_event_to_lifecycle_event,
    normalize_account_event,
    project_intent_events,
    sort_lifecycle_events,
)

_NAMESPACE = "learn-ai/bot-a/v1"


def _intent(
    seq: int,
    event_type: IntentEventType,
    *,
    appended_at_ms: int | None = 1_700_000_000_000,
    drop_reason: DropReason | None = None,
) -> IntentEvent:
    intent_id = f"intent-{seq}"
    return IntentEvent(
        seq=seq,
        event_type=event_type,
        intent_id=intent_id,
        bot_order_namespace=_NAMESPACE,
        order_ref=f"{_NAMESPACE}:{intent_id}",
        drop_reason=drop_reason,
        appended_at_ms=appended_at_ms,
    )


def _event(
    source: str,
    source_local_seq: int,
    *,
    ts_ms: int | None = 1_700_000_000_000,
) -> BotLifecycleEvent:
    return BotLifecycleEvent(
        event_id=f"{source}:{source_local_seq}",
        event_type=source,
        category="evidence",
        ts_ms=ts_ms,
        ts_ms_resolved=ts_ms is not None,
        source=source,
        source_rank=SOURCE_RANKS[source],
        source_local_seq=source_local_seq,
        summary=source,
    )


def test_sort_lifecycle_events_uses_ts_source_rank_then_source_local_seq() -> None:
    events = [
        _event("fill", 1),
        _event("intent_pending", 5),
        _event("decision", 9),
        _event("intent_pending", 2),
    ]

    ordered = sort_lifecycle_events(events)

    assert [event.event_id for event in ordered] == [
        "decision:9",
        "intent_pending:2",
        "intent_pending:5",
        "fill:1",
    ]


def test_normalize_account_event_prefers_clear_timestamp_for_legacy_freeze_clear() -> None:
    projected = normalize_account_event(
        {
            "event_type": "account_freeze_cleared",
            "account_id": "DU123",
            "created_at_ms": 2_000,
            "recorded_at_ms": 1_000,
            "cleared_at_ms": 3_000,
        },
        account_id="DU123",
        file_position=7,
    )

    assert projected.ts_ms == 3_000
    assert projected.ts_ms_source == "cleared_at_ms"
    assert projected.ts_ms_resolved is True
    assert projected.file_position == 7


def test_normalize_account_event_uses_general_timestamp_precedence() -> None:
    projected = normalize_account_event(
        {
            "event_type": "account_instance_binding_recorded",
            "account_id": "DU123",
            "created_at_ms": 2_000,
            "recorded_at_ms": 1_000,
        },
        account_id="DU123",
        file_position=8,
    )

    assert projected.ts_ms == 1_000
    assert projected.ts_ms_source == "recorded_at_ms"


def test_normalize_account_event_surfaces_missing_timestamp() -> None:
    projected = normalize_account_event(
        {"event_type": "legacy_account_note", "account_id": "DU123"},
        account_id="DU123",
        file_position=3,
    )

    assert projected.ts_ms is None
    assert projected.ts_ms_resolved is False
    assert projected.ts_ms_source is None
    assert projected.file_position == 3


def test_account_event_lifts_to_lifecycle_event_with_unresolved_timestamp() -> None:
    projected = normalize_account_event(
        {"event_type": "legacy_account_note", "account_id": "DU123"},
        account_id="DU123",
        file_position=3,
    )

    event = account_event_to_lifecycle_event(projected)

    assert event.ts_ms is None
    assert event.ts_ms_resolved is False
    assert event.status == "unknown"
    assert event.severity == "warning"
    assert event.source_local_seq == 3
    assert event.payload["ts_ms_resolved"] is False


def test_account_restart_intensity_lifts_as_freeze() -> None:
    projected = normalize_account_event(
        {
            "event_type": "account_restart_intensity_breached",
            "account_id": "DU123",
            "window_end_ms": 1_700_000_020_001,
            "operator_next_step": "STOP_RESTARTING_AND_RECOVER_ACCOUNT",
        },
        account_id="DU123",
        file_position=4,
    )

    event = account_event_to_lifecycle_event(projected)

    assert event.category == "freeze"
    assert event.node_id == "account_safety"
    assert event.status == "freeze"
    assert event.severity == "critical"
    assert event.operator_next_step == "STOP_RESTARTING_AND_RECOVER_ACCOUNT"


def test_account_owner_submit_events_lift_to_submit_path_nodes() -> None:
    prepared = account_event_to_lifecycle_event(
        normalize_account_event(
            {
                "event_type": "account_owner_submit_prepared",
                "account_id": "DU123",
                "seq": 9,
                "created_at_ms": 1_700_000_010_000,
                "diagnostics": {
                    "strategy_instance_id": "bot-a",
                    "run_id": "run-1",
                    "intent_id": "intent-a",
                    "order_ref": "learn-ai/bot-a/v1:intent-a",
                },
            },
            account_id="DU123",
            file_position=9,
        )
    )
    accepted = account_event_to_lifecycle_event(
        normalize_account_event(
            {
                "event_type": "account_owner_submit_accepted",
                "account_id": "DU123",
                "seq": 10,
                "created_at_ms": 1_700_000_010_000,
                "diagnostics": {
                    "strategy_instance_id": "bot-a",
                    "run_id": "run-1",
                    "intent_id": "intent-a",
                    "order_ref": "learn-ai/bot-a/v1:intent-a",
                    "order_id": 42,
                },
            },
            account_id="DU123",
            file_position=10,
        )
    )

    assert prepared.node_id == "intent_wal"
    assert prepared.status == "active"
    assert prepared.source == "intent_pending"
    assert prepared.summary == "AccountOwner intent persisted before broker submission."
    assert accepted.node_id == "place_order"
    assert accepted.status == "passed"
    assert accepted.source == "submit"
    assert accepted.summary == "AccountOwner order reached the broker submit boundary."


def test_project_intent_events_surfaces_drop_and_submit_uncertainty() -> None:
    projected = project_intent_events(
        [
            _intent(
                1,
                IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT,
                drop_reason="max_orders_per_day",
            ),
            _intent(2, IntentEventType.ACK_FAILED_UNCERTAIN),
        ],
        bot_id="bot-a",
        account_id="DU123",
        run_id="run-1",
    )

    assert [event.node_id for event in projected] == ["submit_order", "ack_or_reconcile"]
    assert projected[0].event_type == "BotBlocked"
    assert projected[0].status == "blocked"
    assert projected[0].why == "Submission gate dropped the intent: max_orders_per_day."
    assert projected[1].event_type == "BrokerOrderUncertain"
    assert projected[1].operator_next_step == "PROBE_BROKER_BEFORE_RETRY"
