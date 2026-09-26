"""The rule-based backtest job honours a pre-set cancel flag (#2463).

Its worker checks cancellation only three times (phase boundaries), so under
the old 1,000-call default throttle none of them ever read Redis and the jobs
demo page's Cancel did nothing. This drives the worker with the real
``CancellationCheck`` against a store whose flag is already set: the first
check must read it and end the job before a single bar is fetched.
"""

from __future__ import annotations

import asyncio

import pytest

from app.jobs import progress as jobs_progress
from app.jobs.progress import CancellationCheck, JobCancelled
from app.routers import jobs as jobs_router
from app.routers.jobs import RuleBasedBacktestJobRequest


class _FlagSetRedis:
    """The one Redis answer a pre-set cancel flag needs: requested."""

    def pipeline(self) -> _FlagSetRedis:
        return self

    def hget(self, key: str, name: str) -> _FlagSetRedis:
        return self

    def expire(self, key: str, seconds: int) -> _FlagSetRedis:
        return self

    def execute(self) -> list[object]:
        return ["1", True]


class _Emitter:
    def phase(self, name: str) -> None:
        return None

    def log(self, message: str) -> None:
        return None

    def progress(self, **kwargs: object) -> None:
        return None


def _dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_run_in_thread(job_id: str, work: object, **kwargs: object) -> None:
        captured["job_id"] = job_id
        captured["work"] = work
        captured["kwargs"] = kwargs

    monkeypatch.setattr(jobs_router, "run_in_thread", fake_run_in_thread)
    asyncio.run(
        jobs_router.start_rule_based_backtest_job(
            RuleBasedBacktestJobRequest(
                job_id="t",
                symbol="SPY",
                from_date="2025-01-01",
                to_date="2025-01-31",
            )
        )
    )
    return captured


def test_a_preset_cancel_flag_ends_the_job_before_any_bar_is_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs_progress, "get_redis", lambda: _FlagSetRedis())
    fetches: list[dict] = []
    monkeypatch.setattr(
        jobs_router.polygon_client,
        "fetch_aggregates",
        lambda **kwargs: fetches.append(kwargs) or [],
    )
    captured = _dispatch(monkeypatch)

    assert captured["kwargs"]["cancel_check_every_n"] == 1
    with pytest.raises(JobCancelled):
        # The real check at its default interval: the first call must read
        # the flag, not throttle it away.
        captured["work"](_Emitter(), CancellationCheck(job_id="t"))

    assert fetches == []
