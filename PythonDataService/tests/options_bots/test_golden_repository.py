"""Source evidence retention and idempotent approval/event writes on disposable PostgreSQL."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import asyncpg
import pytest

from app.options_backtest.saved_runs import DeleteOutcome, NewRun, complete_run, create_run, delete_run
from app.options_backtest.saved_setups import save_setup
from app.options_bots import golden_repository as repository
from app.options_bots.golden_proof import proof_sha256, setup_sha256
from app.options_bots.golden_repository import CommandConflict, GoldenEvent, GoldenRecord
from app.routers.options_backtest import _listed_setups
from tests.options_backtest._a_run import a_busy_run
from tests.research.conftest import conn as conn
from tests.research.conftest import unique as unique


async def test_qualification_retains_source_evidence_and_commands_cannot_be_repurposed(
    conn: asyncpg.Connection, unique: str,
) -> None:
    result = a_busy_run()
    run_id = await create_run(conn, NewRun(
        job_id="golden-options-" + unique, created_at_ms=1, setup=result.setup, setup_name="Tested setup",
        first_session_open_ms=result.sessions[0].session_open_ms,
        last_session_open_ms=result.sessions[-1].session_open_ms, sessions_requested=len(result.sessions),
    ))
    await complete_run(conn, run_id, result, finished_at_ms=2)
    proof = {"rules_digest": "rules-v1", "result": "source-evidence"}
    record = GoldenRecord(
        id="ogs_" + unique, name="Tested setup", source_run_id=run_id, setup=result.setup.model_dump(mode="json"),
        setup_sha256=setup_sha256(result.setup), rules_digest="rules-v1", proof=proof, proof_sha256=proof_sha256(proof),
        evidence={}, note="Reviewed exact setup", approved_by="owner", created_at_ms=3,
        command_id="approval-" + unique, command_sha256="request-a",
    )
    try:
        stored = await repository.insert_record(conn, record)
        retried = await repository.insert_record(conn, replace(record, id="ogs_retry_" + unique))
        assert stored.id == retried.id == record.id
        assert await delete_run(conn, run_id, is_live=lambda _job: False) == DeleteOutcome.RETAINED
        assert await conn.fetchval("SELECT count(*) FROM research_options_backtest_run_sessions WHERE run_id=$1", run_id) == len(result.sessions)
        with pytest.raises(CommandConflict):
            await repository.insert_record(conn, replace(record, command_sha256="different-request"))

        event = GoldenEvent(0, record.id, "revoked", None, None, "Owner revoked", "owner", 4, "revoke-" + unique, "revoke-a")
        revoked = await repository.insert_event(conn, event)
        retry_event = await repository.insert_event(conn, event)
        assert revoked.id == retry_event.id
        assert (await repository.get_record(conn, record.id)).proof == proof
        await save_setup(conn, "setup-" + unique, result.setup, saved_at_ms=3)
        listed = await _listed_setups(conn)
        matching = next(item for item in listed if item.name == "setup-" + unique)
        assert matching.golden_setup_id == record.id
        assert matching.golden_status == "revoked"
        await save_setup(conn, "setup-" + unique, result.setup.model_copy(update={"starting_cash_usd": Decimal(25000)}), saved_at_ms=5)
        edited = next(item for item in await _listed_setups(conn) if item.name == "setup-" + unique)
        assert edited.golden_setup_id is None
        with pytest.raises(CommandConflict):
            await repository.insert_event(conn, replace(event, reason="Changed reason", command_sha256="revoke-b"))
        with pytest.raises(ValueError, match="revoked"):
            await repository.insert_event(conn, GoldenEvent(
                0, record.id, "reproved", "rules-v2", proof, None, "owner", 5, "reprove-" + unique, "reprove-a",
            ))
    finally:
        await conn.execute("DELETE FROM research_golden_option_setup_events WHERE qualification_id=$1", record.id)
        await conn.execute("DELETE FROM research_golden_option_setups WHERE id=$1", record.id)
        await conn.execute("DELETE FROM research_options_backtest_runs WHERE id=$1", run_id)
        await conn.execute("DELETE FROM research_options_backtest_setups WHERE name=$1", "setup-" + unique)
