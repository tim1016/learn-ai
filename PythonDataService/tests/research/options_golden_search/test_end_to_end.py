"""Complete owner workflow through HTTP, retained lake bytes, real Postgres and independent proof."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import respx
from fastapi import FastAPI

from app.data_lake import catalog_client
from app.lean_sidecar.trading_calendar import expected_sessions, session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest import saved_runs
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_bots import golden_repository
from app.options_bots.golden_proof import setup_sha256
from app.research.golden_search import proof as blob_module
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search import service, stages
from app.research.persistence import db
from app.routers.options_golden_search import router
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import (
    minute_bar,
    raw_root,
    stock_bar,
    store_listing,
    store_option_day,
    store_underlying_day,
    vendor_listing_row,
)
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._market import leg, setup

pytestmark = [pytest.mark.slow, pytest.mark.asyncio]


@respx.mock
@pytest.mark.parametrize("owner_exception", [False, True])
async def test_saved_setup_to_retained_qualification_uses_real_transport_and_atomic_publication(
    conn: asyncpg.Connection, unique: str, fake_catalog: FakeCatalog, tmp_lake: Path,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_exception: bool,
) -> None:
    # The shared research fixture attests this database as disposable before any connection below.
    @asynccontextmanager
    async def connection() -> AsyncIterator[asyncpg.Connection]:
        opened = await asyncpg.connect(os.environ["POSTGRES_URL"])
        try:
            yield opened
        finally:
            await opened.close()

    monkeypatch.setattr(db, "connection", connection)
    monkeypatch.setattr(repo, "connection", connection)

    async def catalog_ready() -> None:
        return None

    monkeypatch.setattr(catalog_client, "init_pool", catalog_ready)
    blobs = BlobStore(tmp_path / "proof-blobs")
    monkeypatch.setattr(blob_module, "default_blob_store", lambda: blobs)
    monkeypatch.setattr(stages, "default_blob_store", lambda: blobs)
    symbol = "".join(chr(ord("A") + int(character, 16)) for character in unique[:5])
    assert not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM research_options_search_exposures WHERE underlying=$1)", symbol)
    written = OptionsBacktestSetup.model_validate({
        **setup(leg(stop=("percent", "10"))).model_dump(mode="python"), "underlying": symbol,
    })
    setup_name = "Whole setup " + unique

    async def setup_named(name: str) -> OptionsBacktestSetup:
        assert name == setup_name
        return written

    monkeypatch.setattr(service, "setup_named", setup_named)
    days = tuple(expected_sessions(date(2025, 1, 1), date(2025, 5, 31)))
    for index, day in enumerate(days):
        window = session_window_for_date(day)
        contract = OptionContract.of(root=symbol, expiry_date=day, right="call", strike=500)
        await store_listing([vendor_listing_row(contract, underlying=symbol)], underlying=symbol, day=day)
        await store_underlying_day(
            fake_catalog, tmp_lake,
            [stock_bar(window.open_ms_utc + minute * 60000, "500", "500", "500", "500", symbol=symbol) for minute in range(65)],
            symbol=symbol, day=day,
        )
        option_rows = []
        exit_price = 2 if index % 2 == 0 else 3
        for minute in range(65):
            opened, closed = (1, 0.85) if minute == 6 else (0.9, 1) if minute == 7 else (exit_price, exit_price) if minute == 60 else (1, 1)
            option_rows.append({**minute_bar(minute, opened, max(opened, closed), min(opened, closed), closed), "t": window.open_ms_utc + minute * 60000})
        await store_option_day(contract, option_rows, day=day, listed_under=symbol)

    plan: dict[str, Any] = {
        "setup_name": setup_name, "expected_setup_hash": setup_sha256(written), "method": "grid",
        "knobs": [{"path": "legs.call.stop_loss.value", "values": ["10", "20"]}],
        "history": {"start_ms": et_midnight_ms(date(2025, 1, 1)), "end_ms": et_midnight_ms(date(2025, 6, 1)),
                    "train_months": 1, "test_months": 1, "final_months": 1},
        "policy": {"min_sharpe": 0, "min_activity_per_year": 1, "min_daily_observations": 2,
                   "min_forward_sessions": 2, "min_final_sessions": 2},
        "finalist_count": 1,
        "stress": [{"key": "fees", "fee_per_contract_usd": "0.65"}],
    }
    app = FastAPI()
    app.include_router(router, prefix="/options-search")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        reviewed = await client.post("/options-search/preflight", json=plan)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["can_lock"], reviewed.json()["blockers"]
        locked = await client.post("/options-search/studies", json={"request": plan,
            "preflight_hash": reviewed.json()["preflight_hash"], "idempotency_key": "lock-" + unique})
        assert locked.status_code == 201, locked.text
        record = locked.json()
        study_id = record["id"]
        # Every later phase, including ordinary source materialization and both
        # independent proof replays, must survive losing the mutable lake.
        shutil.rmtree(raw_root(tmp_lake))

        async def command(kind: str, **extra: Any) -> dict[str, Any]:
            nonlocal record
            response = await client.post(f"/options-search/studies/{study_id}/commands", json={
                "command": kind, "expected_revision": record["revision"], "idempotency_key": kind + "-" + unique, **extra,
            })
            assert response.status_code == 200, response.text
            record = response.json()
            return record

        async def execute() -> None:
            nonlocal record
            token = record["dispatch"]["stage_token"]
            job_id = "e2e-" + record["phase"] + "-" + unique
            assert await db.with_connection(repo.bind, study_id, token, job_id)
            await asyncio.to_thread(stages.run, study_id, token, job_id, cancel=lambda: None, checkpoint=lambda done, total: None)
            response = await client.get(f"/options-search/studies/{study_id}")
            assert response.status_code == 200, response.text
            record = response.json()

        await command("run_research")
        # Cancellation before dispatch cannot consume work or erase the locked plan.
        locked_hash = record["protocol_hash"]
        await command("cancel")
        assert record["status"] == "cancelled" and record["dispatch"] is None
        assert not await repo.evaluations(conn, study_id)
        await command("resume")
        await execute()
        assert record["protocol_hash"] == locked_hash
        assert record["phase"] == "compare" and record["status"] == "completed"
        winner = next(candidate for candidate in record["candidates"] if not candidate["is_original"])
        assert winner["verdict"] == "meets_rules"
        assert all(outcome["status"] == "meets_rules" for outcome in record["gate_outcomes"])
        final_day = next(day for day in days if day.month == 5)
        final_open = session_window_for_date(final_day).open_ms_utc
        unseen = await client.get(f"/options-search/studies/{study_id}/candidates/{winner['candidate_hash']}/sessions/{final_open}?window=final")
        assert unseen.status_code == 404 and "net_profit_usd" not in unseen.text
        if owner_exception:
            async with conn.transaction():
                final_window = (await repo.get(conn, study_id))["protocol"]["final"]
                await repo.reserve_exposure(conn, symbol, final_window["start_ms"], final_window["end_ms"], "other-" + unique)
        await command("open_final", candidate_hashes=[winner["candidate_hash"]])
        frozen = (await repo.get(conn, study_id))["finalists"]
        real_evaluate = stages.evaluate_setup
        real_batch = stages.StudyEvaluator.evaluate_batch
        final_calls = 0

        def stress_first(self: stages.StudyEvaluator, requests: list[tuple[Any, Any, str]]):
            if self.row["phase"] == "final":
                requests = sorted(requests, key=lambda request: request[2] == "base")
            return real_batch(self, requests)

        def interrupted_final(*args: Any, **kwargs: Any):
            nonlocal final_calls
            final_calls += 1
            if final_calls == 2:
                raise RuntimeError("capacity exercise: worker stopped during final batch")
            return real_evaluate(*args, **kwargs)

        monkeypatch.setattr(stages, "evaluate_setup", interrupted_final)
        if owner_exception:
            monkeypatch.setattr(stages.StudyEvaluator, "evaluate_batch", stress_first)
        with pytest.raises(RuntimeError, match="worker stopped during final batch"):
            await execute()
        failed = await client.get(f"/options-search/studies/{study_id}")
        assert failed.status_code == 200, failed.text
        record = failed.json()
        assert record["phase"] == "final" and record["status"] == "interrupted"
        assert record["allowed_commands"] == ["resume", "keep_original"]
        assert len(record["final_scenarios"]) == 1
        assert record["final_scenarios"][0]["scenario"] == ("fees" if owner_exception else "base")
        if owner_exception:
            assert record["final_results"] == [], "Stress evidence must never masquerade as base performance."
        else:
            assert len(record["final_results"]) == 1
            partial = record["final_results"][0]
            assert any(gate["gate_id"] == "final:batch" for gate in partial["gate_outcomes"])
            assert partial["verdict"] != "meets_rules"
        retained = await repo.evaluations(conn, study_id)
        completed_before = {item["key"]: item for item in retained if item["result"] is not None}
        monkeypatch.setattr(stages, "evaluate_setup", real_evaluate)
        monkeypatch.setattr(stages.StudyEvaluator, "evaluate_batch", real_batch)
        await command("resume", idempotency_key="resume-final-" + unique)
        await execute()
        after_resume = {item["key"]: item for item in await repo.evaluations(conn, study_id)}
        assert all(after_resume[key] == value for key, value in completed_before.items())
        assert (await repo.get(conn, study_id))["finalists"] == frozen
        assert record["phase"] == "approval" and len(record["final_results"]) == 2
        assert len(record["final_scenarios"]) == 4
        selected = next(candidate for candidate in record["final_results"] if candidate["candidate_hash"] == winner["candidate_hash"])
        assert selected["verdict"] == "meets_rules"
        observed = await client.get(f"/options-search/studies/{study_id}/candidates/{winner['candidate_hash']}/sessions/{final_open}?window=final")
        assert observed.status_code == 200, observed.text
        assert observed.json()["attempts"][0]["contract"].startswith(symbol)
        await command("approve_exception" if owner_exception else "approve", candidate_hash=winner["candidate_hash"],
            comment="Reviewed the frozen whole-setup evidence and recorded exposure.",
            **({"waived_gate_ids": ["final:exposure"]} if owner_exception else {}))
        await execute()

    assert record["phase"] == "approved" and record["qualification_status"] == "ready"
    assert record["dispatch"] is None and record["allowed_commands"] == []
    linked = await conn.fetchrow(
        """SELECT a.qualification_id,a.manifest::text AS manifest,g.source_run_id,r.status AS source_status
        FROM research_options_search_approvals a JOIN research_golden_option_setups g ON g.id=a.qualification_id
        JOIN research_options_backtest_runs r ON r.id=g.source_run_id WHERE a.study_id=$1""", study_id,
    )
    assert linked is not None and linked["qualification_id"] == record["qualification_id"]
    assert linked["source_status"] == "complete"
    source = await saved_runs.read_result(conn, linked["source_run_id"])
    golden = await golden_repository.get_record(conn, linked["qualification_id"])
    manifest = json.loads(linked["manifest"])
    assert golden.setup_sha256 == winner["candidate_hash"] == setup_sha256(source.setup)
    assert golden.evidence["verdict"] == ("owner_exception" if owner_exception else "meets_rules")
    assert manifest["exception"] == owner_exception
    assert [rule["gate_id"] for rule in manifest["failed_rules"]] == (["final:exposure"] if owner_exception else [])
    assert manifest["selection_count"] == 2
    assert str(source.summary.net_profit_usd) == golden.evidence["net_profit_usd"]
    assert len(source.slippage) > 1 and len(source.summary.cash_ladder) > 1
    assert golden.proof["session_opens_ms"] == [session.session_open_ms for session in source.sessions]
    assert golden.proof["manifest"]
    assert all(blobs.get(digest) for digest in manifest["snapshot"]["manifest"].values())
    assert all(blob.get("bot_id") is None for blob in (golden.evidence, manifest))
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await conn.execute("DELETE FROM research_options_search_approvals WHERE study_id=$1", study_id)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute("DELETE FROM research_options_backtest_runs WHERE id=$1", linked["source_run_id"])
