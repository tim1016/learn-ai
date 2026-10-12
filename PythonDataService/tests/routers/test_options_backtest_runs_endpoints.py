"""The Options Backtest runs over HTTP (#3030): the job that makes a run, Check data, the history, one run, delete.

The job route and Check data need no database: the run store is replaced by one in memory, and the vendor is faked at
the HTTP layer over a lake in a temporary folder. The routes that read the history, open a run and delete one run
only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one, like their
neighbours. Hand-worked figures are in ``tests/options_backtest/test_service.py``, whose scene these tests reuse.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import threading
import uuid
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
from app.jobs.runner import run_in_thread as run_the_job
from app.lean_sidecar.trading_calendar import expected_sessions, session_window_for_date
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
from app.options_backtest.saved_setups import EXAMPLE_SETUPS, SavedSetup, save_setup
from app.options_backtest.setup import OptionsBacktestSetup
from app.research.persistence import lifecycle
from app.routers import options_backtest_runs as runs_router
from app.utils.background_loop import CallerStoppedWaitingError
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_day_end_ms, et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.jobs.conftest import _FakeRedis
from tests.jobs.conftest import fake as fake
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import fake_data
from tests.options_backtest._run_lake import Vendor
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import leg, setup_body
from tests.options_backtest.setup_fixtures import tag as tag
from tests.options_backtest.setup_fixtures import unique as unique
from tests.options_backtest.test_service import FRI, NOW_MS, SESSION_OF_THE_RANGE, THU, TUE, WED, the_scene, the_setup

REAL_SAVED_SETUP = runs_router.saved_setup
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
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
        self.failures: list[tuple[str, str]] = []

    @property
    def phases(self) -> list[str]:
        return [value for kind, value in self.events if kind == "phase"]

    def phase(self, name: str, *, friendly: str | None = None) -> None:
        self.events.append(("phase", name))

    def failed(self, code: str, message: str) -> None:
        self.failures.append((code, message))

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
    # The setups the route finds saved, by name; none until a test saves one.
    saved: dict[str, SavedSetup] = field(default_factory=dict)

    def save(self, name: str, setup_json: dict[str, Any]) -> None:
        setup = OptionsBacktestSetup.model_validate(setup_json)
        self.saved[name] = SavedSetup(name=name, example=False, saved_at_ms=1, setup=setup, cannot_open_reason=None)

    async def saved_setup(self, name: str) -> SavedSetup | None:
        return self.saved.get(name)

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
    monkeypatch.setattr(runs_router, "saved_setup", h.saved_setup)
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
    harness.save("SPY premium momentum, no re-entry", job_body()["setup"])
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


# ── The name a run is recorded under ─────────────────────────────────────
# A run is recorded under the name of a saved setup only when a setup of that name exists and is the setup the run was
# asked with; otherwise it is recorded with no name and is accepted all the same.

SAVED_AS = "my morning call"


def _spelled_out_as_the_server_reads_it(setup_json: dict[str, Any]) -> dict[str, Any]:
    """The same setup with the defaults written out and a fee spelled with a trailing zero: other text, equal setup."""
    return {
        **setup_json,
        "slippage_percent": "0.3",
        "price_step_usd": "0.01",
        "fees": {"kind": "per_contract", "per_contract_usd": "0.650"},
    }


async def _post_named(
    c: httpx.AsyncClient, name: str | None, setup_json: dict[str, Any], job_id: str
) -> httpx.Response:
    changes: dict[str, Any] = {"job_id": job_id, "setup": setup_json}
    if name is not None:
        changes["setup_name"] = name
    return await c.post(JOBS, json=job_body(**changes))


async def test_a_run_is_recorded_under_the_name_of_a_saved_setup_equal_to_its_own_whatever_the_text_of_the_request(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    harness.save(SAVED_AS, _spelled_out_as_the_server_reads_it(job_body()["setup"]))

    async with client as c:
        response = await _post_named(c, SAVED_AS, job_body()["setup"], "job-1")

    assert response.status_code == 202, response.text
    [new] = harness.store.created
    assert new.setup_name == SAVED_AS


async def test_a_run_is_recorded_with_no_name_when_no_setup_of_that_name_is_saved_and_is_accepted_all_the_same(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    async with client as c:
        response = await _post_named(c, "never saved", job_body()["setup"], "job-1")

    assert response.status_code == 202, response.text
    [new] = harness.store.created
    assert new.setup_name is None and len(harness.started) == 1


async def test_a_run_is_recorded_with_no_name_when_the_saved_setup_of_that_name_differs_in_one_field(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    harness.save(SAVED_AS, {**job_body()["setup"], "fees": {"kind": "per_contract", "per_contract_usd": "0.66"}})

    async with client as c:
        response = await _post_named(c, SAVED_AS, job_body()["setup"], "job-1")

    assert response.status_code == 202, response.text
    [new] = harness.store.created
    assert new.setup_name is None


async def test_a_run_is_recorded_with_no_name_when_the_saved_setup_of_that_name_can_no_longer_be_opened(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    harness.saved[SAVED_AS] = SavedSetup(
        name=SAVED_AS, example=False, saved_at_ms=1, setup=None, cannot_open_reason="a rule has changed"
    )

    async with client as c:
        response = await _post_named(c, SAVED_AS, job_body()["setup"], "job-1")

    assert response.status_code == 202, response.text
    [new] = harness.store.created
    assert new.setup_name is None


async def test_a_run_sent_with_no_name_is_recorded_with_none(client: httpx.AsyncClient, harness: Harness) -> None:
    harness.save(SAVED_AS, job_body()["setup"])

    async with client as c:
        response = await _post_named(c, None, job_body()["setup"], "job-1")

    assert response.status_code == 202, response.text
    [new] = harness.store.created
    assert new.setup_name is None


async def test_the_name_of_a_saved_setup_is_found_in_the_database_and_so_is_the_name_of_a_shipped_example(
    client: httpx.AsyncClient, harness: Harness, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection, tag: str
) -> None:
    # The route's own lookup, on a real database: a saved setup of this test's, and the first shipped example.
    monkeypatch.setattr(runs_router, "saved_setup", REAL_SAVED_SETUP)
    mine = f"{SAVED_AS} {tag}"
    await save_setup(conn, mine, OptionsBacktestSetup.model_validate(job_body()["setup"]), saved_at_ms=1)
    [example, *_] = EXAMPLE_SETUPS
    assert example.setup is not None
    different = {**job_body()["setup"], "starting_cash_usd": "25000"}

    async with client as c:
        kept = await _post_named(c, mine, job_body()["setup"], "n1")
        differs = await _post_named(c, mine, different, "n2")
        shipped = await _post_named(c, example.name, example.setup.model_dump(mode="json"), "n3")

    assert (kept.status_code, differs.status_code, shipped.status_code) == (202, 202, 202)
    assert [new.setup_name for new in harness.store.created] == [mine, None, example.name]


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


async def test_the_most_sessions_a_range_may_hold_is_the_limit_served_a_range_of_that_many_runs_and_one_more_does_not(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    async with client as c:
        limit = (await c.get(f"{PREFIX}/limits")).json()["max_range_sessions"]
        # More than limit + 1 sessions, all in the past: the sessions of nearly seven years.
        sessions = expected_sessions(date(2019, 1, 2), date(2025, 12, 31))
        assert len(sessions) > limit + 1
        at_the_limit = await c.post(
            JOBS,
            json=job_body(
                job_id="limit-1", start_ms=et_midnight_ms(sessions[0]), end_ms=et_day_end_ms(sessions[limit - 1])
            ),
        )
        one_more = await c.post(
            JOBS,
            json=job_body(
                job_id="limit-2", start_ms=et_midnight_ms(sessions[0]), end_ms=et_day_end_ms(sessions[limit])
            ),
        )
        check_one_more = await c.post(
            f"{PREFIX}/check-data",
            json={
                "setup": job_body()["setup"],
                "start_ms": et_midnight_ms(sessions[0]),
                "end_ms": et_day_end_ms(sessions[limit]),
            },
        )

    assert at_the_limit.status_code == 202, at_the_limit.text
    assert [new.sessions_requested for new in harness.store.created] == [limit]
    for refused in (one_more, check_one_more):
        assert refused.status_code == 400, refused.text
        assert refused.json()["detail"]["code"] == "OPTIONS_BACKTEST_RANGE_TOO_LONG"
    assert len(harness.store.created) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda body: body.update(job_id=""), id="empty-job-id"),
        pytest.param(lambda body: body.update(job_id="a\u0000b"), id="a-nul-in-the-job-id"),
        pytest.param(lambda body: body.update(job_id="a" * 129), id="a-job-id-of-129-characters"),
        pytest.param(
            lambda body: body.update(
                job_id="".join(random.Random(1).choices("abcdefghijklmnopqrstuvwxyz012345", k=20_000))
            ),
            id="a-job-id-of-20000-random-characters",
        ),
        pytest.param(lambda body: body.update(job_id="a b"), id="a-space-in-the-job-id"),
        pytest.param(lambda body: body.update(job_id="a/b"), id="a-slash-in-the-job-id"),
        pytest.param(lambda body: body.update(job_id="abc\n"), id="a-line-break-ends-the-job-id"),
        pytest.param(lambda body: body.update(job_id="caf\u00e9"), id="a-letter-outside-ascii-in-the-job-id"),
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


@pytest.mark.parametrize("route", [JOBS, f"{PREFIX}/check-data"], ids=["the job route", "check data"])
@pytest.mark.parametrize(
    "payload",
    [{"job_id": "\ud800"}, {"\ud800": "x"}, {"job_id": "job-1", "setup": {"\ud800": 1}}],
    ids=["a lone surrogate in a value", "in a key", "in a key inside the setup"],
)
async def test_a_lone_surrogate_in_a_body_is_a_422_and_not_a_500_on_the_runs_two_routes_and_makes_no_run(
    client: httpx.AsyncClient, harness: Harness, route: str, payload: dict[str, Any]
) -> None:
    # httpx's json= will not encode a lone surrogate, so the body is ASCII-escaped JSON bytes ("\\ud800").
    body = json.dumps(payload, ensure_ascii=True).encode("ascii")
    assert b"\\ud800" in body

    async with client as c:
        response = await c.post(route, content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert harness.store.created == [] and harness.started == []


@pytest.mark.parametrize("job_id", [str(uuid.UUID(int=0xA1B2C3D4E5F60718293A4B5C6D7E8F90)), "a", "A_b-9", "x" * 128])
async def test_a_job_id_of_the_shape_the_jobs_boundary_mints_is_accepted(
    client: httpx.AsyncClient, harness: Harness, job_id: str
) -> None:
    async with client as c:
        response = await c.post(JOBS, json=job_body(job_id=job_id))

    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == job_id


async def test_a_range_whose_instants_are_not_new_york_midnights_is_a_400_and_makes_no_run_and_no_job(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    # Two instants the old reading accepted and read as the dates they fall on: 0, the epoch, which is 19:00 on
    # 1969-12-31 in New York (a day that began before it), and a start 1 ms after Friday 2026-03-06's midnight
    # (1 772 773 200 000 + 1). An end a millisecond short of Monday 2026-03-09's midnight (1 773 028 800 000 - 1) is the
    # third, and a start and an end that are both wrong are refused on the start.
    async with client as c:
        responses = [
            await c.post(JOBS, json=job_body(job_id="e1", start_ms=0, end_ms=5 * 86_400_000)),
            await c.post(JOBS, json=job_body(job_id="e2", start_ms=1_772_773_200_001, end_ms=1_773_028_800_000)),
            await c.post(JOBS, json=job_body(job_id="e3", start_ms=1_772_773_200_000, end_ms=1_773_028_799_999)),
        ]

    for response, named in zip(responses, ("start_ms", "start_ms", "end_ms"), strict=True):
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RANGE_NOT_ON_MIDNIGHT"
        assert named in response.json()["detail"]["message"]
    assert harness.store.created == [] and harness.started == []


async def test_a_range_that_ends_on_a_day_the_clocks_change_is_accepted(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    # Friday 2026-03-06's midnight to Monday the 9th's (the end of Sunday the 8th, a 23-hour day) and Friday 10-30 to
    # Monday 11-02's midnight (the end of Sunday 11-01, a 25-hour day): the page's own helpers give these instants, and
    # each range holds the one session of its Friday.
    async with client as c:
        spring = await c.post(JOBS, json=job_body(job_id="s1", start_ms=1_772_773_200_000, end_ms=1_773_028_800_000))
        autumn = await c.post(JOBS, json=job_body(job_id="s2", start_ms=1_793_332_800_000, end_ms=1_793_595_600_000))

    assert (spring.status_code, autumn.status_code) == (202, 202), (spring.text, autumn.text)
    assert [new.first_session_open_ms for new in harness.store.created] == [
        session_window_for_date(date(2026, 3, 6)).open_ms_utc,
        session_window_for_date(date(2026, 10, 30)).open_ms_utc,
    ]


async def test_a_range_that_starts_on_the_first_new_york_day_of_1970_is_accepted_for_its_first_session_after_the_epoch(
    client: httpx.AsyncClient, harness: Harness
) -> None:
    # 1970-01-01 was a holiday, so New York midnight of it is not a session and the first session is Friday 1970-01-02,
    # opened at 15:00 UTC: 1 day + 15 hours = 24 h + 15 h = 39 h = 39 x 3,600,000 = 140,400,000 ms.
    async with client as c:
        response = await c.post(
            JOBS,
            json=job_body(start_ms=et_midnight_ms(date(1970, 1, 1)), end_ms=et_day_end_ms(date(1970, 1, 2))),
        )

    assert response.status_code == 202, response.text
    [new] = harness.store.created
    assert new.first_session_open_ms == 140_400_000 == new.last_session_open_ms


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

    # The job's log lines say what is being fetched and how much, and name no date.
    logs = [value for kind, value in emitter.events if kind == "log"]
    assert logs and not any(DATE.search(line) for line in logs), logs

    # A phase the job reports is one the vocabulary lists, in the order it lists them.
    listed = [phase.id for phase in JOB_PHASES["options_backtest"]]
    assert emitter.phases == ["checking_data", "downloading_data", "replaying", "saving"]
    assert [listed.index(phase) for phase in emitter.phases] == sorted(listed.index(phase) for phase in emitter.phases)
    # Progress is counted in sessions: four checked, four fetched, then four replayed.
    assert [value for kind, value in emitter.events if kind == "progress"] == [
        (n, 4, "sessions") for n in (1, 2, 3, 4)
    ] * 3
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
async def test_a_failed_run_ends_the_job_failed_with_the_runs_code_and_its_plain_reason(
    client: httpx.AsyncClient, harness: Harness, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # One session older than the options plan reaches: check data refuses the whole run.
    old = date(2024, 10, 11)
    Vendor(the_scene()).mount()
    async with client as c:
        await c.post(JOBS, json=job_body(start_ms=et_midnight_ms(old), end_ms=et_day_end_ms(old)))

    emitter = FakeEmitter()

    result = await asyncio.to_thread(harness.the_work(), emitter, FakeCancel())

    # The work has sent the job's end itself, with the run's own code and reason, so it returns nothing to complete with.
    [(_, failure, _)] = harness.store.failed
    assert failure.code == "SESSIONS_CANNOT_BE_TESTED" and "cannot be tested" in failure.reason
    assert result is None
    assert emitter.failures == [("SESSIONS_CANNOT_BE_TESTED", failure.reason)]


# ── The job as the runner ends it ────────────────────────────────────────
# These start the captured worker through the job runner itself, on the tests' stand-in for the job store, and read the
# events a page would receive and the records the logs got.


async def _the_job_runs_to_its_end(harness: Harness, job_id: str = "job-1") -> None:
    thread = run_the_job(job_id, harness.the_work(), thread_name="options-backtest-test", cancel_check_every_n=1)
    await asyncio.to_thread(thread.join, 60)
    assert not thread.is_alive()


@respx.mock
async def test_each_phase_event_of_a_run_carries_the_plain_label_the_vocabulary_gives_that_phase(
    client: httpx.AsyncClient, harness: Harness, fake: _FakeRedis, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    Vendor(the_scene()).mount()
    async with client as c:
        await c.post(JOBS, json=job_body())

    await _the_job_runs_to_its_end(harness)

    phase_events = [event for event in fake.events("job-1") if event["type"] == "job.phase"]
    labels = {phase.id: phase.label for phase in JOB_PHASES["options_backtest"]}
    # A lone run never waits for the engine: the four phases it enters, in order.
    assert [event["phase"] for event in phase_events] == ["checking_data", "downloading_data", "replaying", "saving"]
    for event in phase_events:
        assert event["friendly"] == labels[event["phase"]]
        assert event["friendly"] != event["phase"].replace("_", " ").title(), "the id made readable is no label"


@respx.mock
async def test_a_run_that_fails_for_a_reason_the_service_explains_ends_the_job_failed_with_the_runs_code_and_logs_no_error(
    client: httpx.AsyncClient,
    harness: Harness,
    fake: _FakeRedis,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # One session older than the options plan reaches: check data refuses the whole run.
    old = date(2024, 10, 11)
    Vendor(the_scene()).mount()
    async with client as c:
        await c.post(JOBS, json=job_body(start_ms=et_midnight_ms(old), end_ms=et_day_end_ms(old)))

    with caplog.at_level(logging.DEBUG):
        await _the_job_runs_to_its_end(harness)

    [(run_id, failure, _)] = harness.store.failed
    assert fake.events("job-1")[-1] == {
        "type": "job.failed",
        "code": "SESSIONS_CANNOT_BE_TESTED",
        "message": failure.reason,
    }
    assert fake.hashes["job:job-1:state"]["status"] == "failed"
    # One line says so, with the run's id (the first run made is 41) and its code, and neither it nor anything else
    # logged is an error or carries a traceback.
    [line] = [record for record in caplog.records if getattr(record, "action", "") == "options_backtest_run_failed"]
    assert (line.levelno, run_id, line.run_id, line.code) == (
        logging.INFO,
        41,
        41,
        "SESSIONS_CANNOT_BE_TESTED",
    )
    assert [record for record in caplog.records if record.levelno >= logging.ERROR or record.exc_info] == []


@respx.mock
async def test_a_run_that_is_cancelled_ends_the_job_cancelled_and_logs_no_error(
    client: httpx.AsyncClient,
    harness: Harness,
    fake: _FakeRedis,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    Vendor(the_scene()).mount()
    async with client as c:
        await c.post(JOBS, json=job_body())
    fake.hset("job:job-1:state", mapping={"cancel_requested": "1"})

    with caplog.at_level(logging.DEBUG):
        await _the_job_runs_to_its_end(harness)

    assert fake.events("job-1")[-1] == {"type": "job.cancelled", "reason": "run 41 was cancelled"}
    assert [run_id for run_id, _ in harness.store.cancelled] == [41]
    assert [record for record in caplog.records if record.levelno >= logging.ERROR or record.exc_info] == []


@pytest.mark.parametrize(
    ("how", "code_of_the_end"),
    [
        pytest.param("the service raises", "KeyError", id="an error the service does not catch"),
        pytest.param("the replay raises", "RunFailedError", id="an error the run records as INTERNAL_ERROR"),
    ],
)
@respx.mock
async def test_an_error_nobody_expected_still_ends_the_job_failed_with_an_error_in_the_log_and_its_traceback(
    how: str,
    code_of_the_end: str,
    client: httpx.AsyncClient,
    harness: Harness,
    fake: _FakeRedis,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Vendor(the_scene()).mount()
    async with client as c:
        await c.post(JOBS, json=job_body())

    def breaks(*_args: Any, **_kwargs: Any) -> None:
        raise KeyError("boom")

    monkeypatch.setattr(service, "execute_run" if how == "the service raises" else "run_sessions", breaks)

    with caplog.at_level(logging.DEBUG):
        await _the_job_runs_to_its_end(harness)

    assert fake.events("job-1")[-1]["type"] == "job.failed"
    assert fake.events("job-1")[-1]["code"] == code_of_the_end
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors and all(record.exc_info for record in errors)
    if how == "the replay raises":
        assert [failure.code for _, failure, _ in harness.store.failed] == ["INTERNAL_ERROR"]


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
    assert not DATE.search(response.text), "the answer names sessions by their opens, not by dates"
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
    assert not DATE.search(response.text)
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


async def test_check_data_on_a_range_whose_instants_are_not_new_york_midnights_is_a_400_and_reads_nothing(
    client: httpx.AsyncClient, now_is_monday_afternoon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_be_asked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a range refused as not on midnight must be refused before anything is read")

    monkeypatch.setattr(runs_router, "check_data", must_not_be_asked)
    setup = job_body()["setup"]

    async with client as c:
        at_the_epoch = await c.post(
            f"{PREFIX}/check-data", json={"setup": setup, "start_ms": 0, "end_ms": 5 * 86_400_000}
        )
        late_start = await c.post(
            f"{PREFIX}/check-data", json={"setup": setup, "start_ms": 1_772_773_200_001, "end_ms": 1_773_028_800_000}
        )
        early_end = await c.post(
            f"{PREFIX}/check-data", json={"setup": setup, "start_ms": 1_772_773_200_000, "end_ms": 1_773_028_799_999}
        )

    for response, named in ((at_the_epoch, "start_ms"), (late_start, "start_ms"), (early_end, "end_ms")):
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RANGE_NOT_ON_MIDNIGHT"
        assert named in response.json()["detail"]["message"]


@respx.mock
async def test_check_data_answers_for_a_range_that_ends_on_a_day_the_clocks_change(
    client: httpx.AsyncClient, now_is_monday_afternoon: None, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # Friday 2026-03-06's midnight to the end of Sunday the 8th (Monday's midnight): the Friday is the one session.
    async with client as c:
        response = await c.post(
            f"{PREFIX}/check-data",
            json={"setup": job_body()["setup"], "start_ms": 1_772_773_200_000, "end_ms": 1_773_028_800_000},
        )

    assert response.status_code == 200, response.text
    assert [s["session_open_ms"] for s in response.json()["sessions"]] == [
        session_window_for_date(date(2026, 3, 6)).open_ms_utc
    ]


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


async def test_check_data_when_the_catalog_does_not_answer_in_time_is_the_same_503_as_when_it_cannot_be_reached(
    client: httpx.AsyncClient, now_is_monday_afternoon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = {}
    async with client as c:
        for name, error in (
            ("unreachable", CatalogUnavailableError("down")),
            ("too slow", CallerStoppedWaitingError("still running on the background-loop after 60.0s")),
        ):

            def fails(*_args: Any, error: Exception = error, **_kwargs: Any) -> None:
                raise error

            monkeypatch.setattr(runs_router, "check_data", fails)
            answers[name] = await c.post(f"{PREFIX}/check-data", json=check_body(TUE, FRI))

    assert answers["too slow"].status_code == 503
    assert answers["too slow"].json() == answers["unreachable"].json()
    assert answers["too slow"].json()["detail"]["code"] == "OPTIONS_BACKTEST_DATA_CATALOG_UNAVAILABLE"


# ── The history reads the liveness of its running jobs once, off the loop ─


def _a_row(run_id: int, status: str, job_id: str) -> dict[str, Any]:
    """A row of the runs table as the history reads it, with nothing of its figures: running, cancelled or failed."""
    return {
        "id": run_id,
        "created_at_ms": 1_791_800_000_000 + run_id,
        "finished_at_ms": None if status == "running" else 1_791_800_000_000 + run_id + 5,
        "job_id": job_id,
        "status": status,
        "setup_name": None,
        "underlying": "SPY",
        "first_session_open_ms": 1_791_379_800_000,
        "last_session_open_ms": 1_791_552_600_000,
        "sessions_requested": 4,
        "fee_policy_text": "No fees are charged.",
        "calendar_identity": "NYSE",
        "calendar_package_version": "x",
        "failure_code": "DOWNLOAD_FAILED" if status == "failed" else None,
        "failure_reason": "The data could not be fetched." if status == "failed" else None,
        "failure_detail_json": "[]" if status == "failed" else None,
        "sessions_tested": None,
        "sessions_traded": None,
        "trades": None,
        "trades_won": None,
        "net_profit_usd": None,
        "ending_cash_usd": None,
    }


async def test_the_history_reads_the_liveness_of_all_its_running_jobs_in_one_worker_thread_call_after_the_connection_is_let_go(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Six rows, newest first: four stored as running (jobs 6, 5, 3 and 2), one cancelled, one failed. Redis says job 6 is
    # alive, jobs 5 and 2 gone, and cannot tell for job 3. Only a job that is gone turns a running row into
    # interrupted, so the rows read: running, interrupted, cancelled, running, interrupted, failed. Four jobs are
    # asked, once each, all on one worker thread that is not the loop's, in one hop to it, and with no connection held.
    table = (
        _a_row(6, "running", "job-6"),
        _a_row(5, "running", "job-5"),
        _a_row(4, "cancelled", "job-4"),
        _a_row(3, "running", "job-3"),
        _a_row(2, "running", "job-2"),
        _a_row(1, "failed", "job-1"),
    )
    answers = {"job-6": True, "job-5": False, "job-3": None, "job-2": False}
    connection_is_held = False
    asked: list[tuple[str | None, int, bool]] = []
    hops = 0
    this_thread = threading.get_ident()
    real_to_thread = asyncio.to_thread

    async def a_connection(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal connection_is_held
        connection_is_held = True
        try:
            return await fn(None, *args, **kwargs)
        finally:
            connection_is_held = False

    async def the_rows(_conn: Any, *, limit: int, job_id: str | None) -> tuple[dict[str, Any], ...]:
        return table

    def is_live(job_id: str | None) -> bool | None:
        asked.append((job_id, threading.get_ident(), connection_is_held))
        return answers[job_id or ""]

    async def counted_to_thread(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        nonlocal hops
        hops += 1
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(runs_router, "with_connection", a_connection)
    monkeypatch.setattr(runs_router.saved_runs, "read_run_rows", the_rows)
    monkeypatch.setattr(lifecycle, "job_is_live", is_live)
    monkeypatch.setattr(asyncio, "to_thread", counted_to_thread)

    async with client as c:
        response = await c.get(f"{PREFIX}/runs")

    assert response.status_code == 200, response.text
    assert [(run["id"], run["status"]) for run in response.json()] == [
        (6, "running"),
        (5, "interrupted"),
        (4, "cancelled"),
        (3, "running"),
        (2, "interrupted"),
        (1, "failed"),
    ]
    assert sorted(job for job, _, _ in asked) == ["job-2", "job-3", "job-5", "job-6"]
    [worker] = {thread for _, thread, _ in asked}
    assert worker != this_thread and hops == 1
    assert not any(held for _, _, held in asked)


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


@pytest.mark.parametrize(
    "job_id",
    [
        "%00",
        "a%00b",
        "x" * 129,
        "a%20b",
        "a%2Fb",
        "abc%0A",
        "%C3%A9",
        "%EF%BF%BD",
        "".join(random.Random(2).choices("abcdef0123456789", k=20_000)),
    ],
)
async def test_a_job_id_to_filter_by_that_cannot_be_one_is_a_422_before_anything_is_asked_of_the_database(
    client: httpx.AsyncClient, job_id: str
) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/runs?job_id={job_id}")

    assert response.status_code == 422, response.text[:200]


@pytest.mark.parametrize("limit", ["0", "-1", "1001", "1000000", "99999999999999999999", "abc", "1.5", ""])
async def test_a_history_limit_outside_1_to_1000_is_a_422(client: httpx.AsyncClient, limit: str) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/runs?limit={limit}")

    assert response.status_code == 422, response.text[:200]


async def test_the_history_gives_at_most_the_limit_asked_200_when_none_is_asked_and_1000_at_the_most(
    client: httpx.AsyncClient, conn: asyncpg.Connection, unique: str, is_the_job_alive: Any
) -> None:
    # 1,001 cancelled runs of this test's own, made in one statement; ids come in the order of the series.
    await conn.execute(
        """
        INSERT INTO research_options_backtest_runs (
            created_at_ms, finished_at_ms, job_id, status, setup_json, underlying, first_session_open_ms,
            last_session_open_ms, sessions_requested, fee_policy_text, calendar_identity, calendar_package_version
        )
        SELECT 1000 + n, 2000 + n, 'many-' || n || '-' || $1, 'cancelled', '{}'::jsonb, 'SPY', 5, 5, 1, 'x', 'NYSE', '1'
          FROM generate_series(1, 1001) AS n
        """,
        unique,
    )
    try:
        async with client as c:
            default = await c.get(f"{PREFIX}/runs")
            smallest = await c.get(f"{PREFIX}/runs?limit=1")
            largest = await c.get(f"{PREFIX}/runs?limit=1000")
    finally:
        await conn.execute("DELETE FROM research_options_backtest_runs WHERE job_id LIKE $1", f"many-%-{unique}")

    # Other tests' runs may sit above these (they are newer); what is asked for is what comes, and never more.
    assert (default.status_code, smallest.status_code, largest.status_code) == (200, 200, 200)
    assert (len(default.json()), len(smallest.json()), len(largest.json())) == (200, 1, 1000)


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


async def test_a_run_whose_stored_setup_no_longer_validates_opens_with_its_figures_no_setup_and_the_reason(
    client: httpx.AsyncClient, conn: asyncpg.Connection, stored_runs: Any, is_the_job_alive: Any
) -> None:
    healthy = await stored_runs("healthy")
    stale = await stored_runs("stale")
    # Edited by hand, as a later rule would leave it: a field the setup no longer has.
    await conn.execute(
        "UPDATE research_options_backtest_runs SET setup_json = setup_json || '{\"lots\": 1}'::jsonb WHERE id = $1",
        stale,
    )

    async with client as c:
        opened = await c.get(f"{PREFIX}/runs/{stale}")
        fine = await c.get(f"{PREFIX}/runs/{healthy}")
        history = await c.get(f"{PREFIX}/runs", params={"limit": 1000})

    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["setup"] is None and "lots" in body["setup_cannot_open_reason"]
    assert body["banner"] is None, "the banner states what the setup says of fills and fees"
    assert body["status"] == "complete" and body["fee_policy"] == "Fees are $0.65 per contract traded."
    assert body["figures"]["net_profit_usd"] == "-69.25" and body["slippage"][0]["net_profit_usd"] == "-63.25"
    assert body["headline"]["trades"] == 3
    assert fine.json()["setup"]["underlying"] == "SPY" and fine.json()["setup_cannot_open_reason"] is None
    assert fine.json()["banner"]["bid_and_ask_are_available"] is False
    assert history.status_code == 200 and {stale, healthy} <= {row["id"] for row in history.json()}


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


@pytest.mark.parametrize(
    "run_id",
    [
        "0",
        "-3",
        "abc",
        str(2**53),
        str(2**63 - 1),
        str(2**63),
        "99999999999999999999999",
        "1.5",
        "1e3",
        "%00",
        "%20",
        "%EF%BF%BD",
    ],
)
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
