from __future__ import annotations

import os

import asyncpg
import pytest

from app.config import settings
from app.schemas.live_runs import BotLifecycleEvent, LifecycleEvidenceRef
from app.services.lifecycle_projection_store import (
    close_pool,
    lifecycle_event_to_projection_row,
    select_timeline,
    upsert_lifecycle_events,
)

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured; skipping live lifecycle projection DB test")
    return url


async def test_upsert_lifecycle_events_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "POSTGRES_URL", _postgres_url())
    monkeypatch.setattr(settings, "LIFECYCLE_PROJECTION_ENABLED", True)
    conn = await asyncpg.connect(settings.POSTGRES_URL)
    try:
        await conn.execute("TRUNCATE TABLE bot_lifecycle_events RESTART IDENTITY CASCADE")
    finally:
        await conn.close()

    try:
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
            operator_next_step="PROBE_BROKER_BEFORE_RETRY",
            evidence_refs=[
                LifecycleEvidenceRef(
                    source="intent_wal",
                    path="/tmp/run-1/intent_events.jsonl",
                    source_local_seq=2,
                    row_id="intent-2",
                )
            ],
            payload={"intent_id": "intent-2"},
        )
        row = lifecycle_event_to_projection_row(event, inserted_at_ms=1_700_000_000_100)

        assert await upsert_lifecycle_events("bot_lifecycle_events", [row]) == 1
        assert await upsert_lifecycle_events("bot_lifecycle_events", [row]) == 1

        rows = await select_timeline(account_id="DU123", strategy_instance_id="bot-a", limit=10)

        assert len(rows) == 1
        assert rows[0].event_id == event.event_id
        assert rows[0].receipt_payload["intent_id"] == "intent-2"
    finally:
        await close_pool()
