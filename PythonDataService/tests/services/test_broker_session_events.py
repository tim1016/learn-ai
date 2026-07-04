from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.broker_session_events import (
    BrokerSessionEventService,
    classify_broker_session_event,
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


def _write_events(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
