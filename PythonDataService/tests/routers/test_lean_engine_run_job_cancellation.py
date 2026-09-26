"""Cancel stops a LEAN run before its container launches, and says so when it is too late (#2463).

The LEAN worker used to check cancellation once at the top of ``work`` while
``run_in_thread`` still throttled the flag at its 1,000-call default, so the
first 999 checks — every check this job ever made — answered "not cancelled"
without reading Redis: the Cancel button did nothing. The seams below pin the
whole class:

* a cancel set before the work starts ends the job ``cancelled`` without ever
  calling the orchestrator;
* a cancel set while data stages ends it ``cancelled`` before the container
  launch;
* a cancel set once the container runs finishes the run, keeps and saves the
  result as always, and acknowledges the request on the log instead of a
  silent no-op — never a false ``cancelled``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.jobs.progress import JobCancelled
from app.routers import jobs as jobs_router
from app.routers.jobs import LeanEngineRunJobRequest
from app.services import lean_sidecar_service

REQUEST = {
    "run_id": "unit_cancel",
    "requested_engine": "both",
    "start_ms_utc": 1_736_778_600_000,
    "end_ms_utc": 1_736_865_000_000,
    "starting_cash": 100_000,
    "template": "ema_crossover",
    "parity_group_id": None,
    "data_policy": {
        "source": "polygon",
        "symbol": "SPY",
        "adjusted": True,
        "session": "regular",
        "input_bars": {"timespan": "minute", "multiplier": 1},
        "strategy_bars": {"timespan": "minute", "multiplier": 15},
        "timestamp_policy": "bar_close_ms_utc",
        "timezone": "America/New_York",
        "provider_kind": "live",
        "fixture_id": None,
        "fixture_sha256": None,
    },
}


class _CancelState:
    """A cancellation check driven by an explicit flag, with the real one's semantics."""

    def __init__(self, requested: bool = False) -> None:
        self.requested = requested

    def should_cancel(self) -> bool:
        return self.requested

    def raise_if_cancelled(self) -> None:
        if self.requested:
            raise JobCancelled("job cancelled by test")


class _Emitter:
    def __init__(self) -> None:
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def phase(self, name: str) -> None:
        self.phases.append(name)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def failed(self, *, code: str, message: str) -> None:
        self.failures.append((code, message))


def _dispatch(monkeypatch: pytest.MonkeyPatch, fake_run_trusted_sample: Any) -> dict[str, Any]:
    """Run the handler with the thread runner stubbed; return what it asked for."""
    captured: dict[str, Any] = {}

    def fake_run_in_thread(job_id: str, work: Any, **kwargs: Any) -> None:
        captured["job_id"] = job_id
        captured["work"] = work
        captured["kwargs"] = kwargs

    monkeypatch.setattr(jobs_router, "run_in_thread", fake_run_in_thread)
    monkeypatch.setattr(lean_sidecar_service, "run_trusted_sample", fake_run_trusted_sample)
    asyncio.run(jobs_router.start_lean_engine_run_job(LeanEngineRunJobRequest(job_id="job-1", request=REQUEST)))
    return captured


def _result_dict() -> dict[str, Any]:
    return {"run_id": "unit_cancel", "exit_code": 0, "strategy_execution_id": 42}


def test_the_worker_reads_the_cancel_flag_on_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unreachable(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("the orchestrator must not be called")

    captured = _dispatch(monkeypatch, unreachable)

    assert captured["kwargs"]["cancel_check_every_n"] == 1


def test_a_cancel_set_before_the_work_starts_never_calls_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[Any] = []

    async def fake_run_trusted_sample(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        called.append(args)
        raise AssertionError("the orchestrator must not be called")

    captured = _dispatch(monkeypatch, fake_run_trusted_sample)
    emitter = _Emitter()

    with pytest.raises(JobCancelled):
        captured["work"](emitter, _CancelState(requested=True))

    assert called == []
    assert emitter.phases == []


def test_a_cancel_set_during_staging_ends_the_job_before_the_container_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    cancel = _CancelState()

    async def fake_run_trusted_sample(request: Any, *, on_phase: Any, on_log: Any) -> Any:
        seen["called"] = True
        on_phase("staging_data")
        on_log("staging…")
        cancel.requested = True  # the operator presses Cancel while data stages
        on_phase("launching_sidecar")  # the between-staging-and-launch seam
        seen["launched"] = True  # must not be reached
        on_phase("sidecar_running")
        return _result_dict()

    captured = _dispatch(monkeypatch, fake_run_trusted_sample)
    emitter = _Emitter()

    with pytest.raises(JobCancelled):
        captured["work"](emitter, cancel)

    assert seen.get("called") is True
    assert "launched" not in seen
    assert "sidecar_running" not in emitter.phases


def test_a_cancel_set_after_the_launch_finishes_the_run_and_acknowledges_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = _CancelState()

    async def fake_run_trusted_sample(request: Any, *, on_phase: Any, on_log: Any) -> Any:
        on_phase("staging_data")
        on_phase("launching_sidecar")
        on_phase("sidecar_running")  # the launch is committed from here on
        cancel.requested = True
        on_phase("parsing_results")  # too late to stop: acknowledged, not cancelled
        on_phase("persisting")
        return _result_dict()

    captured = _dispatch(monkeypatch, fake_run_trusted_sample)
    emitter = _Emitter()

    result = captured["work"](emitter, cancel)

    assert result is not None and result["strategy_execution_id"] == 42
    assert "parsing_results" in emitter.phases and "persisting" in emitter.phases
    acknowledgments = [message for message in emitter.logs if "already launched" in message]
    assert len(acknowledgments) == 1
    assert "Cancel requested" in acknowledgments[0]
    assert emitter.failures == []
