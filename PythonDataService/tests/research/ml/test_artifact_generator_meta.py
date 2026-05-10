from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.research.ml.artifact import (
    DeterministicRuleGenerator,
    PredictionSetManifest,
    QuantConnectPrecomputedFixtureGenerator,
)

# ----- regression: existing deterministic_rule manifests still load --


def _det_manifest_dict() -> dict:
    return {
        "schema_version": "1.0",
        "prediction_set_id": "pred_spy_rsi_rule_v001",
        "symbol": "SPY",
        "resolution_minutes": 15,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {
            "kind": "deterministic_rule",
            "rule_id": "rsi_14_centered",
            "rule_version": "1.0",
        },
        "chunks": [
            {
                "trained_through_ms": 1714521600000,
                "start_ms": 1714608000000,
                "end_ms": 1717199999000,
                "row_count": 173,
                "rows_hash": "0" * 64,
            }
        ],
        "prediction_set_hash": "0" * 64,
    }


def test_deterministic_rule_manifest_round_trips_through_union() -> None:
    m = PredictionSetManifest.model_validate(_det_manifest_dict())
    assert isinstance(m.generator, DeterministicRuleGenerator)
    assert m.generator.rule_id == "rsi_14_centered"


# ----- new variant: QC precomputed fixture --------------------------


def _qc_manifest_dict() -> dict:
    return _det_manifest_dict() | {
        "generator": {
            "kind": "quantconnect_precomputed_fixture",
            "qc_tutorial_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions",
            "qc_exported_at_ms": 1746880496000,
            "qc_calendar_window_start_ms": 1704153600000,
            "qc_calendar_window_end_ms": 1735603200000,
            "qc_symbol_filter": "SPY",
            "qc_dataset_id": "USEquity-Daily-v1",
            "qc_versions": {"sklearn": "1.5.0", "lean": "16000", "numpy": "1.26.4"},
            "qc_daily_anchor_tz": "America/New_York",
            "qc_daily_anchor_hhmm": "16:00",
        }
    }


def test_qc_fixture_manifest_round_trips() -> None:
    m = PredictionSetManifest.model_validate(_qc_manifest_dict())
    assert isinstance(m.generator, QuantConnectPrecomputedFixtureGenerator)
    assert m.generator.qc_symbol_filter == "SPY"
    assert m.generator.qc_versions["sklearn"] == "1.5.0"
    assert m.generator.qc_exported_at_ms == 1746880496000
    assert m.generator.qc_daily_anchor_hhmm == "16:00"


def test_qc_fixture_manifest_rejects_malformed_anchor_hhmm() -> None:
    bad = _qc_manifest_dict()
    bad["generator"]["qc_daily_anchor_hhmm"] = "4pm"
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)


def test_qc_fixture_manifest_rejects_string_exported_at() -> None:
    bad = _qc_manifest_dict()
    bad["generator"]["qc_exported_at_ms"] = "2026-05-10T12:34:56Z"
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)


def test_discriminator_rejects_cross_variant_fields_on_qc_kind() -> None:
    bad = _qc_manifest_dict()
    bad["generator"]["rule_id"] = "rsi_14_centered"
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)


def test_discriminator_rejects_cross_variant_fields_on_deterministic_kind() -> None:
    bad = _det_manifest_dict()
    bad["generator"]["qc_dataset_id"] = "USEquity-Daily-v1"
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)


def test_unknown_generator_kind_rejects() -> None:
    bad = _det_manifest_dict()
    bad["generator"] = {"kind": "mystery_generator"}
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)


def test_qc_versions_must_be_string_to_string() -> None:
    bad = _qc_manifest_dict()
    bad["generator"]["qc_versions"] = {"sklearn": 1.5}
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)
