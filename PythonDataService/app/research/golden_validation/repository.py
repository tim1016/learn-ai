"""Persistence for immutable Validation Golden Runs and their reviews.

The tables are Python-owned in research schema version 7.  This module is
intentionally mechanical: domain decisions about evidence state, promotion
classification, and stale-review protection live in ``service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True, slots=True)
class GoldenRunRow:
    id: int
    source_run_id: int
    command_id: str
    command_sha256: str
    label: str | None
    strategy_name: str
    symbol: str
    validation_case_json: str
    case_sha256: str
    rationale: str
    designated_by: str
    designated_at_ms: int


@dataclass(frozen=True, slots=True)
class GoldenReviewRow:
    id: int
    golden_run_id: int
    command_id: str
    command_sha256: str
    expected_evidence_revision: str
    decision: str
    classification: str | None
    evidence_state: str
    parity_verdict_id: int | None
    evidence_json: str
    evidence_sha256: str
    reason: str
    quantconnect_backtest_id: str | None
    authorized_program_version: str | None
    reviewed_by: str
    reviewed_at_ms: int


_GOLDEN_COLUMNS = """
    id, source_run_id, command_id, command_sha256, label, strategy_name, symbol,
    validation_case_json::text AS validation_case_json, case_sha256, rationale,
    designated_by, designated_at_ms
"""
_REVIEW_COLUMNS = """
    id, golden_run_id, command_id, command_sha256, expected_evidence_revision,
    decision, classification, evidence_state, parity_verdict_id,
    evidence_json::text AS evidence_json, evidence_sha256, reason,
    quantconnect_backtest_id, authorized_program_version, reviewed_by, reviewed_at_ms
"""


async def lock_source_run(conn: asyncpg.Connection, source_run_id: int) -> str | None:
    """Pin one run against deletion until its designation transaction commits."""
    return await conn.fetchval(
        "SELECT source FROM research_backtest_runs WHERE id = $1 FOR KEY SHARE",
        source_run_id,
    )


async def get_golden_run(conn: asyncpg.Connection, golden_run_id: int) -> GoldenRunRow | None:
    row = await conn.fetchrow(
        f"SELECT {_GOLDEN_COLUMNS} FROM research_validation_golden_runs WHERE id = $1",
        golden_run_id,
    )
    return None if row is None else GoldenRunRow(**row)


async def get_golden_run_by_source(conn: asyncpg.Connection, source_run_id: int) -> GoldenRunRow | None:
    row = await conn.fetchrow(
        f"SELECT {_GOLDEN_COLUMNS} FROM research_validation_golden_runs WHERE source_run_id = $1",
        source_run_id,
    )
    return None if row is None else GoldenRunRow(**row)


async def get_golden_run_by_command(conn: asyncpg.Connection, command_id: str) -> GoldenRunRow | None:
    row = await conn.fetchrow(
        f"SELECT {_GOLDEN_COLUMNS} FROM research_validation_golden_runs WHERE command_id = $1",
        command_id,
    )
    return None if row is None else GoldenRunRow(**row)


async def list_golden_runs(
    conn: asyncpg.Connection,
    *,
    strategy_name: str | None,
    symbol: str | None,
    limit: int,
) -> list[GoldenRunRow]:
    rows = await conn.fetch(
        f"""
        SELECT {_GOLDEN_COLUMNS}
          FROM research_validation_golden_runs
         WHERE ($1::text IS NULL OR strategy_name = $1)
           AND ($2::text IS NULL OR symbol = $2)
         ORDER BY designated_at_ms DESC, id DESC
         LIMIT $3
        """,
        strategy_name,
        symbol,
        limit,
    )
    return [GoldenRunRow(**row) for row in rows]


async def insert_golden_run(
    conn: asyncpg.Connection,
    *,
    source_run_id: int,
    command_id: str,
    command_sha256: str,
    label: str | None,
    strategy_name: str,
    symbol: str,
    validation_case_json: str,
    case_sha256: str,
    rationale: str,
    designated_by: str,
    designated_at_ms: int,
) -> GoldenRunRow | None:
    row = await conn.fetchrow(
        f"""
        INSERT INTO research_validation_golden_runs (
            source_run_id, command_id, command_sha256, label, strategy_name, symbol,
            validation_case_json, case_sha256, rationale, designated_by, designated_at_ms
        ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11)
        ON CONFLICT DO NOTHING
        RETURNING {_GOLDEN_COLUMNS}
        """,
        source_run_id,
        command_id,
        command_sha256,
        label,
        strategy_name,
        symbol,
        validation_case_json,
        case_sha256,
        rationale,
        designated_by,
        designated_at_ms,
    )
    return None if row is None else GoldenRunRow(**row)


async def get_review_by_command(conn: asyncpg.Connection, command_id: str) -> GoldenReviewRow | None:
    row = await conn.fetchrow(
        f"SELECT {_REVIEW_COLUMNS} FROM research_golden_validation_reviews WHERE command_id = $1",
        command_id,
    )
    return None if row is None else GoldenReviewRow(**row)


async def list_reviews(conn: asyncpg.Connection, golden_run_id: int) -> list[GoldenReviewRow]:
    rows = await conn.fetch(
        f"""
        SELECT {_REVIEW_COLUMNS}
          FROM research_golden_validation_reviews
         WHERE golden_run_id = $1
         ORDER BY reviewed_at_ms DESC, id DESC
        """,
        golden_run_id,
    )
    return [GoldenReviewRow(**row) for row in rows]


async def parity_verdict_for_case(
    conn: asyncpg.Connection,
    parity_group_id: str,
    *,
    lock: bool = False,
) -> asyncpg.Record | None:
    lock_clause = " FOR SHARE" if lock else ""
    return await conn.fetchrow(
        """
        SELECT id, parity_group_id, left_run_id, right_run_id, verdict_version,
               status, verdict_json::text AS verdict_json, created_at_ms
          FROM research_parity_verdicts
         WHERE parity_group_id = $1
        """
        + lock_clause,
        parity_group_id,
    )


async def insert_review(
    conn: asyncpg.Connection,
    *,
    golden_run_id: int,
    command_id: str,
    command_sha256: str,
    expected_evidence_revision: str,
    decision: str,
    classification: str | None,
    evidence_state: str,
    parity_verdict_id: int | None,
    evidence_json: str,
    evidence_sha256: str,
    reason: str,
    quantconnect_backtest_id: str | None,
    authorized_program_version: str | None,
    reviewed_by: str,
    reviewed_at_ms: int,
) -> GoldenReviewRow | None:
    row = await conn.fetchrow(
        f"""
        INSERT INTO research_golden_validation_reviews (
            golden_run_id, command_id, command_sha256, expected_evidence_revision,
            decision, classification, evidence_state, parity_verdict_id,
            evidence_json, evidence_sha256, reason, quantconnect_backtest_id,
            authorized_program_version, reviewed_by, reviewed_at_ms
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14, $15)
        ON CONFLICT DO NOTHING
        RETURNING {_REVIEW_COLUMNS}
        """,
        golden_run_id,
        command_id,
        command_sha256,
        expected_evidence_revision,
        decision,
        classification,
        evidence_state,
        parity_verdict_id,
        evidence_json,
        evidence_sha256,
        reason,
        quantconnect_backtest_id,
        authorized_program_version,
        reviewed_by,
        reviewed_at_ms,
    )
    return None if row is None else GoldenReviewRow(**row)


async def is_run_protected(conn: asyncpg.Connection, run_id: int) -> bool:
    """Whether a run is a selected baseline or attached parity evidence."""
    protected = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM research_validation_golden_runs WHERE source_run_id = $1
            UNION ALL
            SELECT 1
              FROM research_validation_golden_runs golden
              JOIN research_parity_verdicts verdict
                ON verdict.parity_group_id = (golden.validation_case_json ->> 'parity_group_id')
             WHERE verdict.left_run_id = $1 OR verdict.right_run_id = $1
            UNION ALL
            SELECT 1
              FROM research_golden_validation_reviews review
              JOIN research_parity_verdicts verdict ON verdict.id = review.parity_verdict_id
             WHERE verdict.left_run_id = $1 OR verdict.right_run_id = $1
        )
        """,
        run_id,
    )
    return bool(protected)
