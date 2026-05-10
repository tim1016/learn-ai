from __future__ import annotations

import json
from pathlib import Path

from app.research.ml.generators.quantconnect_fixture import import_qc_fixture

_RECORDS = [
    {"date": "2024-01-02", "prediction_by_symbol": {"SPY": 0.0123, "QQQ": -0.011}},
    {"date": "2024-01-03", "prediction_by_symbol": {"SPY": -0.0045, "QQQ": 0.022}},
    {"date": "2024-01-04", "prediction_by_symbol": {"SPY": 0.0007, "QQQ": 0.0012}},
]


_PROVENANCE = {
    "qc_tutorial_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions",
    "qc_exported_at_ms": 1746880496000,
    "qc_calendar_window_start_ms": 1704153600000,
    "qc_calendar_window_end_ms": 1735603200000,
    "qc_dataset_id": "USEquity-Daily-v1",
    "qc_versions": {"sklearn": "1.5.0", "lean": "16000", "numpy": "1.26.4"},
}


def _run_once(root: Path) -> tuple[str, bytes]:
    root.mkdir(parents=True, exist_ok=True)
    export_path = root / "qc_export.json"
    export_path.write_text(json.dumps(_RECORDS), encoding="utf-8")
    output = root / "artifacts" / "predictions"
    manifest = import_qc_fixture(
        qc_export_path=export_path,
        prediction_set_id="qc_det_test_v001",
        output_root=output,
        symbol="SPY",
        **_PROVENANCE,
    )
    manifest_bytes = (output / "qc_det_test_v001" / "manifest.json").read_bytes()
    return manifest.prediction_set_hash, manifest_bytes


def test_repeated_import_produces_identical_hash_and_manifest(tmp_path: Path) -> None:
    h1, m1 = _run_once(tmp_path / "run_a")
    h2, m2 = _run_once(tmp_path / "run_b")
    assert h1 == h2
    assert m1 == m2
