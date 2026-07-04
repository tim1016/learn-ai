"""Broker session event classification and history reads."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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
)

_EVENT_LOG_RELATIVE = Path("_broker") / "connection_events.jsonl"
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
}


class BrokerSessionEventService:
    """Read and classify broker-session diagnostic events."""

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
            if event.seq > after_seq
            and (client_id is None or event.client_id == client_id)
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

    def purge(
        self,
        request: BrokerSessionEventPurgeRequest,
    ) -> BrokerSessionEventPurgeResult:
        path = self.event_log_path()
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
                kept.append(line)

        if purged_count > 0:
            _atomic_write_lines(path, kept)
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
        except OSError:
            return []
        events: list[BrokerSessionEvent] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            events.append(_event_from_line(seq=index, line=line))
        return events

    @staticmethod
    def event_log_path() -> Path:
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
    category, severity, label = _classify(event_type=event_type, code=code)
    return BrokerSessionEvent(
        seq=seq,
        ts_ms=_int_value(payload.get("ts_ms_utc")) or 0,
        category=category,
        severity=severity,
        label=label,
        message=_string_value(payload.get("message"))
        or _string_value(payload.get("probe_error"))
        or _string_value(payload.get("recovery_error")),
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
) -> tuple[BrokerSessionEventCategory, BrokerSessionEventSeverity, str]:
    if event_type == "IBKR_CODE" and code is not None:
        meaning = IBKR_CODE_MEANINGS.get(code)
        if meaning is not None:
            return meaning.category, meaning.severity, meaning.label
        return "unclassified", "warning", "Unclassified IBKR code"
    meaning = _EVENT_TYPE_MEANINGS.get(event_type)
    if meaning is not None:
        return meaning
    return "unclassified", "warning", "Unclassified broker event"


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
    return classify_broker_session_event(seq=seq, payload=payload)


def _matches_purge_filter(
    event: BrokerSessionEvent,
    request: BrokerSessionEventPurgeRequest,
) -> bool:
    if request.client_id is not None and event.client_id != request.client_id:
        return False
    if request.start_ms is not None and event.ts_ms < request.start_ms:
        return False
    return not (request.end_ms is not None and event.ts_ms > request.end_ms)


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = "".join(f"{line}\n" for line in lines)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


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
