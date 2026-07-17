"""Tests for account-scoped live lifecycle artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live import account_artifacts, account_registry
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountAuditedOverride,
    AccountClerkLease,
    AccountFreezeEvidence,
    AccountRecoveryProof,
    CohortBatchLaunchReceipt,
    RestartIntensityPolicy,
    account_artifacts_root,
    advance_account_clerk_generation,
    clear_account_freeze,
    evaluate_restart_intensity,
    project_restart_intensity_gate,
    read_account_clerk_generation,
    read_account_clerk_lease,
    read_account_events,
    read_account_events_tolerant,
    read_account_freeze,
    record_cohort_batch_launch_receipt,
    repair_account_event_sequence,
    require_active_account_clerk_generation,
    write_account_clerk_lease,
    write_account_freeze,
)
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    write_account_instance_binding,
)
from app.schemas.live_runs import GateResult


def test_account_freeze_round_trips_with_gate_result_and_audit_event(tmp_path: Path) -> None:
    evidence = AccountFreezeEvidence(
        account_id="DU123456",
        reason="watchdog.flatten_failed",
        source="watchdog_halt_executor",
        recorded_at_ms=1_700_000_000_000,
        operator_next_step="CHECK_IBKR",
    )

    path = write_account_freeze(tmp_path, evidence)

    assert path == tmp_path / "accounts" / "DU123456" / "unresolved_exposure.flag"
    loaded = read_account_freeze(tmp_path, "DU123456")
    assert loaded == evidence
    gate = loaded.to_gate_result()
    assert gate.gate_id == "account.unresolved_exposure"
    assert gate.status == "freeze"
    assert gate.source == "watchdog_halt_executor"
    assert gate.operator_reason == "watchdog.flatten_failed"
    assert gate.operator_next_step == "CHECK_IBKR"
    assert gate.evidence_at_ms == 1_700_000_000_000
    event = read_account_events(tmp_path, "DU123456")[-1]
    assert event["event_type"] == "account_freeze_recorded"
    assert event["seq"] == 1
    assert event["ts_ms"] == 1_700_000_000_000
    assert account_artifacts_root(tmp_path, "DU123456") == tmp_path / "accounts" / "DU123456"


def test_account_clerk_generation_and_lease_are_account_rooted(tmp_path: Path) -> None:
    generation = advance_account_clerk_generation(
        tmp_path,
        "DU123456",
        phase="accepting",
        recorded_at_ms=1_700_000_000_000,
        source="host_daemon.clerk_spawn",
    )
    lease = AccountClerkLease(
        account_id="DU123456",
        generation=generation.generation,
        pid=123,
        ibkr_client_id=80,
        started_at_ms=1_700_000_000_000,
        renewed_at_ms=1_700_000_000_100,
        valid_until_ms=1_700_000_060_100,
    )

    path = write_account_clerk_lease(tmp_path, lease)

    assert generation.generation == 1
    assert read_account_clerk_generation(tmp_path, "DU123456") == generation
    assert path == tmp_path / "accounts" / "DU123456" / "clerk_lease.json"
    assert read_account_clerk_lease(tmp_path, "DU123456") == lease
    assert require_active_account_clerk_generation(
        tmp_path,
        "DU123456",
        now_ms=1_700_000_000_200,
    ) == generation.generation

    stale_generation = advance_account_clerk_generation(
        tmp_path,
        "DU123456",
        phase="accepting",
        recorded_at_ms=1_700_000_000_300,
        source="host_daemon.clerk_takeover",
    )
    with pytest.raises(RuntimeError, match="CLERK_LEASE_GENERATION_MISMATCH"):
        require_active_account_clerk_generation(
            tmp_path,
            "DU123456",
            now_ms=1_700_000_000_400,
        )
    assert stale_generation.generation == 2


@pytest.mark.parametrize(
    "account_id",
    [
        "",
        "/tmp/DU123456",
        "../x",
        "du123456",
        " DU123456 ",
        "DU.123456",
        "DU-123456",
        "DU 123456",
        "DU/123456",
        "DU%2F123456",
    ],
)
def test_account_artifacts_root_rejects_path_like_account_id(
    tmp_path: Path,
    account_id: str,
) -> None:
    with pytest.raises(AccountArtifactError, match="invalid account_id"):
        account_artifacts_root(tmp_path, account_id)


def test_account_artifacts_registry_compatibility_exports_delegate_to_account_registry() -> None:
    assert account_artifacts.AccountInstanceBinding is account_registry.AccountInstanceBinding
    assert account_artifacts.read_account_instance_registry is account_registry.read_account_instance_registry
    assert account_artifacts.write_account_instance_binding is account_registry.write_account_instance_binding


def test_account_artifacts_root_rejects_symlink_escape(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts"
    accounts_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    try:
        (accounts_root / "DU123456").symlink_to(outside_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(AccountArtifactError, match="path traversal"):
        account_artifacts_root(tmp_path, "DU123456")


def test_account_artifacts_root_rejects_sibling_prefix_symlink_escape(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts"
    accounts_root.mkdir()
    sibling_root = tmp_path / "accounts-evil" / "DU123456"
    sibling_root.mkdir(parents=True)
    try:
        (accounts_root / "DU123456").symlink_to(sibling_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(AccountArtifactError, match="path traversal"):
        account_artifacts_root(tmp_path, "DU123456")


def test_append_account_event_rejects_symlinked_event_file(tmp_path: Path) -> None:
    root = account_artifacts_root(tmp_path, "DU123456")
    root.mkdir(parents=True)
    outside = tmp_path / "outside-events.jsonl"
    event_path = root / account_artifacts.ACCOUNT_EVENTS_FILENAME
    try:
        event_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(AccountArtifactError, match="artifact path traversal"):
        account_artifacts.append_account_event(
            tmp_path,
            "DU123456",
            {
                "event_type": "account_owner_generation_recorded",
                "recorded_at_ms": 1_700_000_000_000,
            },
        )

    assert not outside.exists()


def test_read_account_events_rejects_symlinked_event_file(tmp_path: Path) -> None:
    root = account_artifacts_root(tmp_path, "DU123456")
    root.mkdir(parents=True)
    outside = tmp_path / "outside-events.jsonl"
    event_path = root / account_artifacts.ACCOUNT_EVENTS_FILENAME
    try:
        event_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(AccountArtifactError, match="artifact path traversal"):
        read_account_events(tmp_path, "DU123456")

    assert not outside.exists()


@pytest.mark.parametrize(
    "reader, filename",
    [
        (read_account_freeze, account_artifacts.ACCOUNT_FREEZE_FILENAME),
        (account_artifacts.read_account_owner_generation, account_artifacts.ACCOUNT_OWNER_GENERATION_FILENAME),
    ],
)
def test_account_artifact_reads_reject_symlinked_static_file(
    tmp_path: Path,
    reader: object,
    filename: str,
) -> None:
    root = account_artifacts_root(tmp_path, "DU123456")
    root.mkdir(parents=True)
    outside = tmp_path / "outside-artifact.json"
    artifact_path = root / filename
    try:
        artifact_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(AccountArtifactError, match="artifact path traversal"):
        reader(tmp_path, "DU123456")


def test_account_event_seq_tolerates_malformed_legacy_rows(tmp_path: Path) -> None:
    root = account_artifacts_root(tmp_path, "DU123456")
    root.mkdir(parents=True)
    path = root / account_artifacts.ACCOUNT_EVENTS_FILENAME
    path.write_text('{"seq":5,"event_type":"legacy"}\nnot-json\n[]\n', encoding="utf-8")

    account_artifacts.append_account_event(
        tmp_path,
        "DU123456",
        {
            "event_type": "account_owner_generation_recorded",
            "recorded_at_ms": 1_700_000_020_000,
        },
    )

    appended = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert appended["seq"] == 6
    assert appended["ts_ms"] == 1_700_000_020_000


def test_account_event_counter_recovers_missing_behind_and_ahead_without_skipping(tmp_path: Path) -> None:
    account_id = "DU123456"
    payload = {"event_type": "account_owner_generation_recorded", "recorded_at_ms": 1_700_000_020_000}
    account_artifacts.append_account_event(tmp_path, account_id, payload)
    root = account_artifacts_root(tmp_path, account_id)
    counter = root / account_artifacts.ACCOUNT_EVENTS_SEQUENCE_FILENAME

    counter.unlink()  # Crash after ledger fsync, before counter write.
    account_artifacts.append_account_event(tmp_path, account_id, payload)
    counter.write_text('{"last_seq":0}', encoding="utf-8")  # Counter behind durable tail.
    account_artifacts.append_account_event(tmp_path, account_id, payload)
    counter.write_text('{"last_seq":999}', encoding="utf-8")  # Counter ahead of durable tail.
    account_artifacts.append_account_event(tmp_path, account_id, payload)

    assert [event["seq"] for event in read_account_events(tmp_path, account_id)] == [1, 2, 3, 4]
    assert json.loads(counter.read_text(encoding="utf-8")) == {"last_seq": 4}


def test_repair_account_event_sequence_resequences_without_discarding_rows(tmp_path: Path) -> None:
    account_id = "DU123456"
    payload = {"event_type": "account_owner_generation_recorded", "recorded_at_ms": 1_700_000_020_000}
    for _ in range(3):
        account_artifacts.append_account_event(tmp_path, account_id, payload)
    path = account_artifacts_root(tmp_path, account_id) / account_artifacts.ACCOUNT_EVENTS_FILENAME
    original = path.read_bytes()
    rows = [json.loads(line) for line in original.decode().splitlines()]
    rows[2]["seq"] = 2
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    receipt = repair_account_event_sequence(tmp_path, account_id)

    assert receipt.account_id == account_id
    assert receipt.rewritten_rows == 3
    assert receipt.backup_path is not None
    assert receipt.backup_path.read_bytes() != path.read_bytes()
    assert [event["seq"] for event in read_account_events(tmp_path, account_id)] == [1, 2, 3]
    account_artifacts.append_account_event(tmp_path, account_id, payload)
    assert [event["seq"] for event in read_account_events(tmp_path, account_id)] == [1, 2, 3, 4]


def test_account_event_steady_state_append_does_not_rescan_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "DU123456"
    payload = {"event_type": "account_owner_generation_recorded", "recorded_at_ms": 1_700_000_020_000}
    account_artifacts.append_account_event(tmp_path, account_id, payload)
    monkeypatch.setattr(
        account_artifacts,
        "read_account_events",
        lambda *_args, **_kwargs: pytest.fail("steady-state append must not rescan the ledger"),
    )

    account_artifacts.append_account_event(tmp_path, account_id, payload)


def test_append_account_event_authors_typed_int64_ms_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(account_artifacts.time, "time_ns", lambda: 1_700_000_030_000_000_000)

    account_artifacts.append_account_event(
        tmp_path,
        "DU123456",
        {
            "account_id": "DU999999",
            "event_type": "account_owner_reconnect_resumed",
            "reason": "manual reconnect complete",
        },
    )

    event = read_account_events(tmp_path, "DU123456")[0]
    assert event["account_id"] == "DU123456"
    assert event["event_type"] == "account_owner_reconnect_resumed"
    assert event["seq"] == 1
    assert event["ts_ms"] == 1_700_000_030_000
    assert isinstance(event["ts_ms"], int)


def test_append_account_event_rejects_bad_explicit_timestamp(tmp_path: Path) -> None:
    with pytest.raises(AccountArtifactError, match="ts_ms"):
        account_artifacts.append_account_event(
            tmp_path,
            "DU123456",
            {
                "event_type": "account_owner_reconnect_resumed",
                "ts_ms": "2026-06-30T12:00:00Z",
            },
        )

    assert read_account_events(tmp_path, "DU123456") == []


def test_append_account_event_rejects_bad_timestamp_extra(tmp_path: Path) -> None:
    with pytest.raises(AccountArtifactError, match="created_at_ms"):
        account_artifacts.append_account_event(
            tmp_path,
            "DU123456",
            {
                "event_type": "account_owner_reconnect_resumed",
                "created_at_ms": "2026-06-30T12:00:00Z",
            },
        )

    assert read_account_events(tmp_path, "DU123456") == []


def test_append_account_event_requires_event_type(tmp_path: Path) -> None:
    with pytest.raises(AccountArtifactError, match="event_type"):
        account_artifacts.append_account_event(
            tmp_path,
            "DU123456",
            {"recorded_at_ms": 1_700_000_020_000},
        )

    assert read_account_events(tmp_path, "DU123456") == []


def test_read_account_events_fails_closed_on_malformed_current_rows(tmp_path: Path) -> None:
    root = account_artifacts_root(tmp_path, "DU123456")
    root.mkdir(parents=True)
    path = root / account_artifacts.ACCOUNT_EVENTS_FILENAME
    path.write_text(
        '{"event_type":"legacy","account_id":"DU123456"}\nnot-json\n[]\n',
        encoding="utf-8",
    )

    with pytest.raises(AccountArtifactError, match="malformed account event row"):
        read_account_events(tmp_path, "DU123456")


def test_read_account_events_tolerant_skips_malformed_legacy_rows(tmp_path: Path) -> None:
    root = account_artifacts_root(tmp_path, "DU123456")
    root.mkdir(parents=True)
    path = root / account_artifacts.ACCOUNT_EVENTS_FILENAME
    path.write_text(
        '{"event_type":"legacy","account_id":"DU123456"}\nnot-json\n[]\n',
        encoding="utf-8",
    )

    events = read_account_events_tolerant(tmp_path, "DU123456")

    assert events == [{"event_type": "legacy", "account_id": "DU123456"}]


def test_read_account_events_tolerant_skips_unreadable_rows_only(tmp_path: Path) -> None:
    root = account_artifacts_root(tmp_path, "DU123456")
    root.mkdir(parents=True)
    path = root / account_artifacts.ACCOUNT_EVENTS_FILENAME
    path.write_bytes(
        b'{"event_type":"first","account_id":"DU123456"}\n'
        b"\xff\xfe\xfa\n"
        b'{"event_type":"second","account_id":"DU123456"}\n'
    )

    events = read_account_events_tolerant(tmp_path, "DU123456")

    assert events == [
        {"event_type": "first", "account_id": "DU123456"},
        {"event_type": "second", "account_id": "DU123456"},
    ]
    with pytest.raises(AccountArtifactError, match="invalid account event UTF-8"):
        read_account_events(tmp_path, "DU123456")


def test_account_freeze_clears_after_clean_recovery_proof(tmp_path: Path) -> None:
    evidence = AccountFreezeEvidence(
        account_id="DU123456",
        reason="watchdog.flatten_failed",
        source="watchdog_halt_executor",
        recorded_at_ms=1_700_000_000_000,
        operator_next_step="CHECK_IBKR",
    )
    freeze_path = write_account_freeze(tmp_path, evidence)
    proof = AccountRecoveryProof(
        account_id="DU123456",
        recovery_id="recovery-1",
        requested_action="emergency_flatten",
        requested_by="operator",
        broker_evidence={"positions": [], "open_orders": []},
        reconciliation_result="clean",
        final_gate_result=GateResult(
            gate_id="account.classifier",
            status="pass",
            source="account_classifier",
            operator_reason="ACCOUNT_STATE_MATCHES_REGISTRY",
            operator_next_step="GATE_PASSING",
            evidence_at_ms=1_700_000_010_000,
        ),
        recorded_at_ms=1_700_000_010_000,
    )

    clear_account_freeze(
        tmp_path,
        recovery_proof=proof,
    )

    assert freeze_path.exists()
    assert read_account_freeze(tmp_path, "DU123456") is None
    events = read_account_events(tmp_path, "DU123456")
    assert [event["event_type"] for event in events] == [
        "account_freeze_recorded",
        "account_recovery_proof_recorded",
        "account_freeze_cleared",
    ]
    assert events[-2]["broker_evidence"]["positions"] == []
    assert events[-1]["cleared_reason"] == "recovery:recovery-1"
    assert events[-1]["cleared_source"] == "account_recovery_proof"
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert events[-1]["ts_ms"] == 1_700_000_010_000


def test_account_freeze_clear_requires_recovery_proof_or_audited_override(tmp_path: Path) -> None:
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU123456",
            reason="watchdog.flatten_failed",
            source="watchdog_halt_executor",
            recorded_at_ms=1_700_000_000_000,
            operator_next_step="CHECK_IBKR",
        ),
    )

    with pytest.raises(AccountArtifactError):
        clear_account_freeze(tmp_path)

    assert read_account_freeze(tmp_path, "DU123456") is not None


def test_account_freeze_clears_after_audited_override_with_prior_evidence(tmp_path: Path) -> None:
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU123456",
            reason="watchdog.flatten_failed",
            source="watchdog_halt_executor",
            recorded_at_ms=1_700_000_000_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    override = AccountAuditedOverride(
        account_id="DU123456",
        override_id="override-1",
        approved_decision="poison_run",
        reason="operator verified orphan fill belongs to retired run",
        approved_by="operator",
        approved_at_ms=1_700_000_010_000,
        valid_until_ms=1_700_000_070_000,
        prior_evidence={"freeze_reason": "watchdog.flatten_failed"},
        next_reconciliation_step="RECHECK_BROKER_ON_RECONNECT",
        strategy_instance_id="spy-ema-paper-1",
        run_id="run-alpha",
        bot_order_namespace="learn-ai/spy-ema-paper-1/v1",
        affected_order_refs=("learn-ai/spy-ema-paper-1/v1:intent-1",),
    )

    clear_account_freeze(tmp_path, audited_override=override, now_ms=1_700_000_020_000)

    assert read_account_freeze(tmp_path, "DU123456") is None
    events = read_account_events(tmp_path, "DU123456")
    assert events[-2]["event_type"] == "account_audited_override_recorded"
    assert events[-2]["prior_evidence"]["freeze_reason"] == "watchdog.flatten_failed"
    assert events[-2]["next_reconciliation_step"] == "RECHECK_BROKER_ON_RECONNECT"
    assert events[-1]["cleared_reason"] == "override:override-1:poison_run"


def test_account_freeze_rejects_stale_audited_override(tmp_path: Path) -> None:
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU123456",
            reason="watchdog.flatten_failed",
            source="watchdog_halt_executor",
            recorded_at_ms=1_700_000_000_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    override = AccountAuditedOverride(
        account_id="DU123456",
        override_id="override-1",
        approved_decision="continue",
        reason="manual review",
        approved_by="operator",
        approved_at_ms=1_700_000_010_000,
        valid_until_ms=1_700_000_020_000,
        prior_evidence={"freeze_reason": "watchdog.flatten_failed"},
        next_reconciliation_step="RECHECK_BROKER_ON_RECONNECT",
    )

    with pytest.raises(AccountArtifactError, match="stale"):
        clear_account_freeze(tmp_path, audited_override=override, now_ms=1_700_000_020_001)

    assert read_account_freeze(tmp_path, "DU123456") is not None


def test_account_freeze_uses_actual_clear_time_when_override_now_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU123456",
            reason="watchdog.flatten_failed",
            source="watchdog_halt_executor",
            recorded_at_ms=1_700_000_000_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    override = AccountAuditedOverride(
        account_id="DU123456",
        override_id="override-1",
        approved_decision="continue",
        reason="manual review",
        approved_by="operator",
        approved_at_ms=1_700_000_010_000,
        valid_until_ms=1_700_000_020_000,
        prior_evidence={"freeze_reason": "watchdog.flatten_failed"},
        next_reconciliation_step="RECHECK_BROKER_ON_RECONNECT",
    )
    monkeypatch.setattr(account_artifacts.time, "time_ns", lambda: 1_700_000_015_000_000_000)

    clear_account_freeze(tmp_path, audited_override=override)

    events = read_account_events(tmp_path, "DU123456")
    assert events[-1]["cleared_at_ms"] == 1_700_000_015_000


def _binding(
    *,
    sid: str = "spy-ema-paper-1",
    run_id: str = "run-alpha",
    namespace: str = "learn-ai/spy-ema-paper-1/v1",
    recorded_at_ms: int = 1_700_000_000_000,
) -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id="DU123456",
        strategy_instance_id=sid,
        run_id=run_id,
        bot_order_namespace=namespace,
        lifecycle_state="ACTIVE",
        recorded_at_ms=recorded_at_ms,
        source="host_daemon.start",
    )


def test_restart_intensity_passes_below_threshold_from_durable_account_events(tmp_path: Path) -> None:
    policy = RestartIntensityPolicy(threshold=3, window_ms=60_000)
    write_account_instance_binding(
        tmp_path,
        _binding(sid="spy-a", run_id="run-a", namespace="learn-ai/spy-a/v1", recorded_at_ms=1_700_000_000_000),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(sid="spy-b", run_id="run-b", namespace="learn-ai/spy-b/v1", recorded_at_ms=1_700_000_010_000),
    )

    gate = evaluate_restart_intensity(
        tmp_path,
        account_id="DU123456",
        now_ms=1_700_000_020_000,
        policy=policy,
    )

    assert gate.status == "pass"
    assert "observed=2" in gate.operator_reason
    assert "threshold=3" in gate.operator_reason
    assert read_account_freeze(tmp_path, "DU123456") is None


def test_restart_intensity_projection_refuses_a_cohort_that_would_breach_without_freezing(tmp_path: Path) -> None:
    policy = RestartIntensityPolicy(threshold=3, window_ms=60_000)
    for index, recorded_at_ms in enumerate((1_700_000_000_000, 1_700_000_010_000), start=1):
        write_account_instance_binding(
            tmp_path,
            _binding(
                sid=f"spy-{index}",
                run_id=f"run-{index}",
                namespace=f"learn-ai/spy-{index}/v1",
                recorded_at_ms=recorded_at_ms,
            ),
        )

    gate = project_restart_intensity_gate(
        tmp_path,
        account_id="DU123456",
        now_ms=1_700_000_020_000,
        policy=policy,
    )

    assert gate.status == "freeze"
    assert "observed=3" in gate.operator_reason
    assert read_account_freeze(tmp_path, "DU123456") is None


def test_restart_intensity_counts_authorized_cohort_bindings_once(tmp_path: Path) -> None:
    policy = RestartIntensityPolicy(threshold=3, window_ms=60_000)
    receipt = CohortBatchLaunchReceipt(
        account_id="DU123456",
        cohort_id="opening-batch-1",
        member_strategy_instance_ids=("spy-a", "spy-b", "spy-c"),
        window_start_ms=1_700_000_000_000,
        window_end_ms=1_700_000_030_000,
        authorized_by="operator.alice",
        recorded_at_ms=1_700_000_000_000,
    )
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    for index, recorded_at_ms in enumerate(
        (1_700_000_001_000, 1_700_000_002_000, 1_700_000_003_000),
        start=1,
    ):
        write_account_instance_binding(
            tmp_path,
            _binding(
                sid=f"spy-{chr(96 + index)}",
                run_id=f"run-{index}",
                namespace=f"learn-ai/spy-{chr(96 + index)}/v1",
                recorded_at_ms=recorded_at_ms,
            ).model_copy(update={"cohort_id": "opening-batch-1"}),
        )

    gate = evaluate_restart_intensity(
        tmp_path,
        account_id="DU123456",
        now_ms=1_700_000_004_000,
        policy=policy,
    )

    assert gate.status == "pass"
    assert "observed=1" in gate.operator_reason
    event = next(event for event in read_account_events(tmp_path, "DU123456") if event["event_type"] == "cohort_batch_launch_authorized")
    assert event["cohort_id"] == "opening-batch-1"
    assert event["member_strategy_instance_ids"] == ["spy-a", "spy-b", "spy-c"]
    assert event["window_start_ms"] == 1_700_000_000_000
    assert event["window_end_ms"] == 1_700_000_030_000
    assert event["authorized_by"] == "operator.alice"


def test_restart_intensity_counts_daemon_crash_restarts_individually_during_cohort_window(tmp_path: Path) -> None:
    policy = RestartIntensityPolicy(threshold=3, window_ms=60_000)
    receipt = CohortBatchLaunchReceipt(
        account_id="DU123456",
        cohort_id="opening-batch-1",
        member_strategy_instance_ids=("spy-a", "spy-b", "spy-c"),
        window_start_ms=1_700_000_000_000,
        window_end_ms=1_700_000_030_000,
        authorized_by="operator.alice",
        recorded_at_ms=1_700_000_000_000,
    )
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    for index, recorded_at_ms in enumerate(
        (1_700_000_001_000, 1_700_000_002_000, 1_700_000_003_000),
        start=1,
    ):
        write_account_instance_binding(
            tmp_path,
            _binding(
                sid=f"spy-{chr(96 + index)}",
                run_id=f"run-{index}",
                namespace=f"learn-ai/spy-{chr(96 + index)}/v1",
                recorded_at_ms=recorded_at_ms,
            ).model_copy(update={"source": "host_daemon.crash_restart"}),
        )

    gate = evaluate_restart_intensity(
        tmp_path,
        account_id="DU123456",
        now_ms=1_700_000_004_000,
        policy=policy,
    )

    assert gate.status == "freeze"
    assert "observed=3" in gate.operator_reason


def test_restart_intensity_breach_records_account_freeze_with_threshold_details(tmp_path: Path) -> None:
    policy = RestartIntensityPolicy(threshold=3, window_ms=60_000)
    for index, recorded_at_ms in enumerate(
        (1_700_000_000_000, 1_700_000_010_000, 1_700_000_020_000),
        start=1,
    ):
        write_account_instance_binding(
            tmp_path,
            _binding(
                sid=f"spy-{index}",
                run_id=f"run-{index}",
                namespace=f"learn-ai/spy-{index}/v1",
                recorded_at_ms=recorded_at_ms,
            ),
        )

    gate = evaluate_restart_intensity(
        tmp_path,
        account_id="DU123456",
        now_ms=1_700_000_020_001,
        policy=policy,
    )

    assert gate.status == "freeze"
    assert "observed=3" in gate.operator_reason
    assert "window_ms=60000" in gate.operator_reason
    freeze = read_account_freeze(tmp_path, "DU123456")
    assert freeze is not None
    assert freeze.source == "account_restart_intensity"
    events = read_account_events(tmp_path, "DU123456")
    breach = next(event for event in events if event["event_type"] == "account_restart_intensity_breached")
    assert breach["observed_count"] == 3
    assert breach["threshold"] == 3
    assert breach["window_start_ms"] == 1_699_999_960_001
    assert breach["window_end_ms"] == 1_700_000_020_001
    assert breach["affected_instance_ids"] == ["spy-1", "spy-2", "spy-3"]


def test_restart_intensity_refolds_after_process_restart_without_reset(tmp_path: Path) -> None:
    policy = RestartIntensityPolicy(threshold=3, window_ms=60_000)
    for index, recorded_at_ms in enumerate(
        (1_700_000_000_000, 1_700_000_010_000, 1_700_000_020_000),
        start=1,
    ):
        write_account_instance_binding(
            tmp_path,
            _binding(
                sid=f"spy-{index}",
                run_id=f"run-{index}",
                namespace=f"learn-ai/spy-{index}/v1",
                recorded_at_ms=recorded_at_ms,
            ),
        )

    first_gate = evaluate_restart_intensity(
        tmp_path,
        account_id="DU123456",
        now_ms=1_700_000_020_001,
        policy=policy,
    )
    second_gate = evaluate_restart_intensity(
        tmp_path,
        account_id="DU123456",
        now_ms=1_700_000_020_002,
        policy=policy,
    )

    assert first_gate.status == "freeze"
    assert second_gate.status == "freeze"
    events = read_account_events(tmp_path, "DU123456")
    assert [event["event_type"] for event in events].count("account_restart_intensity_breached") == 1


def test_restart_intensity_recovery_clear_starts_a_new_window(tmp_path: Path) -> None:
    policy = RestartIntensityPolicy(threshold=3, window_ms=60_000)
    for index, recorded_at_ms in enumerate(
        (1_700_000_000_000, 1_700_000_010_000, 1_700_000_020_000),
        start=1,
    ):
        write_account_instance_binding(
            tmp_path,
            _binding(
                sid=f"spy-{index}",
                run_id=f"run-{index}",
                namespace=f"learn-ai/spy-{index}/v1",
                recorded_at_ms=recorded_at_ms,
            ),
        )
    evaluate_restart_intensity(
        tmp_path,
        account_id="DU123456",
        now_ms=1_700_000_020_001,
        policy=policy,
    )

    clear_account_freeze(
        tmp_path,
        recovery_proof=AccountRecoveryProof(
            account_id="DU123456",
            recovery_id="restart-recovery-1",
            requested_action="reconcile",
            requested_by="operator",
            broker_evidence={"positions": [], "open_orders": []},
            reconciliation_result="clean",
            final_gate_result=GateResult(
                gate_id="account.restart_intensity",
                status="pass",
                source="account_restart_intensity",
                operator_reason="restart intensity recovered",
                operator_next_step="GATE_PASSING",
                evidence_at_ms=1_700_000_030_000,
            ),
            recorded_at_ms=1_700_000_030_000,
        ),
    )

    gate = evaluate_restart_intensity(
        tmp_path,
        account_id="DU123456",
        now_ms=1_700_000_030_001,
        policy=policy,
    )

    assert gate.status == "pass"
    assert "observed=0" in gate.operator_reason
    assert read_account_freeze(tmp_path, "DU123456") is None
