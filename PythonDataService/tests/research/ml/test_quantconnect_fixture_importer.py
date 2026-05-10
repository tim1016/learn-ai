from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.research.ml.generators.quantconnect_fixture import QcExport


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
