"""The Options setups index over a real store (#3512): every kind is read, with the hash it froze, and none is lost to a name.

Runs only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``) and skips without one, like its
neighbours: the pure builders are worked in ``test_options_setup_index.py``.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import asyncpg

from app.options_backtest.saved_runs import NewRun, complete_run, create_run
from app.options_backtest.saved_setups import delete_setup, save_setup
from app.options_bots import golden_repository as repository
from app.options_bots.golden_proof import proof_sha256, setup_sha256
from app.options_bots.golden_repository import GoldenRecord
from app.research.options_setup_index import read_index
from tests.options_backtest._a_run import a_busy_run
from tests.research.conftest import conn as conn
from tests.research.conftest import unique as unique

STUDY = json.loads((Path(__file__).parent.parent / "fixtures" / "options_evidence" / "study-record.json").read_text())


async def _insert_study(conn: asyncpg.Connection, study_id: str, name: str) -> None:
    document = {**STUDY, "id": study_id, "setup_name": name}
    await conn.execute(
        """INSERT INTO research_options_search_studies
        (id, underlying, setup_name, phase, status, document, created_at_ms, updated_at_ms)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)""",
        study_id, STUDY["underlying"], name, STUDY["phase"], STUDY["status"], json.dumps(document),
        STUDY["created_at_ms"], STUDY["updated_at_ms"],
    )


async def test_the_index_reads_every_kind_with_its_exact_content_and_keeps_all_of_it_when_a_name_is_edited_deleted_and_reused(
    conn: asyncpg.Connection, unique: str,
) -> None:
    result = a_busy_run()
    frozen = setup_sha256(result.setup)
    alpha, beta = f"alpha-{unique}", f"beta-{unique}"
    study_id = "os_" + hashlib.sha1(unique.encode()).hexdigest()
    run_id = await create_run(conn, NewRun(
        job_id="index-" + unique, created_at_ms=1, setup=result.setup, setup_name=alpha,
        first_session_open_ms=result.sessions[0].session_open_ms,
        last_session_open_ms=result.sessions[-1].session_open_ms, sessions_requested=len(result.sessions),
    ))
    await complete_run(conn, run_id, result, finished_at_ms=2)
    proof = {"rules_digest": "rules-v1", "result": "source-evidence"}
    record = GoldenRecord(
        id="ogs_" + hashlib.md5(unique.encode()).hexdigest(), name=alpha, source_run_id=run_id,
        setup=result.setup.model_dump(mode="json"), setup_sha256=frozen, rules_digest="rules-v1", proof=proof,
        proof_sha256=proof_sha256(proof), evidence={}, note="Reviewed exact setup", approved_by="owner",
        created_at_ms=3, command_id="approval-" + unique, command_sha256="request-a",
    )
    try:
        await repository.insert_record(conn, record)
        await _insert_study(conn, study_id, alpha)
        # Two names, identical content.
        await save_setup(conn, alpha, result.setup, saved_at_ms=4)
        await save_setup(conn, beta, result.setup, saved_at_ms=5)

        index = await read_index()

        saved = {entry.name: entry for entry in index.setups}
        assert saved[alpha].setup_sha256 == saved[beta].setup_sha256 == frozen
        assert saved[alpha].underlying == result.setup.underlying and saved[alpha].example is False
        run = next(row for row in index.runs if row.id == run_id)
        assert (run.setup_sha256, run.recorded_name, run.status) == (frozen, alpha, "complete")
        assert run.net_profit_usd is not None
        version = next(row for row in index.approvals if row.id == record.id)
        assert (version.setup_sha256, version.origin, version.source_run_id) == (frozen, "backtest", run_id)
        study = next(row for row in index.studies if row.id == study_id)
        assert study.setup_sha256 is not None and study.unreadable_reason is None and study.recorded_name == alpha
        assert min(index.runs_total, index.studies_total, index.approvals_total) >= 1

        # "Alpha" is edited, "Beta" is deleted and its name reused for other content: the evidence keeps what it froze.
        await save_setup(conn, alpha, result.setup.model_copy(update={"starting_cash_usd": Decimal(25000)}), saved_at_ms=6)
        assert await delete_setup(conn, beta)
        await save_setup(conn, beta, result.setup.model_copy(update={"starting_cash_usd": Decimal(40000)}), saved_at_ms=7)

        after = await read_index()

        names = {entry.name: entry for entry in after.setups}
        assert frozen not in {names[alpha].setup_sha256, names[beta].setup_sha256}
        assert names[alpha].setup_sha256 != names[beta].setup_sha256
        assert next(row for row in after.runs if row.id == run_id).setup_sha256 == frozen
        kept = next(row for row in after.approvals if row.id == record.id)
        assert (kept.setup_sha256, kept.recorded_name) == (frozen, alpha)
        assert next(row for row in after.studies if row.id == study_id).setup_sha256 == study.setup_sha256
    finally:
        await conn.execute("DELETE FROM research_options_search_studies WHERE id=$1", study_id)
        await conn.execute("DELETE FROM research_golden_option_setup_events WHERE qualification_id=$1", record.id)
        await conn.execute("DELETE FROM research_golden_option_setups WHERE id=$1", record.id)
        await conn.execute("DELETE FROM research_options_backtest_runs WHERE id=$1", run_id)
        await conn.execute("DELETE FROM research_options_backtest_setups WHERE name = ANY($1::text[])", [alpha, beta])


async def test_a_record_that_cannot_be_read_stays_listed_and_takes_no_other_with_it(conn: asyncpg.Connection, unique: str) -> None:
    result = a_busy_run()
    name = f"broken-{unique}"
    study_id = "os_" + hashlib.sha1(("b" + unique).encode()).hexdigest()
    run_id = await create_run(conn, NewRun(
        job_id="index-broken-" + unique, created_at_ms=1, setup=result.setup, setup_name=None,
        first_session_open_ms=result.sessions[0].session_open_ms,
        last_session_open_ms=result.sessions[-1].session_open_ms, sessions_requested=len(result.sessions),
    ))
    await complete_run(conn, run_id, result, finished_at_ms=2)
    try:
        # A saved setup and a run whose stored setup no longer validates, and a study whose frozen protocol was changed.
        await conn.execute(
            "INSERT INTO research_options_backtest_setups (name, setup_json, saved_at_ms) VALUES ($1, $2::jsonb, 9)",
            name, json.dumps({"underlying": "SPY"}),
        )
        await conn.execute("UPDATE research_options_backtest_runs SET setup_json = $2::jsonb WHERE id = $1", run_id, json.dumps({"underlying": "SPY"}))
        await _insert_study(conn, study_id, name)
        changed = json.loads(await conn.fetchval("SELECT document::text FROM research_options_search_studies WHERE id=$1", study_id))
        changed["protocol"]["policy"]["min_sharpe"] = -999
        await conn.execute("UPDATE research_options_search_studies SET document=$2::jsonb WHERE id=$1", study_id, json.dumps(changed))

        index = await read_index()

        entry = next(row for row in index.setups if row.name == name)
        assert entry.setup_sha256 is None and entry.cannot_open_reason
        run = next(row for row in index.runs if row.id == run_id)
        assert run.setup_sha256 is None and run.unreadable_reason and run.recorded_name is None
        study = next(row for row in index.studies if row.id == study_id)
        assert study.setup_sha256 is None and study.unreadable_reason
    finally:
        await conn.execute("DELETE FROM research_options_search_studies WHERE id=$1", study_id)
        await conn.execute("DELETE FROM research_options_backtest_runs WHERE id=$1", run_id)
        await conn.execute("DELETE FROM research_options_backtest_setups WHERE name=$1", name)
