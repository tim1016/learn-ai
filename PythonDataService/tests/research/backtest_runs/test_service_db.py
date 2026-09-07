"""The service's writes as a reader sees them, on the ephemeral database.

``persist_run_payload`` runs the repository write on the shared writer loop;
these tests read the outcome back through an ordinary connection, so a
persisted LEAN companion is judged by the verdict row it leaves, not by which
functions were called.
"""

from __future__ import annotations

import asyncio

import pytest

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs import service
from app.research.persistence import db
from tests.research.backtest_runs.payloads import engine_payload, lean_payload

pytestmark = pytest.mark.asyncio


async def test_a_lean_companion_persisted_through_the_service_freezes_its_group(conn, unique: str) -> None:
    group = f"pg-{unique}"
    left = await service.persist_run_payload(
        engine_payload(symbol=unique, parity_group_id=group, requested_engine="both")
    )
    assert left is not None
    await asyncio.to_thread(
        service.record_parity_disposition_sync,
        parity_group_id=group,
        left_run_id=left,
        status="pending",
        verdict_json="{}",
    )
    assert (await repo.get_parity_verdict(conn, group)).status == "pending"

    right = await service.persist_run_payload(
        lean_payload(f"companion-{group}", symbol=unique, parity_group_id=group, requested_engine="both")
    )

    assert right is not None
    verdict = await repo.get_parity_verdict(conn, group)
    assert verdict is not None and verdict.status in {"agree", "diverged", "unavailable"}
    assert verdict.left_run_id == left and verdict.right_run_id == right
    assert (await repo.get_run(conn, right)).lean_run_id == f"companion-{group}"


async def test_an_engine_run_with_a_group_leaves_no_verdict_until_its_companion_lands(conn, unique: str) -> None:
    group = f"pg-{unique}"

    left = await service.persist_run_payload(
        engine_payload(symbol=unique, parity_group_id=group, requested_engine="both")
    )

    assert left is not None
    assert await repo.get_parity_verdict(conn, group) is None


async def test_marking_a_group_failed_through_the_service_transitions_only_a_pending_verdict(conn, unique: str) -> None:
    group = f"pg-{unique}"
    left = await service.persist_run_payload(
        engine_payload(symbol=unique, parity_group_id=group, requested_engine="both")
    )
    assert left is not None
    await asyncio.to_thread(
        service.record_parity_disposition_sync,
        parity_group_id=group,
        left_run_id=left,
        status="unavailable",
        verdict_json="{}",
    )

    await asyncio.to_thread(
        service.mark_parity_failed_sync, group, status="run_failed", detail="LEAN exited with code 1"
    )

    assert (await repo.get_parity_verdict(conn, group)).status == "unavailable"  # already terminal: untouched


async def test_a_companion_that_produced_no_result_settles_its_group_at_run_failed(conn, unique: str) -> None:
    """#1977: the group used to sit at ``pending`` for ever and the report polled it for ever."""
    group = f"pg-{unique}"
    left = await service.persist_run_payload(
        engine_payload(symbol=unique, parity_group_id=group, requested_engine="both")
    )
    assert left is not None
    await asyncio.to_thread(
        service.record_parity_disposition_sync,
        parity_group_id=group,
        left_run_id=left,
        status="pending",
        verdict_json="{}",
    )

    right = await service.persist_run_payload(
        lean_payload(
            f"companion-{group}",
            symbol=unique,
            parity_group_id=group,
            requested_engine="both",
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            total_pnl=0.0,
            win_rate=0.0,
            trades=[],
            parity_failure_detail="No normalized/result.json — LEAN run did not produce output",
        )
    )

    assert right is not None  # the failed row still persists into run history
    verdict = await repo.get_parity_verdict(conn, group)
    assert verdict is not None and verdict.status == "run_failed"
    assert "No normalized/result.json" in verdict.verdict_json


async def test_a_landed_companion_supersedes_the_dispatch_failure_that_said_none_was_coming(
    conn, unique: str
) -> None:
    """#1977: a companion read timeout marked the group failed while the run was still going."""
    group = f"pg-{unique}"
    left = await service.persist_run_payload(
        engine_payload(symbol=unique, parity_group_id=group, requested_engine="both")
    )
    assert left is not None
    await asyncio.to_thread(
        service.record_parity_disposition_sync,
        parity_group_id=group,
        left_run_id=left,
        status="pending",
        verdict_json="{}",
    )
    await asyncio.to_thread(
        service.mark_parity_failed_sync, group, status="run_failed", detail="companion dispatch failed: ReadTimeout"
    )

    right = await service.persist_run_payload(
        lean_payload(f"companion-{group}", symbol=unique, parity_group_id=group, requested_engine="both")
    )

    assert right is not None
    verdict = await repo.get_parity_verdict(conn, group)
    assert verdict is not None and verdict.status in {"agree", "diverged"}
    assert verdict.right_run_id == right


async def test_the_settle_completes_after_the_caller_has_stopped_waiting(
    conn, unique: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1977: the ADR's claim, exercised — not asserted through a private name.

    ``run_sync`` does not cancel on timeout, so an insert that outruns the
    caller's budget still commits *and* still settles its group on the writer
    loop. Before the settle was chained onto the insert it was a second hop
    from the calling thread, and that hop never ran.
    """
    group = f"pg-{unique}"
    left = await service.persist_run_payload(
        engine_payload(symbol=unique, parity_group_id=group, requested_engine="both")
    )
    assert left is not None
    await asyncio.to_thread(
        service.record_parity_disposition_sync,
        parity_group_id=group,
        left_run_id=left,
        status="pending",
        verdict_json="{}",
    )

    slow_insert = repo.insert_run

    async def insert_after_the_caller_gives_up(connection, record):
        await asyncio.sleep(0.3)
        return await slow_insert(connection, record)

    monkeypatch.setattr(repo, "insert_run", insert_after_the_caller_gives_up)
    monkeypatch.setattr(db, "DB_CALL_TIMEOUT_SECONDS", 0.05)

    run_id = await service.persist_run_payload(
        lean_payload(f"companion-{group}", symbol=unique, parity_group_id=group, requested_engine="both")
    )

    assert run_id is None  # the caller was told nothing, not that it failed
    for _ in range(100):
        verdict = await repo.get_parity_verdict(conn, group)
        if verdict is not None and verdict.status != "pending":
            break
        await asyncio.sleep(0.05)
    assert verdict is not None and verdict.status in {"agree", "diverged"}
    assert verdict.right_run_id is not None  # the row landed and settled its own group
