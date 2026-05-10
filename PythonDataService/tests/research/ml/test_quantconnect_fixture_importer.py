from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.ml.artifact import (
    PredictionSetManifest,
    QuantConnectPrecomputedFixtureGenerator,
)
from app.research.ml.generators.quantconnect_fixture import QcExport, import_qc_fixture
from app.research.ml.loader import PredictionSet


def _good_export() -> dict:
    return {
        "tutorial_id": "precomputed-ml-predictions",
        "tutorial_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/machine-learning/precomputed-ml-predictions",
        "exported_at": "2026-05-10T12:34:56Z",
        "dataset_id": "USEquity-Daily-v1",
        "calendar_window": {
            "start": "2024-01-02",
            "end": "2024-12-31",
            "tz": "America/New_York",
        },
        "versions": {"sklearn": "1.5.0", "lean": "16000", "numpy": "1.26.4"},
        "predictions": [
            {"symbol": "SPY", "date": "2024-01-02T16:00:00-05:00", "prediction": 0.0123},
            {"symbol": "SPY", "date": "2024-01-03T16:00:00-05:00", "prediction": -0.0045},
        ],
    }


def test_qc_export_round_trips() -> None:
    e = QcExport.model_validate(_good_export())
    assert e.dataset_id == "USEquity-Daily-v1"
    assert len(e.predictions) == 2
    assert e.predictions[0].symbol == "SPY"


def test_qc_export_rejects_extra_top_level_field() -> None:
    bad = _good_export() | {"surprise": 42}
    with pytest.raises(ValidationError):
        QcExport.model_validate(bad)


def test_qc_export_rejects_extra_field_in_prediction_row() -> None:
    bad = _good_export()
    bad["predictions"][0] = bad["predictions"][0] | {"confidence": 0.9}
    with pytest.raises(ValidationError):
        QcExport.model_validate(bad)


def test_qc_export_requires_at_least_one_prediction() -> None:
    bad = _good_export() | {"predictions": []}
    with pytest.raises(ValidationError):
        QcExport.model_validate(bad)


# ----- importer: happy path + validation rails ----------------------


def _write_export(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "qc_export.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_import_qc_fixture_happy_path(tmp_path: Path) -> None:
    export_path = _write_export(tmp_path, _good_export())
    output_root = tmp_path / "artifacts" / "predictions"

    manifest = import_qc_fixture(
        qc_export_path=export_path,
        prediction_set_id="qc_spy_precomputed_v001",
        output_root=output_root,
        symbol="SPY",
    )

    assert isinstance(manifest, PredictionSetManifest)
    assert isinstance(manifest.generator, QuantConnectPrecomputedFixtureGenerator)
    assert manifest.generator.qc_symbol_filter == "SPY"
    assert manifest.symbol == "SPY"
    assert len(manifest.chunks) == 1
    assert manifest.chunks[0].row_count == 2

    pset = PredictionSet.load(output_root / "qc_spy_precomputed_v001")
    assert len(pset.index) == 2


def test_import_rejects_absent_symbol(tmp_path: Path) -> None:
    export_path = _write_export(tmp_path, _good_export())
    with pytest.raises(ValueError, match="absent from QC export"):
        import_qc_fixture(
            qc_export_path=export_path,
            prediction_set_id="qc_qqq_v001",
            output_root=tmp_path / "artifacts" / "predictions",
            symbol="QQQ",
        )


def test_import_rejects_duplicate_date_for_symbol(tmp_path: Path) -> None:
    payload = _good_export()
    payload["predictions"].append(
        {"symbol": "SPY", "date": "2024-01-02T16:00:00-05:00", "prediction": 0.999}
    )
    export_path = _write_export(tmp_path, payload)
    with pytest.raises(ValueError, match="duplicate timestamp"):
        import_qc_fixture(
            qc_export_path=export_path,
            prediction_set_id="qc_spy_v001",
            output_root=tmp_path / "artifacts" / "predictions",
            symbol="SPY",
        )


def test_import_rejects_naive_date_string(tmp_path: Path) -> None:
    payload = _good_export()
    payload["predictions"][0]["date"] = "2024-01-02 16:00:00"
    export_path = _write_export(tmp_path, payload)
    with pytest.raises(ValueError, match="naive timestamp"):
        import_qc_fixture(
            qc_export_path=export_path,
            prediction_set_id="qc_spy_v001",
            output_root=tmp_path / "artifacts" / "predictions",
            symbol="SPY",
        )


def test_import_rejects_path_unsafe_prediction_set_id(tmp_path: Path) -> None:
    export_path = _write_export(tmp_path, _good_export())
    with pytest.raises(ValueError, match="path-safe"):
        import_qc_fixture(
            qc_export_path=export_path,
            prediction_set_id="../evil",
            output_root=tmp_path / "artifacts" / "predictions",
            symbol="SPY",
        )


def test_import_filters_to_one_symbol_in_multi_symbol_export(tmp_path: Path) -> None:
    payload = _good_export()
    payload["predictions"].extend(
        [
            {"symbol": "QQQ", "date": "2024-01-02T16:00:00-05:00", "prediction": 0.5},
            {"symbol": "QQQ", "date": "2024-01-03T16:00:00-05:00", "prediction": 0.6},
        ]
    )
    export_path = _write_export(tmp_path, payload)

    manifest = import_qc_fixture(
        qc_export_path=export_path,
        prediction_set_id="qc_spy_v001",
        output_root=tmp_path / "artifacts" / "predictions",
        symbol="SPY",
    )

    assert manifest.chunks[0].row_count == 2
    assert manifest.symbol == "SPY"
