from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
VALIDATOR_CODE_REF = "PythonDataService/app/lean_sidecar/trusted_samples/deployment_validation.py"
VALIDATOR_CODE_SHA256 = "validator-sha"
SETTINGS_FILE_REF = "PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json"


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
            validator_code_ref=VALIDATOR_CODE_REF,
            validator_code_sha256=VALIDATOR_CODE_SHA256,
            settings_file_ref=SETTINGS_FILE_REF,
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


def test_flag_events_ledger_path_resolves_outside_the_real_artifacts_tree() -> None:
    """#1739 regression: guards the autouse isolation fixture in
    tests/conftest.py (``_isolate_strategy_validation_flag_ledger``). If
    that fixture is ever removed, disabled, or stops patching the
    module-level default, a developer's real, gitignored
    artifacts/strategy_validation/flag_events.json resolves as the ledger
    for every test that reads it through the default path, and local runs
    silently diverge from CI (see PR #1733, where this happened).

    ``strategy_validation_manifest`` is imported module-qualified here
    (not the ``DEFAULT_FLAG_EVENTS_PATH`` name already imported at the top
    of this file) so the assertion reads the *live* value, honoring
    whatever tests/conftest.py's autouse fixture monkeypatched it to."""
    import app.services.strategy_validation_manifest as strategy_validation_manifest

    real_default_path = (
        Path(strategy_validation_manifest.__file__).resolve().parents[2]
        / "artifacts"
        / "strategy_validation"
        / "flag_events.json"
    )

    assert real_default_path != strategy_validation_manifest.DEFAULT_FLAG_EVENTS_PATH


def test_bare_load_strategy_validation_entries_call_tracks_the_patched_ledger_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1739 regression: app/services/broker_v2_panel/panel_data_source.py
    and app/services/strategy_validation_admission.py both call
    ``load_strategy_validation_entries(registry)`` with no explicit
    flag_events_path, relying on the function to resolve the omitted
    argument against whatever DEFAULT_FLAG_EVENTS_PATH currently is. A
    plain ``flag_events_path: Path = DEFAULT_FLAG_EVENTS_PATH`` default is
    bound once, at import time, so if load_strategy_validation_entries
    ever regresses to that form, this bare call keeps reading whatever
    DEFAULT_FLAG_EVENTS_PATH pointed to when the module was first
    imported -- ignoring tests/conftest.py's isolation fixture (and, in
    production, ignoring a hypothetical config reload) -- and this test
    fails."""
    import app.services.strategy_validation_manifest as strategy_validation_manifest

    registry = [
        StrategyRegistrySeed(
            strategy_key="sma_crossover",
            display_name="SMA Crossover",
            description="#1739 regression fixture: default-path tracking.",
        ),
    ]
    fake_ledger_path = tmp_path / "flag_events.json"
    append_strategy_validation_flag_event(
        "sma_crossover",
        StrategyValidationFlagRequest(
            flag="validated",
            reason="Regression fixture for #1739 default-path tracking.",
        ),
        registry,
        flag_events_path=fake_ledger_path,
        flagged_by=TEST_FLAG_ACTOR,
        now_ms=1_700_000_000_000,
    )
    monkeypatch.setattr(strategy_validation_manifest, "DEFAULT_FLAG_EVENTS_PATH", fake_ledger_path)

    (entry,) = load_strategy_validation_entries(registry)

    assert entry.validation_state == "validated"
    assert entry.current_flag_event is not None
    assert entry.current_flag_event.flag == "validated"


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
            validator_code_ref=VALIDATOR_CODE_REF,
            validator_code_sha256=VALIDATOR_CODE_SHA256,
            settings_file_ref=SETTINGS_FILE_REF,
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


def test_operational_harness_deployability_does_not_require_quantconnect_run() -> None:
    registry = [
        StrategyRegistrySeed(
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            description="Two-green-minute deployment validation primitive.",
            strategy_category="operational_validation_harness",
            has_signal_program=True,
            reference_summary="Internal deterministic replay corpus.",
        ),
    ]
    evidence = [
        StrategyEvidenceSeed(
            strategy_key="deployment_validation",
            validator_code_ref=VALIDATOR_CODE_REF,
            validator_code_sha256=VALIDATOR_CODE_SHA256,
            settings_file_ref=SETTINGS_FILE_REF,
            settings_file_sha256="spec-sha",
            qc_cloud_backtest_id=None,
            audit_copy_ref="references/qc-shadow/DeploymentValidationAlgorithm.py",
            audit_copy_sha256="audit-sha",
            reconciliation_ref="tests/fixtures/golden/deployment-validation/trace-corpus.json",
            validation_case_symbol="SPY",
            trades_matched=56,
            trades_validated=56,
            pnl_max_abs_diff="0.00",
            divergence_counts={},
            validator_code_verified=False,
            audit_copy_verified=False,
        ),
    ]
    accepted = _accepted_flag_event().model_copy(
        update={
            "evidence_snapshot": _accepted_flag_event().evidence_snapshot.model_copy(
                update={"qc_cloud_backtest_id": None}
            )
        }
    )

    [entry] = seed_strategy_validation_manifest(registry, evidence, [accepted])

    assert entry.strategy_category == "operational_validation_harness"
    assert entry.deployable is True
    assert entry.proof.state == "current"
    assert all(evidence.label != "Reference audit copy" for stage in entry.proof.stages for evidence in stage.evidence)
    reference_stage = next(stage for stage in entry.proof.stages if stage.stage_id == "reference_run")
    assert reference_stage.state == "not_applicable"


def test_seed_manifest_fails_closed_without_validator_binding() -> None:
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
            settings_file_ref=SETTINGS_FILE_REF,
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

    [entry] = seed_strategy_validation_manifest(registry, evidence, [_accepted_flag_event()])

    assert entry.validation_state == "validated"
    assert entry.deployable is False
    assert entry.diagnostics is not None
    assert "LEAN validator evidence is missing" in " ".join(entry.diagnostics.notes)


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
            validator_code_ref=VALIDATOR_CODE_REF,
            validator_code_sha256=VALIDATOR_CODE_SHA256,
            settings_file_ref=SETTINGS_FILE_REF,
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
            settings_file_ref=SETTINGS_FILE_REF,
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
            settings_file_ref=SETTINGS_FILE_REF,
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
            validator_code_ref=VALIDATOR_CODE_REF,
            validator_code_sha256=VALIDATOR_CODE_SHA256,
            settings_file_ref=SETTINGS_FILE_REF,
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
    assert "validated settings hash no longer matches" in " ".join(entry.diagnostics.notes)


def test_load_manifest_fails_closed_when_audit_copy_hash_mismatches(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    settings_ref = "PythonDataService/app/engine/strategy/spec/fixtures/test.spec.json"
    settings_path = repo_root / settings_ref
    settings_path.parent.mkdir(parents=True)
    settings_payload = b'{"name":"deployment_validation"}'
    settings_path.write_bytes(settings_payload)
    validator_ref = "PythonDataService/app/lean_sidecar/trusted_samples/test_validator.py"
    validator_path = repo_root / validator_ref
    validator_path.parent.mkdir(parents=True)
    validator_payload = b"class MyAlgorithm: pass\n"
    validator_path.write_bytes(validator_payload)
    audit_ref = "references/qc-shadow/DeploymentValidationAlgorithm.py"
    audit_path = repo_root / audit_ref
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text("modified audit copy", encoding="utf-8")
    manifest_path = tmp_path / "strategy_validation_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "validated_strategies": [
                    {
                        "strategy_key": "deployment_validation",
                        "validator_code_ref": validator_ref,
                        "validator_code_sha256": hashlib.sha256(validator_payload).hexdigest(),
                        "settings_file_ref": settings_ref,
                        "settings_file_sha256": hashlib.sha256(settings_payload).hexdigest(),
                        "qc_cloud_backtest_id": "bt-1",
                        "audit_copy_ref": audit_ref,
                        "audit_copy_sha256": "0" * 64,
                        "reconciliation_ref": "references/qc-shadow/backtests/attribution.md",
                        "validation_case_symbol": "SPY",
                        "reconciliation_status": "passed",
                        "diagnostics": {
                            "verdict": "passed",
                            "trades_matched": 1,
                            "trades_validated": 1,
                            "pnl_max_abs_diff": "0.00",
                            "divergence_counts": {},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    [entry] = load_strategy_validation_entries(
        [
            StrategyRegistrySeed(
                strategy_key="deployment_validation",
                display_name="Deployment Validation",
                description="Two-green-minute deployment validation primitive.",
            )
        ],
        manifest_path=manifest_path,
        repo_root=repo_root,
    )

    assert entry.deployable is False
    assert entry.diagnostics is not None
    assert "audit copy no longer matches" in " ".join(entry.diagnostics.notes)
    code = reference_code_for_entry(entry, repo_root=repo_root)
    assert code is not None
    assert code.state == "stale"
    assert code.recorded_sha256 == "0" * 64
    assert code.sha256 == hashlib.sha256(b"modified audit copy").hexdigest()
    assert code.source == "modified audit copy"


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


def test_load_manifest_keeps_legacy_snapshot_hashes_verifiable(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = tmp_path / "strategy_validation_manifest.json"
    snapshot = StrategyEvidenceSnapshot(
        settings_file_ref=SETTINGS_FILE_REF,
        settings_file_sha256="spec-sha",
        qc_cloud_backtest_id="d2fe45a7142e88575f6fbd75229f8681",
        audit_copy_ref="references/qc-shadow/DeploymentValidationAlgorithm.py",
        audit_copy_sha256="audit-sha",
        reconciliation_ref="references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md",
        validation_case_symbol="SPY",
        reconciliation_status="passed",
    )
    legacy_payload = snapshot.model_dump()
    legacy_payload.pop("validator_code_ref", None)
    legacy_payload.pop("validator_code_sha256", None)
    legacy_hash = hashlib.sha256(
        json.dumps(
            legacy_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    event = (
        _accepted_flag_event()
        .model_copy(
            update={
                "evidence_snapshot": snapshot,
                "evidence_snapshot_sha256": legacy_hash,
            }
        )
        .model_dump()
    )

    manifest_path.write_text(
        json.dumps(
            {
                "validated_strategies": [],
                "seed_flag_events": [event],
            },
        ),
        encoding="utf-8",
    )

    entries = load_strategy_validation_entries(
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

    assert entries[0].validation_state == "validated"


def test_append_flag_event_derives_actor_and_snapshots_evidence(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    settings_ref = "PythonDataService/app/engine/strategy/spec/fixtures/test.spec.json"
    settings_path = repo_root / settings_ref
    settings_path.parent.mkdir(parents=True)
    settings_payload = b'{"name":"deployment_validation"}'
    settings_path.write_bytes(settings_payload)
    settings_sha = hashlib.sha256(settings_payload).hexdigest()
    validator_ref = "PythonDataService/app/lean_sidecar/trusted_samples/test_validator.py"
    validator_path = repo_root / validator_ref
    validator_path.parent.mkdir(parents=True)
    validator_payload = b"class MyAlgorithm: pass\n"
    validator_path.write_bytes(validator_payload)
    validator_sha = hashlib.sha256(validator_payload).hexdigest()
    audit_ref = "references/qc-shadow/DeploymentValidationAlgorithm.py"
    audit_path = repo_root / audit_ref
    audit_path.parent.mkdir(parents=True)
    audit_payload = b"class DeploymentValidationAlgorithm: pass\n"
    audit_path.write_bytes(audit_payload)
    audit_sha = hashlib.sha256(audit_payload).hexdigest()
    manifest_path = tmp_path / "strategy_validation_manifest.json"
    flag_events_path = tmp_path / "flag_events.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "validated_strategies": [
                    {
                        "strategy_key": "deployment_validation",
                        "validator_code_ref": validator_ref,
                        "validator_code_sha256": validator_sha,
                        "settings_file_ref": settings_ref,
                        "settings_file_sha256": settings_sha,
                        "qc_cloud_backtest_id": "bt-1",
                        "audit_copy_ref": audit_ref,
                        "audit_copy_sha256": audit_sha,
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
        StrategyValidationFlagRequest(
            flag="validated",
            reason="Operator accepted this evidence.",
            qc_cloud_backtest_id="bt-operator-accepted",
        ),
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
    assert entry.current_flag_event.evidence_snapshot.validator_code_sha256 == validator_sha
    assert entry.current_flag_event.evidence_snapshot.settings_file_sha256 == settings_sha
    assert entry.current_flag_event.evidence_snapshot.qc_cloud_backtest_id == "bt-operator-accepted"
    assert entry.qc_cloud_backtest_id == "bt-operator-accepted"
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
        # #1672 changed the audit copy's session-boundary literals (see
        # docs/references/deployment-validation-consecutive-green.md); this
        # pins the current file's hash, not the manifest's — the manifest's
        # pinned hash is deliberately left stale until a fresh QC Cloud
        # reconciliation is run (see tests/routers/test_strategy_validation.py).
        audit_copy_sha256="64f7293e351be0469a3cd76df1d5a57806cf4cab25c4d2f1737ddbb9b35286a4",
    )

    code = reference_code_for_entry(entry, repo_root=tmp_path)

    assert code is not None
    assert code.path == "references/qc-shadow/DeploymentValidationAlgorithm.py"
    assert "class DeploymentValidationAlgorithm" in code.source


def test_qc_shadow_container_fallback_copies_are_byte_identical_to_references() -> None:
    """The containerized data plane can't mount references/ (see
    docs/archive/plans/live-control-data-plane-topology-investigation-prd.md),
    so reference_code_for_entry falls back to app/data/qc-shadow/ whenever
    references/qc-shadow/ is absent. ruff.toml documents the intent that the
    two stay byte-identical ("Reference artifacts must stay byte-identical to
    the uploaded QC source") but nothing previously enforced it — a future
    edit to one copy without the other would silently drift and only surface
    as a container-only production bug."""
    repo_root = Path(__file__).resolve().parents[3]
    references_dir = repo_root / "references" / "qc-shadow"
    fallback_dir = repo_root / "PythonDataService" / "app" / "data" / "qc-shadow"

    reference_files = sorted(p.name for p in references_dir.glob("*.py"))
    assert reference_files, "expected at least one committed QC audit copy"
    for name in reference_files:
        assert (fallback_dir / name).read_bytes() == (references_dir / name).read_bytes(), (
            f"{name} has drifted between references/qc-shadow and the container fallback copy"
        )


def test_ema_reference_code_uses_service_fallback_when_repo_reference_absent(tmp_path) -> None:
    entry = StrategyValidationEntry(
        strategy_key="ema_crossover_signal",
        display_name="EMA Crossover Signal",
        description="Canonical SPY EMA crossover signal.",
        validation_state="needs_validation",
        deployable=False,
        audit_copy_ref="references/qc-shadow/SpyEmaCrossoverAlgorithm.py",
        audit_copy_sha256="cfc7f18877b8dcf9b99af4bb26e4f36f0b7ac6799fa5f4d6dc286945653d6078",
    )

    code = reference_code_for_entry(entry, repo_root=tmp_path)

    assert code is not None
    assert code.path == "references/qc-shadow/SpyEmaCrossoverAlgorithm.py"
    assert "class SpyEmaCrossoverAlgorithm" in code.source
