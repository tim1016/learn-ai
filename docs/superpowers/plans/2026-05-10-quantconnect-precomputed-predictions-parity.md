# QuantConnect precomputed-predictions parity — Phase 1 §A implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the schema extension and QC-export fixture importer so a captured `qc_export.json` becomes a v0.5-compliant prediction-set artifact. This plan covers §A (synthetic-fixture coverage). §B (real QC fixture capture, gated on Tim's QC Cloud access) and §C (pinned hashes, parity tests) are documented at the bottom but not scheduled here.

**Architecture:** Refactor the existing `GeneratorMeta` Pydantic model into a discriminated union so the manifest can describe two generator kinds (`deterministic_rule` and a new `quantconnect_precomputed_fixture`) without bumping `schema_version`. Add a sibling generator module that converts a closed `qc_export.json` into the existing manifest + parquet chunk format via the existing `compute_rows_hash` / `compute_prediction_set_hash` helpers. No changes to loader, runner, ledger, or evaluator — the v0.5 plumbing already accepts what the importer produces.

**Tech Stack:** Python 3.11, Pydantic v2 (discriminated unions), pyarrow (existing), pandas (timestamp tz parsing), pytest with `asyncio_mode=auto`. All commands run in the `polygon-data-service` container.

---

## File structure

**Modified:**
- `PythonDataService/app/research/ml/artifact.py` — split `GeneratorMeta` into discriminated union of `DeterministicRuleGenerator` + `QuantConnectPrecomputedFixtureGenerator`.
- `PythonDataService/tests/research/ml/test_coverage.py:37` — update `GeneratorMeta(...)` call site to `DeterministicRuleGenerator(...)`.
- `PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions_runtime.py:23` — same update.

**Created:**
- `PythonDataService/app/research/ml/generators/quantconnect_fixture.py` — importer module.
- `PythonDataService/tests/research/ml/test_artifact_generator_meta.py` — schema tests for the union.
- `PythonDataService/tests/research/ml/test_quantconnect_fixture_importer.py` — importer unit tests.
- `PythonDataService/tests/research/ml/test_quantconnect_fixture_determinism.py` — re-run identity test.
- `PythonDataService/tests/research/ml/test_quantconnect_fixture_parity.py` — **skipped** until §B fixture lands.
- `PythonDataService/tests/research/ml/test_quantconnect_fixture_runtime.py` — **skipped** until §B fixture lands.
- `PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/README.md` — fixture skeleton + §B capture instructions.
- `docs/references/quantconnect-precomputed-predictions.md` — reference doc per `numerical-rigor.md` § Reconciliation reports.

---

## Task 1: Split `GeneratorMeta` into a discriminated union (regression-safe refactor)

**Files:**
- Modify: `PythonDataService/app/research/ml/artifact.py` (lines 41–45 region)
- Modify: `PythonDataService/tests/research/ml/test_coverage.py:9, :37`
- Modify: `PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions_runtime.py:10, :23`
- Test: `PythonDataService/tests/research/ml/test_artifact_generator_meta.py` (new)

- [ ] **Step 1.1: Write the failing test for the existing `deterministic_rule` round-trip via the union**

Create `PythonDataService/tests/research/ml/test_artifact_generator_meta.py`:

```python
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
            "qc_tutorial_url": "https://www.quantconnect.com/docs/v2/writing-algorithms/machine-learning/precomputed-ml-predictions",
            "qc_export_date": "2026-05-10",
            "qc_calendar_window_start_ms": 1704153600000,
            "qc_calendar_window_end_ms": 1735603200000,
            "qc_symbol_filter": "SPY",
            "qc_dataset_id": "USEquity-Daily-v1",
            "qc_versions": {"sklearn": "1.5.0", "lean": "16000", "numpy": "1.26.4"},
        }
    }


def test_qc_fixture_manifest_round_trips() -> None:
    m = PredictionSetManifest.model_validate(_qc_manifest_dict())
    assert isinstance(m.generator, QuantConnectPrecomputedFixtureGenerator)
    assert m.generator.qc_symbol_filter == "SPY"
    assert m.generator.qc_versions["sklearn"] == "1.5.0"


def test_discriminator_rejects_cross_variant_fields_on_qc_kind() -> None:
    bad = _qc_manifest_dict()
    bad["generator"]["rule_id"] = "rsi_14_centered"  # belongs to the other variant
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
    bad["generator"]["qc_versions"] = {"sklearn": 1.5}  # number, not string
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)
```

- [ ] **Step 1.2: Run it — expect failure on the imports**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_artifact_generator_meta.py -v`
Expected: FAIL with `ImportError: cannot import name 'DeterministicRuleGenerator'` (or `QuantConnectPrecomputedFixtureGenerator`).

- [ ] **Step 1.3: Refactor `artifact.py` to the discriminated union**

In `PythonDataService/app/research/ml/artifact.py`, replace the existing `GeneratorMeta` class (currently lines 41–45) with:

```python
from typing import Annotated, Literal

# ... existing imports ...
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeterministicRuleGenerator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["deterministic_rule"]
    rule_id: str
    rule_version: str


class QuantConnectPrecomputedFixtureGenerator(BaseModel):
    """Provenance for a prediction-set artifact imported from a QuantConnect
    precomputed-predictions tutorial export.

    All fields are populated from the captured ``qc_export.json`` (raw export)
    + sibling ``attribution.md`` (pinned versions, dates, dataset id). Raw
    QC date strings are NOT stored here — they live in the golden-fixture
    directory only. Production rows are canonical ``int64 ms UTC``.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["quantconnect_precomputed_fixture"]
    qc_tutorial_url: str
    qc_export_date: str
    qc_calendar_window_start_ms: int
    qc_calendar_window_end_ms: int
    qc_symbol_filter: str
    qc_dataset_id: str
    qc_versions: dict[str, str]


GeneratorMeta = Annotated[
    DeterministicRuleGenerator | QuantConnectPrecomputedFixtureGenerator,
    Field(discriminator="kind"),
]
```

The existing `PredictionSetManifest.generator: GeneratorMeta` line stays unchanged — `GeneratorMeta` is now the union type alias and Pydantic v2 will dispatch on `kind`.

- [ ] **Step 1.4: Update the two `GeneratorMeta(...)` constructor call sites**

`GeneratorMeta` is now a type alias and is not callable. Update the two test files:

`PythonDataService/tests/research/ml/test_coverage.py:9` — change the import:

```python
from app.research.ml.artifact import ChunkRef, DeterministicRuleGenerator, PredictionSetManifest
```

And line 37:

```python
        generator=DeterministicRuleGenerator(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
```

`PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions_runtime.py:10` — same import change. Line 23 — same constructor change.

- [ ] **Step 1.5: Run the new test plus the two adjusted tests plus the existing manifest test**

Run:
```
podman exec polygon-data-service python -m pytest \
  tests/research/ml/test_artifact_generator_meta.py \
  tests/research/ml/test_artifact.py \
  tests/research/ml/test_coverage.py \
  app/engine/strategy/spec/tests/test_spec_predictions_runtime.py -v
```
Expected: all PASS.

- [ ] **Step 1.6: Run the full ML test directory to catch any other reference**

Run:
```
podman exec polygon-data-service python -m pytest tests/research/ml/ app/engine/strategy/spec/tests/ -v
```
Expected: all PASS. If a third file references `GeneratorMeta(...)` as a constructor, fix the same way.

- [ ] **Step 1.7: Commit**

```bash
git add PythonDataService/app/research/ml/artifact.py PythonDataService/tests/research/ml/test_artifact_generator_meta.py PythonDataService/tests/research/ml/test_coverage.py PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions_runtime.py
git commit -m "refactor(ml-artifact): split GeneratorMeta into discriminated union

Adds QuantConnectPrecomputedFixtureGenerator as a second variant alongside
the existing DeterministicRuleGenerator. Manifest schema_version stays at
1.0 — all QC-specific provenance lives inside the discriminated union.

Refs spec docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md D2."
```

---

## Task 2: Define the closed Pydantic model for `qc_export.json`

**Files:**
- Modify: `PythonDataService/app/research/ml/generators/quantconnect_fixture.py` (new)
- Test: `PythonDataService/tests/research/ml/test_quantconnect_fixture_importer.py` (new)

> The QC export shape used here is a strawman (see spec R1). Once §B captures a real export, the model adjusts. The synthetic shape is good enough for §A unit testing of the validation rails (closed shape, dup detection, tz handling).

- [ ] **Step 2.1: Write the failing test for the `QcExport` model shape**

Create `PythonDataService/tests/research/ml/test_quantconnect_fixture_importer.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.research.ml.generators.quantconnect_fixture import QcExport, QcPredictionRow


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
```

- [ ] **Step 2.2: Run it — expect ImportError**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_quantconnect_fixture_importer.py -v`
Expected: FAIL — `quantconnect_fixture` module does not exist.

- [ ] **Step 2.3: Implement the module skeleton + `QcExport` model**

Create `PythonDataService/app/research/ml/generators/quantconnect_fixture.py`:

```python
"""Import a QuantConnect precomputed-predictions tutorial export into the
v0.5 prediction-set artifact format.

Phase 1 §A scope: schema validation, symbol filter, timestamp conversion,
manifest + chunk write. Real-fixture parity tests are gated on §B (QC
Cloud capture) — see the parity spec.

Wire and storage format for timestamps is ``int64 ms UTC``. The QC export's
raw date strings cross the ingestion boundary in this module and are
converted on the spot; no string timestamps escape downstream.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class QcCalendarWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str
    tz: str


class QcPredictionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    date: str  # tz-aware ISO 8601; converted to int64 ms UTC at import time
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
```

Also create `PythonDataService/app/research/ml/generators/__init__.py` if it doesn't already exist (it does — verified by glob; no action).

- [ ] **Step 2.4: Run the test**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_quantconnect_fixture_importer.py -v`
Expected: all four tests PASS.

- [ ] **Step 2.5: Commit**

```bash
git add PythonDataService/app/research/ml/generators/quantconnect_fixture.py PythonDataService/tests/research/ml/test_quantconnect_fixture_importer.py
git commit -m "feat(ml-importer): add closed QcExport schema (§A skeleton)

Strawman shape per spec R1; finalized once §B captures a real QC export."
```

---

## Task 3: Add `import_qc_fixture(...)` happy-path

**Files:**
- Modify: `PythonDataService/app/research/ml/generators/quantconnect_fixture.py`
- Modify: `PythonDataService/tests/research/ml/test_quantconnect_fixture_importer.py`

- [ ] **Step 3.1: Append happy-path test**

Append to `tests/research/ml/test_quantconnect_fixture_importer.py`:

```python
import json
from pathlib import Path

from app.research.ml.artifact import (
    PredictionSetManifest,
    QuantConnectPrecomputedFixtureGenerator,
)
from app.research.ml.generators.quantconnect_fixture import import_qc_fixture
from app.research.ml.loader import PredictionSet


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

    # Importer returns the manifest it wrote.
    assert isinstance(manifest, PredictionSetManifest)
    assert isinstance(manifest.generator, QuantConnectPrecomputedFixtureGenerator)
    assert manifest.generator.qc_symbol_filter == "SPY"
    assert manifest.symbol == "SPY"
    assert len(manifest.chunks) == 1
    assert manifest.chunks[0].row_count == 2

    # PredictionSet.load(...) re-validates everything end-to-end.
    pset = PredictionSet.load(output_root / "qc_spy_precomputed_v001")
    assert len(pset.index) == 2
```

- [ ] **Step 3.2: Run it — expect ImportError on `import_qc_fixture`**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_quantconnect_fixture_importer.py::test_import_qc_fixture_happy_path -v`
Expected: FAIL — `import_qc_fixture` not defined.

- [ ] **Step 3.3: Implement `import_qc_fixture`**

Append to `PythonDataService/app/research/ml/generators/quantconnect_fixture.py`:

```python
import json
from pathlib import Path

import pandas as pd

from app.research.ml.artifact import (
    ChunkRef,
    PredictionSetManifest,
    QuantConnectPrecomputedFixtureGenerator,
    compute_prediction_set_hash,
    compute_rows_hash,
    is_path_safe_id,
    write_chunk_rows,
)


def _to_ms_utc(date_str: str) -> int:
    """Convert a tz-aware ISO 8601 date string to int64 ms UTC.

    Naive strings (no tz designator, no offset) are rejected per
    .claude/rules/numerical-rigor.md → "Timestamp rigor → Ban list".
    """
    ts = pd.Timestamp(date_str)
    if ts.tz is None:
        raise ValueError(
            f"naive timestamp {date_str!r} is disallowed at the QC ingestion "
            "boundary; QC date strings must carry an explicit tz designator"
        )
    return int(ts.tz_convert("UTC").value // 1_000_000)


def import_qc_fixture(
    *,
    qc_export_path: Path,
    prediction_set_id: str,
    output_root: Path,
    symbol: str,
) -> PredictionSetManifest:
    """Import a QC precomputed-predictions export into the v0.5 artifact format.

    Filters to the requested ``symbol``, converts QC date strings to
    ``int64 ms UTC``, computes canonical hashes, writes ``manifest.json`` +
    a single chunk parquet under ``output_root/<prediction_set_id>/``.
    Returns the parsed manifest.
    """
    if not is_path_safe_id(prediction_set_id):
        raise ValueError(f"prediction_set_id {prediction_set_id!r} is not path-safe")

    raw = json.loads(Path(qc_export_path).read_text(encoding="utf-8"))
    export = QcExport.model_validate(raw)

    rows = [r for r in export.predictions if r.symbol == symbol]
    if not rows:
        raise ValueError(
            f"symbol {symbol!r} absent from QC export "
            f"(symbols present: {sorted({r.symbol for r in export.predictions})})"
        )

    row_dicts: list[dict] = []
    seen_ts: set[int] = set()
    for r in rows:
        ts_ms = _to_ms_utc(r.date)
        if ts_ms in seen_ts:
            raise ValueError(
                f"duplicate timestamp {ts_ms} for symbol {symbol!r} in QC export"
            )
        seen_ts.add(ts_ms)
        row_dicts.append({"timestamp_ms": ts_ms, "symbol": symbol, "prediction": float(r.prediction)})

    row_dicts.sort(key=lambda d: d["timestamp_ms"])
    timestamps = [d["timestamp_ms"] for d in row_dicts]
    if any(b <= a for a, b in zip(timestamps, timestamps[1:], strict=True)):
        raise ValueError("timestamps must be strictly increasing after sort (duplicate or unordered)")

    start_ms = timestamps[0]
    end_ms = timestamps[-1]
    trained_through_ms = start_ms - 1

    rows_hash = compute_rows_hash(row_dicts)

    qc_window_start_ms = _to_ms_utc(
        f"{export.calendar_window.start}T00:00:00"
        + ("Z" if export.calendar_window.tz == "UTC" else "")  # naive guard; see test 3.4
    ) if export.calendar_window.tz == "UTC" else int(
        pd.Timestamp(export.calendar_window.start, tz=export.calendar_window.tz)
        .tz_convert("UTC").value // 1_000_000
    )
    qc_window_end_ms = int(
        pd.Timestamp(export.calendar_window.end, tz=export.calendar_window.tz)
        .tz_convert("UTC").value // 1_000_000
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
        "resolution_minutes": 1440,  # daily — strawman; revisit per real fixture
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": generator.model_dump(),
        "chunks": [chunk.model_dump()],
    }
    manifest_dict["prediction_set_hash"] = compute_prediction_set_hash(manifest_dict)

    manifest = PredictionSetManifest.model_validate(manifest_dict)

    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_dict, indent=2, sort_keys=True), encoding="utf-8")

    return manifest
```

- [ ] **Step 3.4: Run the happy-path test**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_quantconnect_fixture_importer.py::test_import_qc_fixture_happy_path -v`
Expected: PASS.

- [ ] **Step 3.5: Run the full importer test file**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_quantconnect_fixture_importer.py -v`
Expected: all PASS.

- [ ] **Step 3.6: Commit**

```bash
git add PythonDataService/app/research/ml/generators/quantconnect_fixture.py PythonDataService/tests/research/ml/test_quantconnect_fixture_importer.py
git commit -m "feat(ml-importer): import QC export to v0.5 artifact format (§A happy path)"
```

---

## Task 4: Importer validation rails — symbol absence, duplicates, naive tz

**Files:**
- Modify: `PythonDataService/tests/research/ml/test_quantconnect_fixture_importer.py`

- [ ] **Step 4.1: Append validation tests**

Append to the test file:

```python
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
    payload["predictions"][0]["date"] = "2024-01-02 16:00:00"  # naive
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
```

- [ ] **Step 4.2: Run the test file**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_quantconnect_fixture_importer.py -v`
Expected: all PASS — the implementation in Task 3 already covers these rails.

- [ ] **Step 4.3: Commit**

```bash
git add PythonDataService/tests/research/ml/test_quantconnect_fixture_importer.py
git commit -m "test(ml-importer): assert symbol/dup/naive-tz rejection rails"
```

---

## Task 5: Determinism test — re-running the importer on the same export must produce byte-identical output

**Files:**
- Test: `PythonDataService/tests/research/ml/test_quantconnect_fixture_determinism.py` (new)

- [ ] **Step 5.1: Write the test**

Create `PythonDataService/tests/research/ml/test_quantconnect_fixture_determinism.py`:

```python
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
```

- [ ] **Step 5.2: Run it**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_quantconnect_fixture_determinism.py -v`
Expected: PASS.

If this fails, the importer is non-deterministic (likely cause: `json.dumps` ordering or floating-point conversion drift). Fix the importer, do not loosen the test.

- [ ] **Step 5.3: Commit**

```bash
git add PythonDataService/tests/research/ml/test_quantconnect_fixture_determinism.py
git commit -m "test(ml-importer): pin determinism — same input, same hash + bytes"
```

---

## Task 6: Reference doc + golden-fixture skeleton (no real data yet)

**Files:**
- Create: `docs/references/quantconnect-precomputed-predictions.md`
- Create: `PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/README.md`

- [ ] **Step 6.1: Write the reference doc**

Create `docs/references/quantconnect-precomputed-predictions.md`:

```markdown
# QuantConnect precomputed-predictions parity (Phase 1)

**Reference source:** QuantConnect "Precomputed ML Predictions" tutorial — `https://www.quantconnect.com/docs/v2/writing-algorithms/machine-learning/precomputed-ml-predictions` (URL pinned in the captured fixture's `attribution.md`).

**Spec:** `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md`

**Plan:** `docs/superpowers/plans/2026-05-10-quantconnect-precomputed-predictions-parity.md`

## Status

- §A — schema extension + importer + synthetic-fixture tests: **landed**.
- §B — real QC fixture capture: **pending Tim's QC Cloud run**.
- §C — pinned hashes + parity tests: **gated on §B**.

## Tolerances

| Comparison | Tolerance | Justification |
|---|---|---|
| QC published prediction value vs. importer output value | `atol=1e-9, rtol=0` | QC's export is deterministic; predictions are static numbers. Anything looser is a smell. |
| `prediction_set_hash` reproduction | bit-exact | Hash is a function of canonical row JSON; pyarrow / pandas drift cannot affect it (v0.5 invariant). |
| `RunLedger.prediction_set_hash`, `result_hash` | bit-exact | Same reasoning. |

## Captured fixture provenance (filled in at §B)

- QC tutorial commit / version: TBD at §B
- QC dataset id: TBD
- Calendar window: TBD (pinned start/end, no `datetime.now()`)
- Symbol(s) in export: TBD
- QC sklearn / LEAN / numpy versions: TBD
- Exported at (UTC): TBD
```

- [ ] **Step 6.2: Write the golden-fixture README**

Create `PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/README.md`:

```markdown
# QuantConnect precomputed-predictions golden fixture

This directory holds the **immutable** ground truth captured from QuantConnect Cloud
for Phase 1 parity validation. Per `.claude/rules/numerical-rigor.md` § Golden fixtures,
contents are generated once and re-generated only with justification.

## Pending capture (§B)

When §B runs, this directory must contain:

- `qc_export.json` — raw output of the QC precomputed-ML-predictions tutorial,
  pinned to a deterministic calendar window (no `datetime.now()`).
- `qc_price_history.csv` — the OHLCV bars QC's tutorial saw, for any feature recomputation audit.
- `attribution.md` — pinned dates, sklearn / LEAN / numpy versions, dataset id, tutorial URL,
  screenshot or text log of the QC notebook output, and the command used to regenerate.

## What §A landed (current state)

Importer code path (`app/research/ml/generators/quantconnect_fixture.py`) and its
synthetic-fixture unit tests at `tests/research/ml/test_quantconnect_fixture_*.py`.
The synthetic shape used by tests is a **strawman** for the QC export's actual shape;
once `qc_export.json` is captured, the closed Pydantic model in `quantconnect_fixture.py`
is verified or adjusted, and the parity tests in `test_quantconnect_fixture_parity.py`
are unskipped.
```

- [ ] **Step 6.3: Commit**

```bash
git add docs/references/quantconnect-precomputed-predictions.md PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/README.md
git commit -m "docs(ml-parity): reference doc + golden-fixture skeleton for QC parity"
```

---

## Task 7: Skipped real-fixture parity tests (placeholders that activate at §C)

**Files:**
- Create: `PythonDataService/tests/research/ml/test_quantconnect_fixture_parity.py`
- Create: `PythonDataService/tests/research/ml/test_quantconnect_fixture_runtime.py`

- [ ] **Step 7.1: Write the parity test placeholder**

Create `PythonDataService/tests/research/ml/test_quantconnect_fixture_parity.py`:

```python
"""Real-fixture parity tests. Skipped until §B captures qc_export.json."""

from __future__ import annotations

from pathlib import Path

import pytest

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures" / "golden" / "qc-precomputed-predictions"
)
_QC_EXPORT = _FIXTURE_DIR / "qc_export.json"

pytestmark = pytest.mark.skipif(
    not _QC_EXPORT.is_file(),
    reason="QC fixture not yet captured — see §B in the parity spec",
)


def test_qc_fixture_parity_per_row_predictions_match() -> None:
    pytest.skip("§C parity test — implement when fixture lands")


def test_qc_fixture_prediction_set_hash_pinned() -> None:
    pytest.skip("§C parity test — implement when fixture lands")
```

- [ ] **Step 7.2: Write the runtime test placeholder**

Create `PythonDataService/tests/research/ml/test_quantconnect_fixture_runtime.py`:

```python
"""Real-fixture runtime parity tests. Skipped until §B captures qc_export.json."""

from __future__ import annotations

from pathlib import Path

import pytest

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures" / "golden" / "qc-precomputed-predictions"
)
_QC_EXPORT = _FIXTURE_DIR / "qc_export.json"

pytestmark = pytest.mark.skipif(
    not _QC_EXPORT.is_file(),
    reason="QC fixture not yet captured — see §B in the parity spec",
)


def test_qc_fixture_strategy_spec_run_ledger_hash_pinned() -> None:
    pytest.skip("§C runtime test — implement when fixture lands")


def test_qc_fixture_strategy_spec_result_hash_pinned() -> None:
    pytest.skip("§C runtime test — implement when fixture lands")
```

- [ ] **Step 7.3: Run the new tests — expect them to be collected and skipped**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_quantconnect_fixture_parity.py tests/research/ml/test_quantconnect_fixture_runtime.py -v`
Expected: 4 SKIPPED with reason "QC fixture not yet captured".

- [ ] **Step 7.4: Commit**

```bash
git add PythonDataService/tests/research/ml/test_quantconnect_fixture_parity.py PythonDataService/tests/research/ml/test_quantconnect_fixture_runtime.py
git commit -m "test(ml-parity): skipped placeholders unblock once §B fixture lands"
```

---

## Task 8: Project-scope verification before push

- [ ] **Step 8.1: Lint at project scope**

Run: `podman exec polygon-data-service ruff check /app/app/ /app/tests/`
Expected: 0 issues. Fix any drift introduced by edits before pushing.

- [ ] **Step 8.2: Run the full Python test suite**

Run: `podman exec polygon-data-service python -m pytest /app/tests -q -k "not slow"`
Expected: all pass except inherited pre-existing failures (baseline against `origin/master` per `.claude/rules/testing.md` § Pre-push test-suite hygiene).

- [ ] **Step 8.3: Open a PR**

If on `master`, create a feature branch first per memory `feedback_branch_workflow.md`:

```bash
git switch -c feat/qc-precomputed-predictions-parity-phase-1a
git push -u origin feat/qc-precomputed-predictions-parity-phase-1a
gh pr create --title "feat(ml-parity): QuantConnect precomputed-predictions Phase 1 §A" --body "$(cat <<'EOF'
## Summary
- Spec: docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md
- Plan: docs/superpowers/plans/2026-05-10-quantconnect-precomputed-predictions-parity.md
- §A scope: GeneratorMeta discriminated union + QC fixture importer + synthetic-fixture tests
- §B (real QC fixture capture) and §C (pinned-hash parity tests) gated on Tim's QC Cloud run

## Test plan
- [ ] tests/research/ml/test_artifact_generator_meta.py passes
- [ ] tests/research/ml/test_quantconnect_fixture_importer.py passes
- [ ] tests/research/ml/test_quantconnect_fixture_determinism.py passes
- [ ] tests/research/ml/test_quantconnect_fixture_parity.py + _runtime.py both SKIP with the §B reason
- [ ] Full Python pytest suite green vs. inherited baseline
- [ ] ruff at project scope clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Phase 1 §B and §C — gated, not scheduled here

§B (Tim runs QC Cloud notebook, captures `qc_export.json` + sibling files into the fixture directory) and §C (pin three hashes, replace skipped tests with real parity assertions) are described in the spec but **not** part of this plan's task list. They unlock when Tim has QC Cloud access and a captured fixture. When that happens, write a follow-up plan; do not hand-edit the skipped placeholders into real tests without re-running the brainstorm.

## Self-review notes

- **Spec coverage:** Task 1 covers D2; Tasks 2-5 cover D1 + D5 + D6 + the importer test list; Task 6 covers D3 + D7's reference doc requirement; Task 7 covers D7's pinned-hash test scaffolding (placeholders) and the §A/§B/§C boundary; Task 8 is verification per repo testing rules.
- **Placeholder scan:** §B/§C are explicitly out-of-scope here, marked as gated. The skipped tests in Task 7 are placeholders **on purpose** (per spec D7 + §C); they auto-activate when the fixture file appears.
- **Type consistency:** `DeterministicRuleGenerator`, `QuantConnectPrecomputedFixtureGenerator`, `import_qc_fixture`, `QcExport`, `QcPredictionRow`, `QcCalendarWindow` are used consistently across tasks.
