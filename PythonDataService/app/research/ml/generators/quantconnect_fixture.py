"""Import a QuantConnect precomputed-predictions tutorial export into the
v0.5 prediction-set artifact format.

Phase 1 §A scope: schema validation, symbol filter, timestamp conversion,
manifest + chunk write. Real-fixture parity tests are gated on §B (QC
Cloud capture) — see
``docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md``.

Wire and storage format for timestamps is ``int64 ms UTC``. The QC export's
raw date strings cross the ingestion boundary in this module and are
converted on the spot; no string timestamps escape downstream.

The ``QcExport`` shape used here is a strawman for the QC export's actual
schema (spec R1). Once §B captures a real export, this model is verified
or adjusted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from app.research.ml.artifact import (
    ChunkRef,
    PredictionSetManifest,
    QuantConnectPrecomputedFixtureGenerator,
    compute_prediction_set_hash,
    compute_rows_hash,
    is_path_safe_id,
    write_chunk_rows,
)


class QcCalendarWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str
    tz: str


class QcPredictionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    date: str
    prediction: float


class QcExport(BaseModel):
    """Closed model of the captured ``qc_export.json``.

    Strawman shape; §B fixture capture confirms the real schema.
    """

    model_config = ConfigDict(extra="forbid")
    tutorial_id: str
    tutorial_url: str
    exported_at: str
    dataset_id: str
    calendar_window: QcCalendarWindow
    versions: dict[str, str]
    predictions: list[QcPredictionRow] = Field(min_length=1)


def _to_ms_utc(date_str: str) -> int:
    """Convert a tz-aware ISO 8601 date string to int64 ms UTC.

    Naive strings (no tz designator, no offset) are rejected per
    .claude/rules/numerical-rigor.md -> "Timestamp rigor -> Ban list".
    """
    ts = pd.Timestamp(date_str)
    if ts.tz is None:
        raise ValueError(
            f"naive timestamp {date_str!r} is disallowed at the QC ingestion "
            "boundary; QC date strings must carry an explicit tz designator"
        )
    return int(ts.tz_convert("UTC").value // 1_000_000)


def _window_to_ms_utc(date_str: str, tz: str) -> int:
    """Convert a calendar-window boundary date + tz to int64 ms UTC.

    Unlike per-row prediction dates, calendar-window dates from QC are
    typically date-only (e.g. ``"2024-01-02"``) and require the export's
    declared tz to localize. Always succeeds for a valid (date, tz) pair.
    """
    return int(pd.Timestamp(date_str, tz=tz).tz_convert("UTC").value // 1_000_000)


def import_qc_fixture(
    *,
    qc_export_path: Path,
    prediction_set_id: str,
    output_root: Path,
    symbol: str,
) -> PredictionSetManifest:
    """Import a QC precomputed-predictions export into the v0.5 artifact format.

    Filters to the requested ``symbol``, converts QC date strings to
    ``int64 ms UTC``, computes canonical hashes, writes ``manifest.json``
    plus a single chunk parquet under ``output_root/<prediction_set_id>/``.
    Returns the parsed manifest.
    """
    if not is_path_safe_id(prediction_set_id):
        raise ValueError(
            f"prediction_set_id {prediction_set_id!r} is not path-safe "
            "([A-Za-z0-9_-][A-Za-z0-9_-.]*, no slashes, no traversal)"
        )

    raw = json.loads(Path(qc_export_path).read_text(encoding="utf-8"))
    export = QcExport.model_validate(raw)

    matching = [r for r in export.predictions if r.symbol == symbol]
    if not matching:
        present = sorted({r.symbol for r in export.predictions})
        raise ValueError(
            f"symbol {symbol!r} absent from QC export (symbols present: {present})"
        )

    row_dicts: list[dict] = []
    seen_ts: set[int] = set()
    for r in matching:
        ts_ms = _to_ms_utc(r.date)
        if ts_ms in seen_ts:
            raise ValueError(
                f"duplicate timestamp {ts_ms} for symbol {symbol!r} in QC export"
            )
        seen_ts.add(ts_ms)
        row_dicts.append(
            {"timestamp_ms": ts_ms, "symbol": symbol, "prediction": float(r.prediction)}
        )

    row_dicts.sort(key=lambda d: d["timestamp_ms"])
    timestamps = [d["timestamp_ms"] for d in row_dicts]

    start_ms = timestamps[0]
    end_ms = timestamps[-1]
    trained_through_ms = start_ms - 1

    rows_hash = compute_rows_hash(row_dicts)

    qc_window_start_ms = _window_to_ms_utc(
        export.calendar_window.start, export.calendar_window.tz
    )
    qc_window_end_ms = _window_to_ms_utc(
        export.calendar_window.end, export.calendar_window.tz
    )

    generator = QuantConnectPrecomputedFixtureGenerator(
        kind="quantconnect_precomputed_fixture",
        qc_tutorial_url=export.tutorial_url,
        qc_export_date=export.exported_at,
        qc_calendar_window_start_ms=qc_window_start_ms,
        qc_calendar_window_end_ms=qc_window_end_ms,
        qc_symbol_filter=symbol,
        qc_dataset_id=export.dataset_id,
        qc_versions=dict(export.versions),
    )

    chunk = ChunkRef(
        trained_through_ms=trained_through_ms,
        start_ms=start_ms,
        end_ms=end_ms,
        row_count=len(row_dicts),
        rows_hash=rows_hash,
    )

    artifact_dir = Path(output_root) / prediction_set_id
    chunk_path = artifact_dir / "chunks" / f"{trained_through_ms}.parquet"
    write_chunk_rows(chunk_path, row_dicts, field_names=["prediction"])

    manifest_dict = {
        "schema_version": "1.0",
        "prediction_set_id": prediction_set_id,
        "symbol": symbol,
        "resolution_minutes": 1440,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": generator.model_dump(),
        "chunks": [chunk.model_dump()],
    }
    manifest_dict["prediction_set_hash"] = compute_prediction_set_hash(manifest_dict)

    manifest = PredictionSetManifest.model_validate(manifest_dict)

    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_dict, indent=2, sort_keys=True), encoding="utf-8"
    )

    return manifest
