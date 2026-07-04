from __future__ import annotations

import json
from pathlib import Path

from app.schemas.broker_session import (
    BrokerSessionHistoryPurgeRequest,
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


def test_history_service_returns_recent_absent_rows_as_past_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "_broker" / "session_roster_history.jsonl"
    service = BrokerSessionHistoryService(path=path)
    service.append_snapshot(_snapshot(10, "run-a", client_id=42))
    service.append_snapshot(_snapshot(20, "run-b", client_id=77))
    service.append_snapshot(
        _snapshot(
            30,
            "run-c",
            rows=[
                _row(
                    30,
                    "run-c",
                    client_id=99,
                    recency="past_last_known",
                    socket_present=False,
                )
            ],
        )
    )

    rows = service.past_closed_rows(
        current_rows=[
            _row(40, "run-b", client_id=77),
        ]
    )

    assert [row.run_id for row in rows] == ["run-a"]
    assert rows[0].recency == "past_closed"
    assert rows[0].socket_present is False
    assert rows[0].as_of_ms == 10


def test_history_service_limits_past_closed_rows_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "_broker" / "session_roster_history.jsonl"
    service = BrokerSessionHistoryService(path=path)
    service.append_snapshot(_snapshot(10, "run-a"))
    service.append_snapshot(_snapshot(20, "run-b"))
    service.append_snapshot(_snapshot(30, "run-c"))

    rows = service.past_closed_rows(current_rows=[], limit=2)

    assert [row.run_id for row in rows] == ["run-c", "run-b"]


def test_history_purge_removes_client_rows_without_touching_audit_trail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "_broker" / "session_roster_history.jsonl"
    audit_path = tmp_path / "run-a" / "intent_events.jsonl"
    audit_path.parent.mkdir()
    audit_path.write_text('{"event_type":"PENDING_INTENT"}\n', encoding="utf-8")
    service = BrokerSessionHistoryService(path=path)
    service.append_snapshot(
        _snapshot(
            10,
            "run-a",
            rows=[
                _row(10, "run-a", client_id=42),
                _row(10, "run-b", client_id=77),
            ],
        )
    )
    service.append_snapshot(_snapshot(20, "run-c", client_id=42))

    result = service.purge(
        BrokerSessionHistoryPurgeRequest(
            client_id=42,
            confirm="PURGE_BROKER_SESSION_DIAGNOSTICS",
        )
    )

    assert result.purged_row_count == 2
    assert result.purged_snapshot_count == 0
    assert result.remaining_snapshot_count == 2
    rows_by_snapshot = [snapshot.rows for snapshot in service.history(limit=10).rows]
    assert rows_by_snapshot[0] == []
    assert [row.client_id for row in rows_by_snapshot[1]] == [77]
    assert audit_path.read_text(encoding="utf-8") == '{"event_type":"PENDING_INTENT"}\n'


def test_history_purge_removes_whole_snapshots_by_time_range(tmp_path: Path) -> None:
    path = tmp_path / "_broker" / "session_roster_history.jsonl"
    service = BrokerSessionHistoryService(path=path)
    service.append_snapshot(_snapshot(10, "run-a"))
    service.append_snapshot(_snapshot(20, "run-b"))
    service.append_snapshot(_snapshot(30, "run-c"))

    result = service.purge(
        BrokerSessionHistoryPurgeRequest(
            start_ms=15,
            end_ms=25,
            confirm="PURGE_BROKER_SESSION_DIAGNOSTICS",
        )
    )

    assert result.purged_row_count == 1
    assert result.purged_snapshot_count == 1
    assert result.remaining_snapshot_count == 2
    assert [snapshot.as_of_ms for snapshot in service.history(limit=10).rows] == [30, 10]


def test_history_purge_request_requires_filter_and_confirm() -> None:
    try:
        BrokerSessionHistoryPurgeRequest(
            confirm="PURGE_BROKER_SESSION_DIAGNOSTICS",
        )
    except ValueError as exc:
        assert "at least one purge filter is required" in str(exc)
    else:
        raise AssertionError("expected purge request validation to fail")

    try:
        BrokerSessionHistoryPurgeRequest(
            start_ms=20,
            end_ms=10,
            confirm="PURGE_BROKER_SESSION_DIAGNOSTICS",
        )
    except ValueError as exc:
        assert "start_ms must be <= end_ms" in str(exc)
    else:
        raise AssertionError("expected purge request validation to fail")


def _snapshot(
    as_of_ms: int,
    run_id: str,
    *,
    client_id: int | None = None,
    rows: list[BrokerSessionRosterRow] | None = None,
) -> BrokerSessionMirrorSnapshot:
    return BrokerSessionMirrorSnapshot(
        as_of_ms=as_of_ms,
        gateway_port=4002,
        observer_status="online",
        ghost_detection_status="available",
        rows=rows if rows is not None else [_row(as_of_ms, run_id, client_id=client_id)],
    )


def _row(
    as_of_ms: int,
    run_id: str,
    *,
    client_id: int | None = None,
    recency: str = "current",
    socket_present: bool = True,
) -> BrokerSessionRosterRow:
    return BrokerSessionRosterRow(
        row_id=f"bot:{run_id}",
        identity_type="bot",
        recency=recency,
        socket_present=socket_present,
        run_id=run_id,
        client_id=client_id,
        as_of_ms=as_of_ms,
    )
