"""Import a QuantConnect precomputed-predictions tutorial export into the
v0.5 prediction-set artifact format.

The QC tutorial documented at
https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions
emits a JSON file containing a bare list of daily prediction records:

    [
      {"date": "YYYY-MM-DD", "prediction_by_symbol": {"SPY": 0.1, "QQQ": -0.05}},
      {"date": "YYYY-MM-DD", "prediction_by_symbol": {"SPY": ..., "QQQ": ...}},
      ...
    ]

QC's saved file does not include any provenance (tutorial URL, dataset id,
versions, calendar window). Provenance is captured separately in the
fixture's ``attribution.md`` (or sidecar JSON) and passed to the importer
as explicit arguments.

Phase 1 §A scope: schema validation, symbol filter, date conversion,
manifest + chunk write. Real-fixture parity tests are gated on §B (QC
Cloud capture) — see
``docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md``.

Wire and storage format for timestamps is ``int64 ms UTC``. QC's date-only
strings are paired with the caller-supplied ``daily_anchor_tz`` +
``daily_anchor_hhmm`` and converted to ``int64 ms UTC`` at this ingestion
boundary; no string timestamps escape downstream.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.research.ml.artifact import (
    ChunkRef,
    PredictionSetManifest,
    QuantConnectPrecomputedFixtureGenerator,
    compute_prediction_set_hash,
    compute_rows_hash,
    is_path_safe_id,
    write_chunk_rows,
)


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")


class QcPredictionRecord(BaseModel):
    """One day's predictions, as QC's precomputed-ml-predictions tutorial emits."""

    model_config = ConfigDict(extra="forbid")
    date: str
    prediction_by_symbol: dict[str, float] = Field(min_length=1)


class QcExport(RootModel[list[QcPredictionRecord]]):
    """Closed model of QC's emitted JSON file.

    The file is a bare list — no envelope, no provenance metadata. Validated
    via Pydantic v2 ``RootModel``; ``extra='forbid'`` on each record rejects
    unknown fields.
    """


def _date_only_to_ms_utc(date_str: str, anchor_hhmm: str, anchor_tz: str) -> int:
    """Combine a ``"YYYY-MM-DD"`` date, a ``"HH:MM"`` anchor, and an IANA tz
    into ``int64 ms UTC``.

    Rejects any input that does not look like a strict date-only string;
    a tz-aware ISO 8601 string would be ambiguous (which tz applies?) and
    must be rewritten by the caller before reaching this helper.
    """
    if not _DATE_ONLY_RE.match(date_str):
        raise ValueError(
            f"expected date-only 'YYYY-MM-DD' string, got {date_str!r}; "
            "tz-aware or other formats must be rewritten by the caller"
        )
    if not _HHMM_RE.match(anchor_hhmm):
        raise ValueError(f"expected anchor 'HH:MM' string, got {anchor_hhmm!r}")
    ts = pd.Timestamp(f"{date_str} {anchor_hhmm}", tz=anchor_tz)
    return int(ts.tz_convert("UTC").value // 1_000_000)


def import_qc_fixture(
    *,
    qc_export_path: Path,
    prediction_set_id: str,
    output_root: Path,
    symbol: str,
    qc_tutorial_url: str,
    qc_exported_at_ms: int,
    qc_calendar_window_start_ms: int,
    qc_calendar_window_end_ms: int,
    qc_dataset_id: str,
    qc_versions: dict[str, str],
    qc_daily_anchor_tz: str = "America/New_York",
    qc_daily_anchor_hhmm: str = "16:00",
) -> PredictionSetManifest:
    """Import a QC precomputed-predictions export into the v0.5 artifact format.

    ``qc_export_path`` points at the bare-list JSON file QC's tutorial saves
    to ObjectStore. Provenance fields (``qc_tutorial_url``, dataset id,
    versions, calendar window, exported_at_ms, daily anchor) are not in
    QC's emitted file; the caller supplies them from ``attribution.md`` or a
    sidecar.

    For each daily record, picks the requested ``symbol`` from the
    ``prediction_by_symbol`` map, converts the date-only string +
    daily-anchor convention to ``int64 ms UTC``, and emits one row per day.
    Days where the symbol is absent from ``prediction_by_symbol`` are
    silently skipped (QC's tutorial may exclude a symbol on days the
    universe filter excludes it). Days with a duplicate date raise.
    """
    if not is_path_safe_id(prediction_set_id):
        raise ValueError(
            f"prediction_set_id {prediction_set_id!r} is not path-safe "
            "([A-Za-z0-9_-][A-Za-z0-9_-.]*, no slashes, no traversal)"
        )

    raw = json.loads(Path(qc_export_path).read_text(encoding="utf-8"))
    export = QcExport.model_validate(raw)
    records = export.root

    row_dicts: list[dict] = []
    seen_ts: set[int] = set()
    for record in records:
        if symbol not in record.prediction_by_symbol:
            continue
        ts_ms = _date_only_to_ms_utc(record.date, qc_daily_anchor_hhmm, qc_daily_anchor_tz)
        if ts_ms in seen_ts:
            raise ValueError(
                f"duplicate timestamp {ts_ms} for symbol {symbol!r} (date {record.date!r}) in QC export"
            )
        seen_ts.add(ts_ms)
        prediction = float(record.prediction_by_symbol[symbol])
        row_dicts.append(
            {"timestamp_ms": ts_ms, "symbol": symbol, "prediction": prediction}
        )

    if not row_dicts:
        present = sorted({s for r in records for s in r.prediction_by_symbol})
        raise ValueError(
            f"symbol {symbol!r} absent from QC export "
            f"(symbols present in at least one record: {present})"
        )

    row_dicts.sort(key=lambda d: d["timestamp_ms"])
    timestamps = [d["timestamp_ms"] for d in row_dicts]

    start_ms = timestamps[0]
    end_ms = timestamps[-1]
    trained_through_ms = start_ms - 1

    rows_hash = compute_rows_hash(row_dicts)

    generator = QuantConnectPrecomputedFixtureGenerator(
        kind="quantconnect_precomputed_fixture",
        qc_tutorial_url=qc_tutorial_url,
        qc_exported_at_ms=qc_exported_at_ms,
        qc_calendar_window_start_ms=qc_calendar_window_start_ms,
        qc_calendar_window_end_ms=qc_calendar_window_end_ms,
        qc_symbol_filter=symbol,
        qc_dataset_id=qc_dataset_id,
        qc_versions=dict(qc_versions),
        qc_daily_anchor_tz=qc_daily_anchor_tz,
        qc_daily_anchor_hhmm=qc_daily_anchor_hhmm,
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
