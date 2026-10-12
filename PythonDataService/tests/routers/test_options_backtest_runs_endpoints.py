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

import asyncpg
import httpx
import pytest
import respx
from httpx import ASGITransport

from app.data_lake.catalog_client import CatalogUnavailableError
from app.jobs.phases import JOB_PHASES
from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.main import app
from app.options_backtest import service
from app.options_backtest.fetching import ensure_run_data
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.saved_runs import (
    DuplicateJob,
    FailureItem,
    NewRun,
    RunFailure,
    complete_run,
    create_run,
    fail_run,
)
from app.options_backtest.service import RunFailedError
from app.research.persistence import lifecycle
from app.routers import options_backtest_runs as runs_router
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_day_end_ms, et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import fake_data
from tests.options_backtest._run_lake import Vendor
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import leg, setup_body
from tests.options_backtest.setup_fixtures import unique as unique
from tests.options_backtest.test_service import FRI, NOW_MS, SESSION_OF_THE_RANGE, THU, TUE, WED, the_scene, the_setup

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


# ── Check data ───────────────────────────────────────────────────────────


@pytest.fixture
def now_is_monday_afternoon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs_router, "now_ms_utc", lambda: NOW_MS)


def check_body(first: date, last: date, **changes: Any) -> dict[str, Any]:
    return {
        "setup": job_body()["setup"],
        "start_ms": et_midnight_ms(first),
        "end_ms": et_day_end_ms(last),
        **changes,
    }


@respx.mock
async def test_check_data_says_for_each_session_what_is_stored_what_is_missing_and_what_can_be_tested(
    client: httpx.AsyncClient, now_is_monday_afternoon: None, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # Wednesday and Thursday are fetched; Tuesday and Friday are not.
    Vendor(the_scene()).mount()
    await asyncio.to_thread(ensure_run_data, the_setup(), [WED, THU], now_ms=NOW_MS, should_stop=lambda: False)

    async with client as c:
        response = await c.post(f"{PREFIX}/check-data", json=check_body(TUE, FRI))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["option_prices_from_ms"] == et_midnight_ms(date(2024, 10, 14))
    assert [(s["session_open_ms"], s["verdict"], s["missing"]) for s in body["sessions"]] == [
        (session_window_for_date(TUE).open_ms_utc, "needs_download", ["underlying_bars", "listing"]),
        (session_window_for_date(WED).open_ms_utc, "ready", []),
        (session_window_for_date(THU).open_ms_utc, "ready", []),
        (session_window_for_date(FRI).open_ms_utc, "needs_download", ["underlying_bars", "listing"]),
    ]
    tuesday, wednesday = body["sessions"][0], body["sessions"][1]
    assert tuesday["contracts"] == {"state": "not_yet_known", "needed": None, "missing": None}
    assert wednesday["contracts"] == {"state": "known", "needed": 1, "missing": 0}
    assert body["counts"] == {"ready": 2, "needs_download": 2, "cannot_be_tested": 0}
    assert body["first_testable_session_open_ms"] == session_window_for_date(TUE).open_ms_utc
    assert body["last_testable_session_open_ms"] == session_window_for_date(FRI).open_ms_utc
    # All four can be tested (a session that needs a download can be), so the run available is the whole range, and
    # the pair of instants to ask for it is the range's own.
    assert body["longest_testable_stretch"] == {
        "start_ms": et_midnight_ms(TUE),
        "end_ms": et_day_end_ms(FRI),
        "first_session_open_ms": session_window_for_date(TUE).open_ms_utc,
        "last_session_open_ms": session_window_for_date(FRI).open_ms_utc,
        "sessions": 4,
    }


@respx.mock
async def test_check_data_gives_each_reason_a_session_cannot_be_tested_and_the_range_that_can(
    client: httpx.AsyncClient, now_is_monday_afternoon: None, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # Thursday 2024-10-10 and Friday 2024-10-11 are older than the plan reaches (the floor is 2024-10-14, a Monday).
    first, last = date(2024, 10, 10), date(2024, 10, 14)

    async with client as c:
        response = await c.post(f"{PREFIX}/check-data", json=check_body(first, last))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [s["verdict"] for s in body["sessions"]] == ["cannot_be_tested", "cannot_be_tested", "needs_download"]
    for refused in body["sessions"][:2]:
        assert [problem["code"] for problem in refused["problems"]] == ["older_than_the_data_plan"]
    assert body["counts"] == {"ready": 0, "needs_download": 1, "cannot_be_tested": 2}
    stretch = body["longest_testable_stretch"]
    assert (stretch["start_ms"], stretch["end_ms"], stretch["sessions"]) == (
        et_midnight_ms(last),
        et_day_end_ms(last),
        1,
    )
    assert body["first_testable_session_open_ms"] == body["last_testable_session_open_ms"]


async def test_check_data_on_a_range_that_cannot_be_run_is_a_400_and_a_body_that_is_not_one_a_422(
    client: httpx.AsyncClient, now_is_monday_afternoon: None
) -> None:
    async with client as c:
        reversed_range = await c.post(f"{PREFIX}/check-data", json=check_body(FRI, TUE))
        no_setup = await c.post(f"{PREFIX}/check-data", json={"start_ms": 1, "end_ms": 2})
        extra = await c.post(f"{PREFIX}/check-data", json=check_body(TUE, FRI, job_id="x"))

    assert reversed_range.status_code == 400
    assert reversed_range.json()["detail"]["code"] == "OPTIONS_BACKTEST_RANGE_EMPTY"
    assert no_setup.status_code == 422 and extra.status_code == 422


async def test_check_data_when_the_catalog_cannot_be_reached_is_a_503_with_a_code(
    client: httpx.AsyncClient, now_is_monday_afternoon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*_args: Any, **_kwargs: Any) -> None:
        raise CatalogUnavailableError("down")

    monkeypatch.setattr(runs_router, "check_data", down)

    async with client as c:
        response = await c.post(f"{PREFIX}/check-data", json=check_body(TUE, FRI))

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_DATA_CATALOG_UNAVAILABLE"


# ── The history, one run, delete: on a real database ─────────────────────


@pytest.fixture
def is_the_job_alive(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool | None]:
    """What the routes are told of every running job; a test sets it. The default is 'cannot tell'."""
    answers: dict[str, bool | None] = {"live": None}
    monkeypatch.setattr(lifecycle, "job_is_live", lambda _job_id: answers["live"])
    return answers


@pytest.fixture
async def stored_runs(conn: asyncpg.Connection, unique: str):  # type: ignore[no-untyped-def]
    """Make runs the way the job route and the worker do, and remove every one this test made."""
    result = run_sessions(the_setup(), SESSION_OF_THE_RANGE, fake_data(the_scene()))

    async def make(name: str, end: str = "complete", created_at_ms: int = 1_791_800_000_000) -> int:
        run_id = await create_run(
            conn,
            NewRun(
                job_id=f"{name}-{unique}",
                created_at_ms=created_at_ms,
                setup=result.setup,
                setup_name="the example",
                first_session_open_ms=result.sessions[0].session_open_ms,
                last_session_open_ms=result.sessions[-1].session_open_ms,
                sessions_requested=len(result.sessions),
            ),
        )
        if end == "complete":
            await complete_run(conn, run_id, result, finished_at_ms=created_at_ms + 5)
        elif end == "failed":
            await fail_run(
                conn,
                run_id,
                RunFailure("DOWNLOAD_FAILED", "The data could not be fetched.", (FailureItem(None, "x", "y"),)),
                finished_at_ms=created_at_ms + 5,
            )
        return run_id

    yield make
    await conn.execute("DELETE FROM research_options_backtest_runs WHERE job_id LIKE $1", f"%-{unique}")


async def test_the_history_is_newest_first_and_a_job_finds_its_run(
    client: httpx.AsyncClient, conn: asyncpg.Connection, unique: str, stored_runs: Any, is_the_job_alive: Any
) -> None:
    older = await stored_runs("older", created_at_ms=1_791_800_000_000)
    newer = await stored_runs("newer", end="failed", created_at_ms=1_791_800_000_100)

    async with client as c:
        everything = await c.get(f"{PREFIX}/runs", params={"limit": 1000})
        one_job = await c.get(f"{PREFIX}/runs", params={"job_id": f"older-{unique}"})

    assert everything.status_code == 200, everything.text
    mine = [row for row in everything.json() if row["job_id"].endswith(unique)]
    assert [(row["id"], row["status"]) for row in mine] == [(newer, "failed"), (older, "complete")]
    complete = mine[1]
    assert complete["headline"] == {
        "sessions_tested": 4,
        "sessions_traded": 3,
        "trades": 3,
        "trades_won": 1,
        "net_profit_usd": "-69.25",
        "ending_cash_usd": "9930.75",
    }
    assert complete["failure"] is None and complete["setup_name"] == "the example"
    assert mine[0]["headline"] is None and mine[0]["failure"]["code"] == "DOWNLOAD_FAILED"
    assert [row["id"] for row in one_job.json()] == [older]


async def test_a_run_opens_with_its_banner_figures_and_ladder_and_no_trades(
    client: httpx.AsyncClient, stored_runs: Any, is_the_job_alive: Any
) -> None:
    run_id = await stored_runs("opened")

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/{run_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete" and body["id"] == run_id
    assert body["banner"]["fills_are_simulated_from_trade_prices"] is True
    assert body["banner"]["bid_and_ask_are_available"] is False
    assert body["figures"]["net_profit_usd"] == "-69.25" and body["figures"]["trades"] == 3
    assert body["slippage"][0]["slippage_percent"] == "0" and body["slippage"][0]["net_profit_usd"] == "-63.25"
    assert body["slippage_tolerance"]["first_unprofitable_percent"] == "0"
    assert body["setup"]["underlying"] == "SPY"
    assert "trades" not in body and "events" not in body


async def test_a_run_that_failed_opens_with_why_and_no_figures(
    client: httpx.AsyncClient, stored_runs: Any, is_the_job_alive: Any
) -> None:
    run_id = await stored_runs("failed", end="failed")

    async with client as c:
        body = (await c.get(f"{PREFIX}/runs/{run_id}")).json()

    assert body["status"] == "failed" and body["figures"] is None and body["slippage"] == []
    assert body["failure"] == {
        "code": "DOWNLOAD_FAILED",
        "reason": "The data could not be fetched.",
        "items": [{"session_open_ms": None, "code": "x", "message": "y"}],
    }


async def test_a_running_run_reads_interrupted_when_its_job_is_gone_and_running_when_that_cannot_be_told(
    client: httpx.AsyncClient, stored_runs: Any, is_the_job_alive: dict[str, bool | None]
) -> None:
    run_id = await stored_runs("stuck", end="running")
    statuses = {}
    async with client as c:
        for name, answer in (("alive", True), ("gone", False), ("cannot tell", None)):
            is_the_job_alive["live"] = answer
            statuses[name] = (await c.get(f"{PREFIX}/runs/{run_id}")).json()["status"]

    assert statuses == {"alive": "running", "gone": "interrupted", "cannot tell": "running"}


@pytest.mark.parametrize("run_id", ["0", "-3", "abc", str(2**53)])
async def test_a_run_id_that_cannot_be_one_is_a_422(client: httpx.AsyncClient, run_id: str) -> None:
    async with client as c:
        got = await c.get(f"{PREFIX}/runs/{run_id}")
        deleted = await c.delete(f"{PREFIX}/runs/{run_id}")

    assert got.status_code == 422 and deleted.status_code == 422


async def test_no_such_run_is_a_404_with_a_code(
    client: httpx.AsyncClient, conn: asyncpg.Connection, is_the_job_alive: Any
) -> None:
    missing = 2**52

    async with client as c:
        got = await c.get(f"{PREFIX}/runs/{missing}")
        deleted = await c.delete(f"{PREFIX}/runs/{missing}")

    for response in (got, deleted):
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_NOT_FOUND"


async def test_a_run_is_deleted_with_a_204_and_is_then_gone(
    client: httpx.AsyncClient, stored_runs: Any, is_the_job_alive: Any
) -> None:
    run_id = await stored_runs("doomed")

    async with client as c:
        deleted = await c.delete(f"{PREFIX}/runs/{run_id}")
        after = await c.get(f"{PREFIX}/runs/{run_id}")

    assert deleted.status_code == 204 and deleted.content == b""
    assert after.status_code == 404


async def test_a_run_still_running_is_a_409_and_one_whose_job_is_gone_can_be_deleted(
    client: httpx.AsyncClient, stored_runs: Any, is_the_job_alive: dict[str, bool | None]
) -> None:
    run_id = await stored_runs("busy", end="running")

    async with client as c:
        is_the_job_alive["live"] = True
        refused = await c.delete(f"{PREFIX}/runs/{run_id}")
        is_the_job_alive["live"] = None
        unsure = await c.delete(f"{PREFIX}/runs/{run_id}")
        is_the_job_alive["live"] = False
        gone = await c.delete(f"{PREFIX}/runs/{run_id}")

    assert refused.status_code == 409 and unsure.status_code == 409
    assert refused.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_IS_RUNNING"
    assert gone.status_code == 204
