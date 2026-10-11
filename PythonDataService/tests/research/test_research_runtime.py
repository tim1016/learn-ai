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

from pathlib import Path

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
    """A transport timeout is not a refusal: the durable binding is the truth (#3043)."""
    from app.research.golden_search import service

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "sec")
    async def _bound_to_this(study_id):
        return "job-1"

    monkeypatch.setattr(service, "study_job_binding", _bound_to_this)
    with respx.mock(base_url="http://127.0.0.1:8000") as mock:
        mock.post("/api/jobs-internal/golden-search").mock(side_effect=httpx.ConnectTimeout("boom"))
        answer = await forward_golden_search_job(dict(PAYLOAD))
    assert answer == {"job_id": "job-1", "study_id": "study-1", "status": "queued"}, (
        "bound to this job id: the original acceptance stands"
    )


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

    def fake_run_stage(study_id, **kwargs):
        executed.append(study_id)
        kwargs["on_phase"]("search")
        kwargs["on_progress"](1, 2)
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
