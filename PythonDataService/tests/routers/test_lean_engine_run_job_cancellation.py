"""Cancel stops a LEAN run before its container launches, and says so when it is too late (#2463).

The LEAN worker used to check cancellation once at the top of ``work`` while
``run_in_thread`` still throttled the flag at its 1,000-call default, so the
first 999 checks — every check this job ever made — answered "not cancelled"
without reading Redis: the Cancel button did nothing. The framework fix and
the seams' ownership are pinned here at the worker boundary, and the
orchestrator's own seam behaviour (workspace cleanup, prompt acknowledgment)
is pinned in ``tests/services/test_lean_sidecar_cancellation.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.jobs.progress import JobCancelled
from app.routers import jobs as jobs_router
from app.routers.jobs import LeanEngineRunJobRequest
from app.services import lean_sidecar_service
from app.services.lean_sidecar_service import CANCEL_TOO_LATE_MESSAGE, LeanRunCancelled

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
        self.cancel_acknowledgments: list[str] = []

    def phase(self, name: str) -> None:
        self.phases.append(name)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def failed(self, *, code: str, message: str) -> None:
        self.failures.append((code, message))

    def cancel_acknowledged(self, message: str) -> None:
        self.cancel_acknowledgments.append(message)


def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    fake_run_trusted_sample: Any,
    *,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the handler with the thread runner stubbed; return what it asked for."""
    captured: dict[str, Any] = {}

    def fake_run_in_thread(job_id: str, work: Any, **kwargs: Any) -> None:
        captured["job_id"] = job_id
        captured["work"] = work
        captured["kwargs"] = kwargs

    monkeypatch.setattr(jobs_router, "run_in_thread", fake_run_in_thread)
    monkeypatch.setattr(lean_sidecar_service, "run_trusted_sample", fake_run_trusted_sample)
    asyncio.run(
        jobs_router.start_lean_engine_run_job(
            LeanEngineRunJobRequest(job_id="job-1", request=request or REQUEST)
        )
    )
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


def test_the_orchestrators_cancellation_translates_to_the_frameworks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-launch cancel surfaces as ``JobCancelled`` — the runner's sink
    emits ``job.cancelled`` — and marks a paired group's parity verdict so it
    is not left eternally pending."""
    marked: list[tuple[str, str]] = []

    async def cancelled_while_staging(request: Any, **hooks: Any) -> Any:
        raise LeanRunCancelled("run unit_cancel cancelled before the LEAN container launched")

    paired_request = {**REQUEST, "parity_group_id": "pair-1"}
    captured = _dispatch(monkeypatch, cancelled_while_staging, request=paired_request)

    import app.services.parity_companion as parity_companion

    monkeypatch.setattr(
        parity_companion,
        "mark_parity_failed",
        lambda group_id, *, status, detail: marked.append((group_id, status, detail)),
    )
    emitter = _Emitter()

    with pytest.raises(JobCancelled):
        captured["work"](emitter, _CancelState())

    assert emitter.failures == []
    assert marked == [("pair-1", "run_failed", "cancelled before the LEAN container launched")]


def test_a_too_late_cancel_flows_to_the_typed_acknowledgment_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker hands the orchestrator its flag reader and its typed
    acknowledgment emitter; a too-late acknowledgment lands on that emitter
    while the run finishes and is saved as always."""
    seen_hooks: dict[str, Any] = {}

    async def fake_run_trusted_sample(request: Any, **hooks: Any) -> Any:
        seen_hooks.update(hooks)
        # The orchestrator dedupes to one acknowledgment (pinned in the
        # service-level suite); the worker just wires the emitter through.
        hooks["on_cancel_too_late"](CANCEL_TOO_LATE_MESSAGE)
        return _result_dict()

    captured = _dispatch(monkeypatch, fake_run_trusted_sample)
    emitter = _Emitter()
    cancel = _CancelState()

    result = captured["work"](emitter, cancel)

    assert result is not None and result["strategy_execution_id"] == 42
    assert seen_hooks["cancel_requested"] == cancel.should_cancel
    assert seen_hooks["on_cancel_too_late"] == emitter.cancel_acknowledged
    assert emitter.cancel_acknowledgments == [CANCEL_TOO_LATE_MESSAGE]
    assert emitter.failures == []
