"""Author lifecycle projection batches from canonical artifact projections.

This is the replay seam for the Postgres read model. It consumes the same
backend-authored lifecycle event shape used by the chart and produces database
projection rows. It does not read or mutate canonical artifacts itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.engine.live.intent_events import IntentEvent
from app.schemas.lifecycle_projection import LifecycleProjectionEventRow, LifecycleProjectionTable
from app.schemas.live_runs import BotLifecycleEvent
from app.services.bot_lifecycle_projection import (
    account_event_to_lifecycle_event,
    project_account_events,
    project_intent_events,
)
from app.services.lifecycle_projection_store import (
    LifecycleProjectionStore,
    account_owner_status_snapshot_from_event,
    lifecycle_event_to_projection_row,
)


class LifecycleProjectionReplayStore(Protocol):
    """Store protocol required to persist replay-authored projection rows."""

    async def upsert_lifecycle_events(
        self,
        table: LifecycleProjectionTable,
        rows: list[LifecycleProjectionEventRow],
    ) -> int:
        ...

    async def upsert_account_owner_status_snapshot(self, row: dict[str, Any]) -> None:
        ...


@dataclass(frozen=True)
class LifecycleProjectionReplayBatch:
    """Rows authored during one deterministic projection replay."""

    bot_events: list[LifecycleProjectionEventRow] = field(default_factory=list)
    account_events: list[LifecycleProjectionEventRow] = field(default_factory=list)
    account_owner_status_snapshots: list[dict[str, Any]] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return (
            len(self.bot_events)
            + len(self.account_events)
            + len(self.account_owner_status_snapshots)
        )


def batch_from_lifecycle_events(
    events: list[BotLifecycleEvent],
    *,
    source_artifact: str | None = None,
    source_hash: str | None = None,
    inserted_at_ms: int | None = None,
) -> LifecycleProjectionReplayBatch:
    """Convert common lifecycle events into bot/account projection rows."""

    bot_rows: list[LifecycleProjectionEventRow] = []
    account_rows: list[LifecycleProjectionEventRow] = []
    for event in events:
        row = lifecycle_event_to_projection_row(
            event,
            source_artifact=source_artifact,
            source_hash=source_hash,
            inserted_at_ms=inserted_at_ms,
        )
        if row.strategy_instance_id:
            bot_rows.append(row)
        else:
            account_rows.append(row)
    return LifecycleProjectionReplayBatch(bot_events=bot_rows, account_events=account_rows)


def batch_from_account_events(
    rows: list[dict[str, Any]],
    *,
    account_id: str,
    bot_id: str | None = None,
    source_artifact: str = "account_events",
    source_hash: str | None = None,
    inserted_at_ms: int | None = None,
    start_file_position: int = 1,
) -> LifecycleProjectionReplayBatch:
    """Author projection rows from raw account_events.jsonl rows."""

    account_event_projections = project_account_events(
        rows,
        account_id=account_id,
        start_file_position=start_file_position,
    )
    lifecycle_events = [
        _apply_bot_id(account_event_to_lifecycle_event(event), bot_id)
        for event in account_event_projections
    ]
    batch = batch_from_lifecycle_events(
        lifecycle_events,
        source_artifact=source_artifact,
        source_hash=source_hash,
        inserted_at_ms=inserted_at_ms,
    )
    owner_snapshots = [
        snapshot
        for event in account_event_projections
        if (
            snapshot := account_owner_status_snapshot_from_event(
                event,
                source_artifact=source_artifact,
                source_hash=source_hash,
                inserted_at_ms=inserted_at_ms,
            )
        )
        is not None
    ]
    return LifecycleProjectionReplayBatch(
        bot_events=batch.bot_events,
        account_events=batch.account_events,
        account_owner_status_snapshots=owner_snapshots,
    )


def batch_from_intent_events(
    events: list[IntentEvent],
    *,
    bot_id: str,
    account_id: str,
    run_id: str,
    wal_path: Path | None = None,
    since_ms: int | None = None,
    live_state_last_intent_wal_seq: int | None = None,
    source_hash: str | None = None,
    inserted_at_ms: int | None = None,
) -> LifecycleProjectionReplayBatch:
    """Author projection rows from Intent WAL events."""

    lifecycle_events = project_intent_events(
        events,
        bot_id=bot_id,
        account_id=account_id,
        run_id=run_id,
        wal_path=wal_path,
        since_ms=since_ms,
        live_state_last_intent_wal_seq=live_state_last_intent_wal_seq,
    )
    return batch_from_lifecycle_events(
        lifecycle_events,
        source_artifact=str(wal_path) if wal_path is not None else "intent_wal",
        source_hash=source_hash,
        inserted_at_ms=inserted_at_ms,
    )


async def write_replay_batch(
    batch: LifecycleProjectionReplayBatch,
    *,
    store: LifecycleProjectionReplayStore | None = None,
) -> int:
    """Persist one replay batch through the projection store."""

    target = store or LifecycleProjectionStore()
    written = 0
    if batch.bot_events:
        written += await target.upsert_lifecycle_events("bot_lifecycle_events", batch.bot_events)
    if batch.account_events:
        written += await target.upsert_lifecycle_events("account_lifecycle_events", batch.account_events)
    for snapshot in batch.account_owner_status_snapshots:
        await target.upsert_account_owner_status_snapshot(snapshot)
        written += 1
    return written


def _apply_bot_id(event: BotLifecycleEvent, bot_id: str | None) -> BotLifecycleEvent:
    if bot_id is None:
        return event
    return event.model_copy(update={"bot_id": bot_id})
