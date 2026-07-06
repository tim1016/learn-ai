from __future__ import annotations

import hashlib
import json

import pytest

from app.schemas.strategy_validation import (
    StrategyBehavioralEquivalence,
    StrategyEvidenceSnapshot,
    StrategyValidationEntry,
    StrategyValidationFlagEvent,
    StrategyValidationFlagRequest,
)
from app.services.strategy_validation_manifest import (
    DEFAULT_FLAG_EVENTS_PATH,
    StrategyEvidenceSeed,
    StrategyRegistrySeed,
    StrategyValidationManifestError,
    append_strategy_validation_flag_event,
    load_strategy_validation_entries,
    reference_code_for_entry,
    seed_strategy_validation_manifest,
)

TEST_FLAG_ACTOR = "local:test-operator"


def _accepted_flag_event(
    strategy_key: str = "deployment_validation",
    *,
    event_id: str | None = None,
    flagged_at_ms: int = 1775088000000,
) -> StrategyValidationFlagEvent:
    return StrategyValidationFlagEvent(
        event_id=event_id or f"accepted-{strategy_key}",
        strategy_key=strategy_key,
        flag="validated",
        flagged_by=TEST_FLAG_ACTOR,
        flagged_at_ms=flagged_at_ms,
        reason="Accepted for deployment.",
        behavioral_equivalence=StrategyBehavioralEquivalence(
            verdict="accepted_for_deploy",
            detail="Human validation accepted the current engine evidence for deployment.",
        ),
        evidence_snapshot=StrategyEvidenceSnapshot(
            settings_file_ref="PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json",
            settings_file_sha256="spec-sha",
            qc_cloud_backtest_id="d2fe45a7142e88575f6fbd75229f8681",
            audit_copy_ref="references/qc-shadow/DeploymentValidationAlgorithm.py",
            audit_copy_sha256="audit-sha",
            reconciliation_ref="references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md",
            validation_case_symbol="SPY",
            reconciliation_status="passed",
        ),
        evidence_snapshot_sha256="snapshot-sha",
    )


def test_default_runtime_flag_event_path_uses_ignored_service_artifacts() -> None:
    assert DEFAULT_FLAG_EVENTS_PATH.as_posix().endswith(
        "PythonDataService/artifacts/strategy_validation/flag_events.json"
    )


def test_seed_manifest_marks_deployment_validation_deployable() -> None:
    registry = [
        StrategyRegistrySeed(
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            description="Two-green-minute deployment validation primitive.",
        ),
        StrategyRegistrySeed(
            strategy_key="spy_orb",
            display_name="Opening Range Breakout",
            description="Opening range breakout strategy.",
        ),
    ]
    evidence = [
        StrategyEvidenceSeed(
            strategy_key="deployment_validation",
            settings_file_ref="PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json",
            settings_file_sha256="spec-sha",
            qc_cloud_backtest_id="d2fe45a7142e88575f6fbd75229f8681",
            audit_copy_ref="references/qc-shadow/DeploymentValidationAlgorithm.py",
            audit_copy_sha256="audit-sha",
            reconciliation_ref="references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md",
            validation_case_symbol="SPY",
            trades_matched=56,
            trades_validated=56,
            pnl_max_abs_diff="0.00",
            divergence_counts={},
        )
    ]

    entries = seed_strategy_validation_manifest(registry, evidence, [_accepted_flag_event()])

    assert [entry.strategy_key for entry in entries] == [
        "deployment_validation",
        "spy_orb",
    ]
    validated = entries[0]
    assert validated.validation_state == "validated"
    assert validated.deployable is True
    assert validated.qc_cloud_backtest_id == "d2fe45a7142e88575f6fbd75229f8681"
    assert validated.validation_case_symbol == "SPY"
    assert validated.diagnostics is not None
    assert validated.diagnostics.trades_matched == 56
    assert validated.current_flag_event is not None
    assert validated.current_flag_event.flagged_by == TEST_FLAG_ACTOR
    assert validated.behavioral_equivalence is not None
    assert validated.behavioral_equivalence.verdict == "accepted_for_deploy"

    unvalidated = entries[1]
    assert unvalidated.validation_state == "needs_validation"
    assert unvalidated.deployable is False
    assert unvalidated.qc_cloud_backtest_id is None
    assert unvalidated.diagnostics is None


def test_seed_manifest_does_not_deploy_passing_evidence_without_human_flag() -> None:
    registry = [
        StrategyRegistrySeed(
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            description="Two-green-minute deployment validation primitive.",
        ),
    ]
    evidence = [
        StrategyEvidenceSeed(
            strategy_key="deployment_validation",
            settings_file_ref="PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json",
            settings_file_sha256="spec-sha",
            qc_cloud_backtest_id="d2fe45a7142e88575f6fbd75229f8681",
            audit_copy_ref="references/qc-shadow/DeploymentValidationAlgorithm.py",
            audit_copy_sha256="audit-sha",
            reconciliation_ref="references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md",
            validation_case_symbol="SPY",
            trades_matched=56,
            trades_validated=56,
            pnl_max_abs_diff="0.00",
            divergence_counts={},
        )
    ]

    [entry] = seed_strategy_validation_manifest(registry, evidence)

    assert entry.validation_state == "needs_validation"
    assert entry.deployable is False
    assert entry.diagnostics is not None
    assert entry.current_flag_event is None


def test_seed_manifest_fails_closed_for_failed_reconciliation() -> None:
    registry = [
        StrategyRegistrySeed(
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            description="Two-green-minute deployment validation primitive.",
        ),
    ]
    evidence = [
        StrategyEvidenceSeed(
            strategy_key="deployment_validation",
            settings_file_ref="PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json",
            settings_file_sha256="spec-sha",
            qc_cloud_backtest_id="d2fe45a7142e88575f6fbd75229f8681",
            audit_copy_ref="references/qc-shadow/DeploymentValidationAlgorithm.py",
            audit_copy_sha256="audit-sha",
            reconciliation_ref="references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md",
            validation_case_symbol="SPY",
            trades_matched=56,
            trades_validated=55,
            pnl_max_abs_diff="1.23",
            verdict="failed",
            reconciliation_status="failed",
        )
    ]

    [entry] = seed_strategy_validation_manifest(registry, evidence)

    assert entry.validation_state == "needs_validation"
    assert entry.deployable is False
    assert entry.diagnostics is not None
    assert any("deployability requires passed" in note for note in entry.diagnostics.notes)


def test_validated_failed_reconciliation_remains_auditable_but_not_deployable() -> None:
    registry = [
        StrategyRegistrySeed(
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            description="Two-green-minute deployment validation primitive.",
        ),
    ]
    evidence = [
        StrategyEvidenceSeed(
            strategy_key="deployment_validation",
            settings_file_ref="PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json",
            settings_file_sha256="spec-sha",
            qc_cloud_backtest_id="d2fe45a7142e88575f6fbd75229f8681",
            audit_copy_ref="references/qc-shadow/DeploymentValidationAlgorithm.py",
            audit_copy_sha256="audit-sha",
            reconciliation_ref="references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md",
            validation_case_symbol="SPY",
            trades_matched=0,
            trades_validated=0,
            pnl_max_abs_diff="n/a",
            verdict="failed",
            reconciliation_status="failed",
        )
    ]
    event = _accepted_flag_event()

    [entry] = seed_strategy_validation_manifest(registry, evidence, [event])

    assert entry.validation_state == "validated"
    assert entry.deployable is False
    assert entry.current_flag_event is event


def test_seed_manifest_uses_latest_non_superseded_flag_event_by_timestamp() -> None:
    registry = [
        StrategyRegistrySeed(
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            description="Two-green-minute deployment validation primitive.",
        ),
    ]
    evidence = [
        StrategyEvidenceSeed(
            strategy_key="deployment_validation",
            settings_file_ref="PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json",
            settings_file_sha256="spec-sha",
            qc_cloud_backtest_id="d2fe45a7142e88575f6fbd75229f8681",
            audit_copy_ref="references/qc-shadow/DeploymentValidationAlgorithm.py",
            audit_copy_sha256="audit-sha",
            reconciliation_ref="references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md",
            validation_case_symbol="SPY",
            trades_matched=56,
            trades_validated=56,
            pnl_max_abs_diff="0.00",
            divergence_counts={},
        )
    ]
    older_event = _accepted_flag_event(event_id="older", flagged_at_ms=1000)
    newer_event = StrategyValidationFlagEvent(
        event_id="newer",
        strategy_key="deployment_validation",
        flag="invalidated",
        flagged_by=TEST_FLAG_ACTOR,
        flagged_at_ms=2000,
        reason="Reject the later evidence.",
        behavioral_equivalence=StrategyBehavioralEquivalence(
            verdict="rejected",
            detail="Human validation rejected this strategy for deployment.",
        ),
        evidence_snapshot=StrategyEvidenceSnapshot(),
        evidence_snapshot_sha256="snapshot-sha",
    )

    [entry] = seed_strategy_validation_manifest(registry, evidence, [newer_event, older_event])

    assert entry.validation_state == "needs_validation"
    assert entry.deployable is False
    assert entry.current_flag_event is newer_event


def test_load_manifest_fails_closed_when_settings_hash_mismatches(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    settings_path = repo_root / "PythonDataService/app/engine/strategy/spec/fixtures/test.spec.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text('{"name": "deployment_validation"}', encoding="utf-8")
    manifest_path = tmp_path / "strategy_validation_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "validated_strategies": [
                    {
                        "strategy_key": "deployment_validation",
                        "settings_file_ref": "PythonDataService/app/engine/strategy/spec/fixtures/test.spec.json",
                        "settings_file_sha256": "not-the-current-hash",
                        "qc_cloud_backtest_id": "d2fe45a7142e88575f6fbd75229f8681",
                        "audit_copy_ref": "references/qc-shadow/DeploymentValidationAlgorithm.py",
                        "audit_copy_sha256": "audit-sha",
                        "reconciliation_ref": "references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md",
                        "validation_case_symbol": "SPY",
                        "reconciliation_status": "passed",
                        "diagnostics": {
                            "verdict": "passed",
                            "trades_matched": 56,
                            "trades_validated": 56,
                            "pnl_max_abs_diff": "0.00",
                            "divergence_counts": {},
                            "notes": [],
                        },
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    [entry] = load_strategy_validation_entries(
        [
            StrategyRegistrySeed(
                strategy_key="deployment_validation",
                display_name="Deployment Validation",
                description="Two-green-minute deployment validation primitive.",
            ),
        ],
        manifest_path=manifest_path,
        repo_root=repo_root,
    )

    assert entry.validation_state == "needs_validation"
    assert entry.deployable is False
    assert entry.diagnostics is not None
    assert "Settings file hash no longer matches" in " ".join(entry.diagnostics.notes)


def test_load_manifest_fails_closed_when_event_snapshot_hash_mismatches(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = tmp_path / "strategy_validation_manifest.json"
    event = _accepted_flag_event().model_dump()
    event["evidence_snapshot_sha256"] = "not-the-snapshot-hash"
    manifest_path.write_text(
        json.dumps(
            {
                "validated_strategies": [],
                "seed_flag_events": [event],
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(StrategyValidationManifestError, match="snapshot SHA mismatch"):
        load_strategy_validation_entries([], manifest_path=manifest_path, repo_root=repo_root)


def test_append_flag_event_derives_actor_and_snapshots_evidence(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    settings_ref = "PythonDataService/app/engine/strategy/spec/fixtures/test.spec.json"
    settings_path = repo_root / settings_ref
    settings_path.parent.mkdir(parents=True)
    settings_payload = b'{"name":"deployment_validation"}'
    settings_path.write_bytes(settings_payload)
    settings_sha = hashlib.sha256(settings_payload).hexdigest()
    manifest_path = tmp_path / "strategy_validation_manifest.json"
    flag_events_path = tmp_path / "flag_events.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "validated_strategies": [
                    {
                        "strategy_key": "deployment_validation",
                        "settings_file_ref": settings_ref,
                        "settings_file_sha256": settings_sha,
                        "qc_cloud_backtest_id": "bt-1",
                        "audit_copy_ref": "references/qc-shadow/DeploymentValidationAlgorithm.py",
                        "audit_copy_sha256": "audit-sha",
                        "reconciliation_ref": "references/qc-shadow/backtests/attribution.md",
                        "validation_case_symbol": "SPY",
                        "reconciliation_status": "passed",
                        "diagnostics": {
                            "verdict": "passed",
                            "trades_matched": 1,
                            "trades_validated": 1,
                            "pnl_max_abs_diff": "0.00",
                            "divergence_counts": {},
                            "notes": [],
                        },
                    }
                ],
                "seed_flag_events": [],
            },
        ),
        encoding="utf-8",
    )

    entry = append_strategy_validation_flag_event(
        "deployment_validation",
        StrategyValidationFlagRequest(flag="validated", reason="Operator accepted this evidence."),
        [
            StrategyRegistrySeed(
                strategy_key="deployment_validation",
                display_name="Deployment Validation",
                description="Two-green-minute deployment validation primitive.",
            ),
        ],
        manifest_path=manifest_path,
        flag_events_path=flag_events_path,
        repo_root=repo_root,
        flagged_by=TEST_FLAG_ACTOR,
        now_ms=1234567890,
    )

    assert entry.validation_state == "validated"
    assert entry.deployable is True
    assert entry.current_flag_event is not None
    assert entry.current_flag_event.flagged_by == TEST_FLAG_ACTOR
    assert entry.current_flag_event.flagged_at_ms == 1234567890
    assert entry.current_flag_event.behavioral_equivalence.tolerance == "manifest_reconciliation_passed"
    assert entry.current_flag_event.behavioral_equivalence.gating_divergence_counts == {}
    assert entry.current_flag_event.evidence_snapshot.settings_file_sha256 == settings_sha
    assert entry.current_flag_event.evidence_snapshot_sha256
    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_raw["seed_flag_events"] == []
    ledger_raw = json.loads(flag_events_path.read_text(encoding="utf-8"))
    assert len(ledger_raw["flag_events"]) == 1
    assert ledger_raw["flag_events"][0]["event_version"] == "1.0"
    assert ledger_raw["flag_events"][0]["flagged_by"] == TEST_FLAG_ACTOR


def test_reference_code_uses_service_fallback_when_repo_reference_absent(tmp_path) -> None:
    entry = StrategyValidationEntry(
        strategy_key="deployment_validation",
        display_name="Deployment Validation",
        description="Two-green-minute deployment validation primitive.",
        validation_state="validated",
        deployable=True,
        audit_copy_ref="references/qc-shadow/DeploymentValidationAlgorithm.py",
        audit_copy_sha256="3dbb1c0f54254951828f3c74e4dc6e2f7c1bcc0784465c8532f109ea191c3af0",
    )

    code = reference_code_for_entry(entry, repo_root=tmp_path)

    assert code is not None
    assert code.path == "references/qc-shadow/DeploymentValidationAlgorithm.py"
    assert "class DeploymentValidationAlgorithm" in code.source
