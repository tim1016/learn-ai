from __future__ import annotations

from app.schemas.live_runs import ActivityBrokerEventRow, ActivityEvidenceRef
from app.services.activity_projection_contract import fold_activity_event_rows


def _row(
    row_id: str,
    *,
    ts_ms: int,
    fold_key: str | None,
    request_call: str,
) -> ActivityBrokerEventRow:
    return ActivityBrokerEventRow(
        id=row_id,
        visible_row_id=row_id,
        ts_ms=ts_ms,
        row_type="broker_evidence",
        display_type="Broker evidence",
        source="ibkr",
        source_label="IBKR API evidence",
        status="Captured",
        summary="Broker evidence captured.",
        verdict="evidence",
        fold_key=fold_key,
        evidence=[
            ActivityEvidenceRef(
                source="ibkr",
                seq=int(row_id.removeprefix("e")),
                ts_ms=ts_ms,
                request_call=request_call,
            )
        ],
    )


def test_fold_activity_event_rows_only_folds_consecutive_backend_groups() -> None:
    rows = [
        _row("e4", ts_ms=400, fold_key="positions", request_call="reqPositionsAsync"),
        _row("e3", ts_ms=300, fold_key="positions", request_call="reqPositionsAsync"),
        _row("e2", ts_ms=200, fold_key="executions", request_call="reqExecutionsAsync"),
        _row("e1", ts_ms=100, fold_key="positions", request_call="reqPositionsAsync"),
    ]

    folded = fold_activity_event_rows(rows)

    assert [row.fold_key for row in folded] == ["positions", "executions", "positions"]
    assert [row.fold_count for row in folded] == [2, 1, 1]
    assert folded[0].visible_row_id == "fold:positions:e3"
    assert folded[0].child_evidence_ids == ["e4", "e3"]
    assert folded[2].visible_row_id == "fold:positions:e1"
