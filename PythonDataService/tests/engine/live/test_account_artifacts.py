"""Tests for account-scoped live lifecycle artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live import account_artifacts
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountAuditedOverride,
    AccountFreezeEvidence,
    AccountInstanceBinding,
    AccountRecoveryProof,
    RestartIntensityPolicy,
    account_artifacts_root,
    clear_account_freeze,
    compute_reconcile_namespaces,
    evaluate_account_instance_binding,
    evaluate_restart_intensity,
    read_account_events,
    read_account_events_tolerant,
    read_account_freeze,
    read_account_instance_registry,
    write_account_freeze,
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


def test_account_instance_registry_accepts_current_binding(tmp_path: Path) -> None:
    binding = _binding()

    path = write_account_instance_binding(tmp_path, binding)

    assert path == tmp_path / "accounts" / "DU123456" / "instance_registry.jsonl"
    assert read_account_instance_registry(tmp_path, "DU123456") == [binding]
    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="spy-ema-paper-1",
        run_id="run-alpha",
        bot_order_namespace="learn-ai/spy-ema-paper-1/v1",
    )
    assert gate.status == "pass"
    assert gate.operator_next_step == "GATE_PASSING"


def test_compute_reconcile_namespaces_splits_owned_from_active_siblings(tmp_path: Path) -> None:
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="spy",
            run_id="run-spy",
            namespace="learn-ai/spy/v1",
            recorded_at_ms=2,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="spy",
            run_id="run-old",
            namespace="learn-ai/spy-old/v1",
            recorded_at_ms=1,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="aapl",
            run_id="run-aapl",
            namespace="learn-ai/aapl/v1",
            recorded_at_ms=4,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="retired",
            run_id="run-retired",
            namespace="learn-ai/retired/v1",
            recorded_at_ms=3,
        ).model_copy(update={"lifecycle_state": "RETIRED"}),
    )

    owned, siblings = compute_reconcile_namespaces(
        artifacts_root=tmp_path,
        account_id="DU123456",
        current_namespace="learn-ai/aapl/v1",
    )

    assert owned == frozenset({"learn-ai/aapl/v1"})
    assert siblings == frozenset({"learn-ai/spy/v1"})


def test_compute_reconcile_namespaces_drops_later_retired_and_wrong_account_bindings(
    tmp_path: Path,
) -> None:
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="retiring-spy",
            run_id="run-active",
            namespace="learn-ai/retiring-spy/v1",
            recorded_at_ms=1,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="retiring-spy",
            run_id="run-retired",
            namespace="learn-ai/retiring-spy/v1",
            recorded_at_ms=2,
        ).model_copy(update={"lifecycle_state": "RETIRED"}),
    )
    registry_path = account_artifacts_root(tmp_path, "DU123456") / "instance_registry.jsonl"
    with open(registry_path, "a", encoding="utf-8") as fh:
        fh.write(
            _binding(
                sid="wrong-account",
                run_id="run-wrong",
                namespace="learn-ai/wrong-account/v1",
                recorded_at_ms=3,
            ).model_copy(update={"account_id": "DU999999"}).model_dump_json()
            + "\n"
        )

    owned, siblings = compute_reconcile_namespaces(
        artifacts_root=tmp_path,
        account_id="DU123456",
        current_namespace="learn-ai/aapl/v1",
    )

    assert owned == frozenset({"learn-ai/aapl/v1"})
    assert siblings == frozenset()


def test_account_instance_registry_blocks_unknown_instance(tmp_path: Path) -> None:
    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="missing-instance",
        run_id="run-alpha",
        bot_order_namespace="learn-ai/missing-instance/v1",
    )

    assert gate.status == "block"
    assert gate.operator_reason == "ACCOUNT_REGISTRY_UNKNOWN_INSTANCE"


def test_account_instance_registry_blocks_stale_run_binding(tmp_path: Path) -> None:
    write_account_instance_binding(tmp_path, _binding(run_id="run-alpha"))

    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="spy-ema-paper-1",
        run_id="run-beta",
        bot_order_namespace="learn-ai/spy-ema-paper-1/v1",
    )

    assert gate.status == "block"
    assert gate.operator_reason == "ACCOUNT_REGISTRY_STALE_RUN"


def test_account_instance_registry_blocks_duplicate_namespace(tmp_path: Path) -> None:
    write_account_instance_binding(
        tmp_path,
        _binding(sid="spy-a", run_id="run-a", namespace="learn-ai/shared/v1"),
    )
    write_account_instance_binding(
        tmp_path,
        _binding(
            sid="spy-b",
            run_id="run-b",
            namespace="learn-ai/shared/v1",
            recorded_at_ms=1_700_000_000_100,
        ),
    )

    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id="DU123456",
        strategy_instance_id="spy-b",
        run_id="run-b",
        bot_order_namespace="learn-ai/shared/v1",
    )

    assert gate.status == "block"
    assert gate.operator_reason == "ACCOUNT_REGISTRY_DUPLICATE_NAMESPACE"


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
