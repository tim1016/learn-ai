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

Timestamp policy: the engine logger's ``_StepFormatter`` pins
``converter = time.gmtime`` (``app.engine.live.run_logging``), so live.log
timestamps are wall-clock UTC regardless of the engine host's local TZ.
We surface both:

* ``raw_ts``: the verbatim timestamp string from the log (``YYYY-MM-DD
  HH:MM:SS.mmm`` in UTC). Useful for cross-referencing against the
  on-disk live.log file when the operator opens the raw-log drawer.
* ``ts_ms``: the same instant as canonical ``int64`` ms since Unix epoch
  UTC, parsed via :func:`_parse_header_ts_ms`. Suitable for ordering,
  the ``since_ms`` cursor, and viewer-local rendering (the UI converts
  to the browser's TZ for display).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
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


# Anchors are stable substrings emitted by our own code so they can't
# false-positive on operator prose. Path-extraction patterns use
# ``[^\n]`` (line-local) instead of DOTALL so they cannot reach into a
# traceback frame and grab an unrelated stack-file path when the
# emitting message itself has no path.
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
_SIDECAR_PATH_RE = re.compile(
    r"(?:live-state sidecar write failed|LiveStateSidecarCorruptError)"
    r"[^\n]*?(?P<path>/[\w\./\-]+\.json)\b"
)

# D3 — substrings whose presence in either the message OR the traceback
# of a SHUTDOWN_FLATTEN_FAILED row means the proximate cause is the
# broker socket being dead; the rule's ``refine_source`` then relabels
# the source from APP (default) to BROKER. Anything else stays APP.
_SHUTDOWN_FLATTEN_BROKER_MARKERS: tuple[str, ...] = (
    "NotConnectedError",
    "ConnectionError",
    "Socket disconnect",
    "Peer closed connection",
    "IBKRBarStreamError",
    "IBKR client is not connected",
)


# ---------------------------------------------------------------------------
# Match-predicate combinators, fact extractors, and source refiners.
#
# Combinators (``_search`` / ``_contains`` / ``_contains_any`` /
# ``_logger_eq_and_startswith``) turn the matcher field of ``_RULES``
# into a 1-call declaration of the rule's anchor instead of a per-rule
# named function — 15 trivial matchers collapse to 4 reusable shapes.
# Only ``_matches_ibkr_wrapper_disconnect`` keeps a named function
# because its OR-clause (logger OR message-body name) doesn't fit a
# clean combinator.
# ---------------------------------------------------------------------------


def _haystack(message: str, traceback: str | None) -> str:
    """Combine message + traceback for fact / refinement scans."""
    return message if traceback is None else f"{message}\n{traceback}"


def _search(pattern: re.Pattern[str]) -> Callable[[str, str], bool]:
    """Match when ``pattern`` finds anything in the message body."""

    def predicate(_logger: str, message: str) -> bool:
        return pattern.search(message) is not None

    return predicate


def _contains(token: str) -> Callable[[str, str], bool]:
    """Match when ``token`` is a substring of the message body."""

    def predicate(_logger: str, message: str) -> bool:
        return token in message

    return predicate


def _contains_any(*tokens: str) -> Callable[[str, str], bool]:
    """Match when any of ``tokens`` is a substring of the message body."""

    def predicate(_logger: str, message: str) -> bool:
        return any(token in message for token in tokens)

    return predicate


def _logger_eq_and_startswith(logger_name: str, prefix: str) -> Callable[[str, str], bool]:
    """Match when the header logger is ``logger_name`` AND message starts with ``prefix``."""

    def predicate(logger: str, message: str) -> bool:
        return logger == logger_name and message.startswith(prefix)

    return predicate


def _matches_ibkr_wrapper_disconnect(logger: str, message: str) -> bool:
    """The one matcher that doesn't fit a clean combinator.

    Anchored on ib_async.wrapper either in the header logger field or
    the message body — the latter case is the traceback-fallback pass,
    where the original header logger has been replaced with "" but the
    body text still names the module. The dual anchor prevents
    unrelated modules that quote ``Error 1100`` in prose from being
    mis-classified.
    """
    return _IBKR_DISCONNECT_CODE_RE.search(message) is not None and (
        logger == "ib_async.wrapper" or "ib_async.wrapper" in message
    )


def _extract_tws_disconnect_code(message: str, traceback: str | None) -> dict[str, str | int]:
    m = _IBKR_DISCONNECT_CODE_RE.search(_haystack(message, traceback))
    return {"tws_code": int(m.group(1))} if m is not None else {}


def _extract_tws_data_farm_code(message: str, traceback: str | None) -> dict[str, str | int]:
    # Header-only by design: the emit site in ``app.broker.ibkr.client``
    # always folds the TWS code (2103 / 2105) into the message string,
    # so a traceback fallback would only ever produce a false positive
    # (e.g. an unrelated literal in a stack frame).
    del traceback
    m = _IBKR_DATA_FARM_CODE_RE.search(message)
    return {"tws_code": int(m.group(1))} if m is not None else {}


def _extract_foreign_fill_order_id(message: str, traceback: str | None) -> dict[str, str | int]:
    del traceback
    m = _FOREIGN_FILL_DROPPED_RE.search(message)
    if m is None or m.group("order_id") is None:
        return {}
    return {"order_id": m.group("order_id")}


def _extract_broker_event_log_path(message: str, traceback: str | None) -> dict[str, str | int]:
    # Line-local search: the path is only emitted on the same line as
    # the marker, never in a downstream traceback frame. Anchoring on
    # the marker substring + ``[^\n]`` keeps an unrelated ``.jsonl``
    # path in the stack from posing as the failed-write target.
    m = _BROKER_EVENT_LOG_WRITE_PATH_RE.search(_haystack(message, traceback))
    return {"path": m.group("path")} if m is not None else {}


def _extract_sidecar_path(message: str, traceback: str | None) -> dict[str, str | int]:
    # Same line-local discipline as broker_event_log_write_failed: the
    # path lives on the marker line, not in the traceback.
    m = _SIDECAR_PATH_RE.search(_haystack(message, traceback))
    return {"path": m.group("path")} if m is not None else {}


def _refine_shutdown_flatten_source(
    message: str, traceback: str | None
) -> IncidentSource | None:
    """D3 refinement: APP → BROKER when a broker-side marker is present.

    Returns ``None`` when no marker fires so the caller falls back to
    the rule's default source (APP).
    """
    haystack = _haystack(message, traceback)
    if any(marker in haystack for marker in _SHUTDOWN_FLATTEN_BROKER_MARKERS):
        return IncidentSource.BROKER
    return None


# ---------------------------------------------------------------------------
# Declarative rule table.
#
# Single source of truth for incident classification. Adding a category
# is one new ``IncidentCategory`` value + one row here — the default-source
# map, fact-extractor dispatch, and source refinement are all derived
# from this table at module load. Order matters: the first matching rule
# wins. Multiple rules may share a ``category`` (BROKER_DISCONNECT has
# both an ib_async.wrapper anchor and an ib_async.client anchor), but
# they must share the same ``source`` — enforced by
# ``_build_default_source`` below.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncidentRule:
    """Declarative rule binding a category to its anchors and helpers.

    * ``matches(logger, message)`` returns True iff the (logger, message)
      pair belongs to this category. Predicates may consult the message
      body for an embedded logger token to support the traceback-fallback
      pass in ``classify()``.
    * ``source`` is the operator-facing side that recovers this category
      (D2). Multiple rules sharing a category must agree on source.
    * ``fact_extractor(message, traceback)`` populates ``dynamic_facts``
      for the hybrid-C wire shape (D1). One extractor per category — if
      multiple rules for the same category set it, the first wins.
    * ``refine_source(message, traceback)`` may override ``source`` based
      on message / traceback content (D3 — currently only SHUTDOWN_FLATTEN_FAILED
      uses one). Returning ``None`` keeps the default.
    """

    category: IncidentCategory
    source: IncidentSource
    matches: Callable[[str, str], bool]
    fact_extractor: Callable[[str, str | None], dict[str, str | int]] | None = None
    refine_source: Callable[[str, str | None], IncidentSource | None] | None = None


_RULES: tuple[IncidentRule, ...] = (
    IncidentRule(
        IncidentCategory.BROKER_DISCONNECT, IncidentSource.BROKER,
        _matches_ibkr_wrapper_disconnect, _extract_tws_disconnect_code,
    ),
    IncidentRule(
        IncidentCategory.BROKER_DISCONNECT, IncidentSource.BROKER,
        _logger_eq_and_startswith("ib_async.client", "Peer closed connection"),
    ),
    IncidentRule(
        IncidentCategory.DATA_FARM_DEGRADED, IncidentSource.BROKER,
        _logger_eq_and_startswith("app.broker.ibkr.client", "IBKR data farm degraded"),
        _extract_tws_data_farm_code,
    ),
    IncidentRule(
        IncidentCategory.BROKER_RECONNECT_FAILED, IncidentSource.BROKER,
        _search(_PROBE_FAILED_RE),
    ),
    IncidentRule(
        IncidentCategory.ENGINE_FATAL, IncidentSource.APP,
        _search(_ENGINE_FATAL_RE),
    ),
    IncidentRule(
        IncidentCategory.PORTFOLIO_INIT_FAIL, IncidentSource.APP,
        _search(_PORTFOLIO_INIT_RE),
    ),
    IncidentRule(
        IncidentCategory.RECONCILE_MISSING, IncidentSource.APP,
        _search(_RECONCILE_MISSING_RE),
    ),
    IncidentRule(
        IncidentCategory.BROKER_EVENT_LOG_WRITE_FAILED, IncidentSource.INFRA,
        _contains(_BROKER_EVENT_LOG_WRITE_MARKER), _extract_broker_event_log_path,
    ),
    IncidentRule(
        IncidentCategory.FOREIGN_FILL_DROPPED, IncidentSource.BROKER,
        _search(_FOREIGN_FILL_DROPPED_RE), _extract_foreign_fill_order_id,
    ),
    IncidentRule(
        IncidentCategory.SHUTDOWN_FLATTEN_FAILED, IncidentSource.APP,
        _contains_any(*_SHUTDOWN_FLATTEN_FAILED_MARKERS),
        refine_source=_refine_shutdown_flatten_source,
    ),
    IncidentRule(
        IncidentCategory.CONTROL_PLANE_LEASE_LOST, IncidentSource.INFRA,
        _contains("CONTROL_PLANE_LEASE_LOST"),
    ),
    IncidentRule(
        IncidentCategory.SIDECAR_SCHEMA_DRIFT, IncidentSource.APP,
        _contains_any(*_SIDECAR_SCHEMA_DRIFT_MARKERS), _extract_sidecar_path,
    ),
    IncidentRule(
        IncidentCategory.LOST_FILL, IncidentSource.BROKER, _search(_LOST_FILL_RE),
    ),
    IncidentRule(
        IncidentCategory.OUTSIDE_MUTATION, IncidentSource.BROKER, _search(_OUTSIDE_MUTATION_RE),
    ),
    IncidentRule(
        IncidentCategory.COLD_START_DIVERGENCE, IncidentSource.APP, _search(_COLD_START_RE),
    ),
    IncidentRule(
        IncidentCategory.OPERATOR_HALT, IncidentSource.OPERATOR, _search(_OPERATOR_HALT_RE),
    ),
    IncidentRule(
        IncidentCategory.SUBSCRIPTION_STALE, IncidentSource.BROKER, _search(_SUBSCRIPTION_STALE_RE),
    ),
)


def _build_default_source(
    rules: tuple[IncidentRule, ...],
) -> dict[IncidentCategory, IncidentSource]:
    """Derive the per-category source map from ``rules`` at module load.

    Enforces two invariants:

    * Every non-UNKNOWN ``IncidentCategory`` value appears in at least
      one rule (covered by the round-trip test
      ``test_default_source_map_covers_every_category``).
    * Rules sharing a category must share their ``source`` — otherwise
      ``classify_source(category, …)`` would have no well-defined answer
      for that category. Raises ``RuntimeError`` at module load on conflict.

    UNKNOWN is terminal (no rule matches), so it's added explicitly here.
    """
    by_category: dict[IncidentCategory, IncidentSource] = {}
    for rule in rules:
        existing = by_category.get(rule.category)
        if existing is not None and existing != rule.source:
            raise RuntimeError(
                f"Inconsistent IncidentRule.source for {rule.category}: "
                f"existing {existing}, conflicting rule {rule.source}"
            )
        by_category[rule.category] = rule.source
    by_category[IncidentCategory.UNKNOWN] = IncidentSource.UNKNOWN
    return by_category


def _build_fact_extractors(
    rules: tuple[IncidentRule, ...],
) -> dict[IncidentCategory, Callable[[str, str | None], dict[str, str | int]]]:
    """Per-category fact-extractor map (first rule with one wins).

    Categories without an extractor are simply absent from the map;
    ``extract_facts`` returns an empty dict for them.
    """
    extractors: dict[
        IncidentCategory, Callable[[str, str | None], dict[str, str | int]]
    ] = {}
    for rule in rules:
        if rule.fact_extractor is not None and rule.category not in extractors:
            extractors[rule.category] = rule.fact_extractor
    return extractors


def _build_source_refiners(
    rules: tuple[IncidentRule, ...],
) -> dict[IncidentCategory, Callable[[str, str | None], IncidentSource | None]]:
    """Per-category source refiner map (first rule with one wins)."""
    refiners: dict[
        IncidentCategory, Callable[[str, str | None], IncidentSource | None]
    ] = {}
    for rule in rules:
        if rule.refine_source is not None and rule.category not in refiners:
            refiners[rule.category] = rule.refine_source
    return refiners


_DEFAULT_SOURCE: dict[IncidentCategory, IncidentSource] = _build_default_source(_RULES)
_FACT_EXTRACTORS = _build_fact_extractors(_RULES)
_SOURCE_REFINERS = _build_source_refiners(_RULES)


def _classify_one(logger: str, message: str) -> IncidentCategory | None:
    """Try to classify a single (logger, message) pair against ``_RULES``.

    Returns the first matching rule's category or ``None`` when no rule
    matches — the caller decides whether to consult the traceback as a
    fallback.
    """
    for rule in _RULES:
        if rule.matches(logger, message):
            return rule.category
    return None


def classify(
    logger: str,
    message: str,
    traceback: str | None = None,
) -> IncidentCategory:
    """Classify a log incident into an ``IncidentCategory``.

    The classifier matches the ``logger`` / ``message`` pair against the
    backend-owned ``_RULES`` table (the single source of truth for
    incident categorisation). When the header pair is ambiguous and a
    traceback is available, the same table is re-applied to the traceback
    body so a generic header (e.g., ``Unhandled exception``) can still
    resolve to a specific category when its underlying exception is
    recognisable (e.g., ``IBKRBarStreamError`` rooted in an Error 1100).
    Returns ``UNKNOWN`` when no rule matches in either pass — the frontend
    renders UNKNOWN with the raw-log drawer immediately available so the
    page degrades gracefully when the catalog lags reality.
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

    The default-per-category map (derived from ``_RULES``) gives the
    answer for most rows. Two paths add information:

    * Rules with a ``refine_source`` may override their default based on
      message / traceback content (D3 — currently SHUTDOWN_FLATTEN_FAILED
      flips APP → BROKER when a broker-side marker fires).
    * ``UNKNOWN`` (D6) — derive from the logger namespace.
    """
    if category == IncidentCategory.UNKNOWN:
        return _classify_source_for_unknown(logger)
    refiner = _SOURCE_REFINERS.get(category)
    if refiner is not None:
        refined = refiner(message, traceback)
        if refined is not None:
            return refined
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
    extractor = _FACT_EXTRACTORS.get(category)
    return extractor(message, traceback) if extractor is not None else {}


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
