"""What ``claim_scoped`` records when its caller is cancelled with the worker still in flight.

The wait for an abandoned worker is bounded; past the bound the claim is
released anyway, and the CRITICAL record is the only trace of a worker that may
still be writing. A worker that fails after its caller left is logged, not
dropped as an unretrieved task exception.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from app.broker.alpaca.clerk.sqlite.off_loop import claim_scoped, to_thread

_STARTED_WAIT_S = 10


async def _cancel_while_in_flight(
    started: threading.Event, task: asyncio.Task[None], *, release: threading.Event | None = None
) -> None:
    """Cancel ``task`` once its worker is inside the unit; optionally let the worker go at once."""
    await asyncio.get_running_loop().run_in_executor(None, started.wait, _STARTED_WAIT_S)
    task.cancel()
    if release is not None:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_worker_that_outlives_the_bound_is_logged_critical_and_the_cancel_still_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def stuck_unit() -> None:
        started.set()
        release.wait(timeout=_STARTED_WAIT_S)

    guarded = claim_scoped(to_thread, abandoned_timeout_s=0.05)
    task: asyncio.Task[None] = asyncio.create_task(guarded(stuck_unit))
    try:
        with caplog.at_level(logging.ERROR):
            await _cancel_while_in_flight(started, task)
    finally:
        release.set()

    critical = [record for record in caplog.records if record.levelno == logging.CRITICAL]
    assert len(critical) == 1
    assert critical[0].off_loop_event == "claim_scoped_abandoned_timeout"
    assert critical[0].abandoned_timeout_s == 0.05


async def test_a_worker_that_fails_after_its_caller_was_cancelled_is_logged_with_its_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = threading.Event()
    release = threading.Event()
    failure = RuntimeError("fold failed after the caller left")

    def failing_unit() -> None:
        started.set()
        release.wait(timeout=_STARTED_WAIT_S)
        raise failure

    guarded = claim_scoped(to_thread)
    task: asyncio.Task[None] = asyncio.create_task(guarded(failing_unit))
    with caplog.at_level(logging.ERROR):
        await _cancel_while_in_flight(started, task, release=release)

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].off_loop_event == "claim_scoped_abandoned_error"
    assert errors[0].exc_info is not None and errors[0].exc_info[1] is failure
    assert [record for record in caplog.records if record.levelno == logging.CRITICAL] == []


async def test_the_cancel_is_held_back_until_the_worker_finishes_and_a_clean_finish_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def clean_unit() -> None:
        started.set()
        release.wait(timeout=_STARTED_WAIT_S)
        finished.set()

    guarded = claim_scoped(to_thread)
    task: asyncio.Task[None] = asyncio.create_task(guarded(clean_unit))
    await asyncio.get_running_loop().run_in_executor(None, started.wait, _STARTED_WAIT_S)
    with caplog.at_level(logging.INFO):
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        # The worker is still folding, so the caller's unwind (the claim release) has not run.
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert finished.is_set()
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []
