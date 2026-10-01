"""Seed a Golden Search qualified version against the real research tables, foreign keys and all (#2696).

A qualification cites a study row and an accepted Golden Validation review,
so seeding one means writing those first: a saved engine run, its
designation and review, a minimal study row, then the qualification and,
optionally, the stock's default pointer. Live Postgres only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.backtest_runs import repository as backtest_repo
from app.research.backtest_runs.records import record_from_payload
from app.research.golden_search.qualifications import QualificationRow, insert_qualification, set_default_cas
from app.research.golden_validation import service as golden_validation
from tests.research.backtest_runs.payloads import engine_payload

PROGRAM = "ema_crossover_signal"
CONTRACT = _STRATEGY_REGISTRY[PROGRAM].signal_program_contract
assert CONTRACT is not None


def canonical_point(symbol: str, **overrides: object) -> dict[str, Any]:
    """The EMA program's canonical point for ``symbol``: validated settings plus ``overrides``."""
    return (
        _STRATEGY_REGISTRY[PROGRAM]
        .param_schema.model_validate({**CONTRACT.validated_settings, **overrides, "symbol": symbol})
        .model_dump(mode="json")
    )


async def insert_study(conn: asyncpg.Connection, study_id: str, symbol: str, *, state: str = "approved") -> None:
    await conn.execute(
        """
        INSERT INTO research_golden_search_studies (
            id, strategy_key, symbol, state, created_at_ms, updated_at_ms,
            protocol_json, protocol_hash, receipt_json, budget_cap
        ) VALUES ($1, $2, $3, $4, 1, 1, '{}'::jsonb, $5, '{}'::jsonb, 100)
        """,
        study_id,
        PROGRAM,
        symbol,
        state,
        "h" * 64,
    )


async def accepted_golden_review(conn: asyncpg.Connection, symbol: str, tag: str) -> tuple[int, int]:
    """One accepted Golden Validation review of a saved run: ``(golden_run_id, golden_review_id)``."""
    run_id = (await backtest_repo.insert_run(conn, record_from_payload(engine_payload(symbol=symbol)))).run_id
    designated = await golden_validation.designate(
        conn,
        source_run_id=run_id,
        command_id=f"seed-designate-{tag}",
        label=f"seed {tag}",
        rationale="Seeded for a test.",
        actor="local:owner",
    )
    reviewed = await golden_validation.review(
        conn,
        golden_run_id=designated.golden_run.id,
        command_id=f"seed-review-{tag}",
        expected_evidence_revision=designated.evidence.revision,
        decision="accept",
        reason="Seeded for a test.",
        quantconnect_backtest_id=None,
        authorized_program_version=None,
        actor="local:owner",
    )
    assert reviewed.latest_review is not None
    return designated.golden_run.id, reviewed.latest_review.id


async def seed_qualification(
    conn: asyncpg.Connection,
    *,
    qualification_id: str,
    symbol: str,
    params: Mapping[str, Any],
    artifact_digest: str,
    proof: Mapping[str, Any] | None = None,
    research: Mapping[str, Any] | None = None,
    make_default: bool = False,
    expected_default: str | None = None,
    created_at_ms: int = 1_759_300_000_000,
) -> QualificationRow:
    golden_run_id, golden_review_id = await accepted_golden_review(conn, symbol, qualification_id)
    study_id = f"study-{qualification_id}"
    await insert_study(conn, study_id, symbol)
    row = await insert_qualification(
        conn,
        qualification_id=qualification_id,
        program_key=PROGRAM,
        program_version=CONTRACT.program_version,
        parameter_schema_version=CONTRACT.parameter_schema_version,
        symbol=symbol,
        params=params,
        artifact_digest=artifact_digest,
        wiring_digest="w" * 64,
        study_id=study_id,
        golden_run_id=golden_run_id,
        golden_review_id=golden_review_id,
        proof={"schema_version": 1} if proof is None else proof,
        research={"exam_outcome": "meets_rules", "claim": "confirmatory"} if research is None else research,
        note="Approved after the final test.",
        approved_by="local:owner",
        created_at_ms=created_at_ms,
    )
    if make_default:
        await set_default_cas(
            conn,
            program_key=PROGRAM,
            symbol=symbol,
            qualification_id=row.id,
            expected_qualification_id=expected_default,
            reason="seeded default",
            actor="local:owner",
            now_ms=created_at_ms,
        )
    return row
