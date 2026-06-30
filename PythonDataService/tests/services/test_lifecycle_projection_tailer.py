from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.account_artifacts import append_account_event
from app.engine.live.intent_events import IntentEventType
from app.engine.live.intent_wal import IntentWal
from app.schemas.lifecycle_projection import AccountOwnerStatusSnapshotRow, LifecycleProjectionEventRow
from app.services.lifecycle_projection_tailer import (
    AccountEventsProjectionSource,
    IntentWalProjectionSource,
    default_lifecycle_projection_cursor_path,
    read_lifecycle_projection_cursor,
    tail_lifecycle_projection_sources,
)

_NAMESPACE = "learn-ai/bot-a/v1"


class FakeProjectionStore:
    def __init__(self) -> None:
        self.batch_calls: list[
            tuple[
                list[LifecycleProjectionEventRow],
                list[LifecycleProjectionEventRow],
                list[AccountOwnerStatusSnapshotRow],
            ]
        ] = []

    async def upsert_replay_batch(
        self,
        *,
        bot_events: list[LifecycleProjectionEventRow],
        account_events: list[LifecycleProjectionEventRow],
        account_owner_status_snapshots: list[AccountOwnerStatusSnapshotRow],
    ) -> int:
        self.batch_calls.append((bot_events, account_events, account_owner_status_snapshots))
        return len(bot_events) + len(account_events) + len(account_owner_status_snapshots)


def _intent_wal_event(wal: IntentWal, seq_label: str, event_type: IntentEventType) -> None:
    wal.append(
        event_type=event_type,
        intent_id=f"intent-{seq_label}",
        bot_order_namespace=_NAMESPACE,
        order_ref=f"{_NAMESPACE}:intent-{seq_label}",
    )


@pytest.mark.asyncio
async def test_tailer_projects_account_events_once_and_resumes_from_cursor(tmp_path: Path) -> None:
    store = FakeProjectionStore()
    cursor_path = default_lifecycle_projection_cursor_path(tmp_path)
    source = AccountEventsProjectionSource(artifacts_root=tmp_path, account_id="DU123456")
    append_account_event(
        tmp_path,
        "DU123456",
        {
            "event_type": "account_owner_generation_recorded",
            "generation": 1,
            "phase": "accepting",
            "recorded_at_ms": 1_700_000_000_000,
            "source": "test",
        },
    )
    append_account_event(
        tmp_path,
        "DU123456",
        {
            "event_type": "account_freeze_recorded",
            "reason": "watchdog.flatten_failed",
            "recorded_at_ms": 1_700_000_000_100,
        },
    )

    first = await tail_lifecycle_projection_sources(
        cursor_path=cursor_path,
        account_event_sources=[source],
        store=store,
        inserted_at_ms=1_700_000_000_200,
    )

    assert first.rows_written == 3
    assert first.sources_checked == 1
    assert first.sources_advanced == 1
    assert [row.source_seq for row in store.batch_calls[0][1]] == [1, 2]
    assert len(store.batch_calls[0][2]) == 1
    cursor_source = next(iter(read_lifecycle_projection_cursor(cursor_path).sources.values()))
    assert cursor_source.last_file_position == 2
    assert cursor_source.last_source_seq == 2
    assert cursor_source.source_hash is not None

    second = await tail_lifecycle_projection_sources(
        cursor_path=cursor_path,
        account_event_sources=[source],
        store=store,
        inserted_at_ms=1_700_000_000_300,
    )

    assert second.rows_written == 0
    assert second.sources_advanced == 0

    append_account_event(
        tmp_path,
        "DU123456",
        {
            "event_type": "account_freeze_cleared",
            "reason": "watchdog.flatten_failed",
            "recorded_at_ms": 1_700_000_000_100,
            "cleared_at_ms": 1_700_000_000_400,
        },
    )

    third = await tail_lifecycle_projection_sources(
        cursor_path=cursor_path,
        account_event_sources=[source],
        store=store,
        inserted_at_ms=1_700_000_000_500,
    )

    assert third.rows_written == 1
    assert store.batch_calls[-1][1][0].event_id == "account_event:DU123456:3:account_freeze_cleared"
    cursor_source = next(iter(read_lifecycle_projection_cursor(cursor_path).sources.values()))
    assert cursor_source.last_file_position == 3
    assert cursor_source.last_source_seq == 3


@pytest.mark.asyncio
async def test_tailer_projects_intent_wal_after_saved_seq(tmp_path: Path) -> None:
    store = FakeProjectionStore()
    cursor_path = default_lifecycle_projection_cursor_path(tmp_path)
    wal_path = tmp_path / "run-1" / "intent_events.jsonl"
    wal = IntentWal(wal_path)
    _intent_wal_event(wal, "1", IntentEventType.PENDING_INTENT)
    _intent_wal_event(wal, "2", IntentEventType.ACK_FAILED_UNCERTAIN)
    source = IntentWalProjectionSource(
        wal_path=wal_path,
        account_id="DU123456",
        bot_id="bot-a",
        run_id="run-1",
    )

    first = await tail_lifecycle_projection_sources(
        cursor_path=cursor_path,
        intent_wal_sources=[source],
        store=store,
        inserted_at_ms=1_700_000_001_000,
    )

    assert first.rows_written == 2
    assert first.sources_advanced == 1
    assert [row.source_seq for row in store.batch_calls[0][0]] == [1, 2]
    cursor_source = next(iter(read_lifecycle_projection_cursor(cursor_path).sources.values()))
    assert cursor_source.last_file_position == 2
    assert cursor_source.last_source_seq == 2

    _intent_wal_event(wal, "3", IntentEventType.SUBMITTED)

    second = await tail_lifecycle_projection_sources(
        cursor_path=cursor_path,
        intent_wal_sources=[source],
        store=store,
        inserted_at_ms=1_700_000_001_100,
    )

    assert second.rows_written == 1
    assert store.batch_calls[-1][0][0].event_id == "intent_wal:run-1:3:SUBMITTED"
    cursor_source = next(iter(read_lifecycle_projection_cursor(cursor_path).sources.values()))
    assert cursor_source.last_source_seq == 3


@pytest.mark.asyncio
async def test_tailer_does_not_advance_cursor_when_projection_write_fails(tmp_path: Path) -> None:
    class FailingStore(FakeProjectionStore):
        async def upsert_replay_batch(
            self,
            *,
            bot_events: list[LifecycleProjectionEventRow],
            account_events: list[LifecycleProjectionEventRow],
            account_owner_status_snapshots: list[AccountOwnerStatusSnapshotRow],
        ) -> int:
            raise RuntimeError("db down")

    cursor_path = default_lifecycle_projection_cursor_path(tmp_path)
    append_account_event(
        tmp_path,
        "DU123456",
        {
            "event_type": "account_freeze_recorded",
            "reason": "watchdog.flatten_failed",
            "recorded_at_ms": 1_700_000_000_100,
        },
    )

    with pytest.raises(RuntimeError, match="db down"):
        await tail_lifecycle_projection_sources(
            cursor_path=cursor_path,
            account_event_sources=[AccountEventsProjectionSource(artifacts_root=tmp_path, account_id="DU123456")],
            store=FailingStore(),
            inserted_at_ms=1_700_000_000_200,
        )

    assert not cursor_path.exists()
