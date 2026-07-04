"""Broker session event classification and history reads."""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback for local tooling.
    fcntl = None

from pydantic import JsonValue

from app.broker.ibkr.config import get_settings
from app.broker.ibkr.event_codes import (
    IBKR_CODE_MEANINGS,
    BrokerSessionEventCategory,
    BrokerSessionEventSeverity,
)
from app.schemas.broker_session import (
    BrokerSessionEvent,
    BrokerSessionEventPage,
    BrokerSessionEventPurgeRequest,
    BrokerSessionEventPurgeResult,
    BrokerSessionRosterRow,
)

_EVENT_LOG_RELATIVE = Path("_broker") / "connection_events.jsonl"
_EVENT_LOG_MAX_EVENTS = 5_000
_ROW_EVENT_DETAIL_LIMIT = 10
_ET = ZoneInfo("America/New_York")
_RESET_WINDOW_WARNING_CODES = frozenset({1100, 2110, 2103, 2105})

logger = logging.getLogger(__name__)
_EVENT_TYPE_MEANINGS: dict[
    str,
    tuple[BrokerSessionEventCategory, BrokerSessionEventSeverity, str],
] = {
    "BROKER_PROBE_OK": ("client_lifecycle", "info", "Broker probe succeeded"),
    "BROKER_PROBE_FAILED": ("fault_client_error", "warning", "Broker probe failed"),
    "BROKER_RECOVERY_OK": (
        "recovery_reconnect",
        "info",
        "Broker recovery completed",
    ),
    "BROKER_RECOVERY_FAILED": (
        "recovery_reconnect",
        "warning",
        "Broker recovery failed",
    ),
    "BROKER_RECONNECT_ATTEMPT": (
        "recovery_reconnect",
        "info",
        "Broker reconnect attempt started",
    ),
    "BROKER_RECONNECT_FAILED": (
        "recovery_reconnect",
        "warning",
        "Broker reconnect attempt failed",
    ),
    "BROKER_RECONNECT_SUCCEEDED": (
        "recovery_reconnect",
        "info",
        "Broker reconnect succeeded",
    ),
    "BROKER_RECONNECT_HARD_DOWN": (
        "recovery_reconnect",
        "critical",
        "Broker reconnect exhausted",
    ),
    "BROKER_RECONNECT_LINK_WAIT_EXPIRED": (
        "recovery_reconnect",
        "warning",
        "Broker link wait expired",
    ),
    "BROKER_RECONNECT_PROBE_FAILED": (
        "recovery_reconnect",
        "warning",
        "Broker probe forced reconnect",
    ),
}


@dataclass(frozen=True)
class BrokerSessionRowEventAttachment:
    """Backend-authored event detail and counts for one roster row."""

    events: list[BrokerSessionEvent]
    event_counts: dict[BrokerSessionEventCategory, int]


class BrokerSessionEventService:
    """Read and classify broker-session diagnostic events."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        max_events: int = _EVENT_LOG_MAX_EVENTS,
    ) -> None:
        self._path = path
        self._max_events = max(1, max_events)

    def append_event(self, payload: dict[str, Any]) -> None:
        """Append one diagnostic event while keeping a bounded rolling window."""

        path = self.event_log_path()
        with locked_jsonl_file(path):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            safe_payload = _json_safe_dict(payload)
            safe_payload["broker_session_seq"] = _next_event_seq(lines)
            lines.append(
                json.dumps(
                    safe_payload,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            write_jsonl_lines_atomically(path, lines[-self._max_events :])

    def events(
        self,
        *,
        client_id: int | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> BrokerSessionEventPage:
        rows = [
            event
            for event in self._read_all_events()
            if event.seq > after_seq and (client_id is None or event.client_id == client_id)
        ]
        page = rows[:limit]
        next_seq = page[-1].seq if len(rows) > len(page) and page else None
        return BrokerSessionEventPage(rows=page, next_seq=next_seq)

    def counts_by_client_id(self) -> dict[int, dict[BrokerSessionEventCategory, int]]:
        out: dict[int, dict[BrokerSessionEventCategory, int]] = {}
        for event in self._read_all_events():
            if event.client_id is None:
                continue
            counts = out.setdefault(event.client_id, {})
            counts[event.category] = counts.get(event.category, 0) + 1
        return out

    def counts_for_rows(
        self,
        rows: list[BrokerSessionRosterRow],
    ) -> dict[str, dict[BrokerSessionEventCategory, int]]:
        return {
            row_id: attachment.event_counts
            for row_id, attachment in self.events_for_rows(rows).items()
        }

    def events_for_rows(
        self,
        rows: list[BrokerSessionRosterRow],
        *,
        limit_per_row: int = _ROW_EVENT_DETAIL_LIMIT,
    ) -> dict[str, BrokerSessionRowEventAttachment]:
        events = self._read_all_events()
        out: dict[str, BrokerSessionRowEventAttachment] = {}
        limit = max(1, limit_per_row)
        for row in rows:
            if row.client_id is None or not _row_can_attach_client_events(row):
                continue
            row_events = [
                event for event in events if _event_matches_row(event, row)
            ]
            if row_events:
                out[row.row_id] = BrokerSessionRowEventAttachment(
                    events=list(reversed(row_events[-limit:])),
                    event_counts=_event_counts(row_events),
                )
        return out

    def purge(
        self,
        request: BrokerSessionEventPurgeRequest,
    ) -> BrokerSessionEventPurgeResult:
        path = self.event_log_path()
        with locked_jsonl_file(path):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return BrokerSessionEventPurgeResult(purged_count=0, remaining_count=0)

            kept: list[str] = []
            purged_count = 0
            for index, line in enumerate(lines, start=1):
                event = _event_from_line(seq=index, line=line)
                if _matches_purge_filter(event, request):
                    purged_count += 1
                else:
                    kept.append(_line_with_stable_seq(line=line, seq=index))

            if purged_count > 0:
                write_jsonl_lines_atomically(path, kept)
        return BrokerSessionEventPurgeResult(
            purged_count=purged_count,
            remaining_count=len(kept),
        )

    def _read_all_events(self) -> list[BrokerSessionEvent]:
        path = self.event_log_path()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("failed to read broker session event log: %s", exc)
            raise
        events: list[BrokerSessionEvent] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            events.append(_event_from_line(seq=index, line=line))
        return events

    def event_log_path(self) -> Path:
        if self._path is not None:
            return self._path
        return Path(get_settings().live_runs_root) / _EVENT_LOG_RELATIVE


def get_broker_session_event_service() -> BrokerSessionEventService:
    return _SERVICE


def classify_broker_session_event(
    *,
    seq: int,
    payload: dict[str, Any],
) -> BrokerSessionEvent:
    event_type = _string_value(payload.get("event_type")) or "UNKNOWN"
    code = _int_value(payload.get("ibkr_code"))
    ts_ms = _int_value(payload.get("ts_ms_utc")) or 0
    category, severity, label = _classify(
        event_type=event_type,
        code=code,
        ts_ms=ts_ms,
    )
    return BrokerSessionEvent(
        seq=seq,
        ts_ms=ts_ms,
        category=category,
        severity=severity,
        label=label,
        message=_string_value(payload.get("message"))
        or _string_value(payload.get("probe_error"))
        or _string_value(payload.get("recovery_error"))
        or _string_value(payload.get("error")),
        raw_event_type=event_type,
        client_id=_int_value(payload.get("client_id")),
        account_id=_string_value(payload.get("connected_account")),
        ibkr_code=code,
        connection_state=_string_value(payload.get("connection_state")),
        raw=_json_safe_dict(payload),
    )


def _classify(
    *,
    event_type: str,
    code: int | None,
    ts_ms: int,
) -> tuple[BrokerSessionEventCategory, BrokerSessionEventSeverity, str]:
    if event_type == "IBKR_CODE" and code is not None:
        meaning = IBKR_CODE_MEANINGS.get(code)
        if meaning is not None:
            if code in _RESET_WINDOW_WARNING_CODES and is_ibkr_north_america_reset_window(ts_ms):
                return meaning.category, "info", f"{meaning.label} during scheduled reset"
            return meaning.category, meaning.severity, meaning.label
        return "unclassified", "warning", "Unclassified IBKR code"
    if (
        event_type == "BROKER_RECONNECT_LINK_WAIT_EXPIRED"
        and is_ibkr_north_america_reset_window(ts_ms)
    ):
        return (
            "recovery_reconnect",
            "info",
            "Broker link wait expired during scheduled reset",
        )
    meaning = _EVENT_TYPE_MEANINGS.get(event_type)
    if meaning is not None:
        return meaning
    return "unclassified", "warning", "Unclassified broker event"


def is_ibkr_north_america_reset_window(ts_ms: int) -> bool:
    """Return True during IBKR's published North America reset windows.

    IBKR publishes daily reset windows in Eastern Time. Keep this helper
    local to event observability: it lowers diagnostic severity for expected
    reset-window events but does not alter reconnect behavior.
    """
    if ts_ms <= 0:
        return False
    observed_et = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).astimezone(_ET)
    minutes = observed_et.hour * 60 + observed_et.minute
    weekday = observed_et.weekday()
    if weekday == 5:  # Saturday maintenance: 00:00-02:00 ET.
        return 0 <= minutes <= 120
    # Sunday-Friday daily reset: 00:15-01:45 ET.
    return weekday in {0, 1, 2, 3, 4, 6} and 15 <= minutes <= 105


def _event_from_line(*, seq: int, line: str) -> BrokerSessionEvent:
    try:
        payload = json.loads(line)
    except ValueError as exc:
        return BrokerSessionEvent(
            seq=seq,
            ts_ms=0,
            category="unclassified",
            severity="warning",
            label="Malformed broker event",
            message=str(exc),
            raw_event_type="MALFORMED_JSON",
            raw={"raw_line": line},
        )
    if not isinstance(payload, dict):
        return BrokerSessionEvent(
            seq=seq,
            ts_ms=0,
            category="unclassified",
            severity="warning",
            label="Malformed broker event",
            message=f"expected JSON object, got {type(payload).__name__}",
            raw_event_type="MALFORMED_JSON",
            raw={},
        )
    stable_seq = _int_value(payload.get("broker_session_seq")) or seq
    return classify_broker_session_event(seq=stable_seq, payload=payload)


def _matches_purge_filter(
    event: BrokerSessionEvent,
    request: BrokerSessionEventPurgeRequest,
) -> bool:
    if request.client_id is not None and event.client_id != request.client_id:
        return False
    if request.start_ms is not None and event.ts_ms < request.start_ms:
        return False
    return not (request.end_ms is not None and event.ts_ms > request.end_ms)


def _row_can_attach_client_events(row: BrokerSessionRosterRow) -> bool:
    if row.identity_type == "system":
        return True
    return row.registry_claim is not None and row.registry_claim.started_at_ms is not None


def _event_counts(
    events: list[BrokerSessionEvent],
) -> dict[BrokerSessionEventCategory, int]:
    counts: dict[BrokerSessionEventCategory, int] = {}
    for event in events:
        counts[event.category] = counts.get(event.category, 0) + 1
    return counts


def _event_matches_row(event: BrokerSessionEvent, row: BrokerSessionRosterRow) -> bool:
    if event.client_id != row.client_id:
        return False
    started_at_ms = row.registry_claim.started_at_ms if row.registry_claim is not None else None
    if started_at_ms is not None and event.ts_ms < started_at_ms:
        return False
    return event.ts_ms <= row.as_of_ms


def write_jsonl_lines_atomically(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = "".join(f"{line}\n" for line in lines)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


@contextmanager
def locked_jsonl_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a", encoding="utf-8") as lock_fh:
        if fcntl is not None:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _next_event_seq(lines: list[str]) -> int:
    max_seq = 0
    for index, line in enumerate(lines, start=1):
        try:
            payload = json.loads(line)
        except ValueError:
            max_seq = max(max_seq, index)
            continue
        if not isinstance(payload, dict):
            max_seq = max(max_seq, index)
            continue
        max_seq = max(max_seq, _int_value(payload.get("broker_session_seq")) or index)
    return max_seq + 1


def _line_with_stable_seq(*, line: str, seq: int) -> str:
    try:
        payload = json.loads(line)
    except ValueError:
        return line
    if not isinstance(payload, dict):
        return line
    if _int_value(payload.get("broker_session_seq")) is not None:
        return line
    payload = _json_safe_dict(payload)
    payload["broker_session_seq"] = seq
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _json_safe_dict(payload: dict[str, Any]) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {}
    for key, value in payload.items():
        if isinstance(value, str | int | float | bool) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[key] = _json_safe_list(value)
        elif isinstance(value, dict):
            out[key] = _json_safe_dict(value)
        else:
            out[key] = str(value)
    return out


def _json_safe_list(values: list[Any]) -> list[JsonValue]:
    out: list[JsonValue] = []
    for value in values:
        if isinstance(value, str | int | float | bool) or value is None:
            out.append(value)
        elif isinstance(value, list):
            out.append(_json_safe_list(value))
        elif isinstance(value, dict):
            out.append(_json_safe_dict(value))
        else:
            out.append(str(value))
    return out


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


_SERVICE = BrokerSessionEventService()
