"""Postgres read-model helpers for bot lifecycle projection rows.

Canonical truth remains the file artifacts. This module only persists and reads
the rebuildable projection requested by the operator workbench.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from app.config import settings
from app.schemas.lifecycle_projection import (
    LifecycleProjectionEventRow,
    LifecycleProjectionTable,
)
from app.schemas.live_runs import AccountEventProjection, BotLifecycleEvent

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_EVENT_TABLES: frozenset[str] = frozenset({"bot_lifecycle_events", "account_lifecycle_events"})


class LifecycleProjectionUnavailable(RuntimeError):
    """Raised when the rebuildable Postgres projection is not configured."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _ensure_projection_configured() -> None:
    if not settings.LIFECYCLE_PROJECTION_ENABLED:
        raise LifecycleProjectionUnavailable("LIFECYCLE_PROJECTION_ENABLED is false")
    if not settings.POSTGRES_URL:
        raise LifecycleProjectionUnavailable("POSTGRES_URL is empty")


async def init_pool() -> None:
    """Create the global asyncpg pool. Idempotent."""

    global _pool
    if _pool is not None:
        return
    _ensure_projection_configured()
    _pool = await asyncpg.create_pool(
        settings.POSTGRES_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )
    logger.info("lifecycle_projection_store: asyncpg pool initialized")


async def close_pool() -> None:
    """Close the global asyncpg pool. Idempotent."""

    global _pool
    if _pool is None:
        return
    try:
        await _pool.close()
    except RuntimeError:
        with contextlib.suppress(Exception):
            _pool.terminate()
    _pool = None
    logger.info("lifecycle_projection_store: asyncpg pool closed")


@asynccontextmanager
async def connection():  # type: ignore[return]
    """Yield a connection from the projection pool."""

    if _pool is None:
        await init_pool()
    if _pool is None:
        raise LifecycleProjectionUnavailable("asyncpg pool was not initialized")
    async with _pool.acquire() as conn:
        yield conn


def lifecycle_event_to_projection_row(
    event: BotLifecycleEvent,
    *,
    source_artifact: str | None = None,
    source_hash: str | None = None,
    inserted_at_ms: int | None = None,
) -> LifecycleProjectionEventRow:
    """Convert a backend-authored lifecycle event into a DB projection row."""

    if not event.account_id:
        raise ValueError("lifecycle projection rows require account_id")
    evidence_refs = [ref.model_dump() for ref in event.evidence_refs]
    primary_ref = event.evidence_refs[0] if event.evidence_refs else None
    resolved_source_artifact = (
        source_artifact
        or (primary_ref.path if primary_ref is not None and primary_ref.path else None)
        or (primary_ref.source if primary_ref is not None else None)
        or event.source
    )
    now_ms = inserted_at_ms or _now_ms()
    return LifecycleProjectionEventRow(
        account_id=event.account_id,
        strategy_instance_id=event.bot_id,
        event_id=event.event_id,
        event_type=event.event_type,
        category=event.category,
        node_id=event.node_id,
        status=event.status,
        severity=event.severity,
        ts_ms=event.ts_ms,
        ts_ms_resolved=event.ts_ms_resolved,
        source_artifact=resolved_source_artifact,
        source_type=event.source,
        source_seq=event.source_local_seq,
        source_hash=source_hash,
        summary=event.summary,
        why=event.why,
        operator_next_step=event.operator_next_step,
        receipt_payload=event.payload,
        evidence_refs=evidence_refs,
        rendered_headline=event.summary,
        rendered_template_id=_lifecycle_rendered_template_id(event),
        inserted_at_ms=now_ms,
        updated_at_ms=now_ms,
    )


def _lifecycle_rendered_template_id(event: BotLifecycleEvent) -> str:
    return f"lifecycle_projection.{event.source}.{event.event_type}.v1"


def account_owner_status_snapshot_from_event(
    event: AccountEventProjection,
    *,
    source_artifact: str = "account_events",
    source_hash: str | None = None,
    inserted_at_ms: int | None = None,
) -> dict[str, Any] | None:
    """Project an account_owner_generation_recorded event into a status row."""

    if event.event_type != "account_owner_generation_recorded":
        return None
    generation = event.payload.get("generation")
    phase = event.payload.get("phase")
    recorded_at_ms = event.payload.get("recorded_at_ms") or event.ts_ms
    if not isinstance(generation, int) or not isinstance(phase, str) or not isinstance(recorded_at_ms, int):
        raise ValueError("account_owner_generation_recorded event is missing generation, phase, or recorded_at_ms")
    now_ms = inserted_at_ms or _now_ms()
    return {
        "account_id": event.account_id,
        "generation": generation,
        "phase": phase,
        "recorded_at_ms": recorded_at_ms,
        "ts_ms_resolved": True,
        "source_artifact": source_artifact,
        "source_seq": event.seq or event.file_position,
        "source_offset": event.file_position,
        "source_hash": source_hash,
        "receipt_payload": event.payload,
        "inserted_at_ms": now_ms,
        "updated_at_ms": now_ms,
    }


async def upsert_lifecycle_events(
    table: LifecycleProjectionTable,
    rows: list[LifecycleProjectionEventRow],
) -> int:
    """Idempotently upsert lifecycle projection rows into the requested table."""

    if table not in _EVENT_TABLES:
        raise ValueError(f"unsupported lifecycle projection table: {table!r}")
    if not rows:
        return 0
    query = f"""
        INSERT INTO {table} (
            account_id, strategy_instance_id, run_id, event_id, event_type,
            category, node_id, gate_id, status, severity, ts_ms,
            ts_ms_resolved, source_artifact, source_type, source_seq,
            source_offset, source_hash, summary, why, operator_next_step,
            receipt_payload, evidence_refs, rendered_headline,
            rendered_template_id, inserted_at_ms, updated_at_ms
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9, $10, $11,
            $12, $13, $14, $15,
            $16, $17, $18, $19, $20,
            $21::jsonb, $22::jsonb, $23,
            $24, $25, $26
        )
        ON CONFLICT (event_id) DO UPDATE SET
            strategy_instance_id = EXCLUDED.strategy_instance_id,
            run_id = EXCLUDED.run_id,
            event_type = EXCLUDED.event_type,
            category = EXCLUDED.category,
            node_id = EXCLUDED.node_id,
            gate_id = EXCLUDED.gate_id,
            status = EXCLUDED.status,
            severity = EXCLUDED.severity,
            ts_ms = EXCLUDED.ts_ms,
            ts_ms_resolved = EXCLUDED.ts_ms_resolved,
            source_artifact = EXCLUDED.source_artifact,
            source_type = EXCLUDED.source_type,
            source_seq = EXCLUDED.source_seq,
            source_offset = EXCLUDED.source_offset,
            source_hash = EXCLUDED.source_hash,
            summary = EXCLUDED.summary,
            why = EXCLUDED.why,
            operator_next_step = EXCLUDED.operator_next_step,
            receipt_payload = EXCLUDED.receipt_payload,
            evidence_refs = EXCLUDED.evidence_refs,
            rendered_headline = EXCLUDED.rendered_headline,
            rendered_template_id = EXCLUDED.rendered_template_id,
            updated_at_ms = EXCLUDED.updated_at_ms;
    """
    args = [
        (
            row.account_id,
            row.strategy_instance_id,
            row.run_id,
            row.event_id,
            row.event_type,
            row.category,
            row.node_id,
            row.gate_id,
            row.status,
            row.severity,
            row.ts_ms,
            row.ts_ms_resolved,
            row.source_artifact,
            row.source_type,
            row.source_seq,
            row.source_offset,
            row.source_hash,
            row.summary,
            row.why,
            row.operator_next_step,
            _json_dump(row.receipt_payload),
            _json_dump(row.evidence_refs),
            row.rendered_headline,
            row.rendered_template_id,
            row.inserted_at_ms,
            row.updated_at_ms,
        )
        for row in rows
    ]
    async with connection() as conn:
        await conn.executemany(query, args)
    return len(rows)


async def upsert_account_owner_status_snapshot(row: dict[str, Any]) -> None:
    """Idempotently upsert one AccountOwner generation/phase snapshot."""

    query = """
        INSERT INTO account_owner_status_snapshots (
            account_id, generation, phase, recorded_at_ms, ts_ms_resolved,
            source_artifact, source_seq, source_offset, source_hash,
            receipt_payload, inserted_at_ms, updated_at_ms
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9,
            $10::jsonb, $11, $12
        )
        ON CONFLICT (account_id, generation, phase, recorded_at_ms) DO UPDATE SET
            source_artifact = EXCLUDED.source_artifact,
            source_seq = EXCLUDED.source_seq,
            source_offset = EXCLUDED.source_offset,
            source_hash = EXCLUDED.source_hash,
            receipt_payload = EXCLUDED.receipt_payload,
            updated_at_ms = EXCLUDED.updated_at_ms;
    """
    async with connection() as conn:
        await conn.execute(
            query,
            row["account_id"],
            row["generation"],
            row["phase"],
            row["recorded_at_ms"],
            row["ts_ms_resolved"],
            row["source_artifact"],
            row.get("source_seq"),
            row.get("source_offset"),
            row.get("source_hash"),
            _json_dump(row["receipt_payload"]),
            row["inserted_at_ms"],
            row["updated_at_ms"],
        )


async def select_timeline(
    *,
    account_id: str | None = None,
    strategy_instance_id: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
) -> list[LifecycleProjectionEventRow]:
    """Return newest lifecycle projection rows across bot and account tables."""

    query = """
        WITH unioned AS (
            SELECT * FROM bot_lifecycle_events
            UNION ALL
            SELECT * FROM account_lifecycle_events
        )
        SELECT id, account_id, strategy_instance_id, run_id, event_id,
               event_type, category, node_id, gate_id, status, severity,
               ts_ms, ts_ms_resolved, source_artifact, source_type,
               source_seq, source_offset, source_hash, summary, why,
               operator_next_step, receipt_payload, evidence_refs,
               rendered_headline, rendered_template_id, inserted_at_ms,
               updated_at_ms
          FROM unioned
         WHERE ($1::text IS NULL OR account_id = $1)
           AND ($2::text IS NULL OR strategy_instance_id = $2)
           AND ($3::text IS NULL OR run_id = $3)
         ORDER BY COALESCE(ts_ms, 9223372036854775807) DESC, source_seq DESC NULLS LAST, id DESC
         LIMIT $4;
    """
    async with connection() as conn:
        records = await conn.fetch(query, account_id, strategy_instance_id, run_id, limit)
    return [_row_from_record(record) for record in records]


async def select_safety_triage(
    *,
    account_id: str | None = None,
    strategy_instance_id: str | None = None,
    run_id: str | None = None,
    status: str | None = None,
    event_type: str | None = None,
    node_id: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> list[LifecycleProjectionEventRow]:
    """Return warning/critical projection rows for fleet triage."""

    query = """
        WITH unioned AS (
            SELECT * FROM bot_lifecycle_events
            UNION ALL
            SELECT * FROM account_lifecycle_events
        )
        SELECT id, account_id, strategy_instance_id, run_id, event_id,
               event_type, category, node_id, gate_id, status, severity,
               ts_ms, ts_ms_resolved, source_artifact, source_type,
               source_seq, source_offset, source_hash, summary, why,
               operator_next_step, receipt_payload, evidence_refs,
               rendered_headline, rendered_template_id, inserted_at_ms,
               updated_at_ms
         FROM unioned
         WHERE severity IN ('warning','critical')
           AND ($1::text IS NULL OR account_id = $1)
           AND ($2::text IS NULL OR strategy_instance_id = $2)
           AND ($3::text IS NULL OR run_id = $3)
           AND ($4::text IS NULL OR status = $4)
           AND ($5::text IS NULL OR event_type = $5)
           AND ($6::text IS NULL OR node_id = $6)
           AND ($7::text IS NULL OR severity = $7)
         ORDER BY COALESCE(ts_ms, 9223372036854775807) DESC, source_seq DESC NULLS LAST, id DESC
         LIMIT $8;
    """
    async with connection() as conn:
        records = await conn.fetch(
            query,
            account_id,
            strategy_instance_id,
            run_id,
            status,
            event_type,
            node_id,
            severity,
            limit,
        )
    return [_row_from_record(record) for record in records]


def _decode_jsonb(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _row_from_record(record: asyncpg.Record) -> LifecycleProjectionEventRow:
    data = dict(record)
    data["receipt_payload"] = _decode_jsonb(data["receipt_payload"])
    data["evidence_refs"] = _decode_jsonb(data["evidence_refs"])
    return LifecycleProjectionEventRow.model_validate(data)


class LifecycleProjectionStore:
    """Thin dependency wrapper around module-level projection helpers."""

    async def upsert_lifecycle_events(
        self,
        table: LifecycleProjectionTable,
        rows: list[LifecycleProjectionEventRow],
    ) -> int:
        return await upsert_lifecycle_events(table, rows)

    async def upsert_account_owner_status_snapshot(self, row: dict[str, Any]) -> None:
        await upsert_account_owner_status_snapshot(row)

    async def select_timeline(
        self,
        *,
        account_id: str | None = None,
        strategy_instance_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[LifecycleProjectionEventRow]:
        return await select_timeline(
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            limit=limit,
        )

    async def select_safety_triage(
        self,
        *,
        account_id: str | None = None,
        strategy_instance_id: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        event_type: str | None = None,
        node_id: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[LifecycleProjectionEventRow]:
        return await select_safety_triage(
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            status=status,
            event_type=event_type,
            node_id=node_id,
            severity=severity,
            limit=limit,
        )


_DEFAULT_STORE = LifecycleProjectionStore()


def get_lifecycle_projection_store() -> LifecycleProjectionStore:
    """FastAPI dependency for read-only lifecycle projection routes."""

    return _DEFAULT_STORE
