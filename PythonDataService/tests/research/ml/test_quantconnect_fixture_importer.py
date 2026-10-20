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


def test_qc_export_round_trips_as_bare_list_and_rejects_an_envelope_object() -> None:
    e = QcExport.model_validate(_good_export_records())
    assert len(e.root) == 3, "the export holds its three records"
    assert e.root[0].date == "2024-01-02", "a record keeps its date"
    assert e.root[0].prediction_by_symbol == {"SPY": 0.0123, "QQQ": -0.011}, "a record keeps its predictions by symbol"

    # QC's emitted file is a list, not an object. Reject envelope shapes.
    bad = {"predictions": _good_export_records()}
    with pytest.raises(ValidationError):
        QcExport.model_validate(bad)


@pytest.mark.parametrize(
    ("symbol_predictions", "extra_fields"),
    [
        pytest.param((("SPY", 0.0123),), (("confidence", 0.9),), id="rejects_extra_field"),
        pytest.param((), (), id="requires_at_least_one_symbol"),
    ],
)
def test_qc_prediction_record_rejects_invalid(symbol_predictions: tuple, extra_fields: tuple) -> None:
    bad = {"date": "2024-01-02", "prediction_by_symbol": dict(symbol_predictions)} | dict(extra_fields)
    with pytest.raises(ValidationError):
        QcPredictionRecord.model_validate(bad)


# ----- importer: happy path + validation rails ----------------------


def test_import_qc_fixture_happy_path_skips_days_without_the_symbol_and_anchors_each_day_at_int64_ms(tmp_path: Path) -> None:
    export_path = _write_export(tmp_path, _good_export_records())
    output_root = tmp_path / "artifacts" / "predictions"

    manifest = import_qc_fixture(
        qc_export_path=export_path,
        prediction_set_id="qc_spy_precomputed_v001",
        output_root=output_root,
        symbol="SPY",
        **_provenance_kwargs(),
    )

    assert isinstance(manifest, PredictionSetManifest), "the import returns a prediction-set manifest"
    assert isinstance(manifest.generator, QuantConnectPrecomputedFixtureGenerator), "the generator is the QC precomputed fixture"
    assert manifest.generator.qc_symbol_filter == "SPY", "the generator keeps the symbol filter"
    assert manifest.generator.qc_exported_at_ms == 1746880496000, "the generator keeps the export time"
    assert manifest.generator.qc_daily_anchor_tz == "America/New_York", "the daily anchor is in New York"
    assert manifest.generator.qc_daily_anchor_hhmm == "16:00", "the daily anchor is the 16:00 close"
    assert manifest.symbol == "SPY", "the manifest names the symbol"
    assert len(manifest.chunks) == 1, "the import writes one chunk"
    assert manifest.chunks[0].row_count == 3, "the chunk holds the three days"

    pset = PredictionSet.load(output_root / "qc_spy_precomputed_v001")
    assert len(pset.index) == 3, "the written set loads with the three days"

    # QC may omit a symbol on days the universe filter excludes it. Those records are skipped, not errors.
    qqq = import_qc_fixture(
        qc_export_path=export_path,
        prediction_set_id="qc_qqq_v001",
        output_root=tmp_path / "artifacts" / "predictions",
        symbol="QQQ",
        **_provenance_kwargs(),
    )
    # Only 2 of the 3 records have QQQ.
    assert qqq.chunks[0].row_count == 2, "days without the symbol in the map are skipped"

    # Sanity check: 2024-01-02 16:00 America/New_York is 2024-01-02 21:00 UTC.
    # 21:00 UTC on 2024-01-02 = 1704229200000 ms (DST: NY is UTC-5 in January).
    anchor_path = _write_export(tmp_path, [{"date": "2024-01-02", "prediction_by_symbol": {"SPY": 0.0}}])
    import_qc_fixture(
        qc_export_path=anchor_path,
        prediction_set_id="qc_anchor_test",
        output_root=tmp_path / "artifacts" / "predictions",
        symbol="SPY",
        **_provenance_kwargs(),
    )
    anchored = PredictionSet.load(tmp_path / "artifacts" / "predictions" / "qc_anchor_test")
    assert 1704229200000 in anchored.index, "an anchor of 16:00 New York lands at the right int64 ms"


def test_import_rejects_a_duplicate_date_and_a_tz_aware_date_string(tmp_path: Path) -> None:
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

    # The date-only conversion path expects 'YYYY-MM-DD' strictly. A tz-aware ISO 8601 string is ambiguous (which tz
    # applies — embedded or anchor?) and must be rewritten by the caller.
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


@pytest.mark.parametrize(
    ("prediction_set_id", "symbol", "match"),
    [
        pytest.param("qc_iwm_v001", "IWM", "absent from QC export", id="rejects_symbol_absent_from_every_record"),
        pytest.param("../evil", "SPY", "path-safe", id="rejects_path_unsafe_prediction_set_id"),
    ],
)
def test_import_rejects_invalid_symbol_or_id(tmp_path: Path, prediction_set_id: str, symbol: str, match: str) -> None:
    export_path = _write_export(tmp_path, _good_export_records())
    with pytest.raises(ValueError, match=match):
        import_qc_fixture(
            qc_export_path=export_path,
            prediction_set_id=prediction_set_id,
            output_root=tmp_path / "artifacts" / "predictions",
            symbol=symbol,
            **_provenance_kwargs(),
        )


