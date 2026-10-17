"""Real disposable-Postgres receipts, command races, exposure and stale worker fencing."""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import asyncpg
import pytest

from app.options_bots.golden_proof import setup_sha256
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search import service
from app.research.options_golden_search.planning import freeze_protocol
from app.research.options_golden_search.protocol import Knob
from app.research.persistence.fence import StaleAttemptError
from app.schemas.options_golden_search import Command, History, PlanRequest
from app.utils.session_anchors import et_midnight_ms
from tests.options_backtest._market import leg, setup

pytestmark = pytest.mark.asyncio


def _record(study_id: str, underlying: str) -> dict[str, Any]:
    return {"id": study_id, "underlying": underlying, "setup_name": "Exact setup", "protocol_hash": "locked",
            "phase": "research", "status": "idle", "attempt": 0, "job_id": None, "revision": 0,
            "protocol": {"budget": {"max_elapsed_ms": 86400000}}}


async def _running(conn: asyncpg.Connection, unique: str) -> dict[str, Any]:
    row = await repo.create(conn, _record("os_" + unique, unique.upper()), "lock", "same-lock-request")
    row.update(status="queued", stage_token="token", job_id="job")
    await repo.save(conn, row)
    return await repo.claim(conn, row["id"], "token", "job")


async def test_concurrent_lock_retry_returns_one_receipt_and_rejects_changed_request(
    conn: asyncpg.Connection, second_conn: asyncpg.Connection, unique: str,
) -> None:
    value = _record("os_" + unique, unique.upper())
    left, right = await asyncio.gather(
        repo.create(conn, value, "lock", "request"), repo.create(second_conn, value, "lock", "request"),
    )
    assert left == right
    assert await conn.fetchval("SELECT count(*) FROM research_options_search_commands WHERE study_id=$1", value["id"]) == 1
    with pytest.raises(repo.Conflict, match="different request"):
        await repo.create(conn, value, "lock", "changed-request")


async def test_logical_reservation_deduplicates_and_completed_delivery_commits_once(conn: asyncpg.Connection, unique: str) -> None:
    row = await _running(conn, unique)
    identity = {"setup": "exact", "window": "development"}
    assert await repo.reserve(conn, row["id"], row["attempt"], "key", identity, cap=1) is None
    assert await repo.reserve(conn, row["id"], row["attempt"], "key", identity, cap=1) is None
    before = await repo.evaluations(conn, row["id"])
    assert len(before) == 1 and before[0]["execution_attempts"] == 0
    await repo.begin_execution(conn, row["id"], row["attempt"], "key", max_attempts=1)
    result = {"net_profit_usd": "0.12345678901234567890123456789"}
    await repo.complete(conn, row["id"], row["attempt"], "key", result)
    await repo.complete(conn, row["id"], row["attempt"], "key", result)
    assert await repo.reserve(conn, row["id"], row["attempt"], "key", identity, cap=1) == result
    assert (await repo.evaluations(conn, row["id"]))[0]["execution_attempts"] == 1
    with pytest.raises(repo.Conflict, match="cannot change"):
        await repo.complete(conn, row["id"], row["attempt"], "key", {"net_profit_usd": "2"})
    with pytest.raises(repo.Conflict, match="identity"):
        await repo.reserve(conn, row["id"], row["attempt"], "key", {"setup": "different"}, cap=1)
    with pytest.raises(repo.Conflict, match="bound"):
        await repo.reserve(conn, row["id"], row["attempt"], "second", identity, cap=1)


async def test_unreserved_result_cannot_be_committed_as_success(conn: asyncpg.Connection, unique: str) -> None:
    row = await _running(conn, unique)
    with pytest.raises(repo.Conflict, match="reserv"):
        await repo.complete(conn, row["id"], row["attempt"], "never-reserved", {"profit": "1"})


@pytest.mark.parametrize("status", ["cancelled", "interrupted", "queued", "completed"])
async def test_nonrunning_study_refuses_late_worker_result_even_before_a_new_claim(
    conn: asyncpg.Connection, unique: str, status: str,
) -> None:
    row = await _running(conn, unique)
    await repo.reserve(conn, row["id"], row["attempt"], "key", {}, cap=1)
    row["status"] = status
    await repo.save(conn, row)
    with pytest.raises((StaleAttemptError, repo.Conflict)):
        await repo.complete(conn, row["id"], row["attempt"], "key", {"profit": "1"})
    assert (await repo.evaluations(conn, row["id"]))[0]["result"] is None


async def test_new_task_generation_rejects_old_delivery_and_physical_starts_are_bounded(
    conn: asyncpg.Connection, unique: str,
) -> None:
    row = await _running(conn, unique)
    await repo.reserve(conn, row["id"], row["attempt"], "key", {}, cap=1)
    await repo.begin_execution(conn, row["id"], row["attempt"], "key", max_attempts=2)
    await repo.advance_generation(conn, row["id"], row["attempt"], "key", 2)
    with pytest.raises(repo.Conflict, match="generation"):
        await repo.complete(conn, row["id"], row["attempt"], "key", {"profit": "1"}, task_gen=1)
    await repo.begin_execution(conn, row["id"], row["attempt"], "key", max_attempts=2, task_gen=2)
    with pytest.raises(repo.Conflict, match="bound"):
        await repo.begin_execution(conn, row["id"], row["attempt"], "key", max_attempts=2, task_gen=2)
    await repo.complete(conn, row["id"], row["attempt"], "key", {"profit": "2"}, task_gen=2)


async def test_concurrent_reservations_cannot_overdraw_the_unique_evaluation_bound(
    conn: asyncpg.Connection, second_conn: asyncpg.Connection, unique: str,
) -> None:
    row = await _running(conn, unique)
    outcomes = await asyncio.gather(
        repo.reserve(conn, row["id"], row["attempt"], "a", {"point": "a"}, cap=1),
        repo.reserve(second_conn, row["id"], row["attempt"], "b", {"point": "b"}, cap=1),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, repo.Conflict) for outcome in outcomes) == 1
    assert len(await repo.evaluations(conn, row["id"])) == 1


async def test_two_overlapping_final_reservations_serialize_underlying_exposure(
    conn: asyncpg.Connection, second_conn: asyncpg.Connection, unique: str,
) -> None:
    symbol = unique.upper()
    waiting = asyncio.Event()

    async def second() -> str:
        async with second_conn.transaction():
            waiting.set()
            return await repo.reserve_exposure(second_conn, symbol, 150, 250, "b_" + unique)

    async with conn.transaction():
        assert await repo.reserve_exposure(conn, symbol, 100, 200, "a_" + unique) == "not_opened"
        task = asyncio.create_task(second())
        await waiting.wait()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
    assert await asyncio.wait_for(task, timeout=5) == "previously_used"
    assert await repo.exposure(conn, symbol, 250, 300) == "not_opened"  # half-open touching interval


def _command_record(unique: str) -> dict[str, Any]:
    written = setup(leg(stop=("percent", "10")))
    start_ms, end_ms = et_midnight_ms(date(2024, 1, 1)), et_midnight_ms(date(2025, 7, 1))
    knobs = (Knob(path="legs.call.stop_loss.value", values=("10", "20")),)
    protocol = freeze_protocol(written, knobs, start_ms=start_ms, end_ms=end_ms, identities={"selection": "test"})
    request = PlanRequest(setup_name="saved " + unique, expected_setup_hash=setup_sha256(written), knobs=knobs,
                          history=History(start_ms=start_ms, end_ms=end_ms))
    row = service.initial_record("os_" + unique, request, protocol, {})
    row["underlying"] = unique.upper()
    return row


def _candidate(digest: str) -> dict[str, Any]:
    return {"candidate_hash": digest, "label": "Frozen candidate", "is_original": True,
            "verdict": "meets_rules", "net_profit_usd": "1", "drawdown_usd": "0", "drawdown_fraction": "0",
            "drawdown_percent": "0", "sharpe": 2, "completed_sessions": 30, "recovery": "0 sessions",
            "gate_outcomes": [], "evidence": [], "monthly": [], "equity": [], "sessions": []}


def _connect_commands(monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection) -> None:
    @asynccontextmanager
    async def connection() -> AsyncIterator[asyncpg.Connection]:
        yield conn

    monkeypatch.setattr(repo, "connection", connection)
    monkeypatch.setattr(service, "execution_identity", lambda: {"selection": "test"})


async def test_open_final_commits_shortlist_exposure_dispatch_and_retry_receipt_together(
    conn: asyncpg.Connection, unique: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _command_record(unique)
    digest = setup_sha256(setup(leg(stop=("percent", "10"))))
    row.update(phase="compare", status="completed", candidates=[_candidate(digest)])
    await repo.create(conn, row, "lock", "request")
    await repo.save(conn, row)
    _connect_commands(monkeypatch, conn)
    command = Command(command="open_final", expected_revision=1, idempotency_key="open", candidate_hashes=(digest,))
    result = await service.command(row["id"], command)
    retried = await service.command(row["id"], command)
    assert retried == result
    stored = await repo.get(conn, row["id"])
    assert stored["finalists"] == [digest]
    assert stored["exposure"]["classification"] == "not_opened"
    assert stored["phase"] == "final" and stored["status"] == "queued"
    assert stored["dispatch"]["stage_token"] == stored["stage_token"]
    assert await conn.fetchval("SELECT count(*) FROM research_options_search_exposures WHERE owner_id=$1", row["id"]) == 1


async def test_failed_open_final_transaction_rolls_back_exposure_and_shortlist(
    conn: asyncpg.Connection, unique: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _command_record(unique)
    digest = setup_sha256(setup(leg(stop=("percent", "10"))))
    row.update(phase="compare", status="completed", candidates=[_candidate(digest)])
    await repo.create(conn, row, "lock", "request")
    await repo.save(conn, row)
    _connect_commands(monkeypatch, conn)
    before = copy.deepcopy(await repo.get(conn, row["id"]))

    async def fail_receipt(*args: object) -> None:
        raise RuntimeError("receipt storage unavailable")

    monkeypatch.setattr(repo, "record_command", fail_receipt)
    with pytest.raises(RuntimeError, match="receipt storage"):
        await service.command(row["id"], Command(command="open_final", expected_revision=1, idempotency_key="open", candidate_hashes=(digest,)))
    assert await repo.get(conn, row["id"]) == before
    assert await conn.fetchval("SELECT count(*) FROM research_options_search_exposures WHERE owner_id=$1", row["id"]) == 0


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_revision_checked_commands_commit_once(
    conn: asyncpg.Connection, unique: str, monkeypatch: pytest.MonkeyPatch, same_key: bool,
) -> None:
    row = _command_record(unique)
    await repo.create(conn, row, "lock", "request")

    @asynccontextmanager
    async def connection() -> AsyncIterator[asyncpg.Connection]:
        opened = await asyncpg.connect(os.environ["POSTGRES_URL"])
        try:
            yield opened
        finally:
            await opened.close()

    monkeypatch.setattr(repo, "connection", connection)
    monkeypatch.setattr(service, "execution_identity", lambda: {"selection": "test"})
    left = Command(command="run_research", expected_revision=0, idempotency_key="run")
    right = left if same_key else left.model_copy(update={"idempotency_key": "another-run"})
    results = await asyncio.gather(service.command(row["id"], left), service.command(row["id"], right), return_exceptions=True)
    if same_key:
        assert results[0] == results[1]
    else:
        assert sum(isinstance(result, repo.Conflict) for result in results) == 1
    stored = await repo.get(conn, row["id"])
    assert stored["status"] == "queued" and stored["revision"] == 1
    assert await conn.fetchval("SELECT count(*) FROM research_options_search_commands WHERE study_id=$1", row["id"]) == 2


async def test_exposure_and_decision_history_cannot_be_rewritten_or_deleted(conn: asyncpg.Connection, unique: str) -> None:
    row = await _running(conn, unique)
    await repo.trial(conn, row["id"], row["attempt"], {"exact_point": "original"})
    async with conn.transaction():
        await repo.reserve_exposure(conn, row["underlying"], 100, 200, row["id"])
    for table, where in (
        ("research_options_search_commands", "study_id"),
        ("research_options_search_trials", "study_id"),
        ("research_options_search_exposures", "owner_id"),
    ):
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await conn.execute(f"DELETE FROM {table} WHERE {where}=$1", row["id"])


async def test_research_reserves_development_visibility_and_resume_fences_late_dispatch(
    conn: asyncpg.Connection, unique: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _command_record(unique)
    await repo.create(conn, row, "lock", "request")
    _connect_commands(monkeypatch, conn)
    launch = Command(command="run_research", expected_revision=0, idempotency_key="run")
    original_receipt = await service.command(row["id"], launch)
    protocol = row["protocol"]
    development = protocol["development"]
    assert await repo.exposure(conn, row["underlying"], development["start_ms"], development["end_ms"], owner="another-study") == "previously_used"
    assert await repo.exposure(conn, row["underlying"], protocol["final"]["start_ms"], protocol["final"]["end_ms"], owner="another-study") == "not_opened"
    assert "resume" in original_receipt.allowed_commands
    resumed = await service.command(row["id"], Command(command="resume", expected_revision=1, idempotency_key="resume"))
    assert resumed.dispatch.stage_token != original_receipt.dispatch.stage_token
    with pytest.raises(repo.Conflict, match="not authorized"):
        await repo.bind(conn, row["id"], original_receipt.dispatch.stage_token, "late-old-job")
    assert await repo.bind(conn, row["id"], resumed.dispatch.stage_token, "new-job")
    assert await service.command(row["id"], launch) == original_receipt
    assert (await repo.get(conn, row["id"]))["job_id"] == "new-job"
    assert await conn.fetchval("SELECT count(*) FROM research_options_search_exposures WHERE owner_id=$1", row["id"]) == 1


async def test_history_summaries_exclude_retained_heavy_study_document(conn: asyncpg.Connection, unique: str) -> None:
    row = _record("os_" + unique, unique.upper())
    row["retained_heavy_document"] = {"trace": ["x" * 1000] * 100}
    await repo.create(conn, row, "lock", "request")
    summary = next(item for item in await repo.summaries(conn) if item["id"] == row["id"])
    assert set(summary) == {"id", "setup_name", "underlying", "phase", "status", "updated_at_ms"}
    assert summary["setup_name"] == row["setup_name"]
