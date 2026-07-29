"""Durability and merge regressions for producer-local operational logs."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.engine.live.account_artifacts import (
    ACCOUNT_EVENTS_FILENAME,
    ACCOUNT_EVENTS_SEQUENCE_FILENAME,
    account_artifacts_root,
)
from app.engine.live.account_clerk_journal import AccountClerkJournal, read_account_clerk_journal
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.producer_operational_log import (
    ProducerOperationalLogError,
    append_producer_operational_event,
    append_producer_operational_event_if_absent,
    merge_operator_history,
    producer_operational_log_path,
    read_producer_operational_events,
)

_ACCOUNT_ID = "DU1234567"


def test_append_producer_operational_event_serializes_one_boot_local_sequence(tmp_path: Path) -> None:
    def append(index: int) -> int:
        return append_producer_operational_event(
            tmp_path,
            account_id=_ACCOUNT_ID,
            producer="daemon",
            producer_boot_id="daemon-boot-a",
            idempotency_key=f"daemon-start:{index}",
            event_type="daemon_child_started",
            payload={"instance_id": f"bot-{index}"},
            recorded_at_ms=1_780_000_000_000 + index,
        ).producer_seq

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(append, range(1, 17)))

    records = read_producer_operational_events(tmp_path, account_id=_ACCOUNT_ID)
    assert sorted(sequences) == list(range(1, 17))
    assert sorted(record.producer_seq for record in records) == list(range(1, 17))


def test_clerk_supervisor_and_daemon_append_concurrently_without_a_legacy_global_writer(
    tmp_path: Path,
) -> None:
    """Independent producers never allocate a shared account-event sequence."""

    def append(index: int) -> str:
        producer = "daemon" if index % 2 else "clerk_supervisor"
        return append_producer_operational_event(
            tmp_path,
            account_id=_ACCOUNT_ID,
            producer=producer,
            producer_boot_id=f"{producer}-boot-a",
            idempotency_key=f"{producer}:lifecycle:{index}",
            event_type=f"{producer}_lifecycle_observed",
            payload={"index": index},
            recorded_at_ms=1_780_000_000_000 + index,
        ).producer

    with ThreadPoolExecutor(max_workers=16) as executor:
        producers = list(executor.map(append, range(1, 33)))

    assert producers.count("daemon") == 16
    assert producers.count("clerk_supervisor") == 16
    assert len(read_producer_operational_events(tmp_path, account_id=_ACCOUNT_ID)) == 32
    account_root = account_artifacts_root(tmp_path, _ACCOUNT_ID)
    assert not (account_root / ACCOUNT_EVENTS_FILENAME).exists()
    assert not (account_root / ACCOUNT_EVENTS_SEQUENCE_FILENAME).exists()


def test_append_producer_operational_event_replays_same_boot_evidence(tmp_path: Path) -> None:
    first = append_producer_operational_event(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="data_plane",
        producer_boot_id="plane-boot-a",
        idempotency_key="reconciliation:abc",
        event_type="reconciliation_refresh_requested",
        payload={"receipt_id": "abc"},
        event_at_ms=100,
        arrived_at_ms=110,
        recorded_at_ms=120,
    )
    replay = append_producer_operational_event(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="data_plane",
        producer_boot_id="plane-boot-a",
        idempotency_key="reconciliation:abc",
        event_type="reconciliation_refresh_requested",
        payload={"receipt_id": "abc"},
        event_at_ms=100,
        arrived_at_ms=110,
        recorded_at_ms=999,
    )

    assert replay == first
    assert len(read_producer_operational_events(tmp_path, account_id=_ACCOUNT_ID)) == 1


def test_producer_display_clock_preserves_a_valid_epoch_zero_event_time(tmp_path: Path) -> None:
    record = append_producer_operational_event(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="data_plane",
        producer_boot_id="plane-boot-zero",
        idempotency_key="epoch-zero",
        event_type="callback_observed",
        payload={},
        event_at_ms=0,
        arrived_at_ms=100,
        recorded_at_ms=200,
    )

    assert record.display_clock_ms == 0


@pytest.mark.parametrize(
    ("field", "forged_value", "error"),
    [
        ("producer", "bot", "producer log path"),
        ("producer_boot_id", "forged-boot", "producer boot log path"),
    ],
)
def test_read_rejects_a_row_whose_embedded_stream_identity_disagrees_with_its_path(
    tmp_path: Path,
    field: str,
    forged_value: str,
    error: str,
) -> None:
    record = append_producer_operational_event(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="daemon",
        producer_boot_id="daemon-boot-a",
        idempotency_key="daemon:one",
        event_type="daemon_child_started",
        payload={},
        recorded_at_ms=100,
    )
    path = producer_operational_log_path(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="daemon",
        producer_boot_id="daemon-boot-a",
    )
    forged = record.model_dump(mode="json")
    forged[field] = forged_value
    path.write_text(json.dumps(forged) + "\n", encoding="utf-8")

    with pytest.raises(ProducerOperationalLogError, match=error):
        read_producer_operational_events(tmp_path, account_id=_ACCOUNT_ID)


def test_receipt_append_is_atomic_across_concurrent_producer_boots(tmp_path: Path) -> None:
    def append(boot_number: int) -> bool:
        return append_producer_operational_event_if_absent(
            tmp_path,
            account_id=_ACCOUNT_ID,
            producer="data_plane",
            producer_boot_id=f"plane-boot-{boot_number}",
            idempotency_key="receipt:one-and-only",
            event_type="recovery_receipt_recorded",
            payload={"receipt_id": "one-and-only"},
            recorded_at_ms=100 + boot_number,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        appended = list(executor.map(append, (1, 2)))

    assert appended.count(True) == 1
    assert appended.count(False) == 1
    records = read_producer_operational_events(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="data_plane",
    )
    assert [record.idempotency_key for record in records] == ["receipt:one-and-only"]


def test_receipt_claim_survives_a_corrupt_historical_producer_boot(tmp_path: Path) -> None:
    historical_path = producer_operational_log_path(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="data_plane",
        producer_boot_id="plane-boot-old",
    )
    append_producer_operational_event(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="data_plane",
        producer_boot_id="plane-boot-old",
        idempotency_key="historical:receipt",
        event_type="reconciliation_refresh_requested",
        payload={"receipt_id": "historical"},
        recorded_at_ms=100,
    )
    with historical_path.open("a", encoding="utf-8") as handle:
        handle.write("{truncated\n")

    first = append_producer_operational_event_if_absent(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="data_plane",
        producer_boot_id="plane-boot-new",
        idempotency_key="fresh:receipt",
        event_type="reconciliation_refresh_requested",
        payload={"receipt_id": "fresh"},
        recorded_at_ms=101,
    )
    replay = append_producer_operational_event_if_absent(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="data_plane",
        producer_boot_id="plane-boot-after-restart",
        idempotency_key="fresh:receipt",
        event_type="reconciliation_refresh_requested",
        payload={"receipt_id": "fresh"},
        recorded_at_ms=102,
    )

    assert first is True
    assert replay is False


def test_merge_operator_history_deduplicates_restart_replay_without_global_sequence(tmp_path: Path) -> None:
    first = append_producer_operational_event(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="daemon",
        producer_boot_id="daemon-boot-a",
        idempotency_key="child-stop:bot-1:run-1",
        event_type="daemon_child_stopped",
        payload={"instance_id": "bot-1"},
        recorded_at_ms=200,
    )
    second = append_producer_operational_event(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="daemon",
        producer_boot_id="daemon-boot-b",
        idempotency_key="child-stop:bot-1:run-1",
        event_type="daemon_child_stopped",
        payload={"instance_id": "bot-1"},
        recorded_at_ms=201,
    )
    merged = merge_operator_history(
        legacy_events=[
            {
                "account_id": _ACCOUNT_ID,
                "event_type": "legacy_freeze",
                "seq": 4,
                "ts_ms": 200,
            }
        ],
        producer_records=(second, first),
    )

    assert merged[0].event_id.startswith(f"producer:{_ACCOUNT_ID}:daemon:")
    assert merged[1].event_id == f"{_ACCOUNT_ID}:4"
    assert merged[0].producer_seq == 1
    assert merged[0].display_clock_ms == 200


def test_corrupt_one_producer_log_fails_at_its_read_scope_without_touching_other_logs(tmp_path: Path) -> None:
    journal = AccountClerkJournal(artifacts_root=tmp_path, account_id=_ACCOUNT_ID, now_ms=lambda: 99)
    journal.record_intent(
        AccountOwnerSubmitIntent(
            trace_id="trace-journal-isolated",
            account_id=_ACCOUNT_ID,
            strategy_instance_id="bot-a",
            run_id="run-a",
            bot_order_namespace="learn-ai/bot-a/v1",
            intent_id="journal-isolated",
            order_ref="learn-ai/bot-a/v1:journal-isolated",
            intent_kind="ORDER",
            order_spec={},
            owner_generation=1,
            created_at_ms=98,
        ),
        validate_intent=lambda _: None,
    )
    append_producer_operational_event(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="daemon",
        producer_boot_id="daemon-boot-a",
        idempotency_key="daemon:one",
        event_type="daemon_child_started",
        payload={},
        recorded_at_ms=100,
    )
    append_producer_operational_event(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="data_plane",
        producer_boot_id="plane-boot-a",
        idempotency_key="plane:one",
        event_type="data_plane_refresh",
        payload={},
        recorded_at_ms=101,
    )
    daemon_path = producer_operational_log_path(
        tmp_path,
        account_id=_ACCOUNT_ID,
        producer="daemon",
        producer_boot_id="daemon-boot-a",
    )
    with daemon_path.open("a", encoding="utf-8") as handle:
        handle.write('{"truncated"\n')

    assert len(
        read_producer_operational_events(tmp_path, account_id=_ACCOUNT_ID, producer="data_plane")
    ) == 1
    with pytest.raises(ProducerOperationalLogError, match="invalid producer operational row"):
        read_producer_operational_events(tmp_path, account_id=_ACCOUNT_ID)
    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, _ACCOUNT_ID)] == ["recorded"]
