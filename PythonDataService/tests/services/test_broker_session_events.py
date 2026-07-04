from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.schemas.broker_session import BrokerSessionEventPurgeRequest
from app.services.broker_session_events import (
    BrokerSessionEventService,
    classify_broker_session_event,
    is_ibkr_north_america_reset_window,
)


def test_classifier_maps_ibkr_connectivity_code_from_shared_table() -> None:
    event = classify_broker_session_event(
        seq=1,
        payload={
            "event_type": "IBKR_CODE",
            "ts_ms_utc": 1_783_120_000_000,
            "client_id": 42,
            "ibkr_code": 1100,
            "message": "Connectivity between IB and TWS has been lost",
        },
    )

    assert event.category == "link_connectivity"
    assert event.severity == "warning"
    assert event.label == "IBKR link interrupted"
    assert event.ibkr_code == 1100


def test_classifier_demotes_reset_window_connectivity_code_to_info() -> None:
    event = classify_broker_session_event(
        seq=1,
        payload={
            "event_type": "IBKR_CODE",
            "ts_ms_utc": _ms_et(2026, 7, 3, 0, 30),
            "client_id": 42,
            "ibkr_code": 1100,
            "message": "Connectivity between IB and TWS has been lost",
        },
    )

    assert event.category == "link_connectivity"
    assert event.severity == "info"
    assert event.label == "IBKR link interrupted during scheduled reset"


def test_classifier_keeps_same_code_warning_outside_reset_window() -> None:
    event = classify_broker_session_event(
        seq=1,
        payload={
            "event_type": "IBKR_CODE",
            "ts_ms_utc": _ms_et(2026, 7, 3, 10, 0),
            "client_id": 42,
            "ibkr_code": 1100,
            "message": "Connectivity between IB and TWS has been lost",
        },
    )

    assert event.severity == "warning"
    assert event.label == "IBKR link interrupted"


def test_reset_window_helper_uses_eastern_weekday_schedule() -> None:
    assert is_ibkr_north_america_reset_window(_ms_et(2026, 7, 3, 0, 15))
    assert is_ibkr_north_america_reset_window(_ms_et(2026, 7, 3, 1, 45))
    assert not is_ibkr_north_america_reset_window(_ms_et(2026, 7, 3, 1, 46))
    assert is_ibkr_north_america_reset_window(_ms_et(2026, 7, 4, 0, 0))
    assert is_ibkr_north_america_reset_window(_ms_et(2026, 7, 4, 2, 0))
    assert not is_ibkr_north_america_reset_window(_ms_et(2026, 7, 4, 2, 1))


def test_classifier_fails_unknown_code_visible_as_unclassified() -> None:
    event = classify_broker_session_event(
        seq=1,
        payload={
            "event_type": "IBKR_CODE",
            "ts_ms_utc": 1_783_120_000_000,
            "client_id": 42,
            "ibkr_code": 9999,
            "message": "Vendor added a new code.",
        },
    )

    assert event.category == "unclassified"
    assert event.severity == "warning"
    assert event.label == "Unclassified IBKR code"
    assert event.raw["ibkr_code"] == 9999


@pytest.mark.parametrize(
    ("code", "category", "severity", "label"),
    [
        (100, "pacing_throttling", "warning", "IBKR message rate exceeded"),
        (101, "pacing_throttling", "warning", "IBKR market data line cap reached"),
        (201, "order_execution", "critical", "IBKR order rejected"),
        (202, "order_execution", "info", "IBKR order cancelled"),
        (326, "auth_session", "critical", "IBKR client id already in use"),
        (507, "link_connectivity", "warning", "IBKR socket message framing error"),
        (2102, "order_execution", "warning", "IBKR order still being processed"),
    ],
)
def test_classifier_maps_additional_ibkr_operator_codes(
    code: int,
    category: str,
    severity: str,
    label: str,
) -> None:
    event = classify_broker_session_event(
        seq=1,
        payload={
            "event_type": "IBKR_CODE",
            "ts_ms_utc": _ms_et(2026, 7, 3, 10, 0),
            "client_id": 42,
            "ibkr_code": code,
        },
    )

    assert event.category == category
    assert event.severity == severity
    assert event.label == label


def test_classifier_maps_monitor_reconnect_events() -> None:
    event = classify_broker_session_event(
        seq=1,
        payload={
            "event_type": "BROKER_RECONNECT_HARD_DOWN",
            "ts_ms_utc": _ms_et(2026, 7, 3, 10, 0),
            "client_id": 42,
            "recovery_state": "HARD_DOWN",
            "attempts": 3,
        },
    )

    assert event.category == "recovery_reconnect"
    assert event.severity == "critical"
    assert event.label == "Broker reconnect exhausted"


def test_event_service_pages_filters_and_counts_by_client_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "connection_events.jsonl"
    _write_events(
        path,
        [
            {
                "event_type": "IBKR_CODE",
                "ts_ms_utc": 1,
                "client_id": 42,
                "ibkr_code": 1100,
            },
            {
                "event_type": "BROKER_RECOVERY_OK",
                "ts_ms_utc": 2,
                "client_id": 42,
            },
            {
                "event_type": "IBKR_CODE",
                "ts_ms_utc": 3,
                "client_id": 77,
                "ibkr_code": 2103,
            },
        ],
    )
    monkeypatch.setattr(
        BrokerSessionEventService,
        "event_log_path",
        staticmethod(lambda: path),
    )
    service = BrokerSessionEventService()

    page = service.events(client_id=42, after_seq=1, limit=10)

    assert [row.seq for row in page.rows] == [2]
    assert page.rows[0].category == "recovery_reconnect"
    assert service.counts_by_client_id()[42] == {
        "link_connectivity": 1,
        "recovery_reconnect": 1,
    }
    assert service.counts_by_client_id()[77] == {"data_farm": 1}


def test_event_service_appends_with_bounded_retention_and_stable_cursor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "_broker" / "connection_events.jsonl"
    service = BrokerSessionEventService(path=path, max_events=2)

    service.append_event({"event_type": "BROKER_PROBE_OK", "ts_ms_utc": 10})
    service.append_event({"event_type": "BROKER_PROBE_OK", "ts_ms_utc": 20})
    service.append_event({"event_type": "BROKER_PROBE_OK", "ts_ms_utc": 30})

    page = service.events(limit=10)

    assert [row.seq for row in page.rows] == [2, 3]
    assert [row.ts_ms for row in page.rows] == [20, 30]
    assert [row.seq for row in service.events(after_seq=2, limit=10).rows] == [3]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_event_service_retention_preserves_cursor_after_legacy_line_index_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "_broker" / "connection_events.jsonl"
    path.parent.mkdir()
    _write_events(
        path,
        [
            {"event_type": "BROKER_PROBE_OK", "ts_ms_utc": 10},
            {"event_type": "BROKER_PROBE_OK", "ts_ms_utc": 20},
            {"event_type": "BROKER_PROBE_OK", "ts_ms_utc": 30},
        ],
    )
    service = BrokerSessionEventService(path=path, max_events=2)

    service.append_event({"event_type": "BROKER_PROBE_OK", "ts_ms_utc": 40})

    page = service.events(after_seq=3, limit=10)

    assert [row.seq for row in page.rows] == [4]
    assert page.rows[0].raw["broker_session_seq"] == 4
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_event_service_purges_diagnostic_log_without_touching_audit_trail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker_dir = tmp_path / "_broker"
    broker_dir.mkdir()
    path = broker_dir / "connection_events.jsonl"
    audit_path = tmp_path / "run-a" / "intent_events.jsonl"
    audit_path.parent.mkdir()
    audit_path.write_text('{"event_type":"PENDING_INTENT"}\n', encoding="utf-8")
    _write_events(
        path,
        [
            {
                "event_type": "IBKR_CODE",
                "ts_ms_utc": 10,
                "client_id": 42,
                "ibkr_code": 1100,
            },
            {
                "event_type": "IBKR_CODE",
                "ts_ms_utc": 20,
                "client_id": 77,
                "ibkr_code": 2103,
            },
        ],
    )
    monkeypatch.setattr(
        BrokerSessionEventService,
        "event_log_path",
        staticmethod(lambda: path),
    )
    service = BrokerSessionEventService()

    result = service.purge(
        BrokerSessionEventPurgeRequest(
            client_id=42,
            confirm="PURGE_BROKER_SESSION_DIAGNOSTICS",
        )
    )

    assert result.purged_count == 1
    assert result.remaining_count == 1
    payloads = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [payload["client_id"] for payload in payloads] == [77]
    assert payloads[0]["broker_session_seq"] == 2
    assert audit_path.read_text(encoding="utf-8") == '{"event_type":"PENDING_INTENT"}\n'


def test_event_purge_request_requires_filter_and_confirm() -> None:
    with pytest.raises(ValueError):
        BrokerSessionEventPurgeRequest(
            confirm="PURGE_BROKER_SESSION_DIAGNOSTICS",
        )
    with pytest.raises(ValueError):
        BrokerSessionEventPurgeRequest(
            client_id=42,
            start_ms=20,
            end_ms=10,
            confirm="PURGE_BROKER_SESSION_DIAGNOSTICS",
        )


def _write_events(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _ms_et(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(
        datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=ZoneInfo("America/New_York"),
        ).timestamp()
        * 1000
    )
