"""The event loop says how long it went unserved (#1943).

A stall used to reach an operator only as a healthcheck streak, an execution
lease expiring and reviving itself, or bots dying with ``FEED_DEATH`` — every
symptom at one remove from the cause, and none of them naming a duration.

The assertions are one-sided on purpose. ``time.sleep(n)`` guarantees *at
least* n and says nothing about the ceiling, so an upper bound on a measured
lag is a bound on the machine, not on the code — and this suite runs on a
2-vCPU CI runner alongside everything else.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.utils import loop_lag


async def _block_the_loop(seconds: float) -> None:
    """Exactly what a data-lake artifact build used to do on this loop."""
    await asyncio.sleep(0.01)
    time.sleep(seconds)


@pytest.mark.asyncio
async def test_a_loop_that_is_serving_normally_reports_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.utils.loop_lag"):
        await loop_lag.watch_loop_lag(interval_seconds=0.01, warn_after_seconds=0.5, iterations=3)

    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_blocked_loop_is_reported_with_the_duration_it_was_blocked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The point of the monitor: the stall becomes a number, not an inference."""
    with caplog.at_level(logging.WARNING, logger="app.utils.loop_lag"):
        blocker = asyncio.create_task(_block_the_loop(0.3))
        await loop_lag.watch_loop_lag(interval_seconds=0.01, warn_after_seconds=0.1, iterations=2)
        await blocker

    assert caplog.records, "a 0.3s block produced no report"
    assert any(record.lag_seconds >= 0.25 for record in caplog.records)
    assert all("went unserved" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_a_later_report_still_names_the_worst_stall_seen(caplog: pytest.LogCaptureFixture) -> None:
    """An operator reading any one line learns the worst so far, not just the latest."""

    async def a_long_block_then_a_short_one() -> None:
        await _block_the_loop(0.4)
        await _block_the_loop(0.15)

    with caplog.at_level(logging.WARNING, logger="app.utils.loop_lag"):
        blocker = asyncio.create_task(a_long_block_then_a_short_one())
        await loop_lag.watch_loop_lag(interval_seconds=0.01, warn_after_seconds=0.1, iterations=5)
        await blocker

    assert len(caplog.records) >= 2, "two blocks produced fewer than two reports"
    worst = max(record.lag_seconds for record in caplog.records)
    assert worst >= 0.35
    # The carry-forward property, asserted without depending on which report
    # measured what: every line names the worst lag seen up to that point.
    assert caplog.records[-1].max_lag_seconds == worst
