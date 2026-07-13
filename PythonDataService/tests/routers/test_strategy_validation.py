from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_strategy_validation_catalog_and_detail_expose_manifest(tmp_path) -> None:
    from app.main import app
    from app.routers.strategy_validation import get_strategy_validation_flag_events_path

    app.dependency_overrides[get_strategy_validation_flag_events_path] = lambda: tmp_path / "flag_events.json"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            catalog_response = await client.get("/api/strategy-validation/strategies")
            detail_response = await client.get("/api/strategy-validation/strategies/deployment_validation")
    finally:
        app.dependency_overrides.pop(get_strategy_validation_flag_events_path, None)

    assert catalog_response.status_code == 200, catalog_response.text
    catalog = catalog_response.json()
    strategies = {row["strategy_key"]: row for row in catalog["strategies"]}
    assert "deployment_validation" in strategies
    assert "spy_orb" in strategies
    assert strategies["deployment_validation"]["validation_state"] == "validated"
    assert strategies["deployment_validation"]["deployable"] is True
    assert (
        strategies["deployment_validation"]["current_flag_event"]["flagged_by"]
        == "migration:strategy-validation-prd-seed"
    )
    assert (
        strategies["deployment_validation"]["behavioral_equivalence"]["verdict"]
        == "accepted_for_deploy"
    )
    assert strategies["spy_orb"]["validation_state"] == "needs_validation"
    assert strategies["spy_orb"]["deployable"] is False

    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["strategy_key"] == "deployment_validation"
    assert detail["qc_cloud_backtest_id"] == "d2fe45a7142e88575f6fbd75229f8681"
    assert detail["validation_case_symbol"] == "SPY"
    assert detail["validator_code_ref"].endswith("lean_sidecar/trusted_samples/deployment_validation.py")
    assert detail["settings_file_ref"].endswith("deployment_validation.spec.json")
    assert detail["audit_copy_ref"] == "references/qc-shadow/DeploymentValidationAlgorithm.py"
    assert detail["diagnostics"]["trades_matched"] == 56
    assert detail["diagnostics"]["divergence_counts"] == {}
    assert detail["reference_code"]["path"] == "references/qc-shadow/DeploymentValidationAlgorithm.py"
    assert "class DeploymentValidationAlgorithm" in detail["reference_code"]["source"]
    assert "DeploymentValidationConsecutiveGreen" not in detail["reference_code"]["source"]


@pytest.mark.asyncio
async def test_strategy_validation_detail_404s_unknown_strategy() -> None:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/strategy-validation/strategies/not_real")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_strategy_validation_flag_write_is_guarded_and_appends_server_event(
    tmp_path,
    monkeypatch,
) -> None:
    from app.config import settings
    from app.main import app
    from app.routers.strategy_validation import (
        get_strategy_validation_actor,
        get_strategy_validation_flag_events_path,
        get_strategy_validation_manifest_path,
    )
    from app.security.data_plane_control import CONTROL_SECRET_HEADER
    from app.services.strategy_validation_manifest import DEFAULT_MANIFEST_PATH

    manifest_path = tmp_path / "strategy_validation_manifest.json"
    flag_events_path = tmp_path / "flag_events.json"
    manifest_path.write_text(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app.dependency_overrides[get_strategy_validation_manifest_path] = lambda: manifest_path
    app.dependency_overrides[get_strategy_validation_flag_events_path] = lambda: flag_events_path
    app.dependency_overrides[get_strategy_validation_actor] = lambda: "local:test-operator"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            missing_secret = await client.post(
                "/api/strategy-validation/strategies/deployment_validation/flag",
                json={"flag": "invalidated", "reason": "Reject the seeded evidence."},
            )
            accepted = await client.post(
                "/api/strategy-validation/strategies/deployment_validation/flag",
                headers={CONTROL_SECRET_HEADER: "test-control-secret"},
                json={"flag": "invalidated", "reason": "Reject the seeded evidence."},
            )
            refresh_result = await client.post(
                "/api/strategy-validation/strategies/deployment_validation/refresh",
                headers={CONTROL_SECRET_HEADER: "test-control-secret"},
            )
    finally:
        app.dependency_overrides.pop(get_strategy_validation_manifest_path, None)
        app.dependency_overrides.pop(get_strategy_validation_flag_events_path, None)
        app.dependency_overrides.pop(get_strategy_validation_actor, None)

    assert missing_secret.status_code == 403
    assert accepted.status_code == 200, accepted.text
    detail = accepted.json()
    assert detail["validation_state"] == "needs_validation"
    assert detail["deployable"] is False
    assert detail["current_flag_event"]["flagged_by"] == "local:test-operator"
    assert detail["current_flag_event"]["reason"] == "Reject the seeded evidence."
    assert detail["current_flag_event"]["behavioral_equivalence"]["verdict"] == "rejected"
    assert refresh_result.status_code == 200, refresh_result.text
    assert refresh_result.json()["detail"]["strategy_key"] == "deployment_validation"
    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "flag_events" not in manifest_raw
    ledger_raw = json.loads(flag_events_path.read_text(encoding="utf-8"))
    assert ledger_raw["flag_events"][-1]["flagged_by"] == "local:test-operator"
