from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.schemas.live_runs import BotLifecycleEvent, LifecycleEvidenceRef
from app.services import lifecycle_projection_store as store_module
from app.services.bot_lifecycle_projection import normalize_account_event
from app.services.lifecycle_projection_store import (
    account_owner_status_snapshot_from_event,
    lifecycle_event_to_projection_row,
    select_safety_triage,
)


def test_lifecycle_event_to_projection_row_preserves_provenance_and_receipts() -> None:
    event = BotLifecycleEvent(
        event_id="intent_wal:run-1:2:ACK_FAILED_UNCERTAIN",
        bot_id="bot-a",
        account_id="DU123",
        event_type="BrokerOrderUncertain",
        category="order",
        node_id="ack_or_reconcile",
        status="blocked",
        severity="warning",
        ts_ms=1_700_000_000_000,
        ts_ms_resolved=True,
        source="broker_ack",
        source_rank=50,
        source_local_seq=2,
        summary="Broker acknowledgment failed; submit outcome is uncertain.",
        why="IBKR timeout before acknowledgement.",
        operator_next_step="PROBE_BROKER_BEFORE_RETRY",
        evidence_refs=[
            LifecycleEvidenceRef(
                source="intent_wal",
                source_label="Intent WAL",
                source_local_seq=2,
                path="/tmp/run-1/intent_events.jsonl",
                row_id="intent-2",
                summary="ACK_FAILED_UNCERTAIN",
            )
        ],
        payload={
            "intent_id": "intent-2",
            "order_ref": "learn-ai/bot-a/v1:intent-2",
        },
    )

    row = lifecycle_event_to_projection_row(
        event,
        source_hash="a" * 64,
        inserted_at_ms=1_700_000_000_100,
    )

    assert row.account_id == "DU123"
    assert row.strategy_instance_id == "bot-a"
    assert row.event_id == event.event_id
    assert row.status == "blocked"
    assert row.severity == "warning"
    assert row.source_artifact == "/tmp/run-1/intent_events.jsonl"
    assert row.source_type == "broker_ack"
    assert row.source_rank == 50
    assert row.source_seq == 2
    assert row.source_hash == "a" * 64
    assert row.receipt_payload["order_ref"] == "learn-ai/bot-a/v1:intent-2"
    assert row.evidence_refs[0]["source"] == "intent_wal"
    assert row.inserted_at_ms == 1_700_000_000_100
    assert row.updated_at_ms == 1_700_000_000_100


def test_lifecycle_event_to_projection_row_requires_account_id() -> None:
    event = BotLifecycleEvent(
        event_id="decision:1",
        event_type="BotDecisionEvaluated",
        category="decision",
        ts_ms=1_700_000_000_000,
        ts_ms_resolved=True,
        source="decision",
        source_rank=10,
        source_local_seq=1,
        summary="Decision evaluated.",
    )

    with pytest.raises(ValueError, match="account_id"):
        lifecycle_event_to_projection_row(event)


def test_account_owner_status_snapshot_from_generation_event() -> None:
    event = normalize_account_event(
        {
            "event_type": "account_owner_generation_recorded",
            "account_id": "DU123",
            "seq": 5,
            "generation": 7,
            "phase": "reconnecting",
            "recorded_at_ms": 1_700_000_000_500,
            "source": "account_owner",
        },
        account_id="DU123",
        file_position=5,
    )

    snapshot = account_owner_status_snapshot_from_event(
        event,
        source_artifact="/tmp/accounts/DU123/account_events.jsonl",
        source_hash="b" * 64,
        inserted_at_ms=1_700_000_000_600,
    )

    assert snapshot is not None
    assert snapshot.account_id == "DU123"
    assert snapshot.generation == 7
    assert snapshot.phase == "reconnecting"
    assert snapshot.recorded_at_ms == 1_700_000_000_500
    assert snapshot.source_seq == 5
    assert snapshot.source_offset == 5
    assert snapshot.source_hash == "b" * 64
    assert snapshot.receipt_payload["source"] == "account_owner"


def test_account_owner_status_snapshot_ignores_unrelated_event() -> None:
    event = normalize_account_event(
        {"event_type": "account_freeze_recorded", "account_id": "DU123", "recorded_at_ms": 1_700_000_000_000},
        account_id="DU123",
        file_position=1,
    )

    assert account_owner_status_snapshot_from_event(event) is None


@pytest.mark.asyncio
async def test_select_safety_triage_applies_fleet_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeConnection:
        async def fetch(self, query: str, *args: object) -> list[object]:
            captured["query"] = query
            captured["args"] = args
            return []

    @asynccontextmanager
    async def fake_connection():
        yield FakeConnection()

    monkeypatch.setattr(store_module, "connection", fake_connection)

    rows = await select_safety_triage(
        account_id="DU123",
        strategy_instance_id="bot-a",
        run_id="run-1",
        status="blocked",
        event_type="BrokerOrderUncertain",
        node_id="ack_or_reconcile",
        severity="warning",
        limit=25,
    )

    assert rows == []
    query = str(captured["query"])
    assert "severity IN ('warning','critical')" in query
    assert "AND ($1::text IS NULL OR account_id = $1)" in query
    assert "AND ($2::text IS NULL OR strategy_instance_id = $2)" in query
    assert "AND ($3::text IS NULL OR run_id = $3)" in query
    assert "AND ($4::text IS NULL OR status = $4)" in query
    assert "AND ($5::text IS NULL OR event_type = $5)" in query
    assert "AND ($6::text IS NULL OR node_id = $6)" in query
    assert "AND ($7::text IS NULL OR severity = $7)" in query
    assert captured["args"] == (
        "DU123",
        "bot-a",
        "run-1",
        "blocked",
        "BrokerOrderUncertain",
        "ack_or_reconcile",
        "warning",
        25,
    )
