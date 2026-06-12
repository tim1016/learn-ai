"""Parser for ERROR / CRITICAL log records (with traceback blocks) in live.log.

Reference: live.log format produced by app.engine.live.run's logging config
(``YYYY-MM-DD HH:MM:SS,mmm LEVEL logger message`` plus indented traceback
continuation lines).
Canonical implementation: app/services/live_log_failures.py
Validated against: PythonDataService/tests/test_live_log_failures.py

A failure record begins on a header line at level ERROR or CRITICAL and absorbs
the contiguous block of continuation lines (non-header lines, typically the
``Traceback (most recent call last):`` body) until the next header. WARNING and
below are ignored per the agreed scope.

Timestamp policy: Python's ``logging.Formatter`` defaults to ``time.localtime``
for ``%(asctime)s``, so live.log timestamps are in the engine host's local
TZ — not UTC. We surface both:

* ``raw_ts``: the original timestamp string from the log (always correct for
  display alongside the engine's other UI strings).
* ``ts_ms``: the same timestamp parsed *as if* UTC. This is internally
  consistent for sequencing failures within one run (since every log line in
  a run shares the same TZ) and for the ``since_ms`` cursor, but it is NOT
  guaranteed to equal wall-clock UTC ms when the host TZ ≠ UTC.

The UI should render ``raw_ts`` for absolute display and use ``ts_ms`` only
for ordering and incremental polling.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

_HEADER_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2}),(?P<ms>\d{3})\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>[\w\.\-]+)\s+"
    r"(?P<message>.*)$"
)

FailureLevel = Literal["ERROR", "CRITICAL"]
_GATING_LEVELS: frozenset[str] = frozenset({"ERROR", "CRITICAL"})

# Hard cap on the traceback we keep so a runaway stack can't bloat the API
# response. Tracebacks longer than this are truncated with a sentinel.
_TRACEBACK_CHAR_CAP = 4_000


class FailureRow(BaseModel):
    """One parsed ERROR / CRITICAL block from live.log."""

    ts_ms: int
    raw_ts: str
    level: FailureLevel
    logger: str
    message: str
    traceback: str | None = None


def _parse_header_ts_ms(date: str, time: str, ms: str) -> int:
    dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000) + int(ms)


def parse_failures(text: str) -> list[FailureRow]:
    """Parse a live.log text body into FailureRow records.

    Args:
        text: Full file content (or any subrange that starts on a header line).

    Returns:
        Failures in source order. Each row's ``traceback`` is the joined
        continuation block (without the header line), ``None`` if the failure
        had no continuation lines, or ``"…"`` suffix when truncated.
    """
    failures: list[FailureRow] = []
    current: FailureRow | None = None
    cont: list[str] = []

    def _flush() -> None:
        nonlocal current, cont
        if current is None:
            return
        if cont:
            joined = "\n".join(cont)
            if len(joined) > _TRACEBACK_CHAR_CAP:
                joined = joined[:_TRACEBACK_CHAR_CAP] + "\n… (truncated)"
            current.traceback = joined
        failures.append(current)
        current = None
        cont = []

    for raw in text.splitlines():
        m = _HEADER_RE.match(raw)
        if m is None:
            # Continuation line — only meaningful while a failure is open.
            if current is not None:
                cont.append(raw.rstrip())
            continue

        # New header — close any open failure first.
        _flush()
        if m.group("level") not in _GATING_LEVELS:
            continue
        date_s, time_s, ms_s = m.group("date"), m.group("time"), m.group("ms")
        current = FailureRow(
            ts_ms=_parse_header_ts_ms(date_s, time_s, ms_s),
            raw_ts=f"{date_s} {time_s}.{ms_s}",
            level=m.group("level"),  # type: ignore[arg-type]
            logger=m.group("logger"),
            message=m.group("message").rstrip(),
        )

    _flush()
    return failures
