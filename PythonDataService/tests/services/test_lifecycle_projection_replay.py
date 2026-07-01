from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.schemas.lifecycle_projection import LifecycleProjectionEventRow
from app.services.lifecycle_projection_replay import (
    LifecycleProjectionReplayBatch,
    batch_from_account_events,
    batch_from_intent_events,
    write_replay_batch,
)

_NAMESPACE = "learn-ai/bot-a/v1"


def _intent(seq: int, event_type: IntentEventType) -> IntentEvent:
    intent_id = f"intent-{seq}"
    return IntentEvent(
        seq=seq,
        event_type=event_type,
        intent_id=intent_id,
        bot_order_namespace=_NAMESPACE,
        order_ref=f"{_NAMESPACE}:{intent_id}",
        appended_at_ms=1_700_000_000_000 + seq,
    )


def _projection_row(
    event_id: str,
    *,
    strategy_instance_id: str | None = None,
) -> LifecycleProjectionEventRow:
    return LifecycleProjectionEventRow(
        account_id="DU123",
        strategy_instance_id=strategy_instance_id,
        event_id=event_id,
        event_type="BrokerOrderUncertain",
        category="order",
        node_id="ack_or_reconcile",
        status="blocked",
        severity="warning",
        ts_ms=1_700_000_000_000,
        ts_ms_resolved=True,
        source_artifact="/tmp/source.jsonl",
        source_type="broker_ack",
        source_rank=50,
        source_seq=2,
        summary="Broker acknowledgment failed; submit outcome is uncertain.",
        receipt_payload={"intent_id": "intent-2"},
        evidence_refs=[{"source": "intent_wal"}],
        inserted_at_ms=1_700_000_000_100,
        updated_at_ms=1_700_000_000_100,
    )


def test_batch_from_intent_events_authors_bot_projection_rows() -> None:
    wal_path = Path("/tmp/run-1/intent_events.jsonl")

    batch = batch_from_intent_events(
        [_intent(2, IntentEventType.ACK_FAILED_UNCERTAIN)],
        bot_id="bot-a",
        account_id="DU123",
        run_id="run-1",
        wal_path=wal_path,
        source_hash="a" * 64,
        inserted_at_ms=1_700_000_000_500,
    )

    assert batch.row_count == 1
    assert len(batch.bot_events) == 1
    assert batch.account_events == []
    row = batch.bot_events[0]
    assert row.account_id == "DU123"
    assert row.strategy_instance_id == "bot-a"
    assert row.event_id == "intent_wal:run-1:2:ACK_FAILED_UNCERTAIN"
    assert row.event_type == "BrokerOrderUncertain"
    assert row.status == "blocked"
    assert row.source_artifact == str(wal_path)
    assert row.source_hash == "a" * 64
    assert row.run_id == "run-1"
    assert row.rendered_headline == "Broker acknowledgment failed; submit outcome is uncertain."
    assert row.rendered_template_id == "lifecycle_projection.intent_wal.ack_failed_uncertain.v1"
    assert row.receipt_payload["intent_id"] == "intent-2"
    assert row.evidence_refs[0]["source"] == "intent_wal"


def test_batch_from_intent_events_preserves_distinct_template_ids_after_normalization() -> None:
    wal_path = Path("/tmp/run-1/intent_events.jsonl")

    batch = batch_from_intent_events(
        [
            _intent(2, IntentEventType.ACK_FAILED_UNCERTAIN),
            _intent(3, IntentEventType.SUBMIT_UNCERTAIN_HALTED),
        ],
        bot_id="bot-a",
        account_id="DU123",
        run_id="run-1",
        wal_path=wal_path,
    )

    assert [row.event_type for row in batch.bot_events] == [
        "BrokerOrderUncertain",
        "BrokerOrderUncertain",
    ]
    assert {row.rendered_template_id for row in batch.bot_events} == {
        "lifecycle_projection.intent_wal.ack_failed_uncertain.v1",
        "lifecycle_projection.intent_wal.submit_uncertain_halted.v1",
    }


def test_batch_from_intent_events_requires_wal_path() -> None:
    with pytest.raises(ValueError, match="wal_path"):
        batch_from_intent_events(
            [_intent(2, IntentEventType.ACK_FAILED_UNCERTAIN)],
            bot_id="bot-a",
            account_id="DU123",
            run_id="run-1",
        )


def test_batch_from_account_events_authors_account_rows_and_owner_snapshot() -> None:
    batch = batch_from_account_events(
        [
            {
                "event_type": "account_owner_generation_recorded",
                "account_id": "DU123",
                "seq": 5,
                "generation": 7,
                "phase": "reconnecting",
                "recorded_at_ms": 1_700_000_000_500,
            }
        ],
        account_id="DU123",
        source_artifact="/tmp/accounts/DU123/account_events.jsonl",
        source_hash="b" * 64,
        inserted_at_ms=1_700_000_000_600,
    )

    assert batch.row_count == 2
    assert batch.bot_events == []
    assert len(batch.account_events) == 1
    row = batch.account_events[0]
    assert row.event_id == "account_event:DU123:5:account_owner_generation_recorded"
    assert row.strategy_instance_id is None
    assert row.node_id == "writer_guard"
    assert row.status == "active"
    assert "not R3 daemon/IPC writer authority" in row.summary
    assert row.rendered_headline == row.summary
    assert row.rendered_template_id == "lifecycle_projection.account_event.account_owner_generation_recorded.v1"
    assert row.source_artifact == "/tmp/accounts/DU123/account_events.jsonl"
    assert row.receipt_payload["generation"] == 7

    assert len(batch.account_owner_status_snapshots) == 1
    snapshot = batch.account_owner_status_snapshots[0]
    assert snapshot.account_id == "DU123"
    assert snapshot.generation == 7
    assert snapshot.phase == "reconnecting"
    assert snapshot.recorded_at_ms == 1_700_000_000_500
    assert snapshot.source_hash == "b" * 64


def test_batch_from_account_events_requires_canonical_source_artifact() -> None:
    with pytest.raises(ValueError, match="source_artifact"):
        batch_from_account_events(
            [{"event_type": "account_freeze_recorded", "account_id": "DU123"}],
            account_id="DU123",
        )


def test_batch_from_account_events_derives_bot_scope_from_diagnostics() -> None:
    batch = batch_from_account_events(
        [
            {
                "event_type": "account_owner_submit_uncertain",
                "account_id": "DU123",
                "seq": 9,
                "created_at_ms": 1_700_000_010_000,
                "diagnostics": {
                    "strategy_instance_id": "bot-a",
                    "run_id": "run-1",
                    "intent_id": "intent-a",
                    "order_ref": "learn-ai/bot-a/v1:intent-a",
                },
            }
        ],
        account_id="DU123",
        source_artifact="/tmp/accounts/DU123/account_events.jsonl",
    )

    assert batch.row_count == 1
    assert len(batch.bot_events) == 1
    assert batch.account_events == []
    row = batch.bot_events[0]
    assert row.strategy_instance_id == "bot-a"
    assert row.run_id == "run-1"
    assert row.event_type == "account_owner_submit_uncertain"
    assert row.node_id == "ack_or_reconcile"
    assert row.status == "blocked"


def test_batch_from_account_events_preserves_tailed_file_position() -> None:
    batch = batch_from_account_events(
        [{"event_type": "legacy_account_note", "account_id": "DU123"}],
        account_id="DU123",
        source_artifact="/tmp/accounts/DU123/account_events.jsonl",
        start_file_position=5,
    )

    assert batch.row_count == 1
    row = batch.account_events[0]
    assert row.event_id == "account_event:DU123:5:legacy_account_note"
    assert row.source_seq == 5
    assert row.receipt_payload["ts_ms_resolved"] is False


@pytest.mark.asyncio
async def test_write_replay_batch_uses_store_methods() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.batch_calls: list[
                tuple[
                    list[LifecycleProjectionEventRow],
                    list[LifecycleProjectionEventRow],
                    list[object],
                ]
            ] = []

        async def upsert_replay_batch(
            self,
            *,
            bot_events: list[LifecycleProjectionEventRow],
            account_events: list[LifecycleProjectionEventRow],
            account_owner_status_snapshots: list[object],
        ) -> int:
            self.batch_calls.append((bot_events, account_events, account_owner_status_snapshots))
            return len(bot_events) + len(account_events) + len(account_owner_status_snapshots)

    store = FakeStore()
    batch = LifecycleProjectionReplayBatch(
        bot_events=[_projection_row("bot-event", strategy_instance_id="bot-a")],
        account_events=[_projection_row("account-event")],
        account_owner_status_snapshots=[],
    )

    written = await write_replay_batch(batch, store=store)

    assert written == 2
    assert len(store.batch_calls) == 1
    bot_events, account_events, snapshots = store.batch_calls[0]
    assert bot_events[0].event_id == "bot-event"
    assert account_events[0].event_id == "account-event"
    assert snapshots == []
