"""Parser for live.log records (header + indented continuation blocks).

Reference: live.log format produced by app.engine.live.run's logging config
(``YYYY-MM-DD HH:MM:SS,mmm LEVEL logger message`` plus indented traceback
continuation lines).
Canonical implementation: app/services/live_log_failures.py
Validated against: PythonDataService/tests/test_live_log_failures.py

This module exposes two parsers built on the same header tokenisation:

* ``parse_failures(text) -> list[FailureRow]`` — the legacy ERROR/CRITICAL
  parser consumed by the existing failures-table API. Its shape and
  semantics are frozen so the existing consumers (``app.routers.live_runs``)
  do not need to change.
* ``parse_incidents(text) -> list[IncidentRow]`` — the new WARNING/ERROR/
  CRITICAL parser that backs the trader-first Recent Incidents panel
  (issue #565). Each row carries a backend-classified
  ``incident_category`` so the frontend never re-derives meaning from
  raw Python tracebacks.

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
for ordering and incremental polling. This caveat applies to both
``FailureRow`` and ``IncidentRow``. Treating ``IncidentRow.ts_ms`` as
canonical UTC is gated on a follow-up backend change that emits live log
timestamps as ``int64 ms UTC`` at source.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
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

IncidentLevel = Literal["WARNING", "ERROR", "CRITICAL"]
_INCIDENT_GATING_LEVELS: frozenset[str] = frozenset({"WARNING", "ERROR", "CRITICAL"})

# Hard cap on the traceback we keep so a runaway stack can't bloat the API
# response. Tracebacks longer than this are truncated with a sentinel.
_TRACEBACK_CHAR_CAP = 4_000


class IncidentCategory(StrEnum):
    """Operator-facing classification of a live-engine log incident.

    The frontend's ``INCIDENT_COPY`` map keys on these values to render
    trader-language title / message / severity / recommendedAction. The
    enum is the single source of truth for categorisation; the frontend
    never re-classifies from raw log text. A missing or unrecognised
    category from the backend is rendered as ``UNKNOWN`` on the
    frontend for rollout safety.
    """

    BROKER_DISCONNECT = "broker_disconnect"
    BROKER_RECONNECT_FAILED = "broker_reconnect_failed"
    ENGINE_FATAL = "engine_fatal"
    PORTFOLIO_INIT_FAIL = "portfolio_init_fail"
    RECONCILE_MISSING = "reconcile_missing"
    LOST_FILL = "lost_fill"
    OUTSIDE_MUTATION = "outside_mutation"
    COLD_START_DIVERGENCE = "cold_start_divergence"
    OPERATOR_HALT = "operator_halt"
    SUBSCRIPTION_STALE = "subscription_stale"
    UNKNOWN = "unknown"


class FailureRow(BaseModel):
    """One parsed ERROR / CRITICAL block from live.log."""

    ts_ms: int
    raw_ts: str
    level: FailureLevel
    logger: str
    message: str
    traceback: str | None = None


class IncidentRow(BaseModel):
    """One parsed WARNING / ERROR / CRITICAL block from live.log.

    Shares the log fields used by ``FailureRow`` but widens ``level`` to
    include WARNING (e.g., ib_async surfaces broker-connectivity loss as
    a WARNING-level Error 1100, which an operator needs to see) and
    carries a backend-classified ``incident_category``.
    """

    ts_ms: int
    raw_ts: str
    level: IncidentLevel
    logger: str
    message: str
    traceback: str | None = None
    incident_category: IncidentCategory


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


# Order matters: the first matching rule wins. Each rule is a (predicate,
# category) pair where ``predicate(logger, message)`` returns True iff the
# pair matches. The classifier consults the traceback as a fallback when
# the (logger, message) headers don't match any rule.
_IBKR_DISCONNECT_CODE_RE = re.compile(r"\bError\s+(1100|1101|1102|2110)\b")
_PROBE_FAILED_RE = re.compile(r"probe\s+failed", re.IGNORECASE)
_ENGINE_FATAL_RE = re.compile(r"Unhandled\s+exception\s+in\s+engine\.run", re.IGNORECASE)
_PORTFOLIO_INIT_RE = re.compile(r"LivePortfolio.*cannot\s+be", re.IGNORECASE)
_RECONCILE_MISSING_RE = re.compile(r"no\s+reconcile\s+receipt", re.IGNORECASE)
_LOST_FILL_RE = re.compile(r"(?:poison_sentinel\.)?lost_fill", re.IGNORECASE)
_OUTSIDE_MUTATION_RE = re.compile(r"(?:poison_sentinel\.)?outside_mutation", re.IGNORECASE)
_COLD_START_RE = re.compile(r"(?:poison_sentinel\.)?cold_start_divergence", re.IGNORECASE)
_OPERATOR_HALT_RE = re.compile(r"(?:poison_sentinel\.)?operator_declared", re.IGNORECASE)
_SUBSCRIPTION_STALE_RE = re.compile(
    r"(?:absorb_count.*threshold|live_idempotent.*absorb)",
    re.IGNORECASE,
)


def _classify_one(logger: str, message: str) -> IncidentCategory | None:
    """Try to classify a single (logger, message) pair.

    Returns the category on a match or ``None`` when no rule matches —
    the caller decides whether to consult the traceback as a fallback.
    """
    # BROKER_DISCONNECT must be anchored on the ib_async source so an
    # unrelated module that mentions "Error 1100" in prose isn't
    # mis-classified. The anchor can come from the header's logger field
    # *or* from the message body itself — the latter case is what the
    # traceback fallback exercises (where the original header logger
    # has been replaced with "" but the traceback text still names
    # ib_async.wrapper).
    if _IBKR_DISCONNECT_CODE_RE.search(message) and (
        logger == "ib_async.wrapper" or "ib_async.wrapper" in message
    ):
        return IncidentCategory.BROKER_DISCONNECT
    if _PROBE_FAILED_RE.search(message):
        return IncidentCategory.BROKER_RECONNECT_FAILED
    if _ENGINE_FATAL_RE.search(message):
        return IncidentCategory.ENGINE_FATAL
    if _PORTFOLIO_INIT_RE.search(message):
        return IncidentCategory.PORTFOLIO_INIT_FAIL
    if _RECONCILE_MISSING_RE.search(message):
        return IncidentCategory.RECONCILE_MISSING
    # Poison-sentinel triggers — keep the order LOST_FILL → OUTSIDE_MUTATION
    # → COLD_START_DIVERGENCE → OPERATOR_HALT. Each pattern is anchored on
    # its own token so they don't shadow each other.
    if _LOST_FILL_RE.search(message):
        return IncidentCategory.LOST_FILL
    if _OUTSIDE_MUTATION_RE.search(message):
        return IncidentCategory.OUTSIDE_MUTATION
    if _COLD_START_RE.search(message):
        return IncidentCategory.COLD_START_DIVERGENCE
    if _OPERATOR_HALT_RE.search(message):
        return IncidentCategory.OPERATOR_HALT
    if _SUBSCRIPTION_STALE_RE.search(message):
        return IncidentCategory.SUBSCRIPTION_STALE
    return None


def classify(
    logger: str,
    message: str,
    traceback: str | None = None,
) -> IncidentCategory:
    """Classify a log incident into an ``IncidentCategory``.

    The classifier matches the ``logger`` / ``message`` pair against a
    backend-owned regex catalog (the single source of truth for incident
    categorisation). When the header pair is ambiguous and a traceback is
    available, the same catalog is re-applied to the traceback body so a
    generic header (e.g., ``Unhandled exception``) can still resolve to a
    specific category when its underlying exception is recognisable
    (e.g., ``IBKRBarStreamError`` rooted in an Error 1100). Returns
    ``UNKNOWN`` when no rule matches in either pass — the frontend renders
    UNKNOWN with the raw-log drawer immediately available so the page
    degrades gracefully when the catalog lags reality.
    """
    from_header = _classify_one(logger, message)
    if from_header is not None:
        return from_header
    if traceback is not None:
        # The fallback scan re-runs the same catalog against the
        # traceback body. We pass an empty logger because the logger
        # field doesn't apply at the traceback level.
        from_traceback = _classify_one("", traceback)
        if from_traceback is not None:
            return from_traceback
    return IncidentCategory.UNKNOWN


def parse_incidents(text: str) -> list[IncidentRow]:
    """Parse a live.log text body into IncidentRow records.

    Captures WARNING, ERROR, and CRITICAL header lines (DEBUG / INFO are
    ignored). Continuation blocks are preserved on the most recent row so
    the operator's raw-log drawer always has the original traceback. Each
    row is tagged with an ``incident_category`` via ``classify()``.

    Args:
        text: Full file content (or any subrange that starts on a header line).

    Returns:
        Incidents in source order.
    """
    incidents: list[_PendingIncident] = []
    current: _PendingIncident | None = None
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
        incidents.append(current)
        current = None
        cont = []

    for raw in text.splitlines():
        m = _HEADER_RE.match(raw)
        if m is None:
            if current is not None:
                cont.append(raw.rstrip())
            continue

        _flush()
        if m.group("level") not in _INCIDENT_GATING_LEVELS:
            continue
        date_s, time_s, ms_s = m.group("date"), m.group("time"), m.group("ms")
        current = _PendingIncident(
            ts_ms=_parse_header_ts_ms(date_s, time_s, ms_s),
            raw_ts=f"{date_s} {time_s}.{ms_s}",
            level=m.group("level"),  # type: ignore[arg-type]
            logger=m.group("logger"),
            message=m.group("message").rstrip(),
        )

    _flush()
    return [
        IncidentRow(
            ts_ms=p.ts_ms,
            raw_ts=p.raw_ts,
            level=p.level,
            logger=p.logger,
            message=p.message,
            traceback=p.traceback,
            incident_category=classify(p.logger, p.message, p.traceback),
        )
        for p in incidents
    ]


class _PendingIncident:
    """Mutable scratch record used while we accumulate continuation lines."""

    __slots__ = ("level", "logger", "message", "raw_ts", "traceback", "ts_ms")

    def __init__(
        self,
        *,
        ts_ms: int,
        raw_ts: str,
        level: IncidentLevel,
        logger: str,
        message: str,
    ) -> None:
        self.ts_ms = ts_ms
        self.raw_ts = raw_ts
        self.level = level
        self.logger = logger
        self.message = message
        self.traceback: str | None = None
