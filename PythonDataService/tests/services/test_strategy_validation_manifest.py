from __future__ import annotations

from app.services.strategy_validation_manifest import (
    StrategyEvidenceSeed,
    StrategyRegistrySeed,
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
