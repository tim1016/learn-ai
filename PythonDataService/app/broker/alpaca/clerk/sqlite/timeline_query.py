"""Immutable, cursor-bound custody timeline queries.

The timeline is the one sanctioned append-log read.  Every filter participates
in both the SQL predicate and the signed cursor scope, so an older page cannot
silently change meaning if an operator changes the evidence they are viewing.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from app.broker.alpaca.clerk.sqlite import projection_helpers
from app.broker.alpaca.clerk.sqlite.projection_errors import InvalidTimelineCursor
from app.broker.alpaca.clerk.sqlite.projection_models import TimelinePage


@dataclass(frozen=True)
class TimelineFilters:
    """Exact evidence identities accepted by the immutable timeline."""

    strategy_instance_id: str | None = None
    order_ref: str | None = None
    effect_operation_id: str | None = None
    uncertainty_id: str | None = None
    execution_id: str | None = None
    transition_kind: str | None = None
    sequence: int | None = None

    def cursor_scope(self) -> dict[str, str | int | None]:
        return asdict(self)


def read_timeline_page(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    authority_generation: int,
    control_revision: int,
    filters: TimelineFilters,
    cursor: str | None,
    page_size: int,
) -> TimelinePage:
    """Read a stable keyset page at one already-open SQLite snapshot."""
    if cursor is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM custody_transitions"
        ).fetchone()
        anchor_sequence = int(row["sequence"])
        before_sequence = anchor_sequence + 1
    else:
        anchor_sequence, before_sequence = _decode_cursor(
            cursor,
            account_id=account_id,
            authority_generation=authority_generation,
            filters=filters,
        )

    where, parameters = _where_clause(
        filters,
        anchor_sequence=anchor_sequence,
        before_sequence=before_sequence,
    )
    rows = conn.execute(
        "SELECT ct.sequence, ct.effect_operation_id, ct.command_id, ct.order_ref, "
        "ct.broker_order_id, ct.transition_kind, ct.operation_state, ct.broker_state, "
        "ct.custody_owner, ct.execution_authority, ct.summary_code, ct.proof_reference, "
        "ct.source_event_at_ms, ct.clerk_observed_at_ms, ct.recorded_at_ms "
        "FROM custody_transitions ct "
        f"WHERE {where} ORDER BY ct.sequence DESC LIMIT ?",
        (*parameters, page_size + 1),
    ).fetchall()
    total_where, total_parameters = _total_where_clause(
        filters,
        anchor_sequence=anchor_sequence,
    )
    total = conn.execute(
        f"SELECT COUNT(*) AS count FROM custody_transitions ct WHERE {total_where}",
        total_parameters,
    ).fetchone()

    has_more = len(rows) > page_size
    entries = tuple(projection_helpers.timeline_entry(row) for row in rows[:page_size])
    next_cursor = None
    if has_more and entries:
        next_cursor = _encode_cursor(
            account_id=account_id,
            authority_generation=authority_generation,
            filters=filters,
            anchor_sequence=anchor_sequence,
            before_sequence=entries[-1].sequence,
        )
    return TimelinePage(
        account_id=account_id,
        strategy_instance_id=filters.strategy_instance_id,
        authority_generation=authority_generation,
        control_revision=control_revision,
        anchor_sequence=anchor_sequence,
        total_entries=int(total["count"]),
        entries=entries,
        next_cursor=next_cursor,
    )


def _where_clause(
    filters: TimelineFilters,
    *,
    anchor_sequence: int,
    before_sequence: int,
) -> tuple[str, list[object]]:
    clauses = ["ct.sequence <= ?", "ct.sequence < ?"]
    parameters: list[object] = [anchor_sequence, before_sequence]
    _append_filters(clauses, parameters, filters)
    return " AND ".join(clauses), parameters


def _total_where_clause(
    filters: TimelineFilters,
    *,
    anchor_sequence: int,
) -> tuple[str, list[object]]:
    clauses = ["ct.sequence <= ?"]
    parameters: list[object] = [anchor_sequence]
    _append_filters(clauses, parameters, filters)
    return " AND ".join(clauses), parameters


def _append_filters(
    clauses: list[str],
    parameters: list[object],
    filters: TimelineFilters,
) -> None:
    if filters.strategy_instance_id is not None:
        clauses.append("ct.strategy_instance_id = ?")
        parameters.append(filters.strategy_instance_id)
    if filters.order_ref is not None:
        clauses.append("ct.order_ref = ?")
        parameters.append(filters.order_ref)
    if filters.effect_operation_id is not None:
        clauses.append("ct.effect_operation_id = ?")
        parameters.append(filters.effect_operation_id)
    if filters.transition_kind is not None:
        clauses.append("ct.transition_kind = ?")
        parameters.append(filters.transition_kind)
    if filters.sequence is not None:
        clauses.append("ct.sequence = ?")
        parameters.append(filters.sequence)
    if filters.uncertainty_id is not None:
        _append_uncertainty_filter(clauses, parameters, filters.uncertainty_id)
    if filters.execution_id is not None:
        _append_execution_filter(clauses, parameters, filters.execution_id)


def _append_uncertainty_filter(
    clauses: list[str],
    parameters: list[object],
    uncertainty_id: str,
) -> None:
    """Match an episode's raise, resolution, and retained exact evidence."""
    clauses.append(
        "(ct.sequence = ? OR json_extract(ct.facts_json, '$.uncertainty_id') = ? "
        "OR json_extract(ct.facts_json, '$.uncertainty.uncertainty_id') = ? "
        "OR ct.proof_reference = ?)"
    )
    parameters.extend(
        (_uncertainty_sequence(uncertainty_id), uncertainty_id, uncertainty_id, uncertainty_id)
    )


def _append_execution_filter(
    clauses: list[str],
    parameters: list[object],
    execution_id: str,
) -> None:
    """Match direct, quarantine, resolution, and uncertainty-cause evidence."""
    clauses.append(
        "(json_extract(ct.facts_json, '$.execution_id') = ? "
        "OR json_extract(ct.facts_json, '$.conflict_execution_id') = ? "
        "OR json_extract(ct.facts_json, '$.exact_execution.execution_id') = ? "
        "OR json_extract(ct.facts_json, '$.cause_facts.execution_id') = ? "
        "OR json_extract(ct.facts_json, '$.uncertainty.cause_facts.execution_id') = ? "
        "OR json_extract(ct.facts_json, '$.superseded_execution_ref') = ?)"
    )
    parameters.extend((execution_id,) * 6)


def _uncertainty_sequence(uncertainty_id: str) -> int:
    prefix, separator, raw_sequence = uncertainty_id.partition(":")
    if prefix != "uncertainty" or separator == "" or not raw_sequence.isdecimal():
        return -1
    return int(raw_sequence)


def _encode_cursor(
    *,
    account_id: str,
    authority_generation: int,
    filters: TimelineFilters,
    anchor_sequence: int,
    before_sequence: int,
) -> str:
    encoded = json.dumps(
        {
            "account_id": account_id,
            "authority_generation": authority_generation,
            "filters": filters.cursor_scope(),
            "anchor_sequence": anchor_sequence,
            "before_sequence": before_sequence,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    account_id: str,
    authority_generation: int,
    filters: TimelineFilters,
) -> tuple[int, int]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload: Any = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        expected_scope = (
            payload["account_id"],
            payload["authority_generation"],
            payload["filters"],
        )
        actual_scope = (account_id, authority_generation, filters.cursor_scope())
        anchor = payload["anchor_sequence"]
        before = payload["before_sequence"]
        if (
            expected_scope != actual_scope
            or not isinstance(anchor, int)
            or not isinstance(before, int)
            or anchor < 0
            or before < 1
            or before > anchor + 1
        ):
            raise ValueError("cursor scope or bounds do not match")
        return anchor, before
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidTimelineCursor(
            "Timeline cursor is malformed, stale, or belongs to another scope"
        ) from exc
