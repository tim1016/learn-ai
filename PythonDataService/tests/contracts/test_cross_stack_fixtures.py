"""Validate shared wire fixtures at their FastAPI-owned contract boundaries."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.broker.ibkr.models import DataPlaneHealth
from app.models.responses import SanitizedDataResponse
from app.routers.spec_strategy import SpecBacktestResponse

_FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text())


def test_aggregate_response_fixture_is_the_strict_python_to_dotnet_contract() -> None:
    response = SanitizedDataResponse.model_validate(_fixture("aggregate-response-v1.json"))

    assert response.data[0].timestamp == 1_704_153_600_000
    assert response.summary.removal_percentage == 0.0


def test_aggregate_response_rejects_a_noncanonical_timestamp_or_incomplete_summary() -> None:
    payload = _fixture("aggregate-response-v1.json")
    data = payload["data"]
    assert isinstance(data, list)
    first_bar = data[0]
    assert isinstance(first_bar, dict)
    first_bar["timestamp"] = "2024-01-02T00:00:00Z"

    with pytest.raises(ValidationError):
        SanitizedDataResponse.model_validate(payload)

    payload = _fixture("aggregate-response-v1.json")
    summary = payload["summary"]
    assert isinstance(summary, dict)
    del summary["removal_percentage"]

    with pytest.raises(ValidationError):
        SanitizedDataResponse.model_validate(payload)


def test_spec_strategy_response_fixture_preserves_int64_trade_timestamps() -> None:
    response = SpecBacktestResponse.model_validate(_fixture("spec-strategy-backtest-response-v1.json"))

    trade = response.trades[0]
    assert trade.entry_time == 1_704_153_600_000
    assert trade.exit_time == 1_704_157_200_000
    assert trade.indicators["ema_fast"] == 471.0


def test_data_plane_health_fixture_is_the_direct_fastapi_to_angular_contract() -> None:
    health = DataPlaneHealth.model_validate(_fixture("data-plane-health-v1.json"))

    assert health.service == "polygon-data-service"
    assert health.fetched_at_ms >= health.process_start_ms
