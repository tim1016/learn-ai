"""A queued engine backtest stays cancellable (#1957).

The process-wide engine gate queues a run behind the one in flight instead of
refusing it, which makes two pre-existing facts about this worker matter for
the first time:

* the gate polls ``while_waiting`` so a waiter can give up — without a
  cancellation check wired into it, a queued run is unreachable until the run
  ahead of it finishes;
* ``CancellationCheck`` answers from a counter, not from Redis, until
  ``check_every_n`` calls have been made. This worker makes about eight, so at
  the default of 1000 the Cancel button was inert. Every other job type
  already passes 1.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from app.routers import jobs as jobs_router
from app.routers.jobs import EngineBacktestJobRequest


def _dispatch(monkeypatch: Any) -> dict[str, Any]:
    """Run the handler with the thread runner stubbed; return what it asked for."""
    captured: dict[str, Any] = {}

    def fake_run_in_thread(job_id: str, work: Any, **kwargs: Any) -> None:
        captured["job_id"] = job_id
        captured["work"] = work
        captured["kwargs"] = kwargs

    monkeypatch.setattr(jobs_router, "run_in_thread", fake_run_in_thread)
    asyncio.run(
        jobs_router.start_engine_backtest_job(
            EngineBacktestJobRequest(
                job_id="job-1",
                backtest={
                    "strategy_name": "sma_crossover",
                    "symbol": "SPY",
                    "from_date": "2025-01-01",
                    "to_date": "2025-01-31",
                },
            )
        )
    )
    return captured


def test_the_worker_checks_redis_on_every_cancel_call(monkeypatch: Any) -> None:
    captured = _dispatch(monkeypatch)
    assert captured["kwargs"]["cancel_check_every_n"] == 1


def test_a_queued_run_is_cancelled_by_the_gates_poll(monkeypatch: Any) -> None:
    captured = _dispatch(monkeypatch)

    class _Cancelled(Exception):
        pass

    class FakeCancel:
        def __init__(self) -> None:
            self.calls = 0

        def raise_if_cancelled(self) -> None:
            self.calls += 1
            if self.calls > 1:  # the first is the pre-flight check
                raise _Cancelled("job cancelled")

    seen: dict[str, Any] = {}

    def fake_execute(**kwargs: Any) -> Any:
        seen.update(kwargs)
        # Stand in for the gate: this is what it does while the caller queues.
        kwargs["while_waiting"]()
        raise AssertionError("the queued run should have been cancelled before it started")

    monkeypatch.setattr(jobs_router, "execute_engine_backtest", fake_execute)

    cancel = FakeCancel()

    class FakeEmit:
        def phase(self, name: str) -> None: ...
        def log(self, message: str) -> None: ...

    try:
        captured["work"](FakeEmit(), cancel)
    except _Cancelled:
        pass
    else:
        raise AssertionError("cancellation did not reach the queued run")

    assert seen["while_waiting"] == cancel.raise_if_cancelled


def test_the_engine_entry_point_offers_a_wait_hook() -> None:
    """The parameter the worker passes has to exist on the seam it passes it to."""
    from app.routers.engine import execute_engine_backtest

    assert "while_waiting" in inspect.signature(execute_engine_backtest).parameters
