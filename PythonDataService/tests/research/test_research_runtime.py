"""The isolated research runtime: forwarding, authority, and the boundary contract (#3043).

Fast tier: the reload refusal, the compose containment (distinct cgroup,
opt-in profile), the forwarding client's five answers (acceptance, refusal
passthrough, timeout-reconciliation both ways, missing configuration), and
the router's forward-before-bind order. The slow tier crosses the real ASGI
boundary against ephemeral Postgres: a dispatch through the runtime app
binds, stamps with the runtime's boot generation, executes and answers; a
redelivery joins the original acceptance; and a runtime restart fences the
old generation's jobs out of protection.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
import yaml
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.services import research_runtime_client as client
from app.services.research_runtime_client import (
    forward_golden_search_job,
    is_research_runtime,
    research_forwarding_enabled,
)
from tests._helpers.golden_search_study import Driver, seed_lake, unique_symbol

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FLEET_DEV = REPO_ROOT / "compose.fleet.dev.yaml"
PAYLOAD = {"study_id": "study-1", "stage_token": "tok", "job_id": "job-1"}


@pytest.fixture
def research_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RESEARCH_EXECUTION_MODE", "research")
    monkeypatch.setattr(settings, "RESEARCH_RUNTIME_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("RESEARCH_RUNTIME", raising=False)


# ── Configuration and launch validation ───────────────────────────────────


def test_local_mode_does_not_forward_and_the_runtime_marker_distinguishes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RESEARCH_EXECUTION_MODE", "local")
    monkeypatch.delenv("RESEARCH_RUNTIME", raising=False)
    assert not research_forwarding_enabled()
    assert not is_research_runtime()

    monkeypatch.setattr(settings, "RESEARCH_EXECUTION_MODE", "research")
    assert research_forwarding_enabled()
    monkeypatch.setenv("RESEARCH_RUNTIME", "1")
    assert is_research_runtime()


def test_the_runtime_refuses_to_start_under_a_reloader(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research_runtime import refuse_reload

    monkeypatch.delenv("UVICORN_RELOAD", raising=False)
    refuse_reload()  # the default launch is fine

    for enabled in ("1", "true", "YES", "on"):
        monkeypatch.setenv("UVICORN_RELOAD", enabled)
        with pytest.raises(RuntimeError, match="refuses to start under a development reloader"):
            refuse_reload()
    monkeypatch.delenv("UVICORN_RELOAD")
    monkeypatch.setattr("sys.argv", ["uvicorn", "app.research_runtime:app", "--reload"])
    with pytest.raises(RuntimeError, match="refuses to start"):
        refuse_reload()


def test_the_research_service_is_an_opt_in_profile_with_its_own_cgroup() -> None:
    """Distinct cgroup, never under the API's limits: the compose file is the contract (#3043)."""
    loader = type("ComposeLoader", (yaml.SafeLoader,), {})
    loader.add_multi_constructor(
        "!", lambda l, tag, node: l.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else l.construct_scalar(node)
    )
    compose = yaml.load(COMPOSE_FLEET_DEV.read_text(), Loader=loader)
    research = compose["services"]["research"]
    assert research["profiles"] == ["research"], "the runtime never starts by default"
    # The fleet's top-level containment idiom — this toolchain rejects
    # pids_limit mixed with deploy.resources.limits, and the operator's
    # documented `--profile research up` must start the service (#3043).
    assert (research["cpus"], research["mem_limit"], research["pids_limit"]) == ("8.0", "16g", 512), (
        "the PRD qualification envelope, as one startable block"
    )
    # The API keeps its own smaller envelope and its routing env is opt-in too.
    api = compose["services"]["python-service"]["environment"]
    assert api["RESEARCH_EXECUTION_MODE"] == "${RESEARCH_EXECUTION_MODE:-local}"
    assert research["environment"]["RESEARCH_RUNTIME"] == "1"
    assert research["environment"]["RESEARCH_EXECUTION_MODE"] == "research"


# ── The forwarding client ──────────────────────────────────────────────────


async def test_a_forwarded_acceptance_passes_through(research_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "sec")
    with respx.mock(base_url="http://127.0.0.1:8000") as mock:
        route = mock.post("/api/jobs-internal/golden-search").mock(
            return_value=httpx.Response(202, json={"job_id": "job-1", "study_id": "study-1", "status": "queued"})
        )
        answer = await forward_golden_search_job(dict(PAYLOAD))
    assert answer["status"] == "queued"
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["X-Data-Plane-Control-Secret"] == "sec", "the authenticated internal boundary"


async def test_a_runtime_refusal_passes_through_with_its_status_and_reason(research_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "sec")
    with respx.mock(base_url="http://127.0.0.1:8000") as mock:
        mock.post("/api/jobs-internal/golden-search").mock(
            return_value=httpx.Response(409, json={"detail": "stage_token_mismatch"})
        )
        with pytest.raises(HTTPException) as thrown:
            await forward_golden_search_job(dict(PAYLOAD))
    assert thrown.value.status_code == 409 and "stage_token_mismatch" in str(thrown.value.detail)


async def test_forwarding_without_a_runtime_url_refuses(research_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "RESEARCH_RUNTIME_URL", "")
    with pytest.raises(HTTPException) as thrown:
        await forward_golden_search_job(dict(PAYLOAD))
    assert thrown.value.status_code == 503 and "RESEARCH_RUNTIME_URL" in str(thrown.value.detail)


async def test_a_timeout_after_acceptance_joins_the_original_dispatch(research_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport timeout is not a refusal: binding plus a live worker is the truth (#3043).

    A binding alone only proves acceptance began — the runtime binds before
    the lease exists — so the join also demands a live worker; this test
    holds the lease, and the next one proves a dead lease refuses.
    """
    from app.research.golden_search import service
    from app.research.persistence import lifecycle

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "sec")
    async def _bound_to_this(study_id):
        return "job-1"

    monkeypatch.setattr(service, "study_job_binding", _bound_to_this)
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: True)
    with respx.mock(base_url="http://127.0.0.1:8000") as mock:
        mock.post("/api/jobs-internal/golden-search").mock(side_effect=httpx.ConnectTimeout("boom"))
        answer = await forward_golden_search_job(dict(PAYLOAD))
    assert answer == {"job_id": "job-1", "study_id": "study-1", "status": "queued"}, (
        "bound to this job id with a live worker: the original acceptance stands"
    )


async def test_a_timeout_over_a_dead_leash_is_a_refusal_not_a_join(research_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    """A binding whose worker never took the lease parks nobody: refuse instead of joining (#3043)."""
    from fastapi import HTTPException

    from app.research.golden_search import service
    from app.research.persistence import lifecycle

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "sec")

    async def _bound_to_this(study_id):
        return "job-1"

    monkeypatch.setattr(service, "study_job_binding", _bound_to_this)
    monkeypatch.setattr(lifecycle, "job_is_live", lambda job_id: False)
    with respx.mock(base_url="http://127.0.0.1:8000") as mock:
        mock.post("/api/jobs-internal/golden-search").mock(side_effect=httpx.ConnectTimeout("boom"))
        with pytest.raises(HTTPException) as thrown:
            await forward_golden_search_job(dict(PAYLOAD))
    assert thrown.value.status_code == 502
    assert "did not accept" in str(thrown.value.detail)


async def test_a_timeout_without_a_binding_is_a_refusal_not_silence(research_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from app.research.golden_search import service

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "sec")
    async def _unbound(study_id):
        return None

    monkeypatch.setattr(service, "study_job_binding", _unbound)
    with respx.mock(base_url="http://127.0.0.1:8000") as mock:
        mock.post("/api/jobs-internal/golden-search").mock(side_effect=httpx.ConnectTimeout("boom"))
        with pytest.raises(HTTPException) as thrown:
            await forward_golden_search_job(dict(PAYLOAD))
    assert thrown.value.status_code == 502
    assert "did not accept" in str(thrown.value.detail)


async def test_a_binding_to_a_different_job_does_not_join(research_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from app.research.golden_search import service

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "sec")
    async def _bound_elsewhere(study_id):
        return "job-someone-else"

    monkeypatch.setattr(service, "study_job_binding", _bound_elsewhere)
    with respx.mock(base_url="http://127.0.0.1:8000") as mock:
        mock.post("/api/jobs-internal/golden-search").mock(side_effect=httpx.ConnectTimeout("boom"))
        with pytest.raises(HTTPException) as thrown:
            await forward_golden_search_job(dict(PAYLOAD))
    assert thrown.value.status_code == 502


# ── The router's forward-before-bind order ────────────────────────────────


async def test_forwarding_mode_never_binds_or_starts_a_local_thread(research_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from app.routers import golden_search as router_module
    from app.routers.golden_search import jobs_router

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "sec")
    forwarded: list[dict] = []

    async def fake_forward(payload):
        forwarded.append(payload)
        return {"job_id": payload["job_id"], "study_id": payload["study_id"], "status": "queued"}

    def no_local(*args, **kwargs):
        raise AssertionError("the API must not bind or run when forwarding (#3043)")

    monkeypatch.setattr(client, "forward_golden_search_job", fake_forward)
    monkeypatch.setattr(router_module.service, "bind_dispatch", no_local)
    monkeypatch.setattr(router_module, "run_in_thread", no_local)

    app = FastAPI()
    app.include_router(jobs_router, prefix="/api/jobs-internal")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/api/jobs-internal/golden-search", json=PAYLOAD)
    assert response.status_code == 202 and forwarded == [PAYLOAD]


# ── The isolated boundary, crossed (slow: ephemeral Postgres) ─────────────


@pytest.fixture
def symbol() -> str:
    return unique_symbol()


@pytest.fixture
def lake(tmp_path: Path, symbol: str) -> Path:
    return seed_lake(tmp_path, symbol)


@pytest.fixture
def driver(lake: Path, monkeypatch: pytest.MonkeyPatch, conn) -> Driver:
    from app.research.persistence import lifecycle

    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])
    return Driver(roots=[lake])


@pytest.mark.slow
async def test_a_dispatch_crosses_the_boundary_binds_stamps_and_runs_then_restart_fences_the_generation(
    conn, driver, symbol, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start → execution → durable binding across the internal boundary, and the restart fence (#3043).

    The runtime app runs in-process over the ASGI transport — the real HTTP
    client seam, serialization, auth and lifespan — with the stage execution
    injected (the engine itself is the lifecycle suite's business) and the
    job store faked. What this proves is the boundary: the authenticated
    dispatch binds once, is stamped with the runtime's boot generation,
    executes under the checkpoint-gated lease, answers redelivery by joining
    the original acceptance, and loses sweep protection when its generation
    dies with the runtime.
    """
    from types import SimpleNamespace

    import asyncpg

    from app.research.golden_search import service
    from app.research.persistence import ownership
    from app.research_runtime import app as runtime_app
    from tests.jobs.conftest import _FakeRedis
    from tests.research.conftest import _ephemeral_url

    monkeypatch.setattr(settings, "RESEARCH_EXECUTION_MODE", "research")
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "boundary-secret")
    monkeypatch.setenv("RESEARCH_RUNTIME", "1")  # this process IS the runtime: execute, don't forward
    fake = _FakeRedis()
    import app.jobs.progress as progress_module
    from app.research.persistence import lifecycle as research_lifecycle

    # The job-store seam both modules read: dispatch/emitter through
    # progress, liveness through lifecycle (the jobs conftest patches both).
    monkeypatch.setattr(progress_module, "get_redis", lambda: fake)
    monkeypatch.setattr(research_lifecycle, "get_redis", lambda: fake)

    executed: list[str] = []
    stage_ran = threading.Event()

    def fake_run_stage(study_id, **kwargs):
        executed.append(study_id)
        kwargs["on_phase"]("search")
        kwargs["on_progress"](1, 2)
        stage_ran.set()
        return SimpleNamespace(as_dict=lambda: {"study_id": study_id, "state": "awaiting_validation", "stage": "search"})

    monkeypatch.setattr(service, "run_stage", fake_run_stage)

    row = await driver.lock(symbol)
    outcome = await driver.command(row, "continue")
    token = outcome.dispatch["payload"]["stage_token"]
    job_id = f"job-{symbol}"

    async with runtime_app.router.lifespan_context(runtime_app):
        authority = ownership.process_authority()
        assert authority is not None and authority.boot_generation >= 1
        transport = ASGITransport(app=runtime_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            unauthenticated = await http.post(
                "/api/jobs-internal/golden-search",
                json={"study_id": row.id, "stage_token": token, "job_id": job_id},
            )
            assert unauthenticated.status_code in (401, 403), "the boundary demands the control secret"

            accepted = await http.post(
                "/api/jobs-internal/golden-search",
                json={"study_id": row.id, "stage_token": token, "job_id": job_id},
                headers={"X-Data-Plane-Control-Secret": "boundary-secret"},
            )
            assert accepted.status_code == 202, accepted.text

        # The 202 only proves the thread started; the stage itself runs on a
        # worker thread nobody joins, so synchronize on its own signal before
        # asserting it ran — a scheduling-delayed start must not read as "never
        # executed".
        assert stage_ran.wait(timeout=5.0), "the stage did not run inside the runtime"
        assert executed == [row.id], "the stage ran once, inside the runtime"
        stamped = await conn.fetchrow(
            "SELECT boot_generation FROM research_job_owners WHERE job_id = $1", job_id
        )
        assert stamped is not None and stamped["boot_generation"] == authority.boot_generation

        # Redelivery under the same id joins the original acceptance: the
        # lease the runtime's emitter holds is the liveness authority.
        fake.lease(job_id)
        fake.job(job_id, "running")
        fake.lease(job_id)
        transport = ASGITransport(app=runtime_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            rejoined = await http.post(
                "/api/jobs-internal/golden-search",
                json={"study_id": row.id, "stage_token": token, "job_id": job_id},
                headers={"X-Data-Plane-Control-Secret": "boundary-secret"},
            )
        assert rejoined.status_code == 202, rejoined.text
        assert executed == [row.id], "no second worker beside the first"

    # The runtime died with its lifespan: its generation is dead, so the
    # API's sweep protection no longer covers the job — Finish territory.
    probe = await asyncpg.connect(_ephemeral_url())
    try:
        assert job_id not in await ownership.protected_job_ids(probe)
    finally:
        await probe.close()
    await conn.execute("DELETE FROM research_job_owners WHERE job_id = $1", job_id)


# ── Review hardening: dispatch refusals, health, generation recovery ──────


def test_forwarded_research_dispatches_refuse_loudly_in_research_mode(
    research_mode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0077: the forwarding API never starts grid or walk-forward threads (#3043 review)."""
    from fastapi import HTTPException

    from app.services.research_runtime_client import refuse_forwarded_research_dispatch

    with pytest.raises(HTTPException) as thrown:
        refuse_forwarded_research_dispatch("grid search")
    assert thrown.value.status_code == 503 and "research runtime" in str(thrown.value.detail)

    monkeypatch.setattr(settings, "RESEARCH_EXECUTION_MODE", "local")
    assert refuse_forwarded_research_dispatch("grid search") is None


async def test_unauthorized_dispatch_refuses_before_any_durable_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The singleton refusal states surface as 503 before a route binds anything (#3042 review)."""
    from fastapi import HTTPException

    from app.research.persistence import ownership
    from app.routers.research_records import refuse_unauthorized_dispatch

    monkeypatch.setattr(ownership, "dispatch_refused", lambda: True)
    monkeypatch.setattr(ownership, "process_authority", lambda: None)
    with pytest.raises(HTTPException) as thrown:
        await refuse_unauthorized_dispatch()
    assert thrown.value.status_code == 503 and "another coordinator" in str(thrown.value.detail)

    monkeypatch.setattr(ownership, "dispatch_refused", lambda: False)
    monkeypatch.setattr(
        ownership,
        "process_authority",
        lambda: SimpleNamespace(connected=False, boot_generation=3),
    )
    with pytest.raises(HTTPException) as dead:
        await refuse_unauthorized_dispatch()
    assert dead.value.status_code == 503 and "authority connection was lost" in str(dead.value.detail)

    monkeypatch.setattr(ownership, "process_authority", lambda: None)
    assert await refuse_unauthorized_dispatch() is None


async def test_healthz_fails_once_the_authority_connection_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime holding a dead lock connection answers 503, so the compose check restarts it (#3042 review)."""
    from app.research.persistence import ownership
    from app.research_runtime import healthz

    monkeypatch.setattr(
        ownership,
        "process_authority",
        lambda: SimpleNamespace(connected=True, boot_generation=3),
    )
    healthy = await healthz()
    assert healthy.status_code == 200 and healthy.background is None

    monkeypatch.setattr(
        ownership,
        "process_authority",
        lambda: SimpleNamespace(connected=False, boot_generation=3),
    )
    degraded = await healthz()
    assert degraded.status_code == 503

    monkeypatch.setattr(ownership, "process_authority", lambda: None)
    assert (await healthz()).status_code == 503


async def test_a_claiming_runtime_closes_the_jobs_of_dead_generations(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ownership module's promised generation sweep: old queued records close, others stay (#3042 review)."""
    import app.jobs.progress as progress_module
    from app.research.persistence import ownership
    from app.research_runtime import _close_abandoned_generations
    from tests.jobs.conftest import _FakeRedis
    from tests.research.conftest import _ephemeral_url

    fake = _FakeRedis()
    monkeypatch.setattr(progress_module, "get_redis", lambda: fake)
    await conn.execute("DELETE FROM research_job_owners")
    authority = await ownership.claim_coordinator_authority(
        dsn=_ephemeral_url(), instance_id="sweep-test", role="research"
    )
    try:
        await conn.execute(
            """
            INSERT INTO research_job_owners
                (job_id, kind, record_id, owner_instance_id, boot_generation, dispatched_at_ms)
            VALUES ('job-dead-gen', 'golden_search', 'study-1', 'dead-runtime', $1, 0),
                   ('job-live-gen', 'golden_search', 'study-2', 'this-runtime', $2, 0)
            """,
            authority.boot_generation - 1,
            authority.boot_generation,
        )
        fake.job("job-dead-gen", "queued")
        fake.lease("job-dead-gen")
        fake.job("job-live-gen", "queued")
        fake.lease("job-live-gen")

        await _close_abandoned_generations(authority)

        assert fake.hget("job:job-dead-gen:state", "status") == "failed", "the dead generation's record closed"
        assert not fake.exists("job:job-dead-gen:lease"), "the dead generation's lease is gone"
        assert fake.hget("job:job-live-gen:state", "status") == "queued", "the claiming generation's jobs stay live"
        assert fake.exists("job:job-live-gen:lease")
    finally:
        await authority.release()


# ── The connection envelope (#3047, PRD #3034 slice 12) ────────────────────


def test_the_envelope_caps_the_writer_loop_and_refuses_an_oversized_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admission and coordination live inside the reserved research cap, or startup refuses."""
    import asyncio
    import os

    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    monkeypatch.setattr(settings, "POSTGRES_URL", url)

    from app.data_lake import catalog_client
    from app.research_runtime import enforce_connection_envelope
    from app.utils.background_loop import background_loop, run_on_background_loop

    monkeypatch.setattr(settings, "RESEARCH_ADMISSION_CONNECTIONS", 8)
    monkeypatch.setattr(settings, "RESEARCH_COORDINATION_CONNECTIONS", 4)

    # A configuration that cannot fit the research cap is refused, never
    # repaired by raising anyone's ceiling.
    with monkeypatch.context() as narrower:
        narrower.setattr("app.research.benchmark.CONNECTION_INVENTORY_RESEARCH_CAP", 10, raising=False)
        with pytest.raises(RuntimeError, match="research cap"):
            asyncio.run(enforce_connection_envelope())

    try:
        # The honest envelope initializes the shared writer loop at the
        # coordination bound minus the authority's own lock connection.
        inventory = asyncio.run(enforce_connection_envelope())
        assert inventory["research_cap"] == 12, "eight admission plus four coordination"
        pool = catalog_client._pools.get(background_loop())
        assert pool is not None and pool._maxsize == 3, (
            "the writer loop's pool left room for the authority's connection"
        )
    finally:
        # The sized pool belongs to this test; later tests lazily build their own.
        closer = threading.Thread(
            target=lambda: run_on_background_loop(catalog_client.close_pool(), timeout=5.0), daemon=True
        )
        closer.start()
        closer.join(timeout=5.0)
