"""Log budget for IBKR connect-attempt failures (#2080).

A sustained IB Gateway outage used to produce one WARNING per failed
``connect()`` attempt plus ``ib_async``'s own duplicate ERROR pair per
attempt — roughly 350 log lines per clerk per 30-minute outage. This
module collapses that into: one WARNING when the outage starts, one
periodic summary WARNING per ``SUPPRESSION_WINDOW_MS`` while it
continues (naming how many attempts were suppressed in between — every
suppressed attempt is accounted for, never silently dropped), and one
INFO when the gateway becomes reachable again.

``CONNECT_LOG_BUDGET`` is a module-level singleton with an injectable
clock, mirroring the pattern ``AutoReconnectMonitor`` uses
(``app/broker/ibkr/auto_reconnect_monitor.py``) so tests can freeze
time via ``reset_for_testing(now_ms=...)``.

The ``ib_async.client`` noise filter is installed by the FastAPI
lifespan (``app/main.py``), not at import time — this module is
imported unconditionally by ``client.py``, including by roles with
IBKR disabled and by every pytest process, and none of those should
silently acquire a filter on a logger they don't own. Call
``install_ib_async_noise_filter()`` to attach it; it's idempotent.
"""

from __future__ import annotations

import logging
from typing import Literal

from app.utils.timestamps import Clock, now_ms_utc

SUPPRESSION_WINDOW_MS: int = 900_000  # 15 minutes

type LogVerdict = Literal["report_first", "suppress", "report_summary"]


class ConnectLogBudget:
    """Decides what a single IBKR connect-attempt failure should log.

    One outage is tracked at a time: the first failure always reports,
    a change in failure shape (exception type + errno) always reports
    (it's new information), and once ``SUPPRESSION_WINDOW_MS`` has
    elapsed since the last report the next failure reports a summary
    naming how many attempts were suppressed since. Everything else in
    between is suppressed (demoted to DEBUG by the caller).
    """

    def __init__(self, now_ms: Clock = now_ms_utc) -> None:
        self._now_ms = now_ms
        self._outage_started_ms: int | None = None
        self._last_reported_ms: int | None = None
        self._suppressed: int = 0
        self._last_shape: str | None = None
        self.suppressed_attempts: int = 0

    @property
    def unreachable_since_ms(self) -> int | None:
        """Start of the currently tracked outage, or ``None`` if healthy."""
        return self._outage_started_ms

    @property
    def suppressing(self) -> bool:
        """True while an outage is being tracked (between a first report
        and the recovery that clears it), regardless of whether the most
        recent attempt itself reported or was suppressed. Duplicate noise
        for an attempt that JUST reported is exactly as uninteresting as
        duplicate noise for an attempt that was suppressed outright."""
        return self._outage_started_ms is not None

    def note_failure(self, exc: Exception) -> LogVerdict:
        now = self._now_ms()
        shape = repr(type(exc)) + str(getattr(exc, "errno", ""))

        is_new_outage = self._outage_started_ms is None
        shape_changed = not is_new_outage and shape != self._last_shape
        window_elapsed = (
            not is_new_outage
            and not shape_changed
            and self._last_reported_ms is not None
            and now - self._last_reported_ms >= SUPPRESSION_WINDOW_MS
        )

        if is_new_outage:
            self._outage_started_ms = now
        self._last_shape = shape

        if is_new_outage or shape_changed:
            # A shape change discards whatever accumulated in ``_suppressed``
            # since the last report. ``_report`` copies it onto the public
            # field before zeroing it, so the discard is a visible fact on
            # the verdict rather than a silent reset — the caller can
            # report it (#2113).
            self._report(now)
            return "report_first"

        if window_elapsed:
            self._report(now)
            return "report_summary"

        self._suppressed += 1
        return "suppress"

    def _report(self, now: int) -> None:
        """Snapshot ``_suppressed`` onto the public field and reset the
        window. Shared by the ``report_first`` and ``report_summary``
        verdicts above — both discard whatever was suppressed since the
        last report, and both must leave that count somewhere the caller
        can log it, not just zero it."""
        self.suppressed_attempts = self._suppressed
        self._last_reported_ms = now
        self._suppressed = 0

    def note_success(self) -> int | None:
        """Clear the tracked outage. Returns its duration in ms for one
        recovery INFO, or ``None`` if there was nothing to clear."""
        if self._outage_started_ms is None:
            return None
        duration_ms = self._now_ms() - self._outage_started_ms
        self._outage_started_ms = None
        self._last_reported_ms = None
        self._suppressed = 0
        self._last_shape = None
        self.suppressed_attempts = 0
        return duration_ms

    def reset_for_testing(self, *, now_ms: Clock = now_ms_utc) -> None:
        self._now_ms = now_ms
        self._outage_started_ms = None
        self._last_reported_ms = None
        self._suppressed = 0
        self._last_shape = None
        self.suppressed_attempts = 0


class _IbAsyncConnectNoiseFilter(logging.Filter):
    """Drops ``ib_async``'s own duplicate connect-failure ERROR lines
    while ``CONNECT_LOG_BUDGET`` is suppressing repeats of an outage.

    Narrow on purpose: only the two specific lines ``ib_async`` emits
    per failed ``connectAsync`` (see its ``Client.connectAsync``) are
    eligible, only at ERROR, and only while an outage is active. Every
    other ``ib_async.client`` log line — including a shape change, like
    a TimeoutError replacing a ConnectionRefusedError — passes through.
    """

    _SUPPRESSED_EXACT = "Make sure API port on TWS/IBG is open"
    _SUPPRESSED_PREFIX = "API connection failed: ConnectionRefusedError"

    def filter(self, record: logging.LogRecord) -> bool:
        if not CONNECT_LOG_BUDGET.suppressing or record.levelno != logging.ERROR:
            return True
        message = record.getMessage()
        return not (message == self._SUPPRESSED_EXACT or message.startswith(self._SUPPRESSED_PREFIX))


CONNECT_LOG_BUDGET = ConnectLogBudget()


def install_ib_async_noise_filter() -> None:
    """Attach the outage noise filter to ib_async's client logger once."""
    target = logging.getLogger("ib_async.client")
    if any(isinstance(existing, _IbAsyncConnectNoiseFilter) for existing in target.filters):
        return
    target.addFilter(_IbAsyncConnectNoiseFilter())
