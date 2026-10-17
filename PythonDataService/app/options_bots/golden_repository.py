"""Immutable Golden Options versions and append-only qualification events."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.research.persistence.db import with_connection


@dataclass(frozen=True)
class GoldenRecord:
    id: str
    name: str
    source_run_id: int
    setup: dict[str, Any]
    setup_sha256: str
    rules_digest: str
    proof: dict[str, Any]
    proof_sha256: str
    evidence: dict[str, Any]
    note: str
    approved_by: str
    created_at_ms: int
    command_id: str
    command_sha256: str


@dataclass(frozen=True)
class GoldenEvent:
    id: int
    qualification_id: str
    kind: str
    rules_digest: str | None
    proof: dict[str, Any] | None
    reason: str | None
    actor: str
    created_at_ms: int
    command_id: str
    command_sha256: str


class CommandConflict(ValueError):
    """An idempotency key already names a different command."""


class SourceUnavailable(ValueError):
    """A selected completed run disappeared before approval could commit."""


def _record(row: asyncpg.Record) -> GoldenRecord:
    return GoldenRecord(
        id=row["id"], name=row["name"], source_run_id=row["source_run_id"],
        setup=json.loads(row["setup_json"]), setup_sha256=row["setup_sha256"],
        rules_digest=row["rules_digest"], proof=json.loads(row["proof_json"]),
        proof_sha256=row["proof_sha256"], evidence=json.loads(row["evidence_json"]),
        note=row["note"], approved_by=row["approved_by"], created_at_ms=row["created_at_ms"],
        command_id=row["command_id"], command_sha256=row["command_sha256"],
    )


def _event(row: asyncpg.Record) -> GoldenEvent:
    return GoldenEvent(
        id=row["id"], qualification_id=row["qualification_id"], kind=row["kind"],
        rules_digest=row["rules_digest"], proof=json.loads(row["proof_json"]) if row["proof_json"] else None,
        reason=row["reason"], actor=row["actor"], created_at_ms=row["created_at_ms"],
        command_id=row["command_id"], command_sha256=row["command_sha256"],
    )


_COLUMNS = """id, name, source_run_id, setup_json::text AS setup_json, setup_sha256,
    rules_digest, proof_json::text AS proof_json, proof_sha256, evidence_json::text AS evidence_json,
    note, approved_by, created_at_ms, command_id, command_sha256"""
_EVENT_COLUMNS = """id, qualification_id, kind, rules_digest, proof_json::text AS proof_json,
    reason, actor, created_at_ms, command_id, command_sha256"""


async def list_records(
    conn: asyncpg.Connection, *, limit: int = 200, source_run_id: int | None = None,
) -> tuple[GoldenRecord, ...]:
    rows = await conn.fetch(
        f"SELECT {_COLUMNS} FROM research_golden_option_setups "
        "WHERE ($2::bigint IS NULL OR source_run_id=$2) ORDER BY created_at_ms DESC, id DESC LIMIT $1",
        limit, source_run_id,
    )
    return tuple(_record(row) for row in rows)


async def get_record(conn: asyncpg.Connection, qualification_id: str) -> GoldenRecord | None:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM research_golden_option_setups WHERE id = $1", qualification_id)
    return None if row is None else _record(row)


async def records_for_setups(conn: asyncpg.Connection, hashes: list[str]) -> tuple[GoldenRecord, ...]:
    rows = await conn.fetch(
        f"SELECT {_COLUMNS} FROM research_golden_option_setups WHERE setup_sha256 = ANY($1::text[]) ORDER BY created_at_ms DESC, id DESC",
        hashes,
    )
    return tuple(_record(row) for row in rows)


async def by_command(conn: asyncpg.Connection, command_id: str) -> GoldenRecord | None:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM research_golden_option_setups WHERE command_id = $1", command_id)
    return None if row is None else _record(row)


async def events(conn: asyncpg.Connection, qualification_id: str) -> tuple[GoldenEvent, ...]:
    return await events_for(conn, (qualification_id,))


async def events_for(conn: asyncpg.Connection, qualification_ids: Sequence[str]) -> tuple[GoldenEvent, ...]:
    rows = await conn.fetch(
        f"SELECT {_EVENT_COLUMNS} FROM research_golden_option_setup_events WHERE qualification_id = ANY($1::text[]) ORDER BY id",
        qualification_ids,
    )
    return tuple(_event(row) for row in rows)


async def event_by_command(conn: asyncpg.Connection, command_id: str) -> GoldenEvent | None:
    row = await conn.fetchrow(
        f"SELECT {_EVENT_COLUMNS} FROM research_golden_option_setup_events WHERE command_id = $1", command_id
    )
    return None if row is None else _event(row)


async def insert_record(conn: asyncpg.Connection, record: GoldenRecord) -> GoldenRecord:
    """Publish once, preserving the source row under the same lock deletion uses."""
    async with conn.transaction():
        source = await conn.fetchrow(
            "SELECT status FROM research_options_backtest_runs WHERE id = $1 FOR KEY SHARE", record.source_run_id
        )
        if source is None or source["status"] != "complete":
            raise SourceUnavailable("The selected completed run is no longer retained. Nothing was approved.")
        row = await conn.fetchrow(
            f"""INSERT INTO research_golden_option_setups (
                id, name, source_run_id, setup_json, setup_sha256, rules_digest, proof_json, proof_sha256,
                evidence_json, note, approved_by, created_at_ms, command_id, command_sha256
            ) VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7::jsonb,$8,$9::jsonb,$10,$11,$12,$13,$14)
            ON CONFLICT (command_id) DO NOTHING RETURNING {_COLUMNS}""",
            record.id, record.name, record.source_run_id, json.dumps(record.setup), record.setup_sha256,
            record.rules_digest, json.dumps(record.proof), record.proof_sha256, json.dumps(record.evidence),
            record.note, record.approved_by, record.created_at_ms, record.command_id, record.command_sha256,
        )
        stored = _record(row) if row is not None else await by_command(conn, record.command_id)
        if stored is None or stored.command_sha256 != record.command_sha256:
            raise CommandConflict("This approval key already names a different request.")
        return stored


async def insert_event(conn: asyncpg.Connection, event: GoldenEvent) -> GoldenEvent:
    async with conn.transaction():
        await conn.fetchrow("SELECT id FROM research_golden_option_setups WHERE id = $1 FOR UPDATE", event.qualification_id)
        prior = await event_by_command(conn, event.command_id)
        if prior is not None:
            if prior.command_sha256 != event.command_sha256:
                raise CommandConflict("This event key already names a different request.")
            return prior
        revoked = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM research_golden_option_setup_events WHERE qualification_id = $1 AND kind = 'revoked')",
            event.qualification_id,
        )
        if revoked and event.kind == "reproved":
            raise ValueError("A revoked Golden setup cannot be re-proved. Approve a new version instead.")
        row = await conn.fetchrow(
            f"""INSERT INTO research_golden_option_setup_events (
                qualification_id, kind, rules_digest, proof_json, reason, actor, created_at_ms, command_id, command_sha256
            ) VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9)
            ON CONFLICT (command_id) DO NOTHING RETURNING {_EVENT_COLUMNS}""",
            event.qualification_id, event.kind, event.rules_digest,
            None if event.proof is None else json.dumps(event.proof), event.reason, event.actor,
            event.created_at_ms, event.command_id, event.command_sha256,
        )
        stored = _event(row) if row is not None else await event_by_command(conn, event.command_id)
        if stored is None or stored.command_sha256 != event.command_sha256:
            raise CommandConflict("This event key already names a different request.")
        return stored


class PostgresGoldenStore:
    async def list(self, *, limit: int = 200, source_run_id: int | None = None) -> tuple[GoldenRecord, ...]:
        return await with_connection(list_records, limit=limit, source_run_id=source_run_id)

    async def get(self, qualification_id: str) -> GoldenRecord | None:
        return await with_connection(get_record, qualification_id)

    async def by_command(self, command_id: str) -> GoldenRecord | None:
        return await with_connection(by_command, command_id)

    async def events(self, qualification_id: str) -> tuple[GoldenEvent, ...]:
        return await with_connection(events, qualification_id)

    async def events_for(self, qualification_ids: Sequence[str]) -> tuple[GoldenEvent, ...]:
        return await with_connection(events_for, qualification_ids)

    async def event_by_command(self, command_id: str) -> GoldenEvent | None:
        return await with_connection(event_by_command, command_id)

    async def insert(self, record: GoldenRecord) -> GoldenRecord:
        return await with_connection(insert_record, record)

    async def append(self, event: GoldenEvent) -> GoldenEvent:
        return await with_connection(insert_event, event)
