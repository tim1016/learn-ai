"""The Options Backtest runs over HTTP (#3030): the job that makes a run, Check data, the history, one run, delete.

The job route and Check data need no database: the run store is replaced by one in memory, and the vendor is faked at
the HTTP layer over a lake in a temporary folder. The routes that read the history, open a run and delete one run
only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one, like their
neighbours. Hand-worked figures are in ``tests/options_backtest/test_service.py``, whose scene these tests reuse.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.jobs.phases import JOB_PHASES
from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.main import app
from app.options_backtest import service
from app.options_backtest.run import RunResult
from app.options_backtest.saved_runs import DuplicateJob, NewRun, RunFailure
from app.options_backtest.service import RunFailedError
from app.routers import options_backtest_runs as runs_router
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_day_end_ms, et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_lake import Vendor
from tests.options_backtest.setup_fixtures import leg, setup_body
from tests.options_backtest.test_service import FRI, NOW_MS, TUE, the_scene

PREFIX = "/api/research/options-backtest"
JOBS = "/api/jobs-internal/options-backtest"
D = Decimal


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@dataclass
class MemoryRunStore:
    """The run store without a database: it hands out ids, refuses a job it has seen, and remembers how runs ended."""

    created: list[NewRun] = field(default_factory=list)
    completed: list[tuple[int, RunResult, int]] = field(default_factory=list)
    failed: list[tuple[int, RunFailure, int]] = field(default_factory=list)
    cancelled: list[tuple[int, int]] = field(default_factory=list)

    async def create(self, new: NewRun) -> int:
        if any(each.job_id == new.job_id for each in self.created):
            raise DuplicateJob(f"job {new.job_id} already has a run")
        self.created.append(new)
        return 40 + len(self.created)

    def complete(self, run_id: int, result: RunResult, *, finished_at_ms: int) -> None:
        self.completed.append((run_id, result, finished_at_ms))

    def fail(self, run_id: int, failure: RunFailure, *, finished_at_ms: int) -> None:
        self.failed.append((run_id, failure, finished_at_ms))

    def cancel(self, run_id: int, *, finished_at_ms: int) -> None:
        self.cancelled.append((run_id, finished_at_ms))


class FakeEmitter:
    """The job's emitter, remembered."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    @property
    def phases(self) -> list[str]:
        return [value for kind, value in self.events if kind == "phase"]

    def phase(self, name: str) -> None:
        self.events.append(("phase", name))

    def progress(self, current: int, total: int, *, unit: str = "bars", message: str | None = None) -> None:
        self.events.append(("progress", (current, total, unit)))

    def log(self, message: str, *, level: str = "info") -> None:
        self.events.append(("log", message))


class FakeCancel:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def should_cancel(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled("cancelled")


@dataclass
class Harness:
    store: MemoryRunStore
    started: list[tuple[str, Callable[..., Any], dict[str, Any]]] = field(default_factory=list)

    def capture(self, job_id: str, work: Callable[..., Any], **kwargs: Any) -> None:
        self.started.append((job_id, work, kwargs))

    def the_work(self) -> Callable[..., Any]:
        [(_, work, _)] = self.started
        return work


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = Harness(MemoryRunStore())
    monkeypatch.setattr(runs_router, "run_store", lambda: h.store)
    monkeypatch.setattr(runs_router, "run_in_thread", h.capture)
    monkeypatch.setattr(runs_router, "now_ms_utc", lambda: NOW_MS)
    return h


def job_body(**changes: Any) -> dict[str, Any]:
    return {
        "job_id": "job-1",
        "setup": setup_body(
            legs=[leg(expiry={"kind": "same_day"})],
            fees={"kind": "per_contract", "per_contract_usd": "0.65"},
        ),
        "start_ms": et_midnight_ms(TUE),
        "end_ms": et_day_end_ms(FRI),
        **changes,
    }


# ── The job route ────────────────────────────────────────────────────────


async def test_a_run_is_accepted_with_its_row_made_and_a_worker_started(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    async with client as c:
        response = await c.post(JOBS, json=job_body(setup_name="SPY premium momentum, no re-entry"))

    assert response.status_code == 202, response.text
    assert response.json() == {"job_id": "job-1", "run_id": 41, "status": "queued"}
    [new] = harness.store.created
    assert (new.job_id, new.created_at_ms, new.setup_name, new.sessions_requested) == (
        "job-1",
        NOW_MS,
        "SPY premium momentum, no re-entry",
        4,
    )
    assert new.first_session_open_ms == session_window_for_date(TUE).open_ms_utc
    assert new.last_session_open_ms == session_window_for_date(FRI).open_ms_utc
    assert [each.leg_id for each in new.setup.legs] == ["call"]
    [(job_id, _work, kwargs)] = harness.started
    assert (
        job_id == "job-1"
        and kwargs["cancel_check_every_n"] == 1
        and kwargs["thread_name"].startswith("options-backtest-")
    )


async def test_the_setup_is_stored_as_the_server_read_it_with_its_defaults_filled(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    body = job_body()
    assert "slippage_percent" not in body["setup"] and "price_step_usd" not in body["setup"]

    async with client as c:
        await c.post(JOBS, json=body)

    [new] = harness.store.created
    assert (new.setup.slippage_percent, new.setup.price_step_usd) == (D("0.3"), D("0.01"))


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"start_ms": et_midnight_ms(FRI), "end_ms": et_midnight_ms(TUE)}, "OPTIONS_BACKTEST_RANGE_EMPTY"),
        (
            {"start_ms": et_midnight_ms(date(2026, 10, 10)), "end_ms": et_day_end_ms(date(2026, 10, 11))},
            "OPTIONS_BACKTEST_RANGE_HAS_NO_SESSION",
        ),
        ({"end_ms": et_midnight_ms(date(2300, 1, 1))}, "OPTIONS_BACKTEST_RANGE_TOO_FAR"),
    ],
)
async def test_a_range_that_cannot_be_run_is_a_400_with_a_code_and_makes_no_run_and_no_job(
    client: httpx.AsyncClient, harness: Harness, changes: dict[str, Any], code: str
) -> None:
    async with client as c:
        response = await c.post(JOBS, json=job_body(**changes))

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == code and response.json()["detail"]["message"]
    assert harness.store.created == [] and harness.started == []


async def test_a_range_longer_than_a_run_takes_is_a_400_naming_the_limit(
    client: httpx.AsyncClient, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_RANGE_SESSIONS", 3)

    async with client as c:
        response = await c.post(JOBS, json=job_body())

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RANGE_TOO_LONG"
    assert "4 trading days" in response.json()["detail"]["message"]
    assert "at most 3" in response.json()["detail"]["message"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda body: body.update(job_id=""), id="empty-job-id"),
        pytest.param(lambda body: body.pop("job_id"), id="no-job-id"),
        pytest.param(lambda body: body.pop("setup"), id="no-setup"),
        pytest.param(lambda body: body["setup"].update(legs=[]), id="setup-without-legs"),
        pytest.param(lambda body: body["setup"].update(lots=1), id="setup-with-an-unknown-field"),
        pytest.param(lambda body: body.update(shortcut=True), id="unknown-field"),
        pytest.param(lambda body: body.update(start_ms=-1), id="negative-start"),
        pytest.param(lambda body: body.update(end_ms=MAX_TIMESTAMP_MS + 1), id="end-beyond-the-ceiling"),
        pytest.param(lambda body: body.update(setup_name="semi;colon"), id="a-name-outside-the-rule"),
        pytest.param(lambda body: body.update(start_ms="2026-10-06"), id="a-date-string-for-an-instant"),
    ],
)
async def test_a_body_that_is_not_a_run_request_is_a_422_before_anything_is_made(
    client: httpx.AsyncClient, harness: Harness, mutate: Callable[[dict[str, Any]], Any]
) -> None:
    body = job_body()
    mutate(body)

    async with client as c:
        response = await c.post(JOBS, json=body)

    assert response.status_code == 422
    assert harness.store.created == [] and harness.started == []


async def test_a_job_that_already_has_a_run_is_a_409_and_starts_no_second_worker(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    async with client as c:
        first = await c.post(JOBS, json=job_body())
        second = await c.post(JOBS, json=job_body())

    assert first.status_code == 202 and second.status_code == 409
    assert second.json()["detail"]["code"] == "OPTIONS_BACKTEST_JOB_ALREADY_HAS_A_RUN"
    assert len(harness.started) == 1


async def test_a_worker_that_cannot_be_started_leaves_the_run_failed_and_not_running(
    client: httpx.AsyncClient, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cannot_start(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("redis is down")

    monkeypatch.setattr(runs_router, "run_in_thread", cannot_start)

    async with client as c:
        with pytest.raises(RuntimeError, match="redis is down"):
            await c.post(JOBS, json=job_body())

    [(run_id, failure, finished_at_ms)] = harness.store.failed
    assert (run_id, failure.code, finished_at_ms) == (41, "JOB_NOT_STARTED", NOW_MS)


# ── The worker the route starts ──────────────────────────────────────────


@respx.mock
async def test_the_worker_runs_the_service_and_reports_the_phases_and_progress_the_jobs_boundary_lists(
    client: httpx.AsyncClient, harness: Harness, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    async with client as c:
        await c.post(JOBS, json=job_body())
    emitter = FakeEmitter()

    result = await asyncio.to_thread(harness.the_work(), emitter, FakeCancel())

    # A phase the job reports is one the vocabulary lists, in the order it lists them.
    listed = [phase.id for phase in JOB_PHASES["options_backtest"]]
    assert emitter.phases == ["checking_data", "downloading_data", "replaying", "saving"]
    assert [listed.index(phase) for phase in emitter.phases] == sorted(listed.index(phase) for phase in emitter.phases)
    # Progress is counted in sessions: four fetched, then four replayed.
    assert [value for kind, value in emitter.events if kind == "progress"] == [
        (n, 4, "sessions") for n in (1, 2, 3, 4)
    ] * 2
    # Net profit -52.65 + 36.70 - 53.30 = -69.25 over four sessions and three trades (test_service.py works it out).
    assert result == {
        "run_id": 41,
        "status": "complete",
        "sessions_tested": 4,
        "trades": 3,
        "net_profit_usd": "-69.25",
    }
    [(saved_run_id, saved, _)] = harness.store.completed
    assert saved_run_id == 41 and saved.summary.net_profit_usd == D("-69.25")


@respx.mock
async def test_a_cancel_the_worker_sees_ends_the_job_as_cancelled_and_the_run_as_cancelled(
    client: httpx.AsyncClient, harness: Harness, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    async with client as c:
        await c.post(JOBS, json=job_body())

    with pytest.raises(JobCancelled):
        await asyncio.to_thread(harness.the_work(), FakeEmitter(), FakeCancel(cancelled=True))

    assert [run_id for run_id, _ in harness.store.cancelled] == [41]
    assert harness.store.completed == [] and harness.store.failed == []


@respx.mock
async def test_a_failed_run_fails_the_job_with_the_plain_reason(
    client: httpx.AsyncClient, harness: Harness, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # One session older than the options plan reaches: check data refuses the whole run.
    old = date(2024, 10, 11)
    Vendor(the_scene()).mount()
    async with client as c:
        await c.post(JOBS, json=job_body(start_ms=et_midnight_ms(old), end_ms=et_day_end_ms(old)))

    with pytest.raises(RunFailedError) as caught:
        await asyncio.to_thread(harness.the_work(), FakeEmitter(), FakeCancel())

    assert "cannot be tested" in str(caught.value) and "2024-10-11" in str(caught.value)
    [(_, failure, _)] = harness.store.failed
    assert failure.code == "SESSIONS_CANNOT_BE_TESTED"
