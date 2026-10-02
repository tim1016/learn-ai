"""Golden Search qualified versions at rest: records, events, the default pointer (#2696).

Live Postgres only (``POSTGRES_URL_IS_EPHEMERAL=1``). Every qualification
here references a real study row and a real accepted Golden Validation
review, because the table's foreign keys demand them; each test works on a
stock of its own so the suite is safe in parallel.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date

import asyncpg
import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.backtest_runs import repository as backtest_repo
from app.research.backtest_runs.records import record_from_payload
from app.research.golden_search.proof import ProofRecord, ProofWindow
from app.research.golden_search.qualifications import (
    QUALIFICATION_COVERED,
    QUALIFICATION_REVOKED,
    QUALIFICATION_UNVERIFIABLE,
    DefaultChangedError,
    QualificationEventConflictError,
    QualificationRow,
    QualificationSubject,
    append_event,
    events_for,
    find_qualifications,
    get_default,
    get_qualification,
    insert_qualification,
    list_qualifications,
    load_qualification_evidence,
    params_sha256,
    read_qualification_evidence,
    resolve_coverage,
    set_default_cas,
)
from app.research.golden_validation import service as golden_validation
from app.schemas.signal_program_seal import semantic_payload_hash
from app.utils.session_anchors import et_midnight_ms
from tests.research.backtest_runs.payloads import engine_payload

PROGRAM = "ema_crossover_signal"
CONTRACT = _STRATEGY_REGISTRY[PROGRAM].signal_program_contract
assert CONTRACT is not None
RUNNING = "1" * 64


@pytest.fixture
def symbol(unique: str) -> str:
    return f"GS{unique.upper()}"


@pytest.fixture
async def golden_review(conn: asyncpg.Connection, unique: str) -> tuple[int, int]:
    """One accepted Golden Validation review: the (golden_run_id, golden_review_id) a qualification cites."""
    run_id = (await backtest_repo.insert_run(conn, record_from_payload(engine_payload(symbol=unique)))).run_id
    designated = await golden_validation.designate(
        conn,
        source_run_id=run_id,
        command_id=f"gs-designate-{unique}",
        label=f"Golden Search study {unique[:8]}",
        rationale="Approved after the final test.",
        actor="local:owner",
    )
    reviewed = await golden_validation.review(
        conn,
        golden_run_id=designated.golden_run.id,
        command_id=f"gs-review-{unique}",
        expected_evidence_revision=designated.evidence.revision,
        decision="accept",
        reason="Approved after the final test.",
        quantconnect_backtest_id=None,
        authorized_program_version=None,
        actor="local:owner",
    )
    assert reviewed.latest_review is not None
    return reviewed.golden_run.id, reviewed.latest_review.id


def _point(symbol: str, **overrides: float) -> dict:
    return {**CONTRACT.validated_settings, "rsi_min": 30.0, **overrides, "symbol": symbol}


async def _qualify(
    conn: asyncpg.Connection,
    golden_review: tuple[int, int],
    qualification_id: str,
    *,
    params: dict,
    created_at_ms: int = 1_000,
    program_version: str | None = None,
    artifact_digest: str = RUNNING,
) -> QualificationRow:
    study_id = f"study-{qualification_id}"
    await conn.execute(
        """
        INSERT INTO research_golden_search_studies (
            id, strategy_key, symbol, state, created_at_ms, updated_at_ms,
            protocol_json, protocol_hash, receipt_json, budget_cap
        ) VALUES ($1, $2, $3, 'qualification_pending', $4, $4, '{}'::jsonb, $5, '{}'::jsonb, 100)
        """,
        study_id,
        PROGRAM,
        params["symbol"],
        created_at_ms,
        "h" * 64,
    )
    golden_run_id, golden_review_id = golden_review
    return await insert_qualification(
        conn,
        qualification_id=qualification_id,
        program_key=PROGRAM,
        program_version=CONTRACT.program_version if program_version is None else program_version,
        parameter_schema_version=CONTRACT.parameter_schema_version,
        symbol=params["symbol"],
        params=params,
        artifact_digest=artifact_digest,
        wiring_digest="w" * 64,
        study_id=study_id,
        golden_run_id=golden_run_id,
        golden_review_id=golden_review_id,
        proof={"schema_version": 1, "manifest": {"a.zip": "f" * 64}, "params": params},
        research={"exam_outcome": "meets_rules", "claim": "confirmatory", "override": False},
        note="Approved after the final test.",
        approved_by="local:owner",
        created_at_ms=created_at_ms,
    )


async def _history(conn: asyncpg.Connection, symbol: str) -> list[tuple]:
    rows = await conn.fetch(
        """
        SELECT previous_qualification_id, qualification_id, revision, reason
          FROM research_golden_default_history
         WHERE program_key = $1 AND symbol = $2
         ORDER BY id
        """,
        PROGRAM,
        symbol,
    )
    return [tuple(row) for row in rows]


# ---------------------------------------------------------------------------
# Qualifications
# ---------------------------------------------------------------------------
async def test_insert_qualification_round_trips_and_derives_its_hashes(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    point = _point(symbol)

    inserted = await _qualify(conn, golden_review, f"q-{unique}", params=point)
    fetched = await get_qualification(conn, f"q-{unique}")

    assert fetched == inserted
    assert fetched.params == point
    # The derived hash survives the JSONB round trip: the stored point re-hashes to it.
    assert fetched.params_sha256 == params_sha256(point) == params_sha256(fetched.params)
    assert fetched.proof_sha256 == semantic_payload_hash(fetched.proof)
    assert (fetched.golden_run_id, fetched.golden_review_id) == golden_review
    assert await get_qualification(conn, f"absent-{unique}") is None


async def test_insert_qualification_stores_a_proof_record_that_reads_back_to_its_own_hash(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    # A re-proof reads the stored proof back: it must rebuild the record that
    # was approved, and that record must still hash to proof_sha256.
    point = _point(symbol, gap_bps=0.0)
    window = ProofWindow(
        start_ms=et_midnight_ms(date(2025, 2, 4)),
        end_ms=et_midnight_ms(date(2025, 2, 7)),
        warmup_from_ms=et_midnight_ms(date(2025, 2, 3)),
    )
    record = ProofRecord(
        program_key=PROGRAM,
        program_version=CONTRACT.program_version,
        symbol=symbol,
        params=point,
        window=window,
        adjusted=True,
        manifest={"equity/usa/minute/x/20250204_trade.zip": "f" * 64, "adjustment_versions/x.json": "e" * 64},
        lake_trace_root="d" * 64,
        restored_trace_root="d" * 64,
        trace_count=104,
        artifact_digest=RUNNING,
        wiring_digest="w" * 64,
        created_at_ms=1_738_800_000_000,
    )
    study_id = f"study-{unique}"
    await conn.execute(
        """
        INSERT INTO research_golden_search_studies (
            id, strategy_key, symbol, state, created_at_ms, updated_at_ms,
            protocol_json, protocol_hash, receipt_json, budget_cap
        ) VALUES ($1, $2, $3, 'qualification_pending', 1, 1, '{}'::jsonb, $4, '{}'::jsonb, 100)
        """,
        study_id,
        PROGRAM,
        symbol,
        "h" * 64,
    )

    stored = await insert_qualification(
        conn,
        qualification_id=f"q-{unique}",
        program_key=PROGRAM,
        program_version=CONTRACT.program_version,
        parameter_schema_version=CONTRACT.parameter_schema_version,
        symbol=symbol,
        params=point,
        artifact_digest=RUNNING,
        wiring_digest="w" * 64,
        study_id=study_id,
        golden_run_id=golden_review[0],
        golden_review_id=golden_review[1],
        proof=record.as_dict(),
        research={},
        note="Approved after the final test.",
        approved_by="local:owner",
        created_at_ms=1_000,
    )

    assert stored.proof_sha256 == record.sha256()
    assert ProofRecord.from_dict(stored.proof) == record


async def test_insert_qualification_refuses_a_point_storage_would_rewrite(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    # JSONB reads -0.0 back as 0.0: stored, this point could never be found by its own hash.
    with pytest.raises(ValueError, match="does not survive storage"):
        await _qualify(conn, golden_review, f"q-{unique}", params=_point(symbol, gap_bps=-0.0))

    assert await get_qualification(conn, f"q-{unique}") is None


async def test_insert_qualification_refuses_parameters_for_another_symbol(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    with pytest.raises(ValueError, match="name symbol"):
        await insert_qualification(
            conn,
            qualification_id=f"q-{unique}",
            program_key=PROGRAM,
            program_version=CONTRACT.program_version,
            parameter_schema_version=CONTRACT.parameter_schema_version,
            symbol=symbol,
            params=_point("QQQ"),
            artifact_digest=RUNNING,
            wiring_digest="w" * 64,
            study_id=f"study-{unique}",
            golden_run_id=golden_review[0],
            golden_review_id=golden_review[1],
            proof={},
            research={},
            note="Approved after the final test.",
            approved_by="local:owner",
            created_at_ms=1_000,
        )


async def test_find_qualifications_returns_the_exact_tuple_newest_first(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    point = _point(symbol)
    older = await _qualify(conn, golden_review, f"q-old-{unique}", params=point, created_at_ms=1_000)
    newer = await _qualify(conn, golden_review, f"q-new-{unique}", params=point, created_at_ms=2_000)
    await _qualify(conn, golden_review, f"q-other-{unique}", params=_point(symbol, rsi_min=31.0))
    retired = await _qualify(
        conn,
        golden_review,
        f"q-v0-{unique}",
        params=point,
        created_at_ms=500,
        program_version="ema-crossover-signal/v0",
    )

    every_version = await find_qualifications(
        conn, program_key=PROGRAM, symbol=symbol, params_sha256=params_sha256(point)
    )
    current = await find_qualifications(
        conn,
        program_key=PROGRAM,
        symbol=symbol,
        params_sha256=params_sha256(point),
        program_version=CONTRACT.program_version,
    )

    assert [row.id for row in every_version] == [newer.id, older.id, retired.id]
    assert [row.id for row in current] == [newer.id, older.id]


async def test_list_qualifications_narrows_by_program_and_symbol(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    mine = await _qualify(conn, golden_review, f"q-{unique}", params=_point(symbol))
    other = await _qualify(conn, golden_review, f"q-x-{unique}", params=_point(f"{symbol}X"))

    assert [row.id for row in await list_qualifications(conn, program_key=PROGRAM, symbol=symbol)] == [mine.id]
    # Narrowed per stock rather than listed whole, so a shared database's other rows never crowd it out.
    assert [row.id for row in await list_qualifications(conn, program_key=PROGRAM, symbol=f"{symbol}X")] == [other.id]
    assert await list_qualifications(conn, program_key="sma_crossover", symbol=symbol) == []


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
async def test_events_for_returns_each_qualifications_events_in_append_order(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    reproved = await _qualify(conn, golden_review, f"q-{unique}", params=_point(symbol))
    untouched = await _qualify(conn, golden_review, f"q-quiet-{unique}", params=_point(symbol, rsi_min=31.0))
    first = await append_event(
        conn,
        qualification_id=reproved.id,
        kind="reproved",
        actor="local:owner",
        command_id=f"reprove-1-{unique}",
        created_at_ms=2_000,
        artifact_digest="2" * 64,
        wiring_digest="w" * 64,
        proof={"schema_version": 1},
    )
    second = await append_event(
        conn,
        qualification_id=reproved.id,
        kind="revoked",
        actor="local:owner",
        command_id=f"revoke-{unique}",
        created_at_ms=3_000,
        reason="Parameters withdrawn.",
    )

    events = await events_for(conn, [reproved.id, untouched.id])

    assert events == {reproved.id: [first, second], untouched.id: []}
    assert first.proof == {"schema_version": 1}
    assert await events_for(conn, []) == {}


async def test_append_event_replays_a_command_and_refuses_a_conflicting_reuse(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    row = await _qualify(conn, golden_review, f"q-{unique}", params=_point(symbol))
    command = {"qualification_id": row.id, "kind": "revoked", "actor": "local:owner", "command_id": f"revoke-{unique}"}

    recorded = await append_event(conn, **command, created_at_ms=2_000, reason="Parameters withdrawn.")
    replayed = await append_event(conn, **command, created_at_ms=9_000, reason="Parameters withdrawn.")

    assert replayed == recorded
    with pytest.raises(QualificationEventConflictError, match="different event"):
        await append_event(conn, **command, created_at_ms=9_000, reason="A different reason.")
    assert len((await events_for(conn, [row.id]))[row.id]) == 1


async def test_append_event_requires_the_facts_of_its_kind(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    row = await _qualify(conn, golden_review, f"q-{unique}", params=_point(symbol))

    with pytest.raises(ValueError, match="records its reason"):
        await append_event(
            conn,
            qualification_id=row.id,
            kind="revoked",
            actor="a",
            command_id=f"c1-{unique}",
            created_at_ms=1,
            reason=" ",
        )
    with pytest.raises(ValueError, match="artifact digest, wiring digest and proof"):
        await append_event(
            conn, qualification_id=row.id, kind="reproved", actor="a", command_id=f"c2-{unique}", created_at_ms=1
        )


# ---------------------------------------------------------------------------
# Default pointer
# ---------------------------------------------------------------------------
async def test_set_default_cas_creates_moves_and_clears_the_pointer_with_history(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    first = await _qualify(conn, golden_review, f"q-1-{unique}", params=_point(symbol))
    second = await _qualify(conn, golden_review, f"q-2-{unique}", params=_point(symbol, rsi_min=31.0))
    assert await get_default(conn, PROGRAM, symbol) is None

    created = await set_default_cas(
        conn,
        program_key=PROGRAM,
        symbol=symbol,
        qualification_id=first.id,
        expected_qualification_id=None,
        reason="approved",
        actor="local:owner",
        now_ms=10,
    )
    moved = await set_default_cas(
        conn,
        program_key=PROGRAM,
        symbol=symbol,
        qualification_id=second.id,
        expected_qualification_id=first.id,
        reason="approved",
        actor="local:owner",
        now_ms=20,
    )
    cleared = await set_default_cas(
        conn,
        program_key=PROGRAM,
        symbol=symbol,
        qualification_id=None,
        expected_qualification_id=second.id,
        reason="revoked",
        actor="local:owner",
        now_ms=30,
    )

    assert (created, moved, cleared) == (1, 2, 3)
    pointer = await get_default(conn, PROGRAM, symbol)
    assert (pointer.qualification_id, pointer.revision, pointer.updated_at_ms) == (None, 3, 30)
    assert await _history(conn, symbol) == [
        (None, first.id, 1, "approved"),
        (first.id, second.id, 2, "approved"),
        (second.id, None, 3, "revoked"),
    ]


async def test_set_default_cas_refuses_a_stale_expectation_and_changes_nothing(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    first = await _qualify(conn, golden_review, f"q-1-{unique}", params=_point(symbol))
    second = await _qualify(conn, golden_review, f"q-2-{unique}", params=_point(symbol, rsi_min=31.0))
    await set_default_cas(
        conn,
        program_key=PROGRAM,
        symbol=symbol,
        qualification_id=first.id,
        expected_qualification_id=None,
        reason="approved",
        actor="local:owner",
        now_ms=10,
    )

    with pytest.raises(DefaultChangedError) as excinfo:
        await set_default_cas(
            conn,
            program_key=PROGRAM,
            symbol=symbol,
            qualification_id=second.id,
            expected_qualification_id=None,
            reason="approved",
            actor="local:owner",
            now_ms=20,
        )

    assert (excinfo.value.expected, excinfo.value.current) == (None, first.id)
    pointer = await get_default(conn, PROGRAM, symbol)
    assert (pointer.qualification_id, pointer.revision) == (first.id, 1)
    assert len(await _history(conn, symbol)) == 1


async def test_set_default_cas_refuses_a_qualification_of_another_stock(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    elsewhere = await _qualify(conn, golden_review, f"q-{unique}", params=_point(f"{symbol}X"))

    with pytest.raises(ValueError, match="not ema_crossover_signal on"):
        await set_default_cas(
            conn,
            program_key=PROGRAM,
            symbol=symbol,
            qualification_id=elsewhere.id,
            expected_qualification_id=None,
            reason="approved",
            actor="local:owner",
            now_ms=10,
        )
    assert await get_default(conn, PROGRAM, symbol) is None


async def test_set_default_cas_refuses_a_revoked_qualification(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    revoked = await _qualify(conn, golden_review, f"q-{unique}", params=_point(symbol))
    await append_event(
        conn,
        qualification_id=revoked.id,
        kind="revoked",
        actor="local:owner",
        command_id=f"revoke-{unique}",
        created_at_ms=2_000,
        reason="Parameters withdrawn.",
    )

    with pytest.raises(ValueError, match="revoked"):
        await set_default_cas(
            conn,
            program_key=PROGRAM,
            symbol=symbol,
            qualification_id=revoked.id,
            expected_qualification_id=None,
            reason="approved",
            actor="local:owner",
            now_ms=10,
        )


async def test_set_default_cas_lets_exactly_one_of_two_concurrent_first_pointers_win(
    conn: asyncpg.Connection, second_conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    first = await _qualify(conn, golden_review, f"q-1-{unique}", params=_point(symbol))
    second = await _qualify(conn, golden_review, f"q-2-{unique}", params=_point(symbol, rsi_min=31.0))

    async with conn.transaction():
        await set_default_cas(
            conn,
            program_key=PROGRAM,
            symbol=symbol,
            qualification_id=first.id,
            expected_qualification_id=None,
            reason="approved",
            actor="local:owner",
            now_ms=10,
        )
        racing = asyncio.create_task(
            set_default_cas(
                second_conn,
                program_key=PROGRAM,
                symbol=symbol,
                qualification_id=second.id,
                expected_qualification_id=None,
                reason="approved",
                actor="local:owner",
                now_ms=11,
            )
        )
        await asyncio.sleep(0.3)
        # The second writer is held on the first pointer's uncommitted key, not past it.
        assert not racing.done()

    with pytest.raises(DefaultChangedError) as excinfo:
        await racing
    assert excinfo.value.current == first.id
    pointer = await get_default(conn, PROGRAM, symbol)
    assert (pointer.qualification_id, pointer.revision) == (first.id, 1)
    assert len(await _history(conn, symbol)) == 1


async def test_set_default_cas_inside_a_rolled_back_approval_leaves_no_pointer(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    row = await _qualify(conn, golden_review, f"q-{unique}", params=_point(symbol))

    with pytest.raises(RuntimeError, match="later approval step failed"):
        async with conn.transaction():
            await set_default_cas(
                conn,
                program_key=PROGRAM,
                symbol=symbol,
                qualification_id=row.id,
                expected_qualification_id=None,
                reason="approved",
                actor="local:owner",
                now_ms=10,
            )
            raise RuntimeError("a later approval step failed")

    assert await get_default(conn, PROGRAM, symbol) is None
    assert await _history(conn, symbol) == []


# ---------------------------------------------------------------------------
# Coverage over the store
# ---------------------------------------------------------------------------
async def test_resolve_coverage_reads_a_ready_qualification_then_its_revocation(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    point = _point(symbol)
    row = await _qualify(conn, golden_review, f"q-{unique}", params=point)

    async def lookup(subject: QualificationSubject):
        return await read_qualification_evidence(conn, subject)

    async def resolve():
        return await resolve_coverage(
            program_key=PROGRAM, contract=CONTRACT, params=point, running_artifact_digest=RUNNING, lookup=lookup
        )

    ready = await resolve()
    await append_event(
        conn,
        qualification_id=row.id,
        kind="revoked",
        actor="local:owner",
        command_id=f"revoke-{unique}",
        created_at_ms=2_000,
        reason="Parameters withdrawn.",
    )
    revoked = await resolve()

    assert (ready.state, ready.qualification_id, ready.explanation) == ("COVERED", row.id, QUALIFICATION_COVERED)
    assert (revoked.state, revoked.qualification_id, revoked.explanation) == ("UNCOVERED", None, QUALIFICATION_REVOKED)


async def test_load_qualification_evidence_reads_through_the_shared_research_pool(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str, monkeypatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "POSTGRES_URL", os.environ["POSTGRES_URL"])
    point = _point(symbol)
    row = await _qualify(conn, golden_review, f"q-{unique}", params=point)

    evidence = await load_qualification_evidence(
        QualificationSubject(
            program_key=PROGRAM,
            program_version=CONTRACT.program_version,
            symbol=symbol,
            params_sha256=params_sha256(point),
        )
    )

    assert [(item.qualification.id, item.events) for item in evidence] == [(row.id, ())]


async def test_resolve_coverage_over_a_closed_connection_fails_closed(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    point = _point(symbol)
    await _qualify(conn, golden_review, f"q-{unique}", params=point)
    closed = await asyncpg.connect(os.environ["POSTGRES_URL"])
    await closed.close()

    async def lookup(subject: QualificationSubject):
        return await read_qualification_evidence(closed, subject)

    coverage = await resolve_coverage(
        program_key=PROGRAM, contract=CONTRACT, params=point, running_artifact_digest=RUNNING, lookup=lookup
    )

    assert (coverage.state, coverage.qualification_id, coverage.explanation) == (
        "UNCOVERED",
        None,
        QUALIFICATION_UNVERIFIABLE,
    )


async def test_read_qualification_evidence_joins_a_callers_transaction(
    conn: asyncpg.Connection, golden_review: tuple[int, int], unique: str, symbol: str
) -> None:
    point = _point(symbol)
    subject = QualificationSubject(
        program_key=PROGRAM, program_version=CONTRACT.program_version, symbol=symbol, params_sha256=params_sha256(point)
    )

    async with conn.transaction():
        row = await _qualify(conn, golden_review, f"q-{unique}", params=point)
        evidence = await read_qualification_evidence(conn, subject)

    assert [item.qualification.id for item in evidence] == [row.id]
