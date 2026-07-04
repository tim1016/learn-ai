from __future__ import annotations

import json
from pathlib import Path

from app.schemas.broker_session import (
    BrokerSessionMirrorSnapshot,
    BrokerSessionRosterRow,
)
from app.services.broker_session_history import BrokerSessionHistoryService


def test_history_service_retains_bounded_snapshots_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "_broker" / "session_roster_history.jsonl"
    service = BrokerSessionHistoryService(path=path, max_snapshots=2)

    service.append_snapshot(_snapshot(10, "run-a"))
    service.append_snapshot(_snapshot(20, "run-b"))
    service.append_snapshot(_snapshot(30, "run-c"))

    page = service.history(limit=10)

    assert page.retained_count == 2
    assert [row.as_of_ms for row in page.rows] == [30, 20]
    assert page.rows[0].rows[0].run_id == "run-c"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_history_service_skips_malformed_diagnostic_rows(tmp_path: Path) -> None:
    path = tmp_path / "_broker" / "session_roster_history.jsonl"
    path.parent.mkdir()
    path.write_text(
        "\n".join(
            [
                "{not-json",
                json.dumps(_snapshot(10, "run-a").model_dump(mode="json")),
                json.dumps({"as_of_ms": 20, "rows": "wrong-shape"}),
            ]
        ),
        encoding="utf-8",
    )
    service = BrokerSessionHistoryService(path=path)

    page = service.history(limit=10)

    assert page.retained_count == 1
    assert page.rows[0].as_of_ms == 10


def _snapshot(as_of_ms: int, run_id: str) -> BrokerSessionMirrorSnapshot:
    return BrokerSessionMirrorSnapshot(
        as_of_ms=as_of_ms,
        gateway_port=4002,
        observer_status="online",
        ghost_detection_status="available",
        rows=[
            BrokerSessionRosterRow(
                row_id=f"bot:{run_id}",
                identity_type="bot",
                recency="current",
                socket_present=True,
                run_id=run_id,
                as_of_ms=as_of_ms,
            )
        ],
    )
