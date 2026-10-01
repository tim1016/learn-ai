"""Golden Search HTTP boundary (#2696): refusals as data, command conflicts, the stage-token gate and the control guard.

The first block runs anywhere (no database): capabilities, plan problems that
come back as refusals in a 200, schema violations, refused defaults and the
control secret on unsafe methods. The rest drives real studies through the
HTTP surface against the ephemeral database (``POSTGRES_URL_IS_EPHEMERAL=1``),
with the study evaluator's engine and the approval workflow replaced by the
fakes the study-service suite uses — the real engine is never called. Every
response passes through the router's response models, which refuse
undeclared keys, so a read model that drifts from the wire contract fails
these tests with a 500.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis
from httpx import ASGITransport

from app.config import settings
from app.jobs.phases import JOB_PHASES
from app.main import app
from app.research.golden_search import planning, stages
from app.research.persistence import lifecycle
from app.routers import golden_search as golden_search_router
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests._helpers.golden_search_study import FakeApproval, FakeEngine, plan_request, seed_lake, unique_symbol

BASE = "/api/research/golden-search"
JOBS = "/api/jobs-internal/golden-search"
EMA_KNOB_ORDER = ["gap", "rsi_min", "rsi_max", "fast_period", "slow_period", "hold_bars", "gap_bps"]
APPROVE = {"note": "Reviewed.", "acknowledge_missing_parity": True, "acknowledge_research_weakness": False, "expected_default_qualification_id": None}


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _sealed_plan(symbol: str = "SPY", **overrides: Any) -> dict[str, Any]:
    """A plan whose final interval is empty: preflight then reads nothing outside the plan, so it needs no database."""
    plan = plan_request(symbol, **overrides)
    plan["final_end_ms"] = plan["final_start_ms"]
    return plan


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── No database: the plan surface and the guard ──────────────────────────


async def test_capabilities_offer_the_ema_knobs_and_name_why_other_strategies_are_unavailable(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{BASE}/capabilities")

    assert response.status_code == 200, response.text
    rows = {row["strategy_key"]: row for row in response.json()}
    ema = rows["ema_crossover_signal"]
    assert ema["available"] is True and ema["reason"] is None
    assert [knob["name"] for knob in ema["knobs"]] == EMA_KNOB_ORDER
    assert {knob["name"]: knob["default_step"] for knob in ema["knobs"]}["gap"] == 0.05
    assert ["fast_period", "slow_period"] in ema["default_pair_audits"]
    unavailable = [row for row in rows.values() if not row["available"]]
    assert unavailable and all(row["reason"] and row["knobs"] == [] for row in unavailable)


async def test_preflight_returns_every_problem_of_a_well_formed_plan_as_data_in_a_200(client: httpx.AsyncClient) -> None:
    plan = _sealed_plan(zoom={"points": 99, "refinements": 1, "passes": 1}, budget_cap=0)
    plan["knobs"][0]["step"] = None  # gap is searched without a step

    response = await client.post(f"{BASE}/preflight", json=plan)

    assert response.status_code == 200, response.text
    body = response.json()
    codes = {refusal["code"] for refusal in body["refusals"]}
    assert len(codes) >= 3, codes
    assert "STEP_MISSING" in codes
    assert all(refusal["message"] for refusal in body["refusals"])
    assert body["run_up"] is None and body["exposure"] is None


async def test_preflight_for_a_strategy_without_a_declaration_is_a_refusal_not_an_error(client: httpx.AsyncClient) -> None:
    response = await client.post(f"{BASE}/preflight", json=_sealed_plan(strategy_key="sma_crossover"))

    assert response.status_code == 200, response.text
    assert [(r["code"], r["field"]) for r in response.json()["refusals"]] == [("STRATEGY_UNAVAILABLE", "strategy_key")]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda plan: plan.update(final_end_ms=MAX_TIMESTAMP_MS + 1), id="ms-beyond-the-domain-ceiling"),
        pytest.param(lambda plan: plan.update(unknown_key=1), id="unknown-key"),
        pytest.param(lambda plan: plan["knobs"][0].update(mode="sweep"), id="unknown-knob-mode"),
        pytest.param(lambda plan: plan.pop("incumbent"), id="missing-incumbent"),
        pytest.param(lambda plan: plan["policy"].update(max_drawdown_ceiling=float("nan")), id="non-finite-number"),
    ],
)
async def test_a_body_that_is_not_a_plan_at_all_is_a_422(client: httpx.AsyncClient, mutate: Any) -> None:
    plan = _sealed_plan()
    mutate(plan)
    # The standard encoder writes NaN as a bare token; the schema, not the JSON parser, must refuse it.
    content = json.dumps(plan, allow_nan=True)

    response = await client.post(f"{BASE}/preflight", content=content, headers={"content-type": "application/json"})

    assert response.status_code == 422, response.text


async def test_preflight_accepts_camel_case_keys(client: httpx.AsyncClient) -> None:
    plan = _sealed_plan()
    start = plan.pop("development_start_ms")
    camel = {**plan, "developmentStartMs": start}
    camel["policy"] = {"objective": "sharpe_ratio", "minTrades": 1, "maxDrawdownCeiling": 0.5, "requirePositiveNet": True}

    response = await client.post(f"{BASE}/preflight", json=camel)

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("params", "code", "field"),
    [
        ({"strategy_key": "sma_crossover", "symbol": "SPY"}, "STRATEGY_UNAVAILABLE", "strategy_key"),
        ({"strategy_key": "ema_crossover_signal", "symbol": "NOT A TICKER"}, "SYMBOL_INVALID", "symbol"),
    ],
)
async def test_refused_defaults_are_a_400_carrying_the_code_and_field(client: httpx.AsyncClient, params: dict, code: str, field: str) -> None:
    response = await client.get(f"{BASE}/defaults", params=params)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == {"code": code, "message": response.json()["detail"]["message"], "field": field, "refusals": [], "study": None}
    assert response.json()["detail"]["message"]


async def test_defaults_refuse_a_month_count_below_one_at_the_schema(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{BASE}/defaults", params={"strategy_key": "ema_crossover_signal", "symbol": "SPY", "final_months": 0})

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"{BASE}/preflight", lambda plan: plan),
        ("POST", f"{BASE}/studies", lambda plan: {"protocol": plan, "idempotency_key": "k"}),
        ("POST", f"{BASE}/studies/s1/commands", lambda plan: {"command": "continue", "expected_revision": 0, "idempotency_key": "k", "payload": {}}),
        ("DELETE", f"{BASE}/studies/s1", lambda plan: None),
    ],
)
async def test_every_unsafe_method_needs_the_control_secret(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    refused = await client.request(method, path, json=body(_sealed_plan()))

    assert refused.status_code == 403, refused.text
    assert CONTROL_SECRET_HEADER in refused.json()["message"]


async def test_reads_and_a_guarded_call_with_the_secret_pass_the_guard(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    read = await client.get(f"{BASE}/capabilities")
    guarded = await client.post(f"{BASE}/preflight", json=_sealed_plan(), headers={CONTROL_SECRET_HEADER: "test-control-secret"})

    assert read.status_code == 200
    assert guarded.status_code == 200, guarded.text


def test_the_jobs_entry_carries_no_control_guard_and_the_research_routes_all_do() -> None:
    from fastapi.routing import APIRoute

    from app.security.data_plane_control import require_data_plane_control_secret

    def guarded(route: APIRoute) -> bool:
        return any(dependency.call is require_data_plane_control_secret for dependency in route.dependant.dependencies)

    routes = [route for route in app.routes if isinstance(route, APIRoute) and "golden-search" in route.path]
    research = [route for route in routes if route.path.startswith(BASE)]
    (jobs,) = [route for route in routes if route.path == JOBS]
    assert research and all(guarded(route) for route in research)
    assert not guarded(jobs)


# ── Ephemeral database: studies over HTTP ────────────────────────────────


def _requires_ephemeral_db() -> None:
    if not os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_URL_IS_EPHEMERAL", "").lower() not in ("1", "true"):
        pytest.skip("live-DB endpoint tests need an ephemeral POSTGRES_URL")


class _Emitter:
    def __init__(self) -> None:
        self.phases: list[str] = []

    def phase(self, name: str) -> None:
        self.phases.append(name)

    def progress(self, current: int, total: int, *, unit: str = "bars", message: str | None = None) -> None:
        assert unit == "evaluations" and current >= 0

    def log(self, message: str, *, level: str = "info") -> None:
        assert message


class _Cancel:
    def raise_if_cancelled(self) -> None:
        return None


@dataclass
class Harness:
    """Stands in for the job runtime: captures each dispatched worker and runs it on demand."""

    engine: FakeEngine = field(default_factory=FakeEngine)
    approval: FakeApproval = field(default_factory=FakeApproval)
    workers: dict[str, Any] = field(default_factory=dict)
    live: set[str] = field(default_factory=set)
    phases: list[str] = field(default_factory=list)

    def capture(self, job_id: str, work: Any, **kwargs: Any) -> None:
        assert kwargs["cancel_check_every_n"] == 1
        self.workers[job_id] = work
        self.live.add(job_id)

    def liveness(self, job_id: str | None) -> bool | None:
        return job_id in self.live

    async def run(self, job_id: str) -> dict[str, Any]:
        emitter = _Emitter()
        try:
            return await asyncio.to_thread(self.workers.pop(job_id), emitter, _Cancel())
        finally:
            self.live.discard(job_id)
            self.phases.extend(emitter.phases)


@pytest.fixture
def symbol() -> str:
    return unique_symbol()


@pytest.fixture
def harness(tmp_path: Path, symbol: str, monkeypatch: pytest.MonkeyPatch) -> Harness:
    _requires_ephemeral_db()
    lake = seed_lake(tmp_path, symbol)
    monkeypatch.setattr(planning, "sweep_roots", lambda: [lake])
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])
    h = Harness()
    monkeypatch.setattr(stages, "engine_executor", lambda **kwargs: h.engine)
    monkeypatch.setattr(stages, "default_approval", h.approval.binding)
    monkeypatch.setattr(lifecycle, "job_is_live", h.liveness)
    monkeypatch.setattr(golden_search_router, "run_in_thread", h.capture)
    return h


async def _lock(client: httpx.AsyncClient, symbol: str, *, key: str | None = None, **overrides: Any) -> dict[str, Any]:
    response = await client.post(f"{BASE}/studies", json={"protocol": plan_request(symbol, **overrides), "idempotency_key": key or _key()})
    assert response.status_code == 201, response.text
    return response.json()


async def _command(
    client: httpx.AsyncClient, study: dict[str, Any], command: str, payload: dict[str, Any] | None = None, *, key: str | None = None, revision: int | None = None
) -> httpx.Response:
    return await client.post(
        f"{BASE}/studies/{study['id']}/commands",
        json={
            "command": command,
            "expected_revision": study["revision"] if revision is None else revision,
            "idempotency_key": key or _key(),
            "payload": payload or {},
        },
    )


async def _start(client: httpx.AsyncClient, detail: dict[str, Any], job_id: str | None = None) -> str:
    job = job_id or f"job-{uuid.uuid4().hex[:12]}"
    started = await client.post(JOBS, json={**detail["dispatch"]["payload"], "job_id": job})
    assert started.status_code == 202, started.text
    assert started.json() == {"job_id": job, "study_id": detail["id"], "status": "queued"}
    return job


async def _advance(client: httpx.AsyncClient, harness: Harness, study: dict[str, Any], command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await _command(client, study, command, payload)
    assert response.status_code == 200, response.text
    detail = response.json()
    if detail["dispatch"] is not None:
        result = await harness.run(await _start(client, detail))
        assert result["study_id"] == detail["id"]
    fetched = await client.get(f"{BASE}/studies/{detail['id']}")
    assert fetched.status_code == 200, fetched.text
    return fetched.json()


async def test_a_study_runs_from_lock_to_an_approved_golden_configuration_over_http(
    client: httpx.AsyncClient, harness: Harness, symbol: str
) -> None:
    study = await _lock(client, symbol)
    assert (study["state"], study["presented_status"], study["dispatch"]) == ("locked", "idle", None)
    assert study["permitted_actions"] == ["continue", "close", "revise"]

    authorized = await _command(client, study, "continue")
    assert authorized.status_code == 200, authorized.text
    queued = authorized.json()
    assert queued["dispatch"]["job_type"] == "golden_search" and queued["presented_status"] == "queued"
    assert "finish" not in queued["permitted_actions"]
    await harness.run(await _start(client, queued))
    study = (await client.get(f"{BASE}/studies/{study['id']}")).json()
    assert study["state"] == "awaiting_validation"
    assert harness.phases == ["search", "pair_audits", "recent"]
    search = study["results"]["search"]
    assert len(search["pair_maps"]) == 1 and search["knob_summary"] and search["counts"]["evaluated"] > 0

    study = await _advance(client, harness, study, "continue")
    assert study["state"] == "awaiting_candidate"
    assert study["results"]["validation"]["summary_pills"]["judged"].endswith("folds judged")
    keys = [candidate["key"] for candidate in study["results"]["evidence"]["candidates"]]
    assert keys == ["incumbent", "all_period", "recent"]

    candidate = await client.get(f"{BASE}/studies/{study['id']}/candidates/all_period")
    assert candidate.status_code == 200, candidate.text
    development = candidate.json()["development"]
    assert development["cumulative_return"] and development["trades"] and candidate.json()["exam"] is None
    page = await client.get(f"{BASE}/studies/{study['id']}/evaluations", params={"stage": "search", "page": 1, "page_size": 5})
    assert page.status_code == 200 and page.json()["total"] > 5 and len(page.json()["rows"]) == 5

    study = await _advance(client, harness, study, "select_candidate", {"candidate_key": "all_period"})
    study = await _advance(client, harness, study, "open_exam", {"acknowledge_final_test": True})
    assert study["state"] == "awaiting_review" and study["results"]["exam"]["outcome"] == "meets_rules"
    assert (await client.get(f"{BASE}/studies/{study['id']}/candidates/all_period")).json()["exam"] is not None

    study = await _advance(client, harness, study, "approve", APPROVE)
    assert study["state"] == "approved" and study["guidance"]["headline"] == "Golden configuration ready in Deploy"
    deploy = study["results"]["qualification"]["deploy"]
    assert deploy["symbol"] == symbol and "symbol" not in deploy["parameters"]
    listed = await client.get(f"{BASE}/studies", params={"symbol": symbol})
    assert [row["id"] for row in listed.json()] == [study["id"]] and listed.json()[0]["qualification_id"] is not None
    # Every phase a stage reported has its label in the job vocabulary, in the order the stages ran.
    assert harness.phases == [phase.id for phase in JOB_PHASES["golden_search"]]


async def test_a_stale_revision_is_a_409_carrying_the_study_as_it_now_stands(client: httpx.AsyncClient, harness: Harness, symbol: str) -> None:
    study = await _lock(client, symbol)

    response = await _command(client, study, "close", {"note": "late"}, revision=study["revision"] + 3)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "STALE_REVISION" and detail["message"]
    assert detail["study"]["id"] == study["id"] and detail["study"]["revision"] == study["revision"]
    assert detail["study"]["permitted_actions"] == study["permitted_actions"]


async def test_one_idempotency_key_replays_its_command_and_refuses_a_different_one(client: httpx.AsyncClient, harness: Harness, symbol: str) -> None:
    study = await _lock(client, symbol)
    key = _key()

    first = await _command(client, study, "close", {"note": "done"}, key=key)
    replay = await _command(client, study, "close", {"note": "done"}, key=key)
    reused = await _command(client, study, "close", {"note": "something else"}, key=key)

    assert first.status_code == replay.status_code == 200
    assert replay.json()["revision"] == first.json()["revision"] == study["revision"] + 1
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT" and reused.json()["detail"]["study"]["state"] == "closed"


async def test_a_lock_key_reused_for_another_plan_is_a_409_and_the_same_plan_returns_its_study(
    client: httpx.AsyncClient, harness: Harness, symbol: str
) -> None:
    key = _key()
    first = await _lock(client, symbol, key=key)
    again = await _lock(client, symbol, key=key)

    other = await client.post(f"{BASE}/studies", json={"protocol": plan_request(symbol, budget_cap=4000), "idempotency_key": key})

    assert again["id"] == first["id"] and again["revision"] == first["revision"]
    assert other.status_code == 409 and other.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert other.json()["detail"]["study"]["id"] == first["id"]


async def test_a_refused_lock_is_a_400_naming_every_refusal(client: httpx.AsyncClient, harness: Harness, symbol: str) -> None:
    response = await client.post(
        f"{BASE}/studies",
        json={"protocol": plan_request(symbol, zoom={"points": 99, "refinements": 1, "passes": 1}, exam_min_trades=0), "idempotency_key": _key()},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    codes = [refusal["code"] for refusal in detail["refusals"]]
    assert len(codes) >= 2 and detail["code"] == codes[0] and detail["study"] is None
    assert (await client.get(f"{BASE}/studies", params={"symbol": symbol})).json() == []


async def test_a_command_the_state_does_not_permit_is_a_409_and_a_bad_payload_a_400(client: httpx.AsyncClient, harness: Harness, symbol: str) -> None:
    study = await _lock(client, symbol)

    not_now = await _command(client, study, "approve", APPROVE)
    bad_note = await _command(client, study, "close", {"note": 5})

    assert not_now.status_code == 409
    assert not_now.json()["detail"]["code"] == "COMMAND_NOT_PERMITTED"
    assert not_now.json()["detail"]["message"] == study["action_refusals"]["approve"]
    assert bad_note.status_code == 400
    assert (bad_note.json()["detail"]["code"], bad_note.json()["detail"]["field"]) == ("PAYLOAD_INVALID", "note")


async def test_revise_reads_its_plan_through_the_lock_schema(client: httpx.AsyncClient, harness: Harness, symbol: str) -> None:
    study = await _lock(client, symbol)
    plan = plan_request(symbol, budget_cap=4500)
    minimum = plan.pop("exam_min_trades")
    camel = {**plan, "examMinTrades": minimum}

    malformed = await _command(client, study, "revise", {"protocol": {**camel, "unknown_key": 1}})
    revised = await _command(client, study, "revise", {"protocol": camel})

    assert malformed.status_code == 422
    assert revised.status_code == 200, revised.text
    fork = revised.json()
    assert fork["id"] != study["id"] and fork["parent_study_id"] == study["id"]
    assert (fork["state"], fork["protocol"]["budget_cap"], fork["protocol"]["exam_min_trades"]) == ("locked", 4500, 1)
    assert (await client.get(f"{BASE}/studies/{study['id']}")).json()["revision"] == study["revision"]


async def test_the_jobs_entry_runs_only_the_stage_a_command_authorized(
    client: httpx.AsyncClient, harness: Harness, symbol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The jobs entry is unguarded: with a control secret configured and no header, the token alone decides.
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    guard = {CONTROL_SECRET_HEADER: "test-control-secret"}
    study = await _lock_guarded(client, symbol, guard)

    nothing_authorized = await client.post(JOBS, json={"job_id": "job-x", "study_id": study["id"], "stage_token": "f" * 32})
    authorized = await client.post(
        f"{BASE}/studies/{study['id']}/commands",
        json={"command": "continue", "expected_revision": study["revision"], "idempotency_key": _key(), "payload": {}},
        headers=guard,
    )
    token = authorized.json()["dispatch"]["payload"]["stage_token"]
    forged = await client.post(JOBS, json={"jobId": "job-y", "studyId": study["id"], "stageToken": "0" * 32})
    first = await client.post(JOBS, json={"jobId": "job-a", "studyId": study["id"], "stageToken": token})
    second_job = await client.post(JOBS, json={"jobId": "job-b", "studyId": study["id"], "stageToken": token})
    redelivered = await client.post(JOBS, json={"jobId": "job-a", "studyId": study["id"], "stageToken": token})
    missing = await client.post(JOBS, json={"jobId": "job-z", "studyId": "no-such-study", "stageToken": token})

    assert nothing_authorized.status_code == 409 and nothing_authorized.json()["detail"]["code"] == "STAGE_TOKEN_MISMATCH"
    assert forged.status_code == 409 and forged.json()["detail"]["code"] == "STAGE_TOKEN_MISMATCH"
    assert first.status_code == 202
    assert second_job.status_code == 409 and second_job.json()["detail"]["code"] == "NOTHING_PENDING"
    assert redelivered.status_code == 202
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "NOT_FOUND"
    assert list(harness.workers) == ["job-a"]  # one worker, however often the dispatch arrived

    harness.live.discard("job-a")
    closed_job = await client.post(JOBS, json={"jobId": "job-a", "studyId": study["id"], "stageToken": token})
    assert closed_job.status_code == 409  # a closed job is never replayed under its old id


async def _lock_guarded(client: httpx.AsyncClient, symbol: str, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post(f"{BASE}/studies", json={"protocol": plan_request(symbol), "idempotency_key": _key()}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_running_study_cannot_be_hidden_and_a_hidden_one_leaves_the_history(client: httpx.AsyncClient, harness: Harness, symbol: str) -> None:
    study = await _lock(client, symbol)
    queued = (await _command(client, study, "continue")).json()
    job = await _start(client, queued)

    running = await client.delete(f"{BASE}/studies/{study['id']}")
    await harness.run(job)
    hidden = await client.delete(f"{BASE}/studies/{study['id']}")

    assert running.status_code == 409 and running.json()["detail"]["code"] == "STUDY_RUNNING"
    assert running.json()["detail"]["study"]["presented_status"] == "queued"
    assert hidden.status_code == 204
    assert (await client.get(f"{BASE}/studies", params={"symbol": symbol})).json() == []
    with_hidden = (await client.get(f"{BASE}/studies", params={"symbol": symbol, "include_hidden": "true"})).json()
    assert [(row["id"], row["hidden"]) for row in with_hidden] == [(study["id"], True)]
    assert (await client.get(f"{BASE}/studies/{study['id']}")).status_code == 200


async def test_cancel_with_the_job_store_unreachable_is_a_503_and_records_nothing(
    client: httpx.AsyncClient, harness: Harness, symbol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await _lock(client, symbol)
    queued = (await _command(client, study, "continue")).json()
    await _start(client, queued)

    def unreachable(job_id: str) -> None:
        raise redis.ConnectionError("down")

    monkeypatch.setattr(lifecycle, "request_cancel", unreachable)
    response = await _command(client, queued, "cancel")

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "JOB_STORE_UNREACHABLE"
    assert (await client.get(f"{BASE}/studies/{study['id']}")).json()["revision"] == queued["revision"]


async def test_reads_of_a_missing_study_or_candidate_are_404_with_a_code(client: httpx.AsyncClient, harness: Harness, symbol: str) -> None:
    study = await _lock(client, symbol)
    missing = "0" * 32

    responses = [
        await client.get(f"{BASE}/studies/{missing}"),
        await client.get(f"{BASE}/studies/{missing}/evaluations"),
        await client.get(f"{BASE}/studies/{missing}/candidates/incumbent"),
        await client.delete(f"{BASE}/studies/{missing}"),
        await _command(client, {"id": missing, "revision": 0}, "continue"),
    ]
    before_evidence = await client.get(f"{BASE}/studies/{study['id']}/candidates/all_period")
    unknown_key = await client.get(f"{BASE}/studies/{study['id']}/candidates/best_guess")

    assert [(r.status_code, r.json()["detail"]["code"]) for r in responses] == [(404, "NOT_FOUND")] * 5
    assert before_evidence.status_code == 404 and before_evidence.json()["detail"]["code"] == "CANDIDATE_UNAVAILABLE"
    assert unknown_key.status_code == 422


async def test_preflight_of_a_lockable_plan_reports_its_estimate_folds_exposure_and_run_up(
    client: httpx.AsyncClient, harness: Harness, symbol: str
) -> None:
    plan = plan_request(symbol)

    response = await client.post(f"{BASE}/preflight", json=plan)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refusals"] == []
    assert body["estimate"]["budget_cap"] == plan["budget_cap"] and 0 < body["estimate"]["total_max"] <= plan["budget_cap"]
    assert [fold["fold_index"] for fold in body["folds"]] == [0, 1]
    assert body["exposure"]["state"] == "not_opened"
    assert body["run_up"]["data_start_ms"] < plan["development_start_ms"]


async def test_defaults_are_a_plan_the_client_can_send_straight_back(client: httpx.AsyncClient, harness: Harness, symbol: str) -> None:
    three = await client.get(f"{BASE}/defaults", params={"strategy_key": "ema_crossover_signal", "symbol": symbol.lower()})
    one = await client.get(f"{BASE}/defaults", params={"strategy_key": "ema_crossover_signal", "symbol": symbol, "final_months": 1})

    assert three.status_code == 200, three.text
    defaults = three.json()
    assert defaults["symbol"] == symbol and defaults["incumbent"]["source"] == "registry"
    assert defaults["incumbent_label"] and defaults["exposure"]["state"] == "not_opened"
    assert defaults["final_start_ms"] == defaults["development_end_ms"] < defaults["final_end_ms"]
    assert one.json()["final_end_ms"] == defaults["final_end_ms"] and one.json()["final_start_ms"] > defaults["final_start_ms"]
    plan = {key: value for key, value in defaults.items() if key not in ("incumbent_label", "exposure")}
    reviewed = await client.post(f"{BASE}/preflight", json=plan)
    assert reviewed.status_code == 200, reviewed.text
