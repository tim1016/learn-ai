"""Real-fixture parity tests against the captured QC precomputed-predictions export.

Activates when `tests/fixtures/golden/qc-precomputed-predictions/qc_export.json`
exists. Asserts:

- every per-row prediction the importer emits equals QC's published value
  within `atol=1e-9, rtol=0` (spec D8);
- the importer's `prediction_set_hash` equals the value pinned in
  `tests/research/ml/fixtures/qc_known_hashes.json`.

Provenance values mirror the ones in
`tests/fixtures/golden/qc-precomputed-predictions/attribution.md`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.research.ml.generators.quantconnect_fixture import import_qc_fixture

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures" / "golden" / "qc-precomputed-predictions"
)
_QC_EXPORT = _FIXTURE_DIR / "qc_export.json"
_KNOWN_HASHES = (
    Path(__file__).resolve().parent / "fixtures" / "qc_known_hashes.json"
)
_SYMBOL = "SPY"

_PROVENANCE = {
    "qc_tutorial_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions",
    "qc_exported_at_ms": 1778443824165,
    "qc_calendar_window_start_ms": 1735851600000,  # 2025-01-02 16:00 ET
    "qc_calendar_window_end_ms":   1767214800000,  # 2025-12-31 16:00 ET
    "qc_dataset_id": "QuantConnect/USEquity-Daily",
    "qc_versions": {"sklearn": "1.6.1", "numpy": "1.26.4", "pandas": "2.3.3"},
}

pytestmark = pytest.mark.skipif(
    not _QC_EXPORT.is_file(),
    reason="QC fixture not yet captured — see §B in the parity spec",
)


def _import(tmp_path: Path):
    return import_qc_fixture(
        qc_export_path=_QC_EXPORT,
        prediction_set_id="qc_spy_precomputed_v001",
        output_root=tmp_path / "artifacts" / "predictions",
        symbol=_SYMBOL,
        **_PROVENANCE,
    )


def test_qc_fixture_parity_per_row_predictions_match(tmp_path: Path) -> None:
    """Every row the importer emits must equal QC's published prediction
    within atol=1e-9, rtol=0 (spec D8). Tolerance loosening is forbidden
    without a documented reason in the reference doc."""
    raw = json.loads(_QC_EXPORT.read_text(encoding="utf-8"))
    qc_values_by_date = {
        record["date"]: record["prediction_by_symbol"][_SYMBOL]
        for record in raw
        if _SYMBOL in record["prediction_by_symbol"]
    }

    manifest = _import(tmp_path)
    artifact_dir = (
        tmp_path / "artifacts" / "predictions" / manifest.prediction_set_id
    )

    # Re-read the chunk parquet via the existing helper to round-trip.
    from app.research.ml.artifact import read_chunk_rows

    chunk_path = artifact_dir / "chunks" / f"{manifest.chunks[0].trained_through_ms}.parquet"
    rows = read_chunk_rows(chunk_path, field_names=["prediction"])

    import pandas as pd

    seen_dates: set[str] = set()
    for row in rows:
        ts_ms = row["timestamp_ms"]
        # Reverse the importer's date conversion: ms UTC -> NY date.
        ny_date = (
            pd.Timestamp(ts_ms, unit="ms", tz="UTC")
            .tz_convert("America/New_York")
            .strftime("%Y-%m-%d")
        )
        assert ny_date in qc_values_by_date, (
            f"importer emitted ts={ts_ms} (NY date {ny_date}) which has no QC source row"
        )
        # Reject duplicates explicitly: a count-only check would silently
        # accept a duplicated NY date paired with a missing one.
        assert ny_date not in seen_dates, (
            f"importer emitted duplicate NY date {ny_date} (ts={ts_ms})"
        )
        seen_dates.add(ny_date)

        qc_value = qc_values_by_date[ny_date]
        imported_value = row["prediction"]
        assert math.isclose(imported_value, qc_value, abs_tol=1e-9, rel_tol=0), (
            f"prediction divergence on {ny_date}: imported={imported_value!r}, "
            f"qc={qc_value!r}, |diff|={abs(imported_value - qc_value)!r}"
        )

    # Full-keyset equality: the importer's date set MUST equal QC's date
    # set for the symbol. Rejects both missing-from-importer and
    # extra-in-importer cases that count-only equality would mask.
    assert seen_dates == set(qc_values_by_date), (
        f"date-set mismatch for symbol {_SYMBOL!r}: "
        f"missing-from-importer={sorted(set(qc_values_by_date) - seen_dates)[:5]}, "
        f"extra-in-importer={sorted(seen_dates - set(qc_values_by_date))[:5]}"
    )


def test_qc_fixture_prediction_set_hash_pinned(tmp_path: Path) -> None:
    """The importer's prediction_set_hash must match the pinned value.
    A mismatch means either (a) the importer drifted, (b) the fixture
    drifted, or (c) the provenance arguments drifted — investigate, do
    not regenerate the pin."""
    pinned = json.loads(_KNOWN_HASHES.read_text(encoding="utf-8"))
    expected = pinned["prediction_set_hash"]

    manifest = _import(tmp_path)

    assert manifest.prediction_set_hash == expected, (
        f"prediction_set_hash drift: pinned={expected}, got={manifest.prediction_set_hash}. "
        f"Investigate per spec D8 before updating the pin."
    )
