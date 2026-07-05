from __future__ import annotations

import json

from app.schemas.strategy_validation import StrategyValidationEntry
from app.services.strategy_validation_manifest import (
    StrategyEvidenceSeed,
    StrategyRegistrySeed,
    load_strategy_validation_entries,
    reference_code_for_entry,
    seed_strategy_validation_manifest,
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

    entries = seed_strategy_validation_manifest(registry, evidence)

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

    unvalidated = entries[1]
    assert unvalidated.validation_state == "needs_validation"
    assert unvalidated.deployable is False
    assert unvalidated.qc_cloud_backtest_id is None
    assert unvalidated.diagnostics is None


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
