from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_strategy_validation_catalog_and_detail_expose_manifest() -> None:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        catalog_response = await client.get("/api/strategy-validation/strategies")
        detail_response = await client.get("/api/strategy-validation/strategies/deployment_validation")

    assert catalog_response.status_code == 200, catalog_response.text
    catalog = catalog_response.json()
    strategies = {row["strategy_key"]: row for row in catalog["strategies"]}
    assert "deployment_validation" in strategies
    assert "spy_orb" in strategies
    assert strategies["deployment_validation"]["validation_state"] == "validated"
    assert strategies["deployment_validation"]["deployable"] is True
    assert strategies["spy_orb"]["validation_state"] == "needs_validation"
    assert strategies["spy_orb"]["deployable"] is False

    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["strategy_key"] == "deployment_validation"
    assert detail["qc_cloud_backtest_id"] == "d2fe45a7142e88575f6fbd75229f8681"
    assert detail["validation_case_symbol"] == "SPY"
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
