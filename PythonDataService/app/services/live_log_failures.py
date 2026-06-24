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
    DATA_FARM_DEGRADED = "data_farm_degraded"
    BROKER_EVENT_LOG_WRITE_FAILED = "broker_event_log_write_failed"
    FOREIGN_FILL_DROPPED = "foreign_fill_dropped"
    SHUTDOWN_FLATTEN_FAILED = "shutdown_flatten_failed"
    CONTROL_PLANE_LEASE_LOST = "control_plane_lease_lost"
    SIDECAR_SCHEMA_DRIFT = "sidecar_schema_drift"
    UNKNOWN = "unknown"


class IncidentSource(StrEnum):
    """Operator-facing source dimension paired with ``IncidentCategory``.

    Whereas ``IncidentCategory`` answers *what* failed, ``IncidentSource``
    answers *whose action recovers it* — so the cockpit can badge rows,
    filter the table, and tune ``recommendedAction`` copy per side. The
    five values are exhaustive and resolved by codex 2026-06-24 (D2):

    * ``BROKER``   — operator's first move is to check IBKR Gateway / TWS /
      the IBKR account / the broker connection.
    * ``APP``      — operator's first move is to redeploy with a different
      config, inspect our logs, or escalate to engineering.
    * ``INFRA``    — operator's first move is to check the host / container
      mount / filesystem / control-plane transport (independent of broker
      or engine logic).
    * ``OPERATOR`` — operator initiated the state (manual halt).
    * ``UNKNOWN``  — classifier couldn't decide (fallback only). The
      cockpit treats this as a degraded-classification signal.
    """

    BROKER = "broker"
    APP = "app"
    INFRA = "infra"
    OPERATOR = "operator"
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
    carries a backend-classified ``incident_category`` plus an
    ``incident_source`` (per codex D2 / D8: backend emits it on every
    row). ``dynamic_facts`` carries the hybrid-C named values the
    frontend may interpolate into its category template (codex D1).
    """

    ts_ms: int
    raw_ts: str
    level: IncidentLevel
    logger: str
    message: str
    traceback: str | None = None
    incident_category: IncidentCategory
    incident_source: IncidentSource
    dynamic_facts: dict[str, str | int] = {}


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
_IBKR_DATA_FARM_CODE_RE = re.compile(r"\b(2103|2105)\b")
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

# Catalog expansion (codex 2026-06-24 D-decisions). Patterns are anchored
# on stable substrings emitted by our own code so they can't false-positive
# on operator prose. extract_facts() reads named groups from a subset of
# them to populate dynamic_facts (path / order_id), so changes here must
# keep those groups intact. Path-extraction patterns deliberately use
# ``[^\n]`` (line-local) instead of DOTALL so they cannot reach into a
# traceback frame and grab an unrelated stack-file path when the
# emitting message itself has no path.
_FOREIGN_FILL_DROPPED_RE = re.compile(
    r"Dropping IBKR fill for unknown order_id(?:\s*(?:=|:)\s*(?P<order_id>\w+))?"
)
_BROKER_EVENT_LOG_WRITE_MARKER = "Could not append IBKR broker event log"
_BROKER_EVENT_LOG_WRITE_PATH_RE = re.compile(
    r"Could not append IBKR broker event log[^\n]*?(?P<path>/[\w\./\-]+\.jsonl)\b"
)
_SHUTDOWN_FLATTEN_FAILED_MARKERS: tuple[str, ...] = (
    "Recovery flatten itself failed",
    "broker.cancel_open_orders failed during shutdown_flatten",
    "broker.cancel_open_orders failed during fatal halt",
)
_SIDECAR_SCHEMA_DRIFT_MARKERS: tuple[str, ...] = (
    "live-state sidecar write failed",
    "LiveStateSidecarCorruptError",
)
# Anchored on the emitting marker substring (same line, line-local) so a
# random ``*.json`` path elsewhere in the traceback can't be mistaken
# for the sidecar's path.
_SIDECAR_PATH_RE = re.compile(
    r"(?:live-state sidecar write failed|LiveStateSidecarCorruptError)"
    r"[^\n]*?(?P<path>/[\w\./\-]+\.json)\b"
)

# D3 — substrings whose presence in EITHER the message OR the traceback
# of a SHUTDOWN_FLATTEN_FAILED row means the proximate cause is the
# broker socket being dead; classify_source() then relabels the source
# from APP (default) to BROKER. Anything else stays APP.
_SHUTDOWN_FLATTEN_BROKER_MARKERS: tuple[str, ...] = (
    "NotConnectedError",
    "ConnectionError",
    "Socket disconnect",
    "Peer closed connection",
    "IBKRBarStreamError",
    "IBKR client is not connected",
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
    # Hard TCP-level disconnect from ib_async.client itself.
    if logger == "ib_async.client" and message.startswith("Peer closed connection"):
        return IncidentCategory.BROKER_DISCONNECT
    # DATA_FARM_DEGRADED — IBKR codes 2103 / 2105 surfaced by our client
    # wrapper. Distinct from connectivity loss: order path may still be
    # alive, only market-data farm is degraded.
    if logger == "app.broker.ibkr.client" and message.startswith("IBKR data farm degraded"):
        return IncidentCategory.DATA_FARM_DEGRADED
    if _PROBE_FAILED_RE.search(message):
        return IncidentCategory.BROKER_RECONNECT_FAILED
    if _ENGINE_FATAL_RE.search(message):
        return IncidentCategory.ENGINE_FATAL
    if _PORTFOLIO_INIT_RE.search(message):
        return IncidentCategory.PORTFOLIO_INIT_FAIL
    if _RECONCILE_MISSING_RE.search(message):
        return IncidentCategory.RECONCILE_MISSING
    # Filesystem write failure for the broker forensic JSONL log. This is
    # an INFRA failure (read-only mount / disk full), not a trading
    # failure — the source map routes it to INFRA. Emit-site rate-limit
    # lives in a separate cleanup PR per the plan's §6 phasing.
    if _BROKER_EVENT_LOG_WRITE_MARKER in message:
        return IncidentCategory.BROKER_EVENT_LOG_WRITE_FAILED
    # Fill arrived with an order_id our intent ledger has never seen
    # (cross-restart resolver missed). Surfaces as a BROKER-side
    # incident; downstream may auto-flatten depending on policy.
    if _FOREIGN_FILL_DROPPED_RE.search(message):
        return IncidentCategory.FOREIGN_FILL_DROPPED
    # Shutdown / recovery flatten cascade. Default source is APP; the
    # six broker-side markers in D3 relabel it to BROKER via
    # classify_source().
    for marker in _SHUTDOWN_FLATTEN_FAILED_MARKERS:
        if marker in message:
            return IncidentCategory.SHUTDOWN_FLATTEN_FAILED
    # Child-watchdog control-plane lease loss — INFRA, not engine logic.
    if "CONTROL_PLANE_LEASE_LOST" in message:
        return IncidentCategory.CONTROL_PLANE_LEASE_LOST
    # Live-state sidecar schema drift / corruption. Anchored on either
    # the write-failure log line or the structured exception name.
    for marker in _SIDECAR_SCHEMA_DRIFT_MARKERS:
        if marker in message:
            return IncidentCategory.SIDECAR_SCHEMA_DRIFT
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


# Default source per category (codex D2 / D6 / D5). SHUTDOWN_FLATTEN_FAILED
# starts as APP and is refined to BROKER by classify_source() when one of
# the broker-side markers (D3) appears in the message or traceback.
# UNKNOWN's default is the literal IncidentSource.UNKNOWN — classify_source()
# overrides it with a logger-derived guess per D6.
_DEFAULT_SOURCE: dict[IncidentCategory, IncidentSource] = {
    IncidentCategory.BROKER_DISCONNECT: IncidentSource.BROKER,
    IncidentCategory.BROKER_RECONNECT_FAILED: IncidentSource.BROKER,
    IncidentCategory.LOST_FILL: IncidentSource.BROKER,
    IncidentCategory.OUTSIDE_MUTATION: IncidentSource.BROKER,
    IncidentCategory.SUBSCRIPTION_STALE: IncidentSource.BROKER,
    IncidentCategory.DATA_FARM_DEGRADED: IncidentSource.BROKER,
    IncidentCategory.FOREIGN_FILL_DROPPED: IncidentSource.BROKER,
    IncidentCategory.ENGINE_FATAL: IncidentSource.APP,
    IncidentCategory.PORTFOLIO_INIT_FAIL: IncidentSource.APP,
    IncidentCategory.RECONCILE_MISSING: IncidentSource.APP,
    IncidentCategory.COLD_START_DIVERGENCE: IncidentSource.APP,
    IncidentCategory.SIDECAR_SCHEMA_DRIFT: IncidentSource.APP,
    IncidentCategory.SHUTDOWN_FLATTEN_FAILED: IncidentSource.APP,
    IncidentCategory.BROKER_EVENT_LOG_WRITE_FAILED: IncidentSource.INFRA,
    IncidentCategory.CONTROL_PLANE_LEASE_LOST: IncidentSource.INFRA,
    IncidentCategory.OPERATOR_HALT: IncidentSource.OPERATOR,
    IncidentCategory.UNKNOWN: IncidentSource.UNKNOWN,
}


def _classify_source_for_unknown(logger: str) -> IncidentSource:
    """Logger-based source heuristic for ``UNKNOWN``-category rows (D6).

    The category fell through the classifier; we still want the cockpit
    to badge the row with the side most likely to recover it. The
    heuristic walks the logger namespace from most-specific (the
    INFRA-pinned child watchdog) to least-specific (the ``__main__``
    runner). Anything outside the known namespaces stays UNKNOWN.
    """
    if logger.startswith("ib_async"):
        return IncidentSource.BROKER
    if logger == "app.engine.live.child_watchdog":
        return IncidentSource.INFRA
    if logger.startswith("app.broker."):
        return IncidentSource.BROKER
    if logger.startswith("app.engine."):
        return IncidentSource.APP
    if logger == "__main__":
        return IncidentSource.APP
    return IncidentSource.UNKNOWN


def classify_source(
    category: IncidentCategory,
    logger: str,
    message: str,
    traceback: str | None = None,
) -> IncidentSource:
    """Resolve the source dimension paired with ``category``.

    The default-per-category map (D2) gives the answer for most rows.
    Two categories need extra information:

    * ``SHUTDOWN_FLATTEN_FAILED`` (D3) — refine APP → BROKER when the
      message or traceback contains any of the six broker-side markers.
    * ``UNKNOWN`` (D6) — derive from the logger namespace.
    """
    if category == IncidentCategory.SHUTDOWN_FLATTEN_FAILED:
        haystack = message if traceback is None else f"{message}\n{traceback}"
        for marker in _SHUTDOWN_FLATTEN_BROKER_MARKERS:
            if marker in haystack:
                return IncidentSource.BROKER
        return IncidentSource.APP
    if category == IncidentCategory.UNKNOWN:
        return _classify_source_for_unknown(logger)
    return _DEFAULT_SOURCE[category]


def extract_facts(
    category: IncidentCategory,
    message: str,
    traceback: str | None = None,
) -> dict[str, str | int]:
    """Extract category-specific named facts for the hybrid-C wire shape (D1).

    The frontend owns the category template; this function ships the
    typed facts that fill its placeholders. Categories without an
    extractor return an empty dict (the frontend renders the template
    verbatim). Empty dict on a category that *has* an extractor means
    the runtime emitted the line without enough context to populate the
    facts — the frontend still renders the template literally.
    """
    facts: dict[str, str | int] = {}
    if category == IncidentCategory.BROKER_DISCONNECT:
        haystack = message if traceback is None else f"{message}\n{traceback}"
        m = _IBKR_DISCONNECT_CODE_RE.search(haystack)
        if m is not None:
            facts["tws_code"] = int(m.group(1))
    elif category == IncidentCategory.DATA_FARM_DEGRADED:
        # Header-only by design: the emit site in ``app.broker.ibkr.client``
        # always folds the TWS code (2103 / 2105) into the message string,
        # so a traceback fallback would only ever produce a false positive
        # (e.g. an unrelated literal in a stack frame).
        m = _IBKR_DATA_FARM_CODE_RE.search(message)
        if m is not None:
            facts["tws_code"] = int(m.group(1))
    elif category == IncidentCategory.FOREIGN_FILL_DROPPED:
        m = _FOREIGN_FILL_DROPPED_RE.search(message)
        if m is not None and m.group("order_id") is not None:
            facts["order_id"] = m.group("order_id")
    elif category == IncidentCategory.BROKER_EVENT_LOG_WRITE_FAILED:
        # Line-local search: the path is only emitted on the same line as
        # the marker, never in a downstream traceback frame. Searching the
        # combined haystack would otherwise let an unrelated ``.jsonl`` path
        # in the stack pose as the failed-write target.
        haystack = message if traceback is None else f"{message}\n{traceback}"
        m = _BROKER_EVENT_LOG_WRITE_PATH_RE.search(haystack)
        if m is not None:
            facts["path"] = m.group("path")
    elif category == IncidentCategory.SIDECAR_SCHEMA_DRIFT:
        # Same line-local discipline as broker_event_log_write_failed: the
        # path lives on the marker line, not in the traceback.
        haystack = message if traceback is None else f"{message}\n{traceback}"
        m = _SIDECAR_PATH_RE.search(haystack)
        if m is not None:
            facts["path"] = m.group("path")
    return facts


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
    rows: list[IncidentRow] = []
    for p in incidents:
        category = classify(p.logger, p.message, p.traceback)
        rows.append(
            IncidentRow(
                ts_ms=p.ts_ms,
                raw_ts=p.raw_ts,
                level=p.level,
                logger=p.logger,
                message=p.message,
                traceback=p.traceback,
                incident_category=category,
                incident_source=classify_source(category, p.logger, p.message, p.traceback),
                dynamic_facts=extract_facts(category, p.message, p.traceback),
            )
        )
    return rows


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
