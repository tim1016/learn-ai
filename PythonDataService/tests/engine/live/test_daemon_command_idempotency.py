"""Clerk S3 / #1156 — durable host-daemon command idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live import durable_append_log
from app.engine.live.daemon_command_idempotency import (
    DaemonCommandIdempotencyRepo,
    DaemonCommandIdempotencyService,
    DaemonCommandOutcome,
    canonical_request_sha256,
)


def _success(sequence: int) -> DaemonCommandOutcome:
    return DaemonCommandOutcome(
        status_code=200,
        body={"accepted": True, "sequence": sequence},
        replayed=False,
    )


def test_execute_enabled_account_replays_one_durable_outcome_without_reinvoking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LIVE_RUNNER_DAEMON_COMMAND_IDEMPOTENCY_ENFORCED_ACCOUNTS", "DU123")
    service = DaemonCommandIdempotencyService(tmp_path, now_ms=lambda: 1_700_000_000_000)
    calls = 0

    def invoke() -> DaemonCommandOutcome:
        nonlocal calls
        calls += 1
        return _success(calls)

    first = service.execute(
        idempotency_key="operator-opaque-key",
        command="start",
        account_id="DU123",
        semantic_payload={"run_id": "run-1", "readonly": True},
        invoke=invoke,
    )
    duplicate = service.execute(
        idempotency_key="operator-opaque-key",
        command="start",
        account_id="DU123",
        semantic_payload={"readonly": True, "run_id": "run-1"},
        invoke=invoke,
    )

    assert first.replayed is False
    assert duplicate.replayed is True
    assert duplicate.body == {"accepted": True, "sequence": 1}
    assert calls == 1


def test_execute_key_reused_for_different_command_returns_durable_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LIVE_RUNNER_DAEMON_COMMAND_IDEMPOTENCY_ENFORCED_ACCOUNTS", "DU123")
    service = DaemonCommandIdempotencyService(tmp_path)

    first = service.execute(
        idempotency_key="same-key",
        command="stop",
        account_id="DU123",
        semantic_payload={"run_id": "run-1", "force": False},
        invoke=lambda: _success(1),
    )
    conflict = service.execute(
        idempotency_key="same-key",
        command="start",
        account_id="DU123",
        semantic_payload={"run_id": "run-1", "readonly": True},
        invoke=lambda: _success(2),
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.body["reason_code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_COMMAND"
    assert conflict.body["idempotency_key"] == "same-key"


def test_execute_shadow_account_logs_but_does_not_suppress_duplicate(tmp_path: Path) -> None:
    service = DaemonCommandIdempotencyService(tmp_path)
    calls = 0

    def invoke() -> DaemonCommandOutcome:
        nonlocal calls
        calls += 1
        return _success(calls)

    first = service.execute(
        idempotency_key="shadow-key",
        command="stop",
        account_id="DU999",
        semantic_payload={"run_id": "run-1", "account": "DU999"},
        invoke=invoke,
    )
    duplicate = service.execute(
        idempotency_key="shadow-key",
        command="stop",
        account_id="DU999",
        semantic_payload={"run_id": "run-1", "account": "DU999"},
        invoke=invoke,
    )

    assert first.replayed is False
    assert duplicate.replayed is False
    assert duplicate.body["sequence"] == 2
    assert calls == 2


def test_execute_enabled_account_never_replays_pending_command_after_daemon_interruption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LIVE_RUNNER_DAEMON_COMMAND_IDEMPOTENCY_ENFORCED_ACCOUNTS", "DU123")
    key = "interrupted-key"
    semantic_payload = {"run_id": "run-1", "force": False}
    DaemonCommandIdempotencyRepo(tmp_path).prepare(
        idempotency_key=key,
        command="stop",
        request_sha256=canonical_request_sha256("stop", "DU123", semantic_payload),
        account_id="DU123",
        enforcement_enabled=True,
        now_ms=1_700_000_000_000,
    )
    service = DaemonCommandIdempotencyService(tmp_path)
    calls = 0

    def invoke() -> DaemonCommandOutcome:
        nonlocal calls
        calls += 1
        return _success(calls)

    outcome = service.execute(
        idempotency_key=key,
        command="stop",
        account_id="DU123",
        semantic_payload=semantic_payload,
        invoke=invoke,
    )

    assert outcome.status_code == 409
    assert outcome.body["reason_code"] == "IDEMPOTENCY_OUTCOME_UNKNOWN"
    assert calls == 0


def test_prepare_directory_sync_failure_leaves_a_claim_that_blocks_restart_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the PENDING file fsync still leaves retry fail-closed."""

    monkeypatch.setenv("LIVE_RUNNER_DAEMON_COMMAND_IDEMPOTENCY_ENFORCED_ACCOUNTS", "DU123")
    repo = DaemonCommandIdempotencyRepo(tmp_path)
    key = "directory-sync-failure"
    payload = {"run_id": "run-1"}
    record_path = repo._path_for_key(key)

    def fail_record_directory_sync(path: Path) -> None:
        if path == record_path:
            raise OSError("simulated directory fsync loss")

    monkeypatch.setattr(durable_append_log, "_fsync_parent_dir", fail_record_directory_sync)
    with pytest.raises(OSError, match="simulated directory fsync loss"):
        repo.prepare(
            idempotency_key=key,
            command="start",
            request_sha256=canonical_request_sha256("start", "DU123", payload),
            account_id="DU123",
            enforcement_enabled=True,
            now_ms=1_700_000_000_000,
        )

    calls = 0

    def invoke() -> DaemonCommandOutcome:
        nonlocal calls
        calls += 1
        return _success(calls)

    outcome = DaemonCommandIdempotencyService(tmp_path).execute(
        idempotency_key=key,
        command="start",
        account_id="DU123",
        semantic_payload=payload,
        invoke=invoke,
    )

    assert outcome.status_code == 409
    assert outcome.body["reason_code"] == "IDEMPOTENCY_OUTCOME_UNKNOWN"
    assert calls == 0


def test_execute_key_reused_after_account_rebind_conflicts_without_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LIVE_RUNNER_DAEMON_COMMAND_IDEMPOTENCY_ENFORCED_ACCOUNTS", "DU111,DU222")
    service = DaemonCommandIdempotencyService(tmp_path)
    calls = 0

    def invoke() -> DaemonCommandOutcome:
        nonlocal calls
        calls += 1
        return _success(calls)

    service.execute(
        idempotency_key="rebound-key",
        command="start",
        account_id="DU111",
        semantic_payload={"run_id": "rebound-run", "readonly": True},
        invoke=invoke,
    )
    conflict = service.execute(
        idempotency_key="rebound-key",
        command="start",
        account_id="DU222",
        semantic_payload={"run_id": "rebound-run", "readonly": True},
        invoke=invoke,
    )

    assert conflict.status_code == 409
    assert conflict.body["reason_code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_COMMAND"
    assert calls == 1
