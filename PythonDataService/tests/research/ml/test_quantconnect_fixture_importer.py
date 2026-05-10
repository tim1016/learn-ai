from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.ml.artifact import (
    PredictionSetManifest,
    QuantConnectPrecomputedFixtureGenerator,
)
from app.research.ml.generators.quantconnect_fixture import (
    QcExport,
    QcPredictionRecord,
    import_qc_fixture,
)
from app.research.ml.loader import PredictionSet


def _good_export_records() -> list[dict]:
    """QC's documented shape: bare list of {date, prediction_by_symbol}."""
    return [
        {"date": "2024-01-02", "prediction_by_symbol": {"SPY": 0.0123, "QQQ": -0.011}},
        {"date": "2024-01-03", "prediction_by_symbol": {"SPY": -0.0045, "QQQ": 0.022}},
        {"date": "2024-01-04", "prediction_by_symbol": {"SPY": 0.0007}},
    ]


def _provenance_kwargs() -> dict:
    """Provenance fields the caller supplies (not in QC's emitted file)."""
    return {
        "qc_tutorial_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions",
        "qc_exported_at_ms": 1746880496000,
        "qc_calendar_window_start_ms": 1704153600000,
        "qc_calendar_window_end_ms": 1735603200000,
        "qc_dataset_id": "USEquity-Daily-v1",
        "qc_versions": {"sklearn": "1.5.0", "lean": "16000", "numpy": "1.26.4"},
    }


def _write_export(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "qc_export.json"
    p.write_text(json.dumps(records), encoding="utf-8")
    return p


# ----- QcExport closed shape (bare list) -----------------------------


def test_qc_export_round_trips_as_bare_list() -> None:
    e = QcExport.model_validate(_good_export_records())
    assert len(e.root) == 3
    assert e.root[0].date == "2024-01-02"
    assert e.root[0].prediction_by_symbol == {"SPY": 0.0123, "QQQ": -0.011}


def test_qc_export_rejects_envelope_object() -> None:
    """QC's emitted file is a list, not an object. Reject envelope shapes."""
    bad = {"predictions": _good_export_records()}
    with pytest.raises(ValidationError):
        QcExport.model_validate(bad)


def test_qc_prediction_record_rejects_extra_field() -> None:
    bad = {
        "date": "2024-01-02",
        "prediction_by_symbol": {"SPY": 0.0123},
        "confidence": 0.9,
    }
    with pytest.raises(ValidationError):
        QcPredictionRecord.model_validate(bad)


def test_qc_prediction_record_requires_at_least_one_symbol() -> None:
    bad = {"date": "2024-01-02", "prediction_by_symbol": {}}
    with pytest.raises(ValidationError):
        QcPredictionRecord.model_validate(bad)


# ----- importer: happy path + validation rails ----------------------


def test_import_qc_fixture_happy_path(tmp_path: Path) -> None:
    export_path = _write_export(tmp_path, _good_export_records())
    output_root = tmp_path / "artifacts" / "predictions"

    manifest = import_qc_fixture(
        qc_export_path=export_path,
        prediction_set_id="qc_spy_precomputed_v001",
        output_root=output_root,
        symbol="SPY",
        **_provenance_kwargs(),
    )

    assert isinstance(manifest, PredictionSetManifest)
    assert isinstance(manifest.generator, QuantConnectPrecomputedFixtureGenerator)
    assert manifest.generator.qc_symbol_filter == "SPY"
    assert manifest.generator.qc_exported_at_ms == 1746880496000
    assert manifest.generator.qc_daily_anchor_tz == "America/New_York"
    assert manifest.generator.qc_daily_anchor_hhmm == "16:00"
    assert manifest.symbol == "SPY"
    assert len(manifest.chunks) == 1
    assert manifest.chunks[0].row_count == 3

    pset = PredictionSet.load(output_root / "qc_spy_precomputed_v001")
    assert len(pset.index) == 3


def test_import_skips_days_without_symbol_in_map(tmp_path: Path) -> None:
    """QC may omit a symbol on days the universe filter excludes it.
    Those records are skipped, not errors."""
    records = _good_export_records()
    export_path = _write_export(tmp_path, records)
    manifest = import_qc_fixture(
        qc_export_path=export_path,
        prediction_set_id="qc_qqq_v001",
        output_root=tmp_path / "artifacts" / "predictions",
        symbol="QQQ",
        **_provenance_kwargs(),
    )
    # Only 2 of the 3 records have QQQ.
    assert manifest.chunks[0].row_count == 2


def test_import_rejects_symbol_absent_from_every_record(tmp_path: Path) -> None:
    export_path = _write_export(tmp_path, _good_export_records())
    with pytest.raises(ValueError, match="absent from QC export"):
        import_qc_fixture(
            qc_export_path=export_path,
            prediction_set_id="qc_iwm_v001",
            output_root=tmp_path / "artifacts" / "predictions",
            symbol="IWM",
            **_provenance_kwargs(),
        )


def test_import_rejects_duplicate_date(tmp_path: Path) -> None:
    records = _good_export_records()
    records.append({"date": "2024-01-02", "prediction_by_symbol": {"SPY": 0.999}})
    export_path = _write_export(tmp_path, records)
    with pytest.raises(ValueError, match="duplicate timestamp"):
        import_qc_fixture(
            qc_export_path=export_path,
            prediction_set_id="qc_spy_v001",
            output_root=tmp_path / "artifacts" / "predictions",
            symbol="SPY",
            **_provenance_kwargs(),
        )


def test_import_rejects_tz_aware_date_string(tmp_path: Path) -> None:
    """The date-only conversion path expects 'YYYY-MM-DD' strictly. A
    tz-aware ISO 8601 string is ambiguous (which tz applies — embedded or
    anchor?) and must be rewritten by the caller."""
    records = _good_export_records()
    records[0]["date"] = "2024-01-02T16:00:00-05:00"
    export_path = _write_export(tmp_path, records)
    with pytest.raises(ValueError, match="date-only"):
        import_qc_fixture(
            qc_export_path=export_path,
            prediction_set_id="qc_spy_v001",
            output_root=tmp_path / "artifacts" / "predictions",
            symbol="SPY",
            **_provenance_kwargs(),
        )


def test_import_rejects_path_unsafe_prediction_set_id(tmp_path: Path) -> None:
    export_path = _write_export(tmp_path, _good_export_records())
    with pytest.raises(ValueError, match="path-safe"):
        import_qc_fixture(
            qc_export_path=export_path,
            prediction_set_id="../evil",
            output_root=tmp_path / "artifacts" / "predictions",
            symbol="SPY",
            **_provenance_kwargs(),
        )


def test_import_anchor_time_lands_at_correct_int64_ms(tmp_path: Path) -> None:
    """Sanity check: 2024-01-02 16:00 America/New_York is 2024-01-02 21:00 UTC.
    21:00 UTC on 2024-01-02 = 1704229200000 ms (DST: NY is UTC-5 in January)."""
    records = [{"date": "2024-01-02", "prediction_by_symbol": {"SPY": 0.0}}]
    export_path = _write_export(tmp_path, records)
    import_qc_fixture(
        qc_export_path=export_path,
        prediction_set_id="qc_anchor_test",
        output_root=tmp_path / "artifacts" / "predictions",
        symbol="SPY",
        **_provenance_kwargs(),
    )
    pset = PredictionSet.load(tmp_path / "artifacts" / "predictions" / "qc_anchor_test")
    assert 1704229200000 in pset.index
