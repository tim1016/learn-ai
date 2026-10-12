"""Runs saved to a real database for the closer-look tests (#3030).

``save_run`` makes a run the way the job route and the worker do (``create_run``, then ``complete_run`` /
``fail_run`` / ``cancel_run``) and removes every run the test made afterwards. ``conn`` and ``unique`` skip the test
without a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``). ``is_the_job_alive`` is what the routes
are told of a running job; a test sets ``["live"]``, and the default is "cannot tell". ``client`` calls the real app, and
``no_date_in`` holds that nothing sent names a date: a session is its open in ms.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import asyncpg
import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.options_backtest.run import RunResult
from app.options_backtest.saved_runs import (
    FailureItem,
    NewRun,
    RunFailure,
    cancel_run,
    complete_run,
    create_run,
    fail_run,
)
from app.research.persistence import lifecycle
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import unique as unique

CREATED_AT_MS = 1_791_800_000_000
PREFIX = "/api/research/options-backtest"
A_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

SaveRun = Callable[..., Awaitable[int]]


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def no_date_in(value: Any, path: str = "body") -> None:
    """Nothing on the wire names a date: a session is its open in ms."""
    if isinstance(value, str):
        assert not A_DATE.search(value), f"{path}: {value!r} names a date"
    elif isinstance(value, dict):
        for key, each in value.items():
            no_date_in(key, f"{path}.{key}")
            no_date_in(each, f"{path}.{key}")
    elif isinstance(value, list):
        for at, each in enumerate(value):
            no_date_in(each, f"{path}[{at}]")


@pytest.fixture
def is_the_job_alive(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool | None]:
    answers: dict[str, bool | None] = {"live": None}
    monkeypatch.setattr(lifecycle, "job_is_live", lambda _job_id: answers["live"])
    return answers


@pytest.fixture
async def save_run(conn: asyncpg.Connection, unique: str) -> AsyncIterator[SaveRun]:
    """``await save_run(result, name, end="complete", created_at_ms=..., setup_name=...)`` returns the run's id."""

    async def make(
        result: RunResult,
        name: str = "run",
        *,
        end: str = "complete",
        created_at_ms: int = CREATED_AT_MS,
        setup_name: str | None = "the setup",
    ) -> int:
        run_id = await create_run(
            conn,
            NewRun(
                job_id=f"{name}-{unique}",
                created_at_ms=created_at_ms,
                setup=result.setup,
                setup_name=setup_name,
                first_session_open_ms=result.sessions[0].session_open_ms,
                last_session_open_ms=result.sessions[-1].session_open_ms,
                sessions_requested=len(result.sessions),
            ),
        )
        ended: dict[str, Any] = {"finished_at_ms": created_at_ms + 5}
        if end == "complete":
            await complete_run(conn, run_id, result, **ended)
        elif end == "failed":
            await fail_run(
                conn,
                run_id,
                RunFailure("DOWNLOAD_FAILED", "The data could not be fetched.", (FailureItem(None, "x", "y"),)),
                **ended,
            )
        elif end == "cancelled":
            await cancel_run(conn, run_id, **ended)
        else:
            assert end == "running", end
        return run_id

    yield make
    await conn.execute("DELETE FROM research_options_backtest_runs WHERE job_id LIKE $1", f"%-{unique}")
