from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.artifacts import ExecutionRow, ExecutionWriter
from app.engine.live.broker_callbacks import BrokerCallbackWal, broker_callbacks_wal_path
from app.engine.live.intent_events import IntentEventType
from app.engine.live.intent_wal import IntentWal
from app.engine.live.run_ledger import LiveRunLedger, write_ledger
from app.schemas.broker_activity import BrokerActivityRow, ReasonCode, Verdict
from app.services.broker_activity_reconstruction import reconstruct_broker_activity_for_run
from app.services.broker_activity_wal import (
    BrokerActivityWal,
    instance_broker_activity_wal_path,
)

SID = "sid-reconstruct"
NS = f"learn-ai/{SID}/v1"
RUN_ID = "run-reconstruct"
INTENT_ID = "intent-reconstruct-1"
ORDER_REF = f"{NS}:{INTENT_ID}"


def _run_dir(artifacts_root: Path, run_id: str = RUN_ID) -> Path:
    path = artifacts_root / "live_runs" / run_id
    path.mkdir(parents=True)
    return path


def _write_ledger(run_dir: Path, *, run_id: str = RUN_ID) -> None:
    write_ledger(
        run_dir / "run_ledger.json",
        LiveRunLedger(
            run_id=run_id,
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


def _write_intent_wal(
    run_dir: Path,
    *,
    quantity: int = 100,
    order_id: int = 42,
    perm_id: int | None = 9001,
    order_type: str = "MKT",
    limit_price: float | None = None,
) -> None:
    wal = IntentWal(run_dir / "intent_events.jsonl")
    wal.append(
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=INTENT_ID,
        bot_order_namespace=NS,
        order_ref=ORDER_REF,
        order_spec={
            "symbol": "SPY",
            "action": "BUY" if quantity > 0 else "SELL",
            "quantity": abs(quantity),
            "order_type": order_type,
            **({"limit_price": limit_price} if limit_price is not None else {}),
        },
        ts_ms=1_780_000_000_000,
    )
    wal.append(
        event_type=IntentEventType.SUBMITTED,
        intent_id=INTENT_ID,
        bot_order_namespace=NS,
        order_ref=ORDER_REF,
        order_id=order_id,
        perm_id=perm_id,
        ts_ms=1_780_000_000_001,
    )


def _fill_event(
    *,
    exec_id: str = "exec-raw-1",
    order_ref: str | None = ORDER_REF,
    side: str = "BUY",
    quantity: float = 100.0,
    order_id: int = 42,
    perm_id: int = 9001,
) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        account_id="DU123",
        order_id=order_id,
        perm_id=perm_id,
        event_type="fill",
        status="Filled",
        order_ref=order_ref,
        symbol="SPY",
        side=side,  # type: ignore[arg-type]
        order_type="MKT",
        exec_id=exec_id,
        fill_quantity=quantity,
        avg_fill_price=501.25,
        cumulative_filled=quantity,
        remaining=0.0,
        last_fill_price=501.25,
        exec_time_ms=1_780_000_000_100,
        fee=1.0,
        ts_ms=1_780_000_000_200,
    )


def test_reconstruct_prefers_raw_callback_wal_over_legacy_executions(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir)
    BrokerCallbackWal(broker_callbacks_wal_path(run_dir)).append_event(_fill_event())
    writer = ExecutionWriter(run_dir / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=1_780_000_000_300,
            exec_id="exec-legacy-ignored",
            perm_id=9001,
            client_order_id="live-42",
            account_id="DU123",
            symbol="SPY",
            fill_quantity=100,
            fill_price=501.25,
            fee=1.0,
            exec_time_ms=1_780_000_000_250,
        )
    )
    writer.close()

    result = reconstruct_broker_activity_for_run(RUN_ID, artifacts_root=artifacts_root)

    rows = BrokerActivityWal(instance_broker_activity_wal_path(artifacts_root, SID)).read_all()
    assert result.source == "raw_callback_wal"
    assert result.rows_written == 1
    assert rows[0].exec_id == "exec-raw-1"
    assert rows[0].source_run_id == RUN_ID
    assert rows[0].source_seq == 1
    assert rows[0].recovery_provenance == "reconstructed"
    assert rows[0].recovery_reason == "raw_callback_wal_reprojection"
    assert rows[0].verdict == Verdict.EXPECTED


def test_reconstruct_legacy_executions_preserves_sell_side_and_order_ref(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir, quantity=-50)
    writer = ExecutionWriter(run_dir / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=1_780_000_000_300,
            exec_id="exec-legacy-sell",
            perm_id=9001,
            client_order_id="live-42",
            account_id="DU123",
            symbol="SPY",
            fill_quantity=-50,
            fill_price=501.25,
            fee=1.0,
            exec_time_ms=1_780_000_000_250,
        )
    )
    writer.close()

    result = reconstruct_broker_activity_for_run(RUN_ID, artifacts_root=artifacts_root)

    rows = BrokerActivityWal(instance_broker_activity_wal_path(artifacts_root, SID)).read_all()
    assert result.source == "legacy_execution_artifacts"
    assert result.rows_written == 1
    assert rows[0].exec_id == "exec-legacy-sell"
    assert rows[0].order_ref == ORDER_REF
    assert rows[0].side == "SELL"
    assert rows[0].quantity == 50.0
    assert rows[0].recovery_provenance == "reconstructed"
    assert rows[0].recovery_reason == "legacy_artifacts_missing_activity_wal"


def test_reconstruct_legacy_executions_prefers_client_order_id_over_perm_id(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir, perm_id=None)
    writer = ExecutionWriter(run_dir / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=1_780_000_000_300,
            exec_id="exec-legacy-owned",
            perm_id=9001,
            client_order_id="live-42",
            account_id="DU123",
            symbol="SPY",
            fill_quantity=100,
            fill_price=501.25,
            fee=1.0,
            exec_time_ms=1_780_000_000_250,
        )
    )
    writer.close()

    reconstruct_broker_activity_for_run(RUN_ID, artifacts_root=artifacts_root)

    rows = BrokerActivityWal(instance_broker_activity_wal_path(artifacts_root, SID)).read_all()
    assert rows[0].order_ref == ORDER_REF
    assert rows[0].verdict == Verdict.EXPECTED


def test_reconstruct_legacy_executions_uses_order_type_from_intent_wal(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir, order_type="LMT", limit_price=501.25)
    writer = ExecutionWriter(run_dir / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=1_780_000_000_300,
            exec_id="exec-legacy-limit",
            perm_id=9001,
            client_order_id="live-42",
            account_id="DU123",
            symbol="SPY",
            fill_quantity=100,
            fill_price=501.25,
            fee=1.0,
            exec_time_ms=1_780_000_000_250,
        )
    )
    writer.close()

    reconstruct_broker_activity_for_run(RUN_ID, artifacts_root=artifacts_root)

    rows = BrokerActivityWal(instance_broker_activity_wal_path(artifacts_root, SID)).read_all()
    assert rows[0].order_type == "LMT"


def test_reconstruct_legacy_partial_fill_is_not_marked_terminal_divergence(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir, quantity=100)
    writer = ExecutionWriter(run_dir / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=1_780_000_000_300,
            exec_id="exec-legacy-partial",
            perm_id=9001,
            client_order_id="live-42",
            account_id="DU123",
            symbol="SPY",
            fill_quantity=50,
            fill_price=501.25,
            fee=1.0,
            exec_time_ms=1_780_000_000_250,
        )
    )
    writer.close()

    reconstruct_broker_activity_for_run(RUN_ID, artifacts_root=artifacts_root)

    rows = BrokerActivityWal(instance_broker_activity_wal_path(artifacts_root, SID)).read_all()
    assert rows[0].verdict == Verdict.EXPECTED_WITH_CAVEAT
    assert rows[0].reason_codes == (ReasonCode.PARTIAL_FILL,)


def test_reconstruct_is_idempotent_against_existing_live_rows(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir)
    BrokerCallbackWal(broker_callbacks_wal_path(run_dir)).append_event(_fill_event())

    first = reconstruct_broker_activity_for_run(RUN_ID, artifacts_root=artifacts_root)
    second = reconstruct_broker_activity_for_run(RUN_ID, artifacts_root=artifacts_root)

    rows = BrokerActivityWal(instance_broker_activity_wal_path(artifacts_root, SID)).read_all()
    assert first.rows_written == 1
    assert second.rows_written == 0
    assert second.rows_skipped_existing == 1
    assert [row.exec_id for row in rows] == ["exec-raw-1"]


def test_reconstruct_skips_existing_lifecycle_row_with_different_timestamp(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    _write_intent_wal(run_dir)
    BrokerCallbackWal(broker_callbacks_wal_path(run_dir)).append_event(
        IbkrOrderEvent(
            account_id="DU123",
            order_id=42,
            perm_id=9001,
            event_type="status",
            status="Cancelled",
            order_ref=ORDER_REF,
            symbol="SPY",
            side="BUY",
            order_type="MKT",
            fill_quantity=0.0,
            cumulative_filled=0.0,
            remaining=100.0,
            ts_ms=1_780_000_000_900,
        )
    )
    BrokerActivityWal(instance_broker_activity_wal_path(artifacts_root, SID)).append_row(
        BrokerActivityRow(
            seq=1,
            ts_ms=1_780_000_000_100,
            exec_id=None,
            perm_id=9001,
            order_ref=ORDER_REF,
            symbol="SPY",
            side="BUY",
            quantity=100.0,
            price=None,
            commission=None,
            net_amount=None,
            order_type="MKT",
            exec_ts_ms=None,
            verdict=Verdict.EXPECTED,
            template_key="cancellation",
            template_version=1,
            headline="Cancelled buy of 100 SPY",
            narrative="existing live row",
            reason_codes=(ReasonCode.CANCELLATION,),
        )
    )

    result = reconstruct_broker_activity_for_run(RUN_ID, artifacts_root=artifacts_root)

    rows = BrokerActivityWal(instance_broker_activity_wal_path(artifacts_root, SID)).read_all()
    assert result.rows_written == 0
    assert result.rows_skipped_existing == 1
    assert len(rows) == 1


def test_reconstruct_propagates_corrupt_intent_wal(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    run_dir = _run_dir(artifacts_root)
    _write_ledger(run_dir)
    (run_dir / "intent_events.jsonl").write_text("not-json\n", encoding="utf-8")
    writer = ExecutionWriter(run_dir / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=1_780_000_000_300,
            exec_id="exec-legacy-corrupt-intent",
            perm_id=9001,
            client_order_id="live-42",
            account_id="DU123",
            symbol="SPY",
            fill_quantity=100,
            fill_price=501.25,
            fee=1.0,
            exec_time_ms=1_780_000_000_250,
        )
    )
    writer.close()

    with pytest.raises(ValueError, match="corrupt intent WAL"):
        reconstruct_broker_activity_for_run(RUN_ID, artifacts_root=artifacts_root)
