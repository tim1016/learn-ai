"""A run stored as running is asked about once, off the event loop, with no transaction and no connection held (#3030).

Whether a run stored as ``running`` reads ``running`` or ``interrupted`` depends on whether its job is alive, which is a
blocking Redis round trip (five seconds at most). The four reads of a saved run (its trades, one session, the export,
the comparison of two) used to ask inside their repeatable-read transaction, on the event loop. They must not: the
question is asked after the connection is let go, in a worker thread, once.

No database is needed. The real routes, ``run_detail`` and ``saved_runs`` run over a fake connection
(``_fake_connection.py``) that answers the few statements they send and says whether a transaction is open. Redis is a
function that notes where it was asked and what was open at that moment.

Hand-worked: run 41's job is ``job-41`` and run 42's is ``job-42``. Redis says a job is alive (``True``), gone
(``False``) or cannot tell (``None``). Only "gone" turns a running run into ``interrupted``, so alive and cannot-tell
both read ``running``.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

import httpx
import pytest

from app.options_backtest import run_detail
from app.research.persistence import lifecycle
from tests.options_backtest._fake_connection import (
    FIRST_OPEN_MS,
    FakeConnection,
    Pool,
    a_line_row,
    a_run_row,
    a_skipped_session_row,
)
from tests.options_backtest._stored_runs import PREFIX
from tests.options_backtest._stored_runs import client as client

NOT_COMPLETE = "OPTIONS_BACKTEST_RUN_NOT_COMPLETE"


@dataclass(frozen=True)
class Asked:
    job_id: str | None
    thread: int
    transaction_open: bool
    connection_held: bool


@dataclass
class World:
    """What Redis was asked and what was open when it was, over a fake connection a test fills."""

    fake: FakeConnection
    pool: Pool
    answers: dict[str, bool | None]
    loop_thread: int
    asked: list[Asked] = field(default_factory=list)
    hops: int = 0


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    fake = FakeConnection()
    pool = Pool(fake)
    made = World(fake=fake, pool=pool, answers={}, loop_thread=threading.get_ident())
    real_to_thread = asyncio.to_thread

    def is_live(job_id: str | None) -> bool | None:
        made.asked.append(Asked(job_id, threading.get_ident(), fake.in_transaction, pool.held))
        return made.answers[job_id or ""]

    async def counted_to_thread(fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        made.hops += 1
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(run_detail, "with_connection", pool)
    monkeypatch.setattr(lifecycle, "job_is_live", is_live)
    monkeypatch.setattr(asyncio, "to_thread", counted_to_thread)
    return made


def _store(world: World, *rows: dict[str, object]) -> None:
    world.fake.runs = {row["id"]: row for row in rows}  # type: ignore[misc]


# The three reads of one run, by the path each is asked at.
ONE_RUN = {
    "the trades": "/runs/41/trades",
    "one session": f"/runs/41/sessions/{FIRST_OPEN_MS}",
    "the export": "/runs/41/trades.csv",
}
# What Redis says of job-41 and the word the refusal then uses: only "gone" interrupts.
ANSWERS = [(True, "running"), (False, "interrupted"), (None, "running")]


@pytest.mark.parametrize(("alive", "word"), ANSWERS)
@pytest.mark.parametrize("path", ONE_RUN.values(), ids=ONE_RUN.keys())
async def test_a_run_stored_as_running_has_its_job_asked_once_in_a_worker_thread_with_nothing_held(
    client: httpx.AsyncClient, world: World, path: str, alive: bool | None, word: str
) -> None:
    _store(world, a_run_row(41, "running"))
    world.answers["job-41"] = alive

    async with client as c:
        response = await c.get(f"{PREFIX}{path}")

    assert response.status_code == 409
    assert response.json() == {
        "detail": {"code": NOT_COMPLETE, "message": f"Run 41 is {word}. Only a complete run has trades and sessions."}
    }
    [ask] = world.asked
    assert ask.job_id == "job-41"
    assert ask.thread != world.loop_thread, "Redis was asked on the event loop"
    assert not ask.transaction_open and not ask.connection_held
    assert world.hops == 1
    # The run was read once, in one repeatable-read transaction, and refused straight after: nothing else was read.
    assert world.fake.transactions == [("repeatable_read", True)]
    assert world.fake.log == [("research_options_backtest_runs", 1)]


@pytest.mark.parametrize("alive", [True, False])
@pytest.mark.parametrize("path", ONE_RUN.values(), ids=ONE_RUN.keys())
async def test_a_run_that_is_failed_or_cancelled_is_refused_in_its_own_words_and_nothing_is_asked(
    client: httpx.AsyncClient, world: World, path: str, alive: bool
) -> None:
    _store(world, a_run_row(41, "failed"), a_run_row(43, "cancelled"))
    world.answers["job-41"] = world.answers["job-43"] = alive
    messages = {}

    async with client as c:
        for run_id in (41, 43):
            response = await c.get(f"{PREFIX}{path.replace('41', str(run_id))}")
            assert response.status_code == 409 and response.json()["detail"]["code"] == NOT_COMPLETE
            messages[run_id] = response.json()["detail"]["message"]

    assert messages == {
        41: "Run 41 is failed. Only a complete run has trades and sessions.",
        43: "Run 43 is cancelled. Only a complete run has trades and sessions.",
    }
    assert world.asked == [] and world.hops == 0


async def test_a_run_that_does_not_exist_is_a_404_and_nothing_is_asked(client: httpx.AsyncClient, world: World) -> None:
    async with client as c:
        responses = [await c.get(f"{PREFIX}{path}") for path in ONE_RUN.values()]

    for response in responses:
        assert response.status_code == 404
        assert response.json()["detail"] == {
            "code": "OPTIONS_BACKTEST_RUN_NOT_FOUND",
            "message": "No run has the id 41.",
        }
    assert world.asked == [] and world.hops == 0


async def test_a_complete_run_is_read_and_answered_and_nothing_is_asked(
    client: httpx.AsyncClient, world: World
) -> None:
    # Run 41 is complete, with one session that was skipped on its window (no trade, no lake file to read).
    _store(world, a_run_row(41), a_run_row(42))
    world.fake.sessions = {FIRST_OPEN_MS: a_skipped_session_row()}
    world.fake.lines = {
        41: [a_line_row(FIRST_OPEN_MS, "skipped", "0", 0)],
        42: [a_line_row(FIRST_OPEN_MS, "skipped", "0", 0)],
    }

    async with client as c:
        trades = await c.get(f"{PREFIX}/runs/41/trades")
        session = await c.get(f"{PREFIX}/runs/41/sessions/{FIRST_OPEN_MS}")
        export = await c.get(f"{PREFIX}/runs/41/trades.csv")
        compared = await c.get(f"{PREFIX}/runs/compare", params={"a": 41, "b": 42})

    assert [response.status_code for response in (trades, session, export, compared)] == [200, 200, 200, 200]
    assert (trades.json()["total"], session.json()["status"], compared.json()["overlap"]["shared_sessions"]) == (
        0,
        "skipped",
        1,
    )
    assert export.content.decode("utf-8").count("\r\n") == 1, "the header alone: the run made no trade"
    assert world.asked == []


# ── Two runs ─────────────────────────────────────────────────────────────


def _words(a: str, b: str) -> str:
    return f"Only complete runs can be compared, and run 41 (a) is {a}; run 42 (b) is {b}."


@pytest.mark.parametrize(
    ("alive_41", "alive_42", "a", "b"),
    [
        (True, False, "running", "interrupted"),
        (False, None, "interrupted", "running"),
        (False, False, "interrupted", "interrupted"),
    ],
)
async def test_two_runs_stored_as_running_have_their_jobs_asked_together_in_one_worker_thread_call(
    client: httpx.AsyncClient, world: World, alive_41: bool | None, alive_42: bool | None, a: str, b: str
) -> None:
    _store(world, a_run_row(41, "running"), a_run_row(42, "running"))
    world.answers |= {"job-41": alive_41, "job-42": alive_42}

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/compare", params={"a": 41, "b": 42})

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": NOT_COMPLETE, "message": _words(a, b)}
    assert sorted(ask.job_id or "" for ask in world.asked) == ["job-41", "job-42"]
    assert {ask.thread for ask in world.asked} != {world.loop_thread} and len({ask.thread for ask in world.asked}) == 1
    assert not any(ask.transaction_open or ask.connection_held for ask in world.asked)
    assert world.hops == 1
    assert world.fake.transactions == [("repeatable_read", True)]
    assert world.fake.log == [("research_options_backtest_runs", 1), ("research_options_backtest_runs", 1)]


async def test_only_the_run_stored_as_running_is_asked_about_and_the_failed_one_keeps_its_word(
    client: httpx.AsyncClient, world: World
) -> None:
    _store(world, a_run_row(41, "failed"), a_run_row(42, "running"))
    # Redis would say the failed run's job is alive too; the failed run is not asked, so the word is not its to change.
    world.answers |= {"job-41": True, "job-42": False}

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/compare", params={"a": 41, "b": 42})

    assert response.json()["detail"]["message"] == _words("failed", "interrupted")
    assert [ask.job_id for ask in world.asked] == ["job-42"] and world.hops == 1


async def test_a_run_stored_as_running_compared_with_itself_has_its_job_asked_once(
    client: httpx.AsyncClient, world: World
) -> None:
    _store(world, a_run_row(41, "running"))
    world.answers["job-41"] = False

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/compare", params={"a": 41, "b": 41})

    assert response.json()["detail"]["message"] == (
        "Only complete runs can be compared, and run 41 (a) is interrupted; run 41 (b) is interrupted."
    )
    assert [ask.job_id for ask in world.asked] == ["job-41"] and world.hops == 1
