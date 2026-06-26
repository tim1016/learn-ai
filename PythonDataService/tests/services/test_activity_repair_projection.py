from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.artifacts import ExecutionRow, ExecutionWriter
from app.engine.live.broker_callbacks import BrokerCallbackWal, broker_callbacks_wal_path
from app.engine.live.intent_events import IntentEventType
from app.engine.live.intent_wal import IntentWal
from app.engine.live.run_ledger import LiveRunLedger, write_ledger
from app.schemas.broker_activity import BrokerActivityRow
from app.services import activity_repair_projection
from app.services.activity_repair_projection import load_activity_repair_projection

SID = "sid-repair-cache"
RUN_ID = "run-repair-cache"
NS = f"learn-ai/{SID}/v1"
INTENT_ID = "intent-repair-cache-1"
ORDER_REF = f"{NS}:{INTENT_ID}"


def _run_dir(artifacts_root: Path) -> Path:
    path = artifacts_root / "live_runs" / RUN_ID
    path.mkdir(parents=True)
    return path


def _write_ledger(run_dir: Path) -> None:
    write_ledger(
        run_dir / "run_ledger.json",
        LiveRunLedger(
            run_id=RUN_ID,
            code_sha="abc123",
            strategy_instance_id=SID,
            strategy_spec_path="spec.json",
            strategy_spec_sha256="spec-sha",
            qc_audit_copy_path="qc.py",
            qc_audit_copy_sha256="qc-sha",
            qc_cloud_backtest_id="qc-1",
            account_id="DU123",
            start_date_ms=1_780_000_000_000,
            live_config={},
        ),
    )


def _write_intent_wal(run_dir: Path) -> None:
    wal = IntentWal(run_dir / "intent_events.jsonl")
    wal.append(
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=INTENT_ID,
        bot_order_namespace=NS,
        order_ref=ORDER_REF,
        order_spec={
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 100,
            "order_type": "MKT",
        },
        ts_ms=1_780_000_000_000,
    )
    wal.append(
        event_type=IntentEventType.SUBMITTED,
        intent_id=INTENT_ID,
        bot_order_namespace=NS,
        order_ref=ORDER_REF,
        order_id=42,
        perm_id=9001,
        ts_ms=1_780_000_000_001,
    )


def _write_execution(run_dir: Path) -> None:
    writer = ExecutionWriter(run_dir / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=1_780_000_000_200,
            exec_id="exec-repair-cache-1",
            perm_id=9001,
            client_order_id="live-42",
            account_id="DU123",
            symbol="SPY",
            fill_quantity=100,
            fill_price=501.25,
            fee=1.0,
            exec_time_ms=1_780_000_000_100,
        )
    )
    writer.close()


def _write_callback_wal(run_dir: Path) -> None:
    BrokerCallbackWal(broker_callbacks_wal_path(run_dir)).append_event(
        IbkrOrderEvent(
            account_id="DU123",
            order_id=42,
            perm_id=9001,
            event_type="fill",
            status="Filled",
            order_ref=ORDER_REF,
            symbol="SPY",
            side="BUY",
            order_type="MKT",
            exec_id="exec-callback-only-1",
            fill_quantity=100,
            avg_fill_price=501.25,
            cumulative_filled=100,
            remaining=0.0,
            last_fill_price=501.25,
            exec_time_ms=1_780_000_000_100,
            fee=1.0,
            ts_ms=1_780_000_000_200,
        )
    )


def _write_live_state(artifacts_root: Path, *, cursor: int) -> None:
    path = artifacts_root / "live_state" / SID / "live_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "bot_order_namespace": NS,
                "cursor": cursor,
                "sizing_resolutions": [],
                "submitted_orders": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _existing_live_row() -> BrokerActivityRow:
    return BrokerActivityRow.model_validate(
        {
            "seq": 7,
            "ts_ms": 1_780_000_000_200,
            "exec_id": "exec-repair-cache-1",
            "perm_id": 9001,
            "order_ref": ORDER_REF,
            "symbol": "SPY",
            "side": "BUY",
            "quantity": 100.0,
            "price": 501.25,
            "commission": 1.0,
            "net_amount": -50_126.0,
            "order_type": "MKT",
            "exec_ts_ms": 1_780_000_000_100,
            "verdict": "expected",
            "template_key": "normal_fill_v1",
            "template_version": 1,
            "headline": "BUY 100 SPY @ $501.25",
            "narrative": "Filled as intended.",
            "reason_codes": ["normal_fill"],
        }
    )


def test_activity_repair_projection_cache_hit_does_not_scan_parquet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir)
    _write_execution(run_dir)
    runs = [{"run_id": RUN_ID, "run_dir": str(run_dir)}]

    first = load_activity_repair_projection(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        runs=runs,
        start_ms=1_779_999_999_000,
        end_ms=1_780_000_001_000,
        existing_rows=[],
    )

    assert [row.exec_id for row in first.broker_rows] == ["exec-repair-cache-1"]

    def fail_read_table(*_args, **_kwargs):
        raise AssertionError("warm repair projection should not read parquet")

    monkeypatch.setattr(activity_repair_projection.pq, "read_table", fail_read_table)

    cached = load_activity_repair_projection(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        runs=runs,
        start_ms=1_779_999_999_000,
        end_ms=1_780_000_001_000,
        existing_rows=[],
    )

    assert [row.exec_id for row in cached.broker_rows] == ["exec-repair-cache-1"]


def test_activity_repair_projection_cache_ignores_live_state_cursor_only_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir)
    _write_execution(run_dir)
    _write_live_state(artifacts_root, cursor=1)
    runs = [{"run_id": RUN_ID, "run_dir": str(run_dir)}]

    first = load_activity_repair_projection(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        runs=runs,
        start_ms=1_779_999_999_000,
        end_ms=1_780_000_001_000,
        existing_rows=[],
    )
    assert [row.exec_id for row in first.broker_rows] == ["exec-repair-cache-1"]

    def fail_read_table(*_args, **_kwargs):
        raise AssertionError("cursor-only live_state updates must not invalidate cache")

    monkeypatch.setattr(activity_repair_projection.pq, "read_table", fail_read_table)
    _write_live_state(artifacts_root, cursor=2)

    cached = load_activity_repair_projection(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        runs=runs,
        start_ms=1_779_999_999_000,
        end_ms=1_780_000_001_000,
        existing_rows=[],
    )

    assert [row.exec_id for row in cached.broker_rows] == ["exec-repair-cache-1"]


def test_activity_repair_projection_cache_is_independent_of_live_wal_rows(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir)
    _write_execution(run_dir)
    runs = [{"run_id": RUN_ID, "run_dir": str(run_dir)}]

    first = load_activity_repair_projection(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        runs=runs,
        start_ms=1_779_999_999_000,
        end_ms=1_780_000_001_000,
        existing_rows=[_existing_live_row()],
    )

    assert first.broker_rows == ()

    cached = load_activity_repair_projection(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        runs=runs,
        start_ms=1_779_999_999_000,
        end_ms=1_780_000_001_000,
        existing_rows=[],
    )

    assert [row.exec_id for row in cached.broker_rows] == ["exec-repair-cache-1"]


def test_activity_repair_projection_includes_callback_wal_only_runs(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir)
    _write_callback_wal(run_dir)
    runs = [{"run_id": RUN_ID, "run_dir": str(run_dir)}]

    result = load_activity_repair_projection(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        runs=runs,
        start_ms=1_779_999_999_000,
        end_ms=1_780_000_001_000,
        existing_rows=[],
    )

    assert [row.exec_id for row in result.broker_rows] == ["exec-callback-only-1"]
    assert result.broker_rows[0].recovery_reason == "raw_callback_wal_reprojection"
