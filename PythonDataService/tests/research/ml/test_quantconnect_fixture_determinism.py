from __future__ import annotations

import json
from pathlib import Path

from app.research.ml.generators.quantconnect_fixture import import_qc_fixture


_EXPORT = {
    "tutorial_id": "precomputed-ml-predictions",
    "tutorial_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/machine-learning/precomputed-ml-predictions",
    "exported_at": "2026-05-10T12:34:56Z",
    "dataset_id": "USEquity-Daily-v1",
    "calendar_window": {"start": "2024-01-02", "end": "2024-12-31", "tz": "America/New_York"},
    "versions": {"sklearn": "1.5.0", "lean": "16000", "numpy": "1.26.4"},
    "predictions": [
        {"symbol": "SPY", "date": "2024-01-02T16:00:00-05:00", "prediction": 0.0123},
        {"symbol": "SPY", "date": "2024-01-03T16:00:00-05:00", "prediction": -0.0045},
        {"symbol": "SPY", "date": "2024-01-04T16:00:00-05:00", "prediction": 0.0007},
    ],
}


def _run_once(root: Path) -> tuple[str, bytes]:
    export_path = root / "qc_export.json"
    root.mkdir(parents=True, exist_ok=True)
    export_path.write_text(json.dumps(_EXPORT), encoding="utf-8")
    output = root / "artifacts" / "predictions"
    manifest = import_qc_fixture(
        qc_export_path=export_path,
        prediction_set_id="qc_det_test_v001",
        output_root=output,
        symbol="SPY",
    )
    manifest_bytes = (output / "qc_det_test_v001" / "manifest.json").read_bytes()
    return manifest.prediction_set_hash, manifest_bytes


def test_repeated_import_produces_identical_hash_and_manifest(tmp_path: Path) -> None:
    h1, m1 = _run_once(tmp_path / "run_a")
    h2, m2 = _run_once(tmp_path / "run_b")
    assert h1 == h2
    assert m1 == m2
