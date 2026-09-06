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
