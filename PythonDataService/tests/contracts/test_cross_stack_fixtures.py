"""Validate shared wire fixtures at their FastAPI-owned contract boundaries."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.responses import SanitizedDataResponse
from app.routers.spec_strategy import SpecBacktestResponse

_FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text())


def test_aggregate_response_fixture_is_the_strict_python_to_angular_contract() -> None:
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


def test_signal_engine_job_result_fixture_is_what_the_job_serializer_writes() -> None:
    """The Angular runner's mapper reads this fixture; it must be the serializer's own output, field for field."""
    from app.routers.jobs import _serialize_signal_report
    from tests._helpers.stored_signal_report import a_busy_signal_report

    written = json.loads(json.dumps(_serialize_signal_report(a_busy_signal_report())))

    assert _fixture("signal-engine-job-result-v1.json") == written


def test_feature_study_bar_sizes_offered_by_the_page_are_the_ones_the_server_accepts() -> None:
    """The Feature page offers exactly this list (it imports the file); the study accepts exactly these minute bars."""
    from app.services.feature_study_service import SUPPORTED_BAR_MINUTES

    manifest = json.loads((_FIXTURES.parent / "feature-study-bar-minutes.json").read_text())

    assert manifest["canonical"] == "PythonDataService/app/services/feature_study_service.py::SUPPORTED_BAR_MINUTES"
    assert tuple(manifest["bar_minutes"]) == SUPPORTED_BAR_MINUTES
