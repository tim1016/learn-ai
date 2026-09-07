"""Measure how long the app's event loop goes unserved, and say so (#1943).

A stall on the FastAPI loop has no signature of its own. What an operator sees
is a healthcheck streak, an execution lease expiring and reviving itself
(ADR 0050), bots dying with ``FEED_DEATH`` (#1921) — every symptom at one
remove from the cause, and none of them naming a duration. The nine-minute
data-lake stalls of 2026-09-06 were attributed only by attaching ``py-spy`` to
a running container while one was happening.

The measurement is the oldest one there is: sleep a known interval, and see how
much later than that you wake. The excess is time this task was not free to
run — a lower bound on the block, since whatever part of it fell before the
deadline is invisible. It costs one timer wakeup per interval and no
allocation.

What it deliberately does **not** do is name a cause. The measurement cannot
tell a coroutine holding the loop from the process being descheduled by the
podman VM (which is what #1921 turned out to be), from a long GC pause, or from
the loop thread losing the GIL to a worker. A module whose whole product is
"the stall is a number rather than an inference" must not ship an inference in
its own log line.

A blocked loop cannot report while it is blocked; the wakeup lands after the
block clears, and the lag it reports *is* the block's duration. So one stall
produces exactly one line, after the fact, with a number in it — which is the
thing that was missing.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# A loop that is serving normally wakes within a few ms of its timer. 1.0 s is
# far enough above scheduling noise that a report means something went wrong,
# and far below the tens of seconds it takes for a stall to become someone
# else's incident.
DEFAULT_WARN_AFTER_SECONDS = 1.0

# Sampled well under the threshold, because a block only shows up as lag to the
# extent that it outlasts the deadline it interrupted. A block of B seconds
# starting t into an interval of I reports ``t + B - I``, so the worst case
# (t = 0) reports ``B - I``: sampling at the threshold would leave every block
# between 1 s and 2 s detected or missed depending on when it happened to
# start. At 0.25 s, anything past 1.25 s is caught however it lands, and the
# number in the log is a lower bound on the block — never an overstatement.
DEFAULT_INTERVAL_SECONDS = 0.25


async def watch_loop_lag(
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    warn_after_seconds: float = DEFAULT_WARN_AFTER_SECONDS,
    iterations: int | None = None,
) -> None:
    """Report every wakeup that arrives late. Runs until cancelled.

    ``iterations`` bounds the loop for tests; production leaves it ``None``.
    """
    worst = 0.0
    stalls = 0
    completed = 0
    while iterations is None or completed < iterations:
        slept_from = time.monotonic()
        await asyncio.sleep(interval_seconds)
        lag = time.monotonic() - slept_from - interval_seconds
        completed += 1
        if lag < warn_after_seconds:
            continue

        stalls += 1
        worst = max(worst, lag)
        logger.warning(
            "[LOOP] Event loop went unserved for at least %.1fs (worst so far %.1fs, %d this process). "
            "Either something held the loop, a worker thread held the GIL, or the process "
            "itself was descheduled — this measures the gap, not the cause.",
            lag,
            worst,
            stalls,
            extra={"lag_seconds": lag, "max_lag_seconds": worst},
        )
