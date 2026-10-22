"""The public jobs facade (``/api/jobs``): start, watch, collect and cancel a job.

The job store is an in-memory double and the worker routes are stubs on a small
app, so these tests prove what a browser sees and what the workers' own
``ProgressEmitter`` / ``CancellationCheck`` find in Redis afterwards. The real
emitter plays the worker wherever a job has to progress or finish.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import settings
from app.jobs import facade, progress
from app.jobs.progress import CancellationCheck, ProgressEmitter
from app.routers import jobs_facade
from app.security.data_plane_control import CONTROL_SECRET_HEADER

STATE = "job:{}:state"
ACTIVE = "jobs:active"


def _stream_id(entry_id: str) -> tuple[int, int]:
    millis, _, sequence = entry_id.partition("-")
    return int(millis), int(sequence or 0)


class _Pipeline:
    """redis-py's buffered pipeline: every command is queued and answered by ``execute``."""

    def __init__(self, store: _JobStore) -> None:
        self._store = store
        self._ops: list[Any] = []

    def __getattr__(self, command: str) -> Any:
        method = getattr(self._store, command)

        def queue(*args: Any, **kwargs: Any) -> _Pipeline:
            self._ops.append(lambda: method(*args, **kwargs))
            return self

        return queue

    def execute(self) -> list[Any]:
        results = [op() for op in self._ops]
        self._ops.clear()
        return results


class _BytesClient:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    def set(self, key: str, body: bytes, ex: int | None = None) -> None:
        self._blobs[key] = body

    def get(self, key: str) -> bytes | None:
        return self._blobs.get(key)

    def close(self) -> None:
        pass


class _JobStore:
    """Enough Redis for the job contract: hashes, sets, strings, blobs and event streams."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.strings: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.expires: list[tuple[str, int]] = []

    def pipeline(self) -> _Pipeline:
        return _Pipeline(self)

    def hset(
        self, key: str, name: str | None = None, value: str | None = None, mapping: dict[str, str] | None = None
    ) -> None:
        fields = self.hashes.setdefault(key, {})
        if name is not None:
            fields[name] = str(value)
        fields.update(mapping or {})

    def hget(self, key: str, name: str) -> str | None:
        return self.hashes.get(key, {}).get(name)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def exists(self, key: str) -> int:
        return int(key in self.hashes or key in self.strings or key in self.streams)

    def expire(self, key: str, seconds: int) -> int:
        self.expires.append((key, seconds))
        return 1

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value

    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    def srem(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).discard(member)

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def xadd(self, key: str, fields: dict[str, str], maxlen: int | None = None, approximate: bool = True) -> str:
        entries = self.streams.setdefault(key, [])
        entry_id = f"{len(entries) + 1}-0"
        entries.append((entry_id, dict(fields)))
        return entry_id

    def xrange(self, key: str, min: str = "-", max: str = "+") -> list[tuple[str, dict[str, str]]]:
        entries = self.streams.get(key, [])
        if min == "-":
            return list(entries)
        if min.startswith("("):
            return [entry for entry in entries if _stream_id(entry[0]) > _stream_id(min[1:])]
        return [entry for entry in entries if _stream_id(entry[0]) >= _stream_id(min)]

    def xread(self, streams: dict[str, str], count: int | None = None) -> list[Any]:
        ((key, last_id),) = streams.items()
        entries = [entry for entry in self.streams.get(key, []) if _stream_id(entry[0]) > _stream_id(last_id)]
        return [[key, entries[:count]]] if entries else []

    def delete(self, key: str) -> None:
        for table in (self.hashes, self.strings, self.streams):
            table.pop(key, None)


class _Worker:
    """The worker routes' stand-in: records every dispatch and answers as told."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.refuse: tuple[int, str] | None = None
        self.crash: Exception | None = None


def _build_app(worker: _Worker) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.get_trusted_hosts())
    app.include_router(jobs_facade.router)

    async def accept(request: Request) -> Response:
        body = await request.json()
        worker.received.append(
            {"route": request.url.path, "body": body, "secret": request.headers.get(CONTROL_SECRET_HEADER)}
        )
        if worker.crash is not None:
            raise worker.crash
        if worker.refuse is not None:
            code, text = worker.refuse
            return Response(content=text, status_code=code, media_type="application/json")
        return JSONResponse({"job_id": body["job_id"], "status": "queued"}, status_code=202)

    for route in set(facade.JOB_TYPE_ROUTES.values()):
        app.add_api_route(route, accept, methods=["POST"])
    return app


@pytest.fixture
def job_store(monkeypatch: pytest.MonkeyPatch) -> _JobStore:
    store = _JobStore()
    monkeypatch.setattr(progress, "get_redis", lambda: store)
    monkeypatch.setattr(progress, "_bytes_redis", lambda timeout_s=None: _BytesClient(store.blobs))
    return store


@pytest.fixture
def worker() -> _Worker:
    return _Worker()


@pytest.fixture
async def client(job_store: _JobStore, worker: _Worker) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_build_app(worker))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http


async def _start(client: httpx.AsyncClient, job_type: str = "engine_backtest", body: Any = None) -> str:
    response = await client.post(f"/api/jobs/{job_type}", json={"symbol": "SPY"} if body is None else body)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def _frames(text: str) -> list[tuple[str, dict[str, Any]]]:
    """The (entry id, event) pairs of an SSE body."""
    frames = []
    for chunk in filter(None, text.split("\n\n")):
        match = re.fullmatch(r"id: (\S+)\ndata: (.*)", chunk)
        assert match, f"not an SSE frame: {chunk!r}"
        frames.append((match.group(1), json.loads(match.group(2))))
    return frames


# ── start ────────────────────────────────────────────────────────────────


async def test_a_started_job_is_recorded_queued_and_dispatched_with_its_id(
    client: httpx.AsyncClient, job_store: _JobStore, worker: _Worker
) -> None:
    request_body = {"backtest": {"symbol": "SPY", "fromDate": "2026-01-05"}, "note": "é"}

    response = await client.post("/api/jobs/engine_backtest", json=request_body)

    assert response.status_code == 202
    job_id = response.json()["id"]
    assert response.json() == {"id": job_id, "status": "queued"}
    assert response.headers["location"] == f"/api/jobs/{job_id}/events"
    state = job_store.hashes[STATE.format(job_id)]
    assert {key: state[key] for key in ("id", "type", "status", "cancel_requested")} == {
        "id": job_id,
        "type": "engine_backtest",
        "status": "queued",
        "cancel_requested": "0",
    }
    assert state["created_at"].isdigit()
    assert json.loads(state["params"]) == request_body
    assert (STATE.format(job_id), 60 * 60 * 24) in job_store.expires
    assert job_store.sets[ACTIVE] == {job_id}
    assert worker.received == [
        {"route": "/api/jobs-internal/engine-backtest", "body": {**request_body, "job_id": job_id}, "secret": None}
    ]


async def test_algorithm_source_reaches_the_worker_but_is_never_persisted(
    client: httpx.AsyncClient, job_store: _JobStore, worker: _Worker
) -> None:
    request_body = {
        "request": {"template": "t", "algorithm_source": "class A: ..."},
        "algorithm_source": "top level",
        "runs": [{"algorithm_source": "in a list", "keep": 1}, [{"algorithm_source": "nested list"}]],
    }

    job_id = await _start(client, "lean_engine_run", request_body)

    persisted = json.loads(job_store.hashes[STATE.format(job_id)]["params"])
    assert persisted == {"request": {"template": "t"}, "runs": [{"keep": 1}, [{}]]}
    assert worker.received[0]["body"] == {**request_body, "job_id": job_id}


async def test_an_unsupported_job_type_is_refused_before_anything_is_recorded(
    client: httpx.AsyncClient, job_store: _JobStore, worker: _Worker
) -> None:
    response = await client.post("/api/jobs/make_coffee", json={})

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported job type 'make_coffee'"
    assert response.json()["supported"] == list(facade.JOB_TYPE_ROUTES)
    assert "data_lake_backfill" in response.json()["supported"]
    assert job_store.hashes == {} and job_store.sets == {}
    assert worker.received == []


@pytest.mark.parametrize(
    ("raw", "dispatched_without_id", "refused"),
    [
        pytest.param(b"{not json", None, True, id="malformed_json_is_refused"),
        pytest.param(b"", None, True, id="an_empty_body_is_refused"),
        pytest.param(b"[1, 2]", {}, False, id="a_non_object_starts_the_job_with_no_parameters"),
        pytest.param(b"null", {}, False, id="null_starts_the_job_with_no_parameters"),
    ],
)
async def test_only_a_json_object_carries_parameters(
    client: httpx.AsyncClient,
    job_store: _JobStore,
    worker: _Worker,
    raw: bytes,
    dispatched_without_id: dict[str, Any] | None,
    refused: bool,
) -> None:
    response = await client.post("/api/jobs/engine_backtest", content=raw, headers={"Content-Type": "application/json"})

    if refused:
        assert response.status_code == 400
        assert job_store.hashes == {} and worker.received == []
        return
    assert response.status_code == 202
    job_id = response.json()["id"]
    assert worker.received[0]["body"] == {**(dispatched_without_id or {}), "job_id": job_id}
    assert json.loads(job_store.hashes[STATE.format(job_id)]["params"]) == {}


async def test_a_lone_surrogate_in_the_body_is_carried_to_the_worker_as_sent(
    client: httpx.AsyncClient, worker: _Worker
) -> None:
    response = await client.post(
        "/api/jobs/engine_backtest",
        content=b'{"note": "\\ud800"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 202
    assert worker.received[0]["body"]["note"] == "\ud800"


@pytest.mark.parametrize(
    ("job_type", "configured_secret", "expected_header"),
    [
        pytest.param("data_lake_backfill", "s3cret", "s3cret", id="the_backfill_carries_the_secret"),
        pytest.param("options_golden_search_history", "s3cret", "s3cret", id="the_history_capture_carries_the_secret"),
        pytest.param("engine_backtest", "s3cret", None, id="any_other_type_does_not"),
        pytest.param("data_lake_backfill", "", None, id="an_unconfigured_secret_is_not_sent"),
    ],
)
async def test_the_control_secret_is_sent_only_to_the_two_data_writing_job_types(
    client: httpx.AsyncClient,
    worker: _Worker,
    monkeypatch: pytest.MonkeyPatch,
    job_type: str,
    configured_secret: str,
    expected_header: str | None,
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", configured_secret)

    await _start(client, job_type)

    assert worker.received[0]["secret"] == expected_header


@pytest.mark.parametrize("refusal", [(409, '{"detail":"a study is already running"}'), (422, "")])
async def test_a_refused_start_marks_the_job_failed_and_returns_the_refusal(
    client: httpx.AsyncClient, job_store: _JobStore, worker: _Worker, refusal: tuple[int, str]
) -> None:
    worker.refuse = refusal
    code, text = refusal

    response = await client.post("/api/jobs/grid_search", json={"x": 1})

    assert response.status_code == code
    assert response.headers["content-type"].startswith("application/problem+json")
    # The Frontend parses the worker's JSON out of this string (toRefusal).
    assert response.json()["detail"] == text
    assert response.json()["status"] == code
    [job_id] = {key.split(":")[1] for key in job_store.hashes}
    state = job_store.hashes[STATE.format(job_id)]
    assert (state["status"], state["error_code"], state["error_message"]) == ("failed", "PythonRejected", text)
    assert job_store.sets[ACTIVE] == set()


async def test_a_worker_route_that_cannot_answer_marks_the_job_failed_with_a_502(
    client: httpx.AsyncClient, job_store: _JobStore, worker: _Worker
) -> None:
    worker.crash = RuntimeError("the route fell over")

    response = await client.post("/api/jobs/grid_search", json={"x": 1})

    assert response.status_code == 502
    assert response.json()["detail"] == "the route fell over"
    [job_id] = {key.split(":")[1] for key in job_store.hashes}
    state = job_store.hashes[STATE.format(job_id)]
    assert (state["status"], state["error_code"], state["error_message"]) == (
        "failed",
        "RuntimeError",
        "the route fell over",
    )
    assert job_store.sets[ACTIVE] == set()


async def test_a_worker_thread_starts_a_job_through_the_running_app(
    job_store: _JobStore, worker: _Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_module

    monkeypatch.setattr(main_module, "app", _build_app(worker))

    outcome = await asyncio.to_thread(
        facade.launch_job_from_worker_thread, "lean_engine_run", {"request": {"run_id": "r1"}}
    )

    assert outcome.accepted and outcome.status_code == 202
    assert worker.received == [
        {
            "route": "/api/jobs-internal/lean-engine-run",
            "body": {"request": {"run_id": "r1"}, "job_id": outcome.job_id},
            "secret": None,
        }
    ]
    assert job_store.hashes[STATE.format(outcome.job_id)]["status"] == "queued"


def test_every_job_type_runs_on_a_route_the_served_app_has() -> None:
    from app.main import app

    posted = {route.path for route in app.routes if "POST" in getattr(route, "methods", ())}

    assert {job_type: route for job_type, route in facade.JOB_TYPE_ROUTES.items() if route not in posted} == {}


@pytest.mark.parametrize(
    ("secret", "allow_unauthenticated", "status_code", "evidence"),
    [
        pytest.param("s3cret", False, 422, '"loc":["body","spec"]', id="a_configured_secret_passes_the_guard"),
        pytest.param(
            "",
            False,
            503,
            "fleet_control_plane_not_installed",
            id="an_unconfigured_secret_is_refused_as_for_any_caller",
        ),
    ],
)
async def test_a_data_writing_job_goes_through_the_real_apps_host_and_control_secret_guards(
    job_store: _JobStore,
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
    allow_unauthenticated: bool,
    status_code: int,
    evidence: str,
) -> None:
    """The empty body is refused by the route's own validation, which runs only after the guard let the call in."""
    from app.main import app

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", secret)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", allow_unauthenticated)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http:
        response = await http.post("/api/jobs/data_lake_backfill", json={})

    assert response.status_code == status_code
    assert evidence in response.json()["detail"].replace(" ", "")
    [state] = job_store.hashes.values()
    assert state["status"] == "failed" and state["error_code"] == "PythonRejected"


# ── events (SSE) ─────────────────────────────────────────────────────────


async def test_a_finished_jobs_events_replay_in_order_with_their_entry_ids(
    client: httpx.AsyncClient,
) -> None:
    job_id = await _start(client)
    emitter = ProgressEmitter(job_id)
    emitter.started()
    emitter.progress(1, 2)
    emitter.completed({"ok": True})

    response = await client.get(f"/api/jobs/{job_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    frames = _frames(response.text)
    assert [(entry_id, event["type"]) for entry_id, event in frames] == [
        ("1-0", "job.started"),
        ("2-0", "job.progress"),
        ("3-0", "job.completed"),
    ]
    assert frames[2][1]["result_url"] == f"/api/jobs/{job_id}/result"


async def test_last_event_id_resumes_after_that_entry(client: httpx.AsyncClient) -> None:
    job_id = await _start(client)
    emitter = ProgressEmitter(job_id)
    emitter.started()
    emitter.progress(1, 2)
    emitter.failed("boom", "it broke")

    response = await client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": "2-0"})

    assert [(entry_id, event["type"]) for entry_id, event in _frames(response.text)] == [("3-0", "job.failed")]


async def test_a_malformed_last_event_id_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/jobs/any/events", headers={"Last-Event-ID": "not-an-id"})

    assert response.status_code == 400


async def test_a_running_job_holds_its_stream_open_until_it_finishes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(facade, "SSE_POLL_INTERVAL_SECONDS", 0.01)
    job_id = await _start(client)
    emitter = ProgressEmitter(job_id)
    emitter.started()
    stream = asyncio.create_task(client.get(f"/api/jobs/{job_id}/events"))
    try:
        await asyncio.sleep(0.1)
        assert not stream.done(), "a running job's stream ended early"

        emitter.progress(1, 2)
        emitter.completed({"ok": True})
        response = await asyncio.wait_for(stream, timeout=5)
    finally:
        stream.cancel()

    assert [event["type"] for _, event in _frames(response.text)] == ["job.started", "job.progress", "job.completed"]


async def test_a_client_that_goes_away_ends_the_stream_of_a_job_still_running(
    job_store: _JobStore, worker: _Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(facade, "SSE_POLL_INTERVAL_SECONDS", 0.01)
    job_store.hashes[STATE.format("running-job")] = {"id": "running-job", "status": "running"}
    ProgressEmitter("running-job").started()
    disconnected = asyncio.Event()
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/jobs/running-job/events",
        "raw_path": b"/api/jobs/running-job/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 1),
        "server": ("localhost", 80),
    }
    request = asyncio.create_task(_build_app(worker)(scope, receive, send))
    await asyncio.sleep(0.1)
    assert not request.done() and any(m["type"] == "http.response.body" and m.get("body") for m in sent)

    disconnected.set()

    await asyncio.wait_for(request, timeout=5)


async def test_an_event_landing_between_the_last_read_and_the_status_flip_is_still_delivered(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = await _start(client)
    emitter = ProgressEmitter(job_id)
    emitter.started()

    def final_emit_lands_after_an_empty_read(_job_id: str, _last_id: str) -> list[Any]:
        emitter.completed({"ok": True})
        return []

    monkeypatch.setattr(facade, "_read_after", final_emit_lands_after_an_empty_read)

    response = await asyncio.wait_for(client.get(f"/api/jobs/{job_id}/events"), timeout=5)

    assert [event["type"] for _, event in _frames(response.text)] == ["job.started", "job.completed"]


# ── result and download ──────────────────────────────────────────────────


async def test_a_jobs_result_is_served_as_the_json_it_stored(client: httpx.AsyncClient) -> None:
    job_id = await _start(client)
    missing = await client.get(f"/api/jobs/{job_id}/result")
    ProgressEmitter(job_id).completed({"answer": 42, "rows": [1, 2]})

    found = await client.get(f"/api/jobs/{job_id}/result")

    assert missing.status_code == 404
    assert missing.json() == {"error": "result not found or expired"}
    assert found.status_code == 200
    assert found.headers["content-type"] == "application/json"
    assert found.text == '{"answer": 42, "rows": [1, 2]}'


@pytest.mark.parametrize(
    ("filename", "disposition"),
    [
        pytest.param("bundle 1.zip", 'attachment; filename="bundle 1.zip"', id="an_ascii_name_travels_as_is"),
        pytest.param(
            'é"x.zip',
            "attachment; filename=\"__x.zip\"; filename*=UTF-8''%C3%A9%22x.zip",
            id="a_non_ascii_name_also_travels_as_an_rfc_5987_name",
        ),
    ],
)
async def test_a_jobs_blob_downloads_under_the_name_and_type_it_recorded(
    client: httpx.AsyncClient, filename: str, disposition: str
) -> None:
    job_id = await _start(client)
    ProgressEmitter(job_id).completed_blob(
        filename=filename, content_type="application/zip", body=b"PK\x03\x04\x00\xff"
    )

    response = await client.get(f"/api/jobs/{job_id}/download")

    assert response.status_code == 200
    assert response.content == b"PK\x03\x04\x00\xff"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == disposition


async def test_a_download_that_is_absent_says_which_part_is_missing(
    client: httpx.AsyncClient, job_store: _JobStore
) -> None:
    nothing = await client.get("/api/jobs/gone/download")
    job_store.hashes["job:half:result-meta"] = {"filename": "x.zip"}
    half = await client.get("/api/jobs/half/download")

    assert (nothing.status_code, nothing.json()) == (404, {"error": "blob result not found or expired"})
    assert (half.status_code, half.json()) == (404, {"error": "blob payload missing (meta present)"})


# ── cancel and list ──────────────────────────────────────────────────────


async def test_cancelling_a_job_raises_the_flag_its_worker_polls(client: httpx.AsyncClient) -> None:
    job_id = await _start(client)
    check = CancellationCheck(job_id)
    assert check.should_cancel() is False

    response = await client.delete(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json() == {"id": job_id, "cancel_requested": True}
    assert CancellationCheck(job_id).should_cancel() is True


async def test_cancelling_an_unknown_job_is_a_404(client: httpx.AsyncClient) -> None:
    response = await client.delete("/api/jobs/never-existed")

    assert (response.status_code, response.json()) == (404, {"error": "job not found"})


async def test_the_active_list_returns_live_state_records_and_forgets_expired_ones(
    client: httpx.AsyncClient, job_store: _JobStore
) -> None:
    first = await _start(client)
    second = await _start(client, "grid_search")
    job_store.sets[ACTIVE].add("expired-job")
    ProgressEmitter(second).failed("boom", "it broke")  # leaves the active set itself
    third = await _start(client, "signal_engine")

    listed = await client.get("/api/jobs", params={"active": "true"})
    by_default = await client.get("/api/jobs")
    inactive = await client.get("/api/jobs", params={"active": "false"})

    assert [job["id"] for job in listed.json()] == sorted([first, third])
    assert {job["type"] for job in listed.json()} == {"engine_backtest", "signal_engine"}
    assert by_default.json() == listed.json()
    assert inactive.json() == []
    assert "expired-job" not in job_store.sets[ACTIVE]


async def test_internal_job_uses_normal_cancel_and_result_without_an_extra_public_card(
    client: httpx.AsyncClient, job_store: _JobStore,
) -> None:
    parent_id = await _start(client)
    child_id = facade.queue_internal_job("internal-preparation", {"parent_job_id": parent_id})
    emit = ProgressEmitter(child_id)
    emit.started()
    listed = await client.get("/api/jobs")
    assert [row["id"] for row in listed.json()] == [parent_id]
    refused = await client.post("/api/jobs/internal-preparation", json={})
    assert refused.status_code == 400
    cancelled = await client.delete(f"/api/jobs/{child_id}")
    assert cancelled.status_code == 200
    assert CancellationCheck(child_id).should_cancel()
    emit.completed({"frozen": "verified"})
    result = await client.get(f"/api/jobs/{child_id}/result")
    assert result.status_code == 200 and result.json() == {"frozen": "verified"}
