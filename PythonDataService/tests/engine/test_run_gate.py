"""One engine backtest in flight per process (#1957).

The gate exists because a full-history minute-resolution run costs ~480 MB
against the data service's 2 GiB cgroup (measured in #1944), and until now
only cells *within one sweep* were counted: a Strategy Lab run started while a
sweep executed a cell, or two Lab tabs, ran concurrently and unaccounted.

Every test here synchronises on events rather than sleeps. A sleep guarding a
negative assertion ("nothing overlapped") passes vacuously when the machine is
loaded, which is exactly when concurrency bugs show up.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from app.engine.run_gate import one_backtest_in_flight


class _Cancelled(Exception):
    """Stands in for ``app.jobs.runner.JobCancelled`` — what a worker raises to give up."""


def _spawn(target, *, name: str) -> threading.Thread:
    """Start a daemon thread; a hung gate must fail a test, not wedge pytest."""
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def _join(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), f"{thread.name} never finished; the gate is stuck"


def test_a_second_backtest_waits_for_the_one_in_flight() -> None:
    first_exited = threading.Event()
    first_inside = threading.Event()
    release_first = threading.Event()
    saw_first_exited: list[bool] = []

    def first() -> None:
        with one_backtest_in_flight():
            first_inside.set()
            release_first.wait(timeout=5)
        first_exited.set()

    def second() -> None:
        with one_backtest_in_flight():
            # Recorded from inside the gate: if the second run got in while the
            # first still held it, this is False and no timing can hide it.
            saw_first_exited.append(first_exited.is_set())

    a = _spawn(first, name="first")
    assert first_inside.wait(timeout=5)
    b = _spawn(second, name="second")

    release_first.set()
    _join(a, b)
    assert saw_first_exited == [True]


def test_a_caller_that_waits_is_told_once_before_it_blocks() -> None:
    waits: list[str] = []
    waited = threading.Event()
    first_inside = threading.Event()
    release_first = threading.Event()

    def first() -> None:
        with one_backtest_in_flight(on_wait=lambda: waits.append("first")):
            first_inside.set()
            release_first.wait(timeout=5)

    def second() -> None:
        with one_backtest_in_flight(on_wait=lambda: (waits.append("second"), waited.set())):
            pass

    a = _spawn(first, name="first")
    assert first_inside.wait(timeout=5)
    b = _spawn(second, name="second")

    # The gate is held, so the second caller must report before it blocks.
    assert waited.wait(timeout=5)
    release_first.set()
    _join(a, b)

    # The run that got straight through reports nothing; only the one that queued does.
    assert waits == ["second"]


def test_a_queued_caller_can_be_cancelled_while_it_waits() -> None:
    """The Cancel button has to reach a run that has not started yet.

    Without a poll the waiter parks on an uninterruptible ``acquire()`` and
    stays queued until the run ahead of it finishes, however long that is.
    """
    first_inside = threading.Event()
    release_first = threading.Event()
    polled = threading.Event()
    outcome: list[BaseException | None] = []

    def first() -> None:
        with one_backtest_in_flight():
            first_inside.set()
            release_first.wait(timeout=5)

    def cancel_on_first_poll() -> None:
        polled.set()
        raise _Cancelled("job cancelled")

    def second() -> None:
        try:
            with one_backtest_in_flight(while_waiting=cancel_on_first_poll):
                outcome.append(None)
        except _Cancelled as exc:
            outcome.append(exc)

    a = _spawn(first, name="first")
    assert first_inside.wait(timeout=5)
    b = _spawn(second, name="second")

    # The waiter gives up long before the run ahead of it releases.
    _join(b)
    assert polled.is_set()
    assert isinstance(outcome[0], _Cancelled)

    release_first.set()
    _join(a)

    # Abandoning the wait must not have taken the gate with it.
    entered = False
    with one_backtest_in_flight():
        entered = True
    assert entered


def test_the_wait_is_logged_before_it_starts_not_after_it_ends(caplog: pytest.LogCaptureFixture) -> None:
    """A caller with no progress channel still leaves evidence *during* the stall."""
    first_inside = threading.Event()
    release_first = threading.Event()
    logged_while_waiting = threading.Event()

    def first() -> None:
        with one_backtest_in_flight():
            first_inside.set()
            release_first.wait(timeout=5)

    def second() -> None:
        with one_backtest_in_flight(while_waiting=logged_while_waiting.set):
            pass

    with caplog.at_level(logging.INFO, logger="app.engine.run_gate"):
        a = _spawn(first, name="first")
        assert first_inside.wait(timeout=5)
        b = _spawn(second, name="second")

        # One poll means the waiter is blocked; the queue line must be out already.
        assert logged_while_waiting.wait(timeout=5)
        assert any("Queued behind the backtest in flight" in r.message for r in caplog.records)

        release_first.set()
        _join(a, b)


def test_the_gate_refuses_an_event_loop_caller_instead_of_freezing_it() -> None:
    """A blocking acquire on the app loop would stall every route, ``/health`` included."""

    async def enter_from_the_loop() -> None:
        with one_backtest_in_flight():
            pass

    with pytest.raises(RuntimeError, match="running event loop"):
        asyncio.run(enter_from_the_loop())

    entered = False
    with one_backtest_in_flight():
        entered = True
    assert entered  # the refusal happened before the gate was touched


def test_the_gate_is_released_when_a_backtest_raises() -> None:
    with pytest.raises(ValueError), one_backtest_in_flight():
        raise ValueError("engine exploded")

    entered = False
    with one_backtest_in_flight():
        entered = True
    assert entered  # a leaked gate would deadlock the whole service


def test_nesting_fails_loudly_instead_of_deadlocking() -> None:
    with one_backtest_in_flight():
        nested = _reentry_outcome()

    assert isinstance(nested, RuntimeError) and "must not nest" in str(nested)

    entered = False
    with one_backtest_in_flight():
        entered = True
    assert entered  # the refused re-entry did not disturb the outer hold


def _reentry_outcome() -> BaseException | None:
    """Re-enter the gate on a thread that already holds it, without hanging."""
    try:
        with one_backtest_in_flight():
            return None
    except RuntimeError as exc:
        return exc


def test_runs_that_never_overlap_pay_nothing() -> None:
    waits: list[str] = []
    for _ in range(3):
        with one_backtest_in_flight(on_wait=lambda: waits.append("waited")):
            pass
    assert waits == []


def test_the_engine_entry_point_holds_the_gate_for_its_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring, not just the primitive.

    ``execute_engine_backtest`` is where the sync endpoint, the Strategy Lab
    job worker, Grid Search and Walk-Forward cells and the Recency runner
    converge; gating it is what counts those five together.
    """
    from app.routers import engine as engine_router

    concurrent: list[int] = []
    running = 0
    lock = threading.Lock()
    queued = threading.Semaphore(0)
    phases: list[str] = []

    def counting_core(**kwargs: object) -> str:
        nonlocal running
        with lock:
            running += 1
            concurrent.append(running)
            first = len(concurrent) == 1
        if first:
            # The run that got in first holds the gate until the other three
            # have reported that they are queued behind it. No sleep: the
            # overlap window is open for as long as the test needs it to be.
            for _ in range(3):
                assert queued.acquire(timeout=5), "a caller neither ran nor queued"
        with lock:
            running -= 1
        return "response"

    def record_phase(phase: str) -> None:
        phases.append(phase)
        queued.release()

    monkeypatch.setattr(engine_router, "_execute_engine_backtest_core", counting_core)

    threads = [
        _spawn(
            lambda: engine_router.execute_engine_backtest(
                request=object(),  # type: ignore[arg-type] - the core is stubbed out
                on_phase=record_phase,
                on_log=lambda _: None,
            ),
            name=f"caller-{n}",
        )
        for n in range(4)
    ]
    _join(*threads)

    assert max(concurrent) == 1, f"{max(concurrent)} backtests ran at once; the gate did not hold"
    assert len(concurrent) == 4  # all four still ran — the gate queues, it never refuses
    assert phases == ["waiting_for_engine"] * 3  # the three that queued said so, once each


def test_only_the_engine_router_may_call_the_ungated_core() -> None:
    """The gate is a wrapper, so a caller reaching past it silently loses the gate.

    Naming the private core in another module is how that would happen — a
    sweep author dodging what looks like double-gating, say — so it fails here
    rather than in production memory.
    """
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[2] / "app"
    offenders = [
        path.relative_to(app_root).as_posix()
        for path in app_root.rglob("*.py")
        if path.name != "engine.py" and "_execute_engine_backtest_core" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"these modules run the engine without the gate: {offenders}"
