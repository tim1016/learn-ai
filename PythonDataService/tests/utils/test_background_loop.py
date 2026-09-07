"""The shared writer loop's two timeouts, which are not the same timeout (#1977).

``run_on_background_loop`` blocks its caller for a coroutine running on the
process-wide loop. Two different things can raise ``TimeoutError`` there — the
caller's own wait expiring, and the coroutine timing out internally (asyncpg's
``command_timeout``) — and since Python 3.11 ``asyncio.TimeoutError`` *is*
``TimeoutError``, so they arrive at the call site indistinguishable. They mean
opposite things: the first leaves the work running, the second means it stopped.
"""

from __future__ import annotations

import asyncio

import pytest

from app.utils.background_loop import CallerStoppedWaitingError, run_on_background_loop


def test_a_wait_that_expires_says_the_caller_stopped_waiting() -> None:
    async def slow() -> str:
        await asyncio.sleep(0.3)
        return "done"

    with pytest.raises(CallerStoppedWaitingError, match="stopped waiting"):
        run_on_background_loop(slow(), timeout=0.05)


def test_the_abandoned_coroutine_still_runs_to_its_conclusion() -> None:
    reached: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(0.2)
        reached.append("committed")

    with pytest.raises(CallerStoppedWaitingError):
        run_on_background_loop(slow(), timeout=0.05)

    run_on_background_loop(asyncio.sleep(0.4), timeout=2.0)
    assert reached == ["committed"]


def test_a_timeout_the_coroutine_raised_itself_propagates_unchanged() -> None:
    """asyncpg's ``command_timeout`` looks like this: the work stopped."""

    async def times_out_internally() -> None:
        raise TimeoutError("query timed out")

    with pytest.raises(TimeoutError) as caught:
        run_on_background_loop(times_out_internally(), timeout=5.0)

    assert not isinstance(caught.value, CallerStoppedWaitingError)
    assert str(caught.value) == "query timed out"


def test_any_other_exception_propagates_unchanged() -> None:
    async def explodes() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_on_background_loop(explodes(), timeout=5.0)


def test_a_result_that_lands_in_time_is_returned() -> None:
    async def quick() -> int:
        return 7

    assert run_on_background_loop(quick(), timeout=5.0) == 7
