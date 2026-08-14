"""Small pure helpers shared by the bounded SQLite Clerk read projections."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from app.broker.alpaca.clerk.sqlite.projection_models import TimelineEntry


def bounded_limit(limit: int, *, maximum: int) -> int:
    return min(max(limit, 1), maximum)


def operation_cursor_key(value: object) -> tuple[int, str]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], int)
        or not isinstance(value[1], str)
        or value[0] < 0
        or not value[1]
    ):
        raise ValueError("invalid operation cursor key")
    return value[0], value[1]


def scope_filter(column: str, strategy_instance_id: str | None) -> tuple[str, tuple[object, ...]]:
    if strategy_instance_id is None:
        return "", ()
    return f"WHERE {column} = ?", (strategy_instance_id,)


def json_string_tuple(raw: str | None, *, error_type: type[Exception]) -> tuple[str, ...]:
    if raw is None:
        return ()
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise error_type("Fold evidence_refs_json is not valid JSON") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise error_type("Fold evidence_refs_json must contain a string list")
    return tuple(decoded)


def operation_ref(row: sqlite3.Row) -> str:
    for key in ("effect_operation_id", "command_id", "order_ref"):
        value = row[key]
        if value is not None:
            return str(value)
    return f"transition:{row['sequence']}"


def timeline_entry(row: sqlite3.Row) -> TimelineEntry:
    return TimelineEntry(
        sequence=row["sequence"],
        operation_ref=operation_ref(row),
        effect_operation_id=row["effect_operation_id"],
        command_id=row["command_id"],
        order_ref=row["order_ref"],
        broker_order_id=row["broker_order_id"],
        transition_kind=row["transition_kind"],
        operation_state=row["operation_state"],
        broker_state=row["broker_state"],
        custody_owner=row["custody_owner"],
        execution_authority=row["execution_authority"],
        summary_code=row["summary_code"],
        proof_reference=row["proof_reference"],
        source_event_at_ms=row["source_event_at_ms"],
        clerk_observed_at_ms=row["clerk_observed_at_ms"],
        recorded_at_ms=row["recorded_at_ms"],
    )


def timeline_sequences(entries: Iterable[TimelineEntry]) -> tuple[int, ...]:
    """Small public seam used by qualification tests and cursor assertions."""
    return tuple(entry.sequence for entry in entries)
