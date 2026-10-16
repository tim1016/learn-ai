"""QuantLib worker lifetime, concurrency and global-setting isolation (#3212)."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from app.services import quantlib_worker


def worker_probe(delay: float) -> tuple[int, float, float]:
    """A picklable job reporting its own process and occupied interval."""
    started = time.monotonic()
    time.sleep(delay)
    return os.getpid(), started, time.monotonic()


async def test_pricing_calls_share_one_worker_and_never_overlap() -> None:
    first, second = await asyncio.gather(
        quantlib_worker.run_quantlib(worker_probe, delay=0.05),
        quantlib_worker.run_quantlib(worker_probe, delay=0.05),
    )

    assert first[0] == second[0] != os.getpid()
    assert first[2] <= second[1] or second[2] <= first[1]


async def test_a_timed_out_c_call_is_killed_and_the_next_call_gets_a_new_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    before = await quantlib_worker.run_quantlib(worker_probe, delay=0.0)
    with monkeypatch.context() as bounded:
        bounded.setattr(quantlib_worker, "QUANTLIB_WAIT_S", 0.05)
        with pytest.raises(TimeoutError):
            await quantlib_worker.run_quantlib(worker_probe, delay=30.0)

    after = await quantlib_worker.run_quantlib(worker_probe, delay=0.0)
    assert after[0] != before[0]


def set_worker_evaluation_date() -> int:
    """Change QuantLib's process-global setting without changing it in the service."""
    import QuantLib as ql

    ql.Settings.instance().evaluationDate = ql.Date(2, 1, 2099)
    return ql.Settings.instance().evaluationDate.serialNumber()


async def test_the_pricing_evaluation_date_cannot_change_the_services_solver_date() -> None:
    ql = pytest.importorskip("QuantLib")
    settings = ql.Settings.instance()
    previous = settings.evaluationDate
    try:
        settings.evaluationDate = ql.Date(15, 1, 2020)
        changed = await quantlib_worker.run_quantlib(set_worker_evaluation_date)
        assert changed != settings.evaluationDate.serialNumber()
        assert settings.evaluationDate == ql.Date(15, 1, 2020)
    finally:
        settings.evaluationDate = previous
