"""Crash and acknowledgement faults across real API/runtime sockets and saved replay."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import redis

from app.config import settings
from app.options_backtest import saved_runs
from app.options_backtest.frozen_inputs import FrozenRunInputs
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run import run_sessions
from app.options_bots.golden_proof import _canonical
from app.research.persistence.schema import ensure_schema
from app.research.runs.hashing import hash_payload
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.session_anchors import et_midnight_ms
from tests.options_backtest import test_golden_spy_2025_06_10 as golden
from tests.options_backtest._golden_vendor import recorded_vendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.research.conftest import _ephemeral_url
from tests.research.conftest import scratch_db as scratch_db

pytestmark = pytest.mark.slow

_SOURCE = Path(__file__).resolve().parents[2]
_AUTH = {CONTROL_SECRET_HEADER: "http-boundary-test-secret"}


@pytest.fixture(scope="module")
def isolated_redis() -> Iterator[str]:
    """A dedicated disposable service, never the developer's shared job store."""
    if shutil.which("podman") is None:
        pytest.skip("real socket boundary tests need Podman for a disposable Redis")
    name = f"options-http-{uuid.uuid4().hex[:12]}"
    subprocess.run(["podman", "run", "--rm", "-d", "--name", name, "-p", "127.0.0.1::6379",
                    "docker.io/library/redis:7-alpine", "redis-server", "--save", "", "--appendonly", "no"],
                   check=True, capture_output=True, text=True)
    try:
        address = subprocess.run(["podman", "port", name, "6379"], check=True, capture_output=True, text=True).stdout.strip()
        url = f"redis://{address}/0"
        client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        deadline = time.monotonic() + 10
        while True:
            try:
                client.ping()
                break
            except redis.ConnectionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        yield url
    finally:
        subprocess.run(["podman", "rm", "-f", name], check=True, capture_output=True, text=True)


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


class _Server:
    def __init__(self, role: str, port: int, environment: dict[str, str], root: Path) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.log = root / f"{role}-{uuid.uuid4().hex[:6]}.log"
        with self.log.open("wb") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "tests.options_backtest._runtime_http_app:app",
                 "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                env={**environment, "RESEARCH_RUNTIME": "1" if role == "runtime" else ""},
                cwd=_SOURCE, stdout=output, stderr=subprocess.STDOUT,
            )

    async def ready(self) -> None:
        deadline = time.monotonic() + 35
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    pytest.fail(f"HTTP server exited before readiness:\n{self.log.read_text()}")
                try:
                    response = await client.get(f"{self.base}/healthz")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)
            pytest.fail(f"HTTP server never became ready:\n{self.log.read_text()}")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)


async def _wait_file(path: Path, *, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"the real boundary never reached {path.name}")
        await asyncio.sleep(0.05)


async def _seed_catalog(conn: asyncpg.Connection, catalog: Any) -> None:
    """Seed exact capture receipts into a scratch catalog; admission reads real SQL."""
    await conn.execute('''CREATE TABLE "DataLakeArtifacts" (
        "Id" bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "DataRootId" uuid NOT NULL, "ArtifactKind" varchar NOT NULL,
        "Market" varchar, "Symbol" varchar, "TradingDate" date, "Resolution" varchar, "DataType" varchar,
        "Provider" varchar NOT NULL, "ProviderParams" jsonb NOT NULL, "PriceAdjustmentMode" varchar,
        "DataContractHash" char(64) NOT NULL, "RowCount" integer, "FirstBarStartMs" bigint,
        "LastBarStartMs" bigint, "CorpActionRevision" char(64), "FilePath" text NOT NULL,
        "FileSizeBytes" bigint, "FileSha256" char(64), "Status" varchar NOT NULL,
        "LeaseOwner" varchar, "LeaseExpiresAtMs" bigint, "LeaseGeneration" integer NOT NULL,
        "AttemptCount" integer NOT NULL, "LastError" text, "ErrorMessage" text,
        "FetchedAtMs" bigint NOT NULL, "CompletedAtMs" bigint);
        CREATE UNIQUE INDEX minute_identity ON "DataLakeArtifacts"
          ("DataRootId","Market","Symbol","TradingDate","DataType","Provider","PriceAdjustmentMode")
          WHERE "ArtifactKind"='time_series_bars' AND "Resolution"='minute';
        CREATE UNIQUE INDEX metadata_identity ON "DataLakeArtifacts" ("DataRootId","DataContractHash")
          WHERE "ArtifactKind"='metadata';''')
    fields = ("id", "data_root_id", "artifact_kind", "market", "symbol", "trading_date", "resolution", "data_type",
              "provider", "price_adjustment_mode", "data_contract_hash", "row_count", "first_bar_start_ms",
              "last_bar_start_ms", "file_path", "file_size_bytes", "file_sha256", "status", "lease_generation",
              "attempt_count")
    columns = ('"Id","DataRootId","ArtifactKind","Market","Symbol","TradingDate","Resolution","DataType",'
               '"Provider","PriceAdjustmentMode","DataContractHash","RowCount","FirstBarStartMs","LastBarStartMs",'
               '"FilePath","FileSizeBytes","FileSha256","Status","LeaseGeneration","AttemptCount"')
    for row in catalog.rows.values():
        assert row["status"] == "complete"
        await conn.execute(f'INSERT INTO "DataLakeArtifacts" ({columns},"ProviderParams","FetchedAtMs") '
                           f'VALUES ({",".join(f"${i}" for i in range(1, 21))},\'{{}}\',1)',
                           *(row.get(field) for field in fields))
    await conn.execute('SELECT setval(pg_get_serial_sequence(\'"DataLakeArtifacts"\',\'Id\'),10000)')


@pytest.mark.parametrize("fault", ["api_restart", "dispatch_ack", "save_ack", "generation_fence"])
async def test_real_http_boundary_keeps_one_fenced_exact_run_across_faults(
    scratch_db: asyncpg.Connection, isolated_redis: str, fake_catalog: Any, tmp_lake: Path,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    # These are the real golden vendor bytes, written by the normal lake writers.
    # Subsequent subprocesses have no provider mocks and cannot fetch missing bytes.
    monkeypatch.setattr(settings, "POSTGRES_URL", "")
    with recorded_vendor(golden.RECORDING, golden.RECORDED_FILES, golden.STOCK_PATH):
        await golden.capture_the_recording()
    given = golden.a_setup()
    expected = await asyncio.to_thread(run_sessions, given, (golden.TRADING_DATE,), LakeRunData("SPY"))
    await _seed_catalog(scratch_db, fake_catalog)
    await ensure_schema(scratch_db)
    database = await scratch_db.fetchval("SELECT current_database()")
    parsed = urlsplit(_ephemeral_url())
    dsn = urlunsplit(parsed._replace(path=f"/{database}"))
    api_port, runtime_port = _free_port(), _free_port()
    faults = tmp_path / "faults"
    faults.mkdir()
    if fault == "dispatch_ack":
        (faults / "lose-dispatch-ack").touch()
    if fault == "save_ack":
        (faults / "lose-save-ack").touch()
    inputs = tmp_path / "frozen-inputs"
    environment = {**os.environ, "PYTHONPATH": str(_SOURCE), "POSTGRES_URL": dsn,
        "REDIS_URL": isolated_redis, "POLYGON_API_KEY": "test-key", "LEAN_DATA_WRITE_ROOT": str(tmp_lake),
        "DATA_LAKE_ROOT_ID": str(settings.DATA_LAKE_ROOT_ID), "RESEARCH_EXECUTION_MODE": "research",
        "RESEARCH_RUNTIME_URL": f"http://127.0.0.1:{runtime_port}",
        "OPTIONS_BACKTEST_DATA_SERVICE_URL": f"http://127.0.0.1:{api_port}",
        "OPTIONS_BACKTEST_INPUT_ROOT": str(inputs), "OPTIONS_HTTP_TEST_FAULTS": str(faults),
        "DATA_PLANE_CONTROL_SECRET": _AUTH[CONTROL_SECRET_HEADER], "TRUSTED_HOSTS": "127.0.0.1,localhost",
        "RESEARCH_WORKER_COUNT": "2", "OPTIONS_SEARCH_WORKER_COUNT": "1"}
    api = _Server("api", api_port, environment, tmp_path)
    runtime = _Server("runtime", runtime_port, environment, tmp_path)
    job_id = f"socket_{uuid.uuid4().hex[:16]}"
    req = OptionsBacktestJobRequest(job_id=job_id, setup=given, start_ms=et_midnight_ms(golden.TRADING_DATE),
                                   end_ms=et_midnight_ms(golden.TRADING_DATE + timedelta(days=1)))
    try:
        await asyncio.gather(api.ready(), runtime.ready())
        async with httpx.AsyncClient(timeout=20, trust_env=False, headers=_AUTH) as client:
            accepted = await client.post(f"{api.base}/api/jobs-internal/options-backtest", json=req.model_dump(mode="json"))
            assert accepted.status_code == 202, f"{accepted.text}\n{runtime.log.read_text()}\n{api.log.read_text()}"
            run_id = accepted.json()["run_id"]
            await _wait_file(faults / "checkpoint")
            assert await scratch_db.fetchval("SELECT status FROM research_options_backtest_runs WHERE id=$1", run_id) == "running"
            assert await scratch_db.fetchval("SELECT count(*) FROM research_options_backtest_run_sessions WHERE run_id=$1", run_id) == 0
            first_checkpoint = json.loads((faults / "checkpoint").read_text())
            duplicate = await client.post(f"{api.base}/api/jobs-internal/options-backtest", json=req.model_dump(mode="json"))
            assert duplicate.status_code == 202 and duplicate.json()["run_id"] == run_id
            changed = req.model_dump(mode="json")
            changed["setup"]["starting_cash_usd"] = "99999"
            mismatch = await client.post(f"{api.base}/api/jobs-internal/options-backtest", json=changed)
            assert mismatch.status_code == 409
            if fault == "api_restart":
                runtime_pid = runtime.process.pid
                api.stop()
                # The API is gone while the coordinator and its authority remain live.
                await asyncio.sleep(0.5)
                assert runtime.process.poll() is None
                assert await scratch_db.fetchval("SELECT boot_generation FROM research_runtime_owners") == 1
                assert redis.Redis.from_url(isolated_redis).exists(f"job:{job_id}:lease")
                api = _Server("api", api_port, environment, tmp_path)
                await api.ready()
                assert runtime.process.pid == runtime_pid
            elif fault == "generation_fence":
                released = await client.post(f"{runtime.base}/test/release-authority")
                assert released.status_code == 200
            (faults / "release").touch()
            deadline = time.monotonic() + 45
            state = redis.Redis.from_url(isolated_redis, decode_responses=True)
            while state.hget(f"job:{job_id}:state", "status") not in ("completed", "failed", "cancelled"):
                if time.monotonic() >= deadline:
                    pytest.fail(f"runtime job did not settle:\n{runtime.log.read_text()}\n{api.log.read_text()}")
                await asyncio.sleep(0.1)
            if fault == "generation_fence":
                assert state.hget(f"job:{job_id}:state", "status") == "failed"
                assert await saved_runs.read_result(scratch_db, run_id) is None
                assert await scratch_db.fetchval("SELECT count(*) FROM research_options_backtest_run_sessions WHERE run_id=$1", run_id) == 0
            else:
                assert state.hget(f"job:{job_id}:state", "status") == "completed", runtime.log.read_text()
                stored = await saved_runs.read_result(scratch_db, run_id)
                assert stored == expected
                assert await scratch_db.fetchval("SELECT count(*) FROM research_options_backtest_runs WHERE job_id=$1", job_id) == 1
                assert await scratch_db.fetchval("SELECT count(*) FROM research_options_backtest_run_sessions WHERE run_id=$1", run_id) == 1
                closed = await client.post(f"{api.base}/api/jobs-internal/options-backtest", json=req.model_dump(mode="json"))
                assert closed.status_code == (502 if fault == "dispatch_ack" else 409)
                if fault == "dispatch_ack":
                    assert (faults / "dispatch-ack-delayed").exists()
                    assert (faults / "dispatch-ack-lost").exists()
                if fault == "save_ack":
                    assert (faults / "save-ack-lost").exists()
                metadata = json.loads(await scratch_db.fetchval(
                    "SELECT execution_metadata_json::text FROM research_options_backtest_runs WHERE id=$1", run_id))
                assert metadata["frozen_input_digest"] == FrozenRunInputs.model_validate(first_checkpoint).digest
                expected_digest = hash_payload(_canonical(expected))
                stored_digest = hash_payload(_canonical(stored))
                assert stored_digest == expected_digest
                (tmp_path / "evidence.json").write_text(json.dumps({"fault": fault, "job_id": job_id, "run_id": run_id,
                    "expected_result_digest": expected_digest, "saved_result_digest": stored_digest,
                    "execution_metadata": metadata}, indent=2))
            assert len(first_checkpoint["sessions"]) == 1
            assert not (faults / "unexpected-provider-request").exists()
            await _wait_file(inputs / ".reservations.json")
            assert job_id not in json.loads((inputs / ".reservations.json").read_text())
            assert not (inputs / job_id).exists()
    finally:
        (faults / "release").touch()
        api.stop()
        runtime.stop()
