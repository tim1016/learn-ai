from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.broker_callbacks import (
    BrokerCallbackWal,
    BrokerCallbackWalCorruptError,
    broker_callback_idempotency_key,
    broker_callbacks_wal_path,
)


def _event(*, exec_id: str | None = "exec-1", ts_ms: int = 1_780_000_000_000) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        account_id="DU123",
        order_id=42,
        perm_id=99,
        event_type="fill",
        status="Filled",
        order_ref="learn-ai/S1/v1:intent-1",
        symbol="SPY",
        side="BUY",
        order_type="MKT",
        exec_id=exec_id,
        fill_quantity=1.0,
        avg_fill_price=500.0,
        last_fill_price=500.0,
        exec_time_ms=1_780_000_000_001,
        ts_ms=ts_ms,
    )


def test_canonical_path_is_run_dir_sibling_wal(tmp_path: Path) -> None:
    assert broker_callbacks_wal_path(tmp_path) == tmp_path / "broker_callbacks.jsonl"


def test_append_and_read_round_trip(tmp_path: Path) -> None:
    wal = BrokerCallbackWal(tmp_path / "broker_callbacks.jsonl")

    first = wal.append_event(_event(exec_id="exec-1"))
    second = wal.append_event(_event(exec_id="exec-2", ts_ms=1_780_000_000_010))

    assert [first.seq, second.seq] == [1, 2]
    records = wal.read_all()
    assert [record.seq for record in records] == [1, 2]
    assert records[0].callback_type == "fill"
    assert records[0].event.exec_id == "exec-1"


def test_idempotency_key_includes_non_exec_fields_for_callbacks_without_exec_id() -> None:
    event = _event(exec_id=None)

    key = broker_callback_idempotency_key(event)

    assert key == "fill||99|learn-ai/S1/v1:intent-1|Filled|1780000000001"


def test_idempotency_key_ignores_observation_time_for_redelivery() -> None:
    first = _event(exec_id="exec-1", ts_ms=1_780_000_000_000)
    redelivered = _event(exec_id="exec-1", ts_ms=1_780_000_010_000)

    assert broker_callback_idempotency_key(first) == broker_callback_idempotency_key(
        redelivered
    )


def test_seq_continues_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "broker_callbacks.jsonl"
    first = BrokerCallbackWal(path)
    first.append_event(_event(exec_id="exec-1"))

    second = BrokerCallbackWal(path)
    record = second.append_event(_event(exec_id="exec-2", ts_ms=1_780_000_000_010))

    assert record.seq == 2


def test_read_tolerates_single_trailing_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "broker_callbacks.jsonl"
    wal = BrokerCallbackWal(path)
    wal.append_event(_event())

    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "callback_type": "fill"')

    assert [record.seq for record in wal.read_all()] == [1]


def test_append_after_tolerated_partial_tail_truncates_tail(tmp_path: Path) -> None:
    path = tmp_path / "broker_callbacks.jsonl"
    wal = BrokerCallbackWal(path)
    wal.append_event(_event(exec_id="exec-1"))

    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "callback_type": "fill"')

    record = BrokerCallbackWal(path).append_event(_event(exec_id="exec-2"))

    assert record.seq == 2
    records = BrokerCallbackWal(path).read_all()
    assert [item.seq for item in records] == [1, 2]
    assert records[1].event.exec_id == "exec-2"


def test_malformed_complete_line_poisons(tmp_path: Path) -> None:
    path = tmp_path / "broker_callbacks.jsonl"
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(BrokerCallbackWalCorruptError):
        BrokerCallbackWal(path).read_all()


def test_non_monotonic_seq_poisons(tmp_path: Path) -> None:
    path = tmp_path / "broker_callbacks.jsonl"
    record = BrokerCallbackWal(path).append_event(_event())
    path.write_text(
        record.model_dump_json() + "\n" + record.model_dump_json() + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BrokerCallbackWalCorruptError, match="non-monotonic"):
        BrokerCallbackWal(path).read_all()
