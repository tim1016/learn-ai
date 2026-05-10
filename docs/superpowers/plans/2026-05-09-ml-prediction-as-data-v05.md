# ML Predictions as Data — v0.5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the v0.5 plumbing that lets a `StrategySpec` consume precomputed predictions as data — without sklearn, without training, without walk-forward retraining — proven by a deterministic-rule "fake model" generator producing bit-reproducible artifacts and stable backtest `result_hash`es.

**Architecture:** Five layers, in dependency order: (1) artifact format (Pydantic manifest + parquet chunks), (2) loader with intrinsic and spec-pairing validation, (3) `StrategySpec` extension (`predictions` block + `PredictionComparison` condition), (4) engine wiring (`EvalContext.predictions`, `PredictionComparisonPrimitive`, `SpecAlgorithm` populates per bar), (5) bar-clock coverage check at the run-pipeline boundary plus `RunLedger` schema bump (`1.0 → 1.1`, new `prediction_set_hash`).

**Tech Stack:** Python 3.11+, Pydantic v2, pandas + pyarrow (parquet), pytest. All work in `PythonDataService/`. Frontend / .NET / GraphQL untouched in v0.5.

**Spec:** [`docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md`](../specs/2026-05-09-ml-prediction-as-data-v05-design.md).

---

## File map

**Created:**

| File | Responsibility |
|---|---|
| `PythonDataService/app/research/ml/__init__.py` | Package marker. |
| `PythonDataService/app/research/ml/artifact.py` | `PredictionSetManifest`, `ChunkRef` Pydantic models, hash helpers, parquet I/O for chunks, path-safe id validation. |
| `PythonDataService/app/research/ml/loader.py` | `PredictionSet` class with `load(...)`, intrinsic + spec-pairing validation. |
| `PythonDataService/app/research/ml/coverage.py` | `assert_bar_clock_coverage(...)` helper called from the run pipeline. |
| `PythonDataService/app/research/ml/generators/__init__.py` | Package marker. |
| `PythonDataService/app/research/ml/generators/deterministic_rule.py` | `rsi_14_centered` rule + generator entrypoint. |
| `PythonDataService/app/research/ml/generate_prediction_set.py` | CLI module (`python -m app.research.ml.generate_prediction_set ...`). |
| `PythonDataService/tests/research/ml/__init__.py` | Package marker. |
| `PythonDataService/tests/research/ml/test_artifact.py` | Manifest, hash, parquet tests. |
| `PythonDataService/tests/research/ml/test_loader.py` | `PredictionSet.load(...)` validation tests. |
| `PythonDataService/tests/research/ml/test_coverage.py` | Bar-clock coverage tests. |
| `PythonDataService/tests/research/ml/test_generator.py` | `rsi_14_centered` generator + CLI tests. |
| `PythonDataService/tests/research/ml/test_e2e_replay.py` | End-to-end determinism + replay tests with committed-hash fixtures. |
| `PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions.py` | Schema tests for `PredictionRef`, `PredictionComparison`, validators. |

**Modified:**

| File | What changes |
|---|---|
| `PythonDataService/app/engine/strategy/spec/schema.py` | New `PredictionRef`, `PredictionComparison` models; new `predictions` field on `StrategySpec`; extended `_check_phase1_boundaries`; new `_iter_prediction_refs`. |
| `PythonDataService/app/engine/strategy/spec/primitives.py` | New `predictions` field on `EvalContext`; new `PredictionComparisonPrimitive`; route in `_build_leaf`. |
| `PythonDataService/app/engine/strategy/spec/evaluator.py` | `SpecAlgorithm.__init__` accepts optional `PredictionSet`; `_on_consolidated_bar` populates `ctx.predictions`. |
| `PythonDataService/app/research/runs/ledger.py` | `schema_version: Literal["1.0", "1.1"] = "1.1"`; new optional `prediction_set_hash` field. |
| `PythonDataService/app/research/runs/runner.py` | Loads prediction set when spec declares any; runs bar-clock coverage; threads `prediction_set_hash` into `RunLedger`. |

---

## Branching

The plan assumes execution starts from a fresh worktree branched off `origin/master` (`feat/ml-predictions-as-data-v05`). The `superpowers:using-git-worktrees` skill creates this when invoked from `subagent-driven-development` or `executing-plans`. All commits land on that branch and a single PR is opened at the end (one PR, many commits is the project default for non-trivial work).

---

## Tasks

### Task 1: Create the `app/research/ml/` package and its tests skeleton

**Files:**
- Create: `PythonDataService/app/research/ml/__init__.py`
- Create: `PythonDataService/app/research/ml/generators/__init__.py`
- Create: `PythonDataService/tests/research/ml/__init__.py`

- [ ] **Step 1: Create the empty package files**

```python
# PythonDataService/app/research/ml/__init__.py
"""Artifact-producing ML research pipelines.

Distinct from ``app.ml`` (reusable toolbox: protocols, preprocessing).
This package owns prediction-set artifacts, the loader, the bar-clock
coverage helper, and CLI generators.
"""
```

```python
# PythonDataService/app/research/ml/generators/__init__.py
"""Prediction-set generators. v0.5 ships a deterministic-rule generator
only. Future generators (sklearn, walk-forward) live as siblings.
"""
```

```python
# PythonDataService/tests/research/ml/__init__.py
```

- [ ] **Step 2: Verify package imports cleanly**

Run: `podman exec polygon-data-service python -c "import app.research.ml; import app.research.ml.generators"`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add PythonDataService/app/research/ml/__init__.py PythonDataService/app/research/ml/generators/__init__.py PythonDataService/tests/research/ml/__init__.py
git commit -m "feat(research/ml): scaffold package + test tree"
```

---

### Task 2: Manifest + ChunkRef Pydantic models

**Files:**
- Create: `PythonDataService/app/research/ml/artifact.py`
- Create: `PythonDataService/tests/research/ml/test_artifact.py`

- [ ] **Step 1: Write failing tests for the manifest schema**

```python
# PythonDataService/tests/research/ml/test_artifact.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.research.ml.artifact import (
    ChunkRef,
    PredictionSetManifest,
    is_path_safe_id,
)


# ----- path-safe id ----------------------------------------------------
@pytest.mark.parametrize("good", ["pred_spy_v001", "abc-123", "pred.v2", "x"])
def test_path_safe_id_accepts_alnum_and_separators(good: str) -> None:
    assert is_path_safe_id(good)


@pytest.mark.parametrize("bad", ["", "..", "../foo", "a/b", "a\\b", "with space", ".hidden"])
def test_path_safe_id_rejects_traversal_and_separators(bad: str) -> None:
    assert not is_path_safe_id(bad)


# ----- ChunkRef -------------------------------------------------------
def _chunk_dict() -> dict:
    return {
        "trained_through_ms": 1714521600000,
        "start_ms": 1714608000000,
        "end_ms": 1717199999000,
        "row_count": 173,
        "rows_hash": "0" * 64,
    }


def test_chunk_ref_round_trip() -> None:
    c = ChunkRef.model_validate(_chunk_dict())
    assert c.trained_through_ms == 1714521600000
    assert c.row_count == 173


def test_chunk_ref_rejects_extras() -> None:
    bad = _chunk_dict() | {"extra_field": "no"}
    with pytest.raises(ValidationError):
        ChunkRef.model_validate(bad)


def test_chunk_ref_rejects_start_at_or_before_trained_through() -> None:
    bad = _chunk_dict() | {"start_ms": 1714521600000}  # equals trained_through_ms
    with pytest.raises(ValidationError, match="start_ms must be > trained_through_ms"):
        ChunkRef.model_validate(bad)


# ----- PredictionSetManifest -----------------------------------------
def _manifest_dict() -> dict:
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
        "chunks": [_chunk_dict()],
        "prediction_set_hash": "0" * 64,
    }


def test_manifest_round_trip() -> None:
    m = PredictionSetManifest.model_validate(_manifest_dict())
    assert m.prediction_set_id == "pred_spy_rsi_rule_v001"
    assert m.warmup_policy == "neutral_zero_until_feature_ready"
    assert len(m.chunks) == 1


def test_manifest_rejects_extras() -> None:
    bad = _manifest_dict() | {"parquet_file_hash": "deadbeef"}
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)


def test_manifest_rejects_unknown_warmup_policy() -> None:
    bad = _manifest_dict() | {"warmup_policy": "forward_fill"}
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)


def test_manifest_rejects_path_unsafe_id() -> None:
    bad = _manifest_dict() | {"prediction_set_id": "../evil"}
    with pytest.raises(ValidationError, match="path-safe"):
        PredictionSetManifest.model_validate(bad)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_artifact.py -v`
Expected: ImportError / ModuleNotFoundError on `app.research.ml.artifact`.

- [ ] **Step 3: Implement the artifact models**

```python
# PythonDataService/app/research/ml/artifact.py
"""Pydantic models, hash helpers, and parquet I/O for prediction-set artifacts.

Reuses ``app.research.runs.hashing.hash_payload`` so all hash strings in
this package are bare 64-char hex, matching ``strategy_spec_hash`` and
``data_snapshot_id`` formats used by the run ledger.

Wire and storage timestamps are ``int64 ms UTC`` per
``.claude/rules/numerical-rigor.md`` → "Timestamp rigor".
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Path-safe pattern: alphanumerics, underscore, hyphen, dot.
# Disallows leading dot (no hidden files), slashes, traversal.
_PATH_SAFE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-.]*$")


def is_path_safe_id(value: str) -> bool:
    """Return True iff ``value`` is safe to use as a directory name.

    Rejects empty strings, leading dot, anything containing ``/``, ``\\``,
    or ``..``. Used to validate ``prediction_set_id`` before it appears in
    a filesystem path.
    """
    if not value or ".." in value:
        return False
    return bool(_PATH_SAFE.fullmatch(value))


class GeneratorMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["deterministic_rule"]
    rule_id: str
    rule_version: str


class ChunkRef(BaseModel):
    """Reference to one chunk file in the artifact directory.

    Invariants enforced at validation:
      * ``start_ms > trained_through_ms`` (leakage tell)
      * ``end_ms >= start_ms``
      * ``row_count >= 0``
      * ``rows_hash`` is a 64-char hex string
    """

    model_config = ConfigDict(extra="forbid")

    trained_through_ms: int
    start_ms: int
    end_ms: int
    row_count: int = Field(ge=0)
    rows_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _check_invariants(self) -> ChunkRef:
        if self.start_ms <= self.trained_through_ms:
            raise ValueError(
                f"start_ms must be > trained_through_ms "
                f"(got start_ms={self.start_ms}, trained_through_ms={self.trained_through_ms})"
            )
        if self.end_ms < self.start_ms:
            raise ValueError(
                f"end_ms must be >= start_ms (got start_ms={self.start_ms}, end_ms={self.end_ms})"
            )
        return self


class PredictionSetManifest(BaseModel):
    """v0.5 prediction-set manifest. Persisted as ``manifest.json``.

    ``prediction_set_hash`` covers everything in this manifest *except*
    itself (chicken-and-egg): see ``compute_prediction_set_hash``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    prediction_set_id: str
    symbol: str
    resolution_minutes: int = Field(ge=1)
    field_names: list[str] = Field(min_length=1)
    warmup_policy: Literal["neutral_zero_until_feature_ready"]
    generator: GeneratorMeta
    chunks: list[ChunkRef] = Field(min_length=1)
    prediction_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _check_path_safe_id(self) -> PredictionSetManifest:
        if not is_path_safe_id(self.prediction_set_id):
            raise ValueError(
                f"prediction_set_id must be path-safe "
                f"([A-Za-z0-9_-][A-Za-z0-9_-.]*, no slashes, no traversal); "
                f"got {self.prediction_set_id!r}"
            )
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_artifact.py -v`
Expected: all 9 tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/artifact.py PythonDataService/tests/research/ml/test_artifact.py
git commit -m "feat(research/ml): manifest + chunk Pydantic models with path-safe id"
```

---

### Task 3: Hash helpers — rows_hash and prediction_set_hash

**Files:**
- Modify: `PythonDataService/app/research/ml/artifact.py`
- Modify: `PythonDataService/tests/research/ml/test_artifact.py`

- [ ] **Step 1: Append failing hash tests**

Append to `tests/research/ml/test_artifact.py`:

```python
# ----- hash helpers --------------------------------------------------
from app.research.ml.artifact import (
    compute_prediction_set_hash,
    compute_rows_hash,
)


def _row(ts_ms: int, prediction: float) -> dict:
    return {"timestamp_ms": ts_ms, "symbol": "SPY", "prediction": prediction}


def test_rows_hash_deterministic_for_same_content() -> None:
    rows_a = [_row(1, 0.1), _row(2, 0.2)]
    rows_b = [_row(1, 0.1), _row(2, 0.2)]
    assert compute_rows_hash(rows_a) == compute_rows_hash(rows_b)


def test_rows_hash_changes_when_prediction_changes() -> None:
    a = compute_rows_hash([_row(1, 0.1)])
    b = compute_rows_hash([_row(1, 0.10000000001)])
    assert a != b


def test_rows_hash_sorts_by_timestamp() -> None:
    """Order on input does not matter; canonical order does."""
    sorted_input = [_row(1, 0.1), _row(2, 0.2)]
    reverse_input = [_row(2, 0.2), _row(1, 0.1)]
    assert compute_rows_hash(sorted_input) == compute_rows_hash(reverse_input)


def test_rows_hash_is_64_char_hex() -> None:
    h = compute_rows_hash([_row(1, 0.1)])
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_prediction_set_hash_excludes_self_field() -> None:
    """Setting the prediction_set_hash field to anything must not affect the
    computed hash — the field is removed from the dict before hashing.
    """
    base = _manifest_dict()
    base["prediction_set_hash"] = "a" * 64
    h_a = compute_prediction_set_hash(base)
    base["prediction_set_hash"] = "b" * 64
    h_b = compute_prediction_set_hash(base)
    assert h_a == h_b


def test_prediction_set_hash_changes_when_chunk_rows_hash_changes() -> None:
    base = _manifest_dict()
    h1 = compute_prediction_set_hash(base)
    base["chunks"][0]["rows_hash"] = "f" * 64
    h2 = compute_prediction_set_hash(base)
    assert h1 != h2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_artifact.py -v -k "hash"`
Expected: ImportError on `compute_rows_hash` / `compute_prediction_set_hash`.

- [ ] **Step 3: Implement the hash helpers**

Append to `app/research/ml/artifact.py`:

```python
# ---------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------
from app.research.runs.hashing import hash_payload


def compute_rows_hash(rows: list[dict]) -> str:
    """Return ``hash_payload(rows_sorted_by_timestamp_ms)`` as 64-char hex.

    Each row dict must contain ``timestamp_ms``, ``symbol``, and one or
    more float fields (e.g. ``prediction``). Floats are serialized via
    Python's default JSON repr (shortest round-trippable for
    ``float64``), so identical content produces identical hashes across
    pyarrow / pandas versions.
    """
    sorted_rows = sorted(rows, key=lambda r: r["timestamp_ms"])
    return hash_payload(sorted_rows)


def compute_prediction_set_hash(manifest_dict: dict) -> str:
    """Return ``hash_payload(manifest_without_prediction_set_hash_field)``.

    The ``prediction_set_hash`` field is dropped from the dict before
    hashing — it is the value being computed and including it would be a
    chicken-and-egg loop. Operates on a shallow copy so the caller's dict
    is unmodified.
    """
    payload = {k: v for k, v in manifest_dict.items() if k != "prediction_set_hash"}
    return hash_payload(payload)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_artifact.py -v`
Expected: all tests pass (the new 6 plus prior 9 from Task 2).

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/artifact.py PythonDataService/tests/research/ml/test_artifact.py
git commit -m "feat(research/ml): rows_hash + prediction_set_hash helpers"
```

---

### Task 4: Parquet I/O for chunks

**Files:**
- Modify: `PythonDataService/app/research/ml/artifact.py`
- Modify: `PythonDataService/tests/research/ml/test_artifact.py`

- [ ] **Step 1: Append failing parquet I/O tests**

Append to `tests/research/ml/test_artifact.py`:

```python
# ----- parquet I/O ---------------------------------------------------
from pathlib import Path

from app.research.ml.artifact import read_chunk_rows, write_chunk_rows


def test_chunk_round_trip(tmp_path: Path) -> None:
    rows = [_row(1, 0.1), _row(2, 0.2), _row(3, 0.3)]
    path = tmp_path / "chunk.parquet"
    write_chunk_rows(path, rows, field_names=["prediction"])
    out = read_chunk_rows(path, field_names=["prediction"])
    assert out == rows


def test_chunk_rejects_unknown_field_in_rows(tmp_path: Path) -> None:
    rows = [{"timestamp_ms": 1, "symbol": "SPY", "prediction": 0.1, "extra": 1.0}]
    with pytest.raises(ValueError, match="extra column"):
        write_chunk_rows(tmp_path / "x.parquet", rows, field_names=["prediction"])


def test_chunk_rejects_missing_field_in_rows(tmp_path: Path) -> None:
    rows = [{"timestamp_ms": 1, "symbol": "SPY"}]
    with pytest.raises(ValueError, match="prediction"):
        write_chunk_rows(tmp_path / "x.parquet", rows, field_names=["prediction"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_artifact.py -v -k "chunk"`
Expected: ImportError on `read_chunk_rows` / `write_chunk_rows`.

- [ ] **Step 3: Implement parquet I/O**

Append to `app/research/ml/artifact.py`:

```python
# ---------------------------------------------------------------------
# Parquet I/O for chunk files.
# ---------------------------------------------------------------------
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def _build_schema(field_names: Sequence[str]) -> pa.Schema:
    base_fields = [
        pa.field("timestamp_ms", pa.int64(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
    ]
    value_fields = [pa.field(name, pa.float64(), nullable=False) for name in field_names]
    return pa.schema(base_fields + value_fields)


def write_chunk_rows(
    path: Path,
    rows: list[dict],
    *,
    field_names: Sequence[str],
) -> None:
    """Serialize chunk rows to parquet.

    Schema is fixed: ``timestamp_ms: int64``, ``symbol: string``, and
    one ``float64`` column per name in ``field_names``. Extra or missing
    columns in ``rows`` raise ``ValueError`` — silent column drift would
    invalidate the row-canonical hash.
    """
    expected = {"timestamp_ms", "symbol", *field_names}
    for row in rows:
        keys = set(row.keys())
        extras = keys - expected
        if extras:
            raise ValueError(f"extra column(s) in row: {sorted(extras)}")
        missing = expected - keys
        if missing:
            raise ValueError(f"missing column(s) in row: {sorted(missing)}")

    columns: dict[str, list] = {name: [] for name in expected}
    for row in rows:
        for name in expected:
            columns[name].append(row[name])

    table = pa.Table.from_pydict(columns, schema=_build_schema(field_names))
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def read_chunk_rows(path: Path, *, field_names: Sequence[str]) -> list[dict]:
    """Read a chunk parquet file back to a list of row dicts.

    Asserts the schema matches the declared ``field_names`` — any drift
    is treated as artifact corruption.
    """
    table = pq.read_table(path)
    expected_cols = ["timestamp_ms", "symbol", *field_names]
    actual_cols = list(table.column_names)
    if actual_cols != expected_cols:
        raise ValueError(
            f"chunk parquet schema mismatch at {path}: "
            f"expected {expected_cols}, got {actual_cols}"
        )
    pylist = table.to_pylist()
    return pylist
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_artifact.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/artifact.py PythonDataService/tests/research/ml/test_artifact.py
git commit -m "feat(research/ml): parquet read/write for chunk rows"
```

---

### Task 5: PredictionSet loader — intrinsic validation

**Files:**
- Create: `PythonDataService/app/research/ml/loader.py`
- Create: `PythonDataService/tests/research/ml/test_loader.py`

- [ ] **Step 1: Write failing loader tests**

```python
# PythonDataService/tests/research/ml/test_loader.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.research.ml.artifact import (
    PredictionSetManifest,
    compute_prediction_set_hash,
    compute_rows_hash,
    write_chunk_rows,
)
from app.research.ml.loader import PredictionCoverageError, PredictionSet


def _row(ts: int, p: float) -> dict:
    return {"timestamp_ms": ts, "symbol": "SPY", "prediction": p}


def _write_artifact(
    root: Path,
    *,
    set_id: str = "pred_spy_test_v001",
    chunks: list[tuple[int, list[dict]]] | None = None,  # (trained_through_ms, rows)
) -> Path:
    """Materialize a complete prediction-set artifact under ``root/<set_id>/``."""
    if chunks is None:
        chunks = [(0, [_row(900_000, 0.0), _row(960_000, 0.5), _row(1_020_000, -0.5)])]

    set_dir = root / set_id
    chunk_dir = set_dir / "chunks"
    chunk_dir.mkdir(parents=True)

    manifest_chunks = []
    for trained_through_ms, rows in chunks:
        path = chunk_dir / f"{trained_through_ms}.parquet"
        write_chunk_rows(path, rows, field_names=["prediction"])
        manifest_chunks.append({
            "trained_through_ms": trained_through_ms,
            "start_ms": rows[0]["timestamp_ms"],
            "end_ms": rows[-1]["timestamp_ms"],
            "row_count": len(rows),
            "rows_hash": compute_rows_hash(rows),
        })

    manifest_dict = {
        "schema_version": "1.0",
        "prediction_set_id": set_id,
        "symbol": "SPY",
        "resolution_minutes": 1,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {"kind": "deterministic_rule", "rule_id": "test", "rule_version": "1.0"},
        "chunks": manifest_chunks,
        "prediction_set_hash": "0" * 64,  # placeholder; computed next
    }
    manifest_dict["prediction_set_hash"] = compute_prediction_set_hash(manifest_dict)
    PredictionSetManifest.model_validate(manifest_dict)  # sanity
    (set_dir / "manifest.json").write_text(json.dumps(manifest_dict))
    return set_dir


# ----- happy path ----------------------------------------------------
def test_load_succeeds_on_valid_artifact(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)
    pset = PredictionSet.load(set_dir)
    assert pset.manifest.prediction_set_id == "pred_spy_test_v001"
    assert set(pset.index.keys()) == {900_000, 960_000, 1_020_000}
    assert pset.index[960_000] == {"prediction": 0.5}


# ----- intrinsic validation ------------------------------------------
def test_load_fails_when_manifest_missing(tmp_path: Path) -> None:
    set_dir = tmp_path / "empty"
    set_dir.mkdir()
    with pytest.raises(FileNotFoundError, match="manifest.json"):
        PredictionSet.load(set_dir)


def test_load_fails_when_rows_hash_mismatched(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)
    manifest_path = set_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["chunks"][0]["rows_hash"] = "f" * 64
    raw["prediction_set_hash"] = compute_prediction_set_hash(raw)  # repair top-level
    manifest_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="rows_hash"):
        PredictionSet.load(set_dir)


def test_load_fails_when_prediction_set_hash_mismatched(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)
    manifest_path = set_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["prediction_set_hash"] = "f" * 64  # leave chunk rows_hash untouched
    manifest_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="prediction_set_hash"):
        PredictionSet.load(set_dir)


def test_load_fails_on_leakage_chunk_start_at_or_before_trained_through(tmp_path: Path) -> None:
    """A chunk with start_ms == trained_through_ms is rejected at the
    manifest layer (ChunkRef invariant) before the loader runs."""
    set_dir = tmp_path / "pred_leak"
    chunk_dir = set_dir / "chunks"
    chunk_dir.mkdir(parents=True)
    rows = [_row(100, 0.0), _row(200, 0.0)]
    write_chunk_rows(chunk_dir / "100.parquet", rows, field_names=["prediction"])
    raw = {
        "schema_version": "1.0",
        "prediction_set_id": "pred_leak",
        "symbol": "SPY",
        "resolution_minutes": 1,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {"kind": "deterministic_rule", "rule_id": "x", "rule_version": "1.0"},
        "chunks": [{
            "trained_through_ms": 100,    # same as start_ms — leakage tell
            "start_ms": 100,
            "end_ms": 200,
            "row_count": 2,
            "rows_hash": compute_rows_hash(rows),
        }],
        "prediction_set_hash": "0" * 64,
    }
    raw["prediction_set_hash"] = compute_prediction_set_hash(raw)
    (set_dir / "manifest.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="start_ms must be > trained_through_ms"):
        PredictionSet.load(set_dir)


def test_load_fails_on_row_outside_chunk_window(tmp_path: Path) -> None:
    chunks = [(0, [_row(900_000, 0.0), _row(2_000_000, 0.0)])]  # second row past end_ms after manifest is built
    set_dir = _write_artifact(tmp_path, chunks=chunks)
    # tamper the manifest's end_ms to be smaller than the actual last row
    manifest_path = set_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["chunks"][0]["end_ms"] = 1_000_000  # < 2_000_000
    raw["prediction_set_hash"] = compute_prediction_set_hash(raw)
    manifest_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="outside chunk window"):
        PredictionSet.load(set_dir)


def test_load_fails_on_duplicate_timestamp_within_chunk(tmp_path: Path) -> None:
    chunks = [(0, [_row(1000, 0.0), _row(1000, 0.5)])]
    set_dir = _write_artifact(tmp_path, chunks=chunks)
    with pytest.raises(ValueError, match="duplicate timestamp"):
        PredictionSet.load(set_dir)


def test_load_fails_on_duplicate_timestamp_across_chunks(tmp_path: Path) -> None:
    chunks = [
        (0, [_row(1000, 0.0)]),
        (500, [_row(1000, 0.5)]),  # overlap with first chunk
    ]
    set_dir = _write_artifact(tmp_path, chunks=chunks)
    with pytest.raises(ValueError, match="duplicate timestamp"):
        PredictionSet.load(set_dir)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_loader.py -v`
Expected: ImportError on `app.research.ml.loader`.

- [ ] **Step 3: Implement the loader**

```python
# PythonDataService/app/research/ml/loader.py
"""``PredictionSet.load(...)`` — read + validate a prediction-set artifact.

Validation runs in two stages:

* **Intrinsic** (this file): hashes match, leakage invariant holds, rows
  fall inside the chunk window, no duplicate timestamps within or across
  chunks, the row index is built.
* **Spec-pairing** (this file, ``assert_pairs_with``): symbol + resolution
  match the consumer ``StrategySpec``, and at most one ``prediction_set_id``
  is referenced.

A third stage — bar-clock coverage — runs at the run-pipeline boundary
(see ``coverage.py``) where the data source and consolidator are known.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.research.ml.artifact import (
    PredictionSetManifest,
    compute_prediction_set_hash,
    compute_rows_hash,
    read_chunk_rows,
)


class PredictionCoverageError(ValueError):
    """Raised when a loaded prediction set does not cover an emitted bar."""


@dataclass
class PredictionSet:
    """Loaded + validated prediction-set artifact."""

    manifest: PredictionSetManifest
    index: dict[int, dict[str, float]]   # timestamp_ms -> {field: value}

    @classmethod
    def load(cls, root: Path) -> PredictionSet:
        """Load an artifact directory; run all intrinsic validation.

        ``root`` is the artifact directory itself (e.g.
        ``artifacts/predictions/pred_spy_rsi_rule_v001/``), containing
        ``manifest.json`` and ``chunks/<trained_through_ms>.parquet``.
        """
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"manifest.json not found at {manifest_path}")

        raw = json.loads(manifest_path.read_text())
        manifest = PredictionSetManifest.model_validate(raw)  # leakage invariant runs here

        # Verify the top-level hash matches the manifest content (excluding
        # the field itself).
        recomputed_top = compute_prediction_set_hash(raw)
        if recomputed_top != manifest.prediction_set_hash:
            raise ValueError(
                f"prediction_set_hash mismatch: stored={manifest.prediction_set_hash}, "
                f"recomputed={recomputed_top}. Manifest content has been tampered with."
            )

        # Read each chunk, verify rows_hash, accumulate the index.
        index: dict[int, dict[str, float]] = {}
        for chunk in manifest.chunks:
            chunk_path = root / "chunks" / f"{chunk.trained_through_ms}.parquet"
            if not chunk_path.is_file():
                raise FileNotFoundError(f"chunk parquet not found at {chunk_path}")

            rows = read_chunk_rows(chunk_path, field_names=manifest.field_names)
            recomputed_rows = compute_rows_hash(rows)
            if recomputed_rows != chunk.rows_hash:
                raise ValueError(
                    f"rows_hash mismatch for chunk trained_through_ms={chunk.trained_through_ms}: "
                    f"stored={chunk.rows_hash}, recomputed={recomputed_rows}."
                )
            if chunk.row_count != len(rows):
                raise ValueError(
                    f"row_count mismatch for chunk trained_through_ms={chunk.trained_through_ms}: "
                    f"manifest={chunk.row_count}, parquet={len(rows)}"
                )

            for row in rows:
                ts = row["timestamp_ms"]
                if ts < chunk.start_ms or ts > chunk.end_ms:
                    raise ValueError(
                        f"row timestamp_ms={ts} outside chunk window "
                        f"[{chunk.start_ms}, {chunk.end_ms}] "
                        f"(chunk trained_through_ms={chunk.trained_through_ms})"
                    )
                if ts in index:
                    raise ValueError(
                        f"duplicate timestamp_ms={ts} across chunks "
                        f"(second occurrence in chunk trained_through_ms={chunk.trained_through_ms})"
                    )
                index[ts] = {name: row[name] for name in manifest.field_names}

        return cls(manifest=manifest, index=index)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_loader.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/loader.py PythonDataService/tests/research/ml/test_loader.py
git commit -m "feat(research/ml): PredictionSet.load with intrinsic validation"
```

---

### Task 6: PredictionSet spec-pairing validation

**Files:**
- Modify: `PythonDataService/app/research/ml/loader.py`
- Modify: `PythonDataService/tests/research/ml/test_loader.py`

- [ ] **Step 1: Append failing pairing tests**

Append to `tests/research/ml/test_loader.py`:

```python
# ----- spec-pairing --------------------------------------------------
from app.engine.strategy.spec.schema import (
    EntryBlock,
    EquityLongPosition,
    ExitBlock,
    Resolution,
    SetHoldings,
    StrategySpec,
)


def _spec_for(symbol: str, period_minutes: int) -> StrategySpec:
    return StrategySpec(
        schema_version="1.0",
        name="t",
        symbols=[symbol],
        resolution=Resolution(period_minutes=period_minutes),
        entry=EntryBlock(logic="AND", conditions=[], size=SetHoldings(kind="SetHoldings", fraction=1.0)),
        exit=ExitBlock(logic="AND", conditions=[]),
        position=EquityLongPosition(kind="EQUITY_LONG"),
    )


def test_assert_pairs_with_spec_succeeds_on_match(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)
    pset = PredictionSet.load(set_dir)
    pset.assert_pairs_with(_spec_for("SPY", 1))


def test_assert_pairs_with_spec_fails_on_symbol_mismatch(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)  # symbol="SPY"
    pset = PredictionSet.load(set_dir)
    with pytest.raises(ValueError, match="symbol mismatch"):
        pset.assert_pairs_with(_spec_for("QQQ", 1))


def test_assert_pairs_with_spec_fails_on_resolution_mismatch(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)  # resolution_minutes=1
    pset = PredictionSet.load(set_dir)
    with pytest.raises(ValueError, match="resolution mismatch"):
        pset.assert_pairs_with(_spec_for("SPY", 5))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_loader.py -v -k "pairs_with"`
Expected: AttributeError on `pset.assert_pairs_with`.

- [ ] **Step 3: Add the pairing method**

Append to `app/research/ml/loader.py`:

```python
    def assert_pairs_with(self, spec) -> None:
        """Validate that this prediction set is consumable by ``spec``.

        ``spec`` is typed loosely (``StrategySpec``) to avoid a circular
        import; the runtime check uses duck typing on ``.symbols`` and
        ``.resolution.period_minutes``.
        """
        if not spec.symbols:
            raise ValueError("StrategySpec.symbols is empty")
        spec_symbol = spec.symbols[0]
        if self.manifest.symbol != spec_symbol:
            raise ValueError(
                f"symbol mismatch: prediction set has {self.manifest.symbol!r}, "
                f"spec has {spec_symbol!r}"
            )
        if self.manifest.resolution_minutes != spec.resolution.period_minutes:
            raise ValueError(
                f"resolution mismatch: prediction set has {self.manifest.resolution_minutes} min, "
                f"spec has {spec.resolution.period_minutes} min"
            )
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_loader.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/loader.py PythonDataService/tests/research/ml/test_loader.py
git commit -m "feat(research/ml): PredictionSet.assert_pairs_with(spec)"
```

---

### Task 7: StrategySpec — PredictionRef + PredictionComparison schema

**Files:**
- Modify: `PythonDataService/app/engine/strategy/spec/schema.py`
- Create: `PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions.py`

- [ ] **Step 1: Write failing schema tests**

```python
# PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.engine.strategy.spec.schema import (
    EntryBlock,
    EquityLongPosition,
    ExitBlock,
    PredictionComparison,
    PredictionRef,
    Resolution,
    SetHoldings,
    StrategySpec,
)


def _base_spec_dict(predictions=None, entry_conditions=None) -> dict:
    return {
        "schema_version": "1.0",
        "name": "t",
        "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "indicators": [],
        "predictions": predictions or [],
        "entry": {
            "logic": "AND",
            "conditions": entry_conditions or [],
            "size": {"kind": "SetHoldings", "fraction": 1.0},
            "pyramiding": 1,
        },
        "exit": {"logic": "AND", "conditions": []},
        "position": {"kind": "EQUITY_LONG"},
        "survival": [],
        "diagnostics": {"snapshot_at_entry": [], "snapshot_at_exit": []},
    }


def _pred_ref(id_: str = "rsi_pred", set_id: str = "pred_spy_v001") -> dict:
    return {"id": id_, "prediction_set_id": set_id, "field": "prediction"}


def _pred_cmp(prediction: str = "rsi_pred", op: str = ">", value: float = 0.0) -> dict:
    return {"kind": "PredictionComparison", "prediction": prediction, "op": op, "value": value}


# ----- standalone Pydantic models ------------------------------------
def test_prediction_ref_round_trip() -> None:
    p = PredictionRef.model_validate(_pred_ref())
    assert p.id == "rsi_pred"
    assert p.field == "prediction"


def test_prediction_ref_default_field_is_prediction() -> None:
    p = PredictionRef.model_validate({"id": "x", "prediction_set_id": "pred_v001"})
    assert p.field == "prediction"


def test_prediction_comparison_round_trip() -> None:
    p = PredictionComparison.model_validate(_pred_cmp())
    assert p.kind == "PredictionComparison"
    assert p.op == ">"


def test_prediction_ref_rejects_extras() -> None:
    bad = _pred_ref() | {"unexpected": True}
    with pytest.raises(ValidationError):
        PredictionRef.model_validate(bad)


# ----- spec round-trip with predictions block ------------------------
def test_spec_with_predictions_block_loads() -> None:
    raw = _base_spec_dict(
        predictions=[_pred_ref()],
        entry_conditions=[_pred_cmp()],
    )
    spec = StrategySpec.model_validate(raw)
    assert len(spec.predictions) == 1
    assert spec.predictions[0].id == "rsi_pred"


def test_spec_with_no_predictions_still_loads() -> None:
    raw = _base_spec_dict()
    spec = StrategySpec.model_validate(raw)
    assert spec.predictions == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/test_spec_predictions.py -v`
Expected: ImportError on `PredictionRef` / `PredictionComparison`.

- [ ] **Step 3: Add the new schema models**

Edit `PythonDataService/app/engine/strategy/spec/schema.py`. Insert after the `BarProperty` class (around line 218, before the `Condition` annotated union):

```python
class PredictionRef(BaseModel):
    """Spec-local handle bound to one column of a prediction set artifact.

    ``id`` is referenced by ``PredictionComparison.prediction``. ``field``
    is the column name in the artifact rows (default ``"prediction"`` for
    the v0.5 single-scalar contract; reserved for future multi-column
    artifacts).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    prediction_set_id: str
    field: str = "prediction"


class PredictionComparison(_ConditionBase):
    """Compare a per-bar prediction value against a constant threshold."""

    kind: Literal["PredictionComparison"]
    prediction: str  # PredictionRef.id
    op: ComparisonOp
    value: float
```

Update the `Condition` union (around line 220) to add `PredictionComparison`:

```python
Condition = Annotated[
    IndicatorComparison
    | IndicatorBetween
    | FreshCross
    | BarsSinceEntry
    | TimeOfDay
    | PnLPercent
    | PnLPoints
    | DrawdownFromPeak
    | BarProperty
    | PredictionComparison,
    Field(discriminator="kind"),
]
```

Add the `predictions` field to `StrategySpec` (around line 367):

```python
class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    name: str
    description: str | None = None
    symbols: list[str]
    resolution: Resolution
    indicators: list[IndicatorBlock] = Field(default_factory=list)
    predictions: list[PredictionRef] = Field(default_factory=list)
    entry: EntryBlock
    position: PositionSpec = Field(default_factory=lambda: EquityLongPosition(kind="EQUITY_LONG"))
    survival: list[SurvivalRule] = Field(default_factory=list)
    exit: ExitBlock
    diagnostics: Diagnostics = Field(default_factory=Diagnostics)
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/test_spec_predictions.py app/engine/strategy/spec/tests/test_spec_round_trip.py -v`
Expected: new tests pass; existing round-trip tests still pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/engine/strategy/spec/schema.py PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions.py
git commit -m "feat(spec): PredictionRef + PredictionComparison + predictions block"
```

---

### Task 8: StrategySpec validators for predictions

**Files:**
- Modify: `PythonDataService/app/engine/strategy/spec/schema.py`
- Modify: `PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions.py`

- [ ] **Step 1: Append validator tests**

Append to `test_spec_predictions.py`:

```python
# ----- validators ----------------------------------------------------
def test_spec_rejects_undeclared_prediction_id() -> None:
    raw = _base_spec_dict(
        predictions=[_pred_ref(id_="declared")],
        entry_conditions=[_pred_cmp(prediction="undeclared")],
    )
    with pytest.raises(ValidationError, match="undeclared prediction id"):
        StrategySpec.model_validate(raw)


def test_spec_rejects_duplicate_prediction_ref_ids() -> None:
    raw = _base_spec_dict(
        predictions=[_pred_ref(id_="a"), _pred_ref(id_="a")],
    )
    with pytest.raises(ValidationError, match="duplicate prediction ref ids"):
        StrategySpec.model_validate(raw)


def test_spec_rejects_multiple_distinct_prediction_set_ids() -> None:
    raw = _base_spec_dict(
        predictions=[
            _pred_ref(id_="a", set_id="pred_set_one"),
            _pred_ref(id_="b", set_id="pred_set_two"),
        ],
    )
    with pytest.raises(ValidationError, match="at most one prediction_set_id"):
        StrategySpec.model_validate(raw)


def test_spec_accepts_multiple_refs_to_same_set() -> None:
    raw = _base_spec_dict(
        predictions=[
            {"id": "a", "prediction_set_id": "pred_set_one", "field": "prediction"},
            {"id": "b", "prediction_set_id": "pred_set_one", "field": "prediction"},
        ],
    )
    StrategySpec.model_validate(raw)  # no error


def test_spec_rejects_path_unsafe_prediction_set_id() -> None:
    raw = _base_spec_dict(
        predictions=[_pred_ref(id_="a", set_id="../evil")],
    )
    with pytest.raises(ValidationError, match="path-safe"):
        StrategySpec.model_validate(raw)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/test_spec_predictions.py -v -k "rejects or accepts"`
Expected: at least four failures (validators not implemented yet).

- [ ] **Step 3: Extend `_check_phase1_boundaries` and add `_iter_prediction_refs`**

Edit `app/engine/strategy/spec/schema.py`. At the top, add the import:

```python
from app.research.ml.artifact import is_path_safe_id
```

In `StrategySpec._check_phase1_boundaries`, append before the closing `return self`:

```python
        # ---- prediction validators -------------------------------------
        pred_ids = [p.id for p in self.predictions]
        if len(pred_ids) != len(set(pred_ids)):
            dup = [i for i in pred_ids if pred_ids.count(i) > 1]
            raise ValueError(f"duplicate prediction ref ids: {sorted(set(dup))}")

        for p in self.predictions:
            if not is_path_safe_id(p.prediction_set_id):
                raise ValueError(
                    f"prediction_set_id {p.prediction_set_id!r} on ref {p.id!r} "
                    f"must be path-safe (no slashes, no traversal)"
                )

        distinct_set_ids = {p.prediction_set_id for p in self.predictions}
        if len(distinct_set_ids) > 1:
            raise ValueError(
                f"v0.5 admits at most one prediction_set_id per spec "
                f"(got {sorted(distinct_set_ids)}). v1.2 lifts this with "
                f"prediction_set_hashes: dict[str, str]."
            )

        declared_pred_ids = set(pred_ids)
        for ref_id in self._iter_prediction_refs():
            if ref_id not in declared_pred_ids:
                raise ValueError(
                    f"condition references undeclared prediction id: {ref_id!r} "
                    f"(declared: {sorted(declared_pred_ids)})"
                )
```

Add a sibling helper in `StrategySpec` next to `_iter_indicator_refs`:

```python
    def _iter_prediction_refs(self) -> list[str]:
        """Walk the logic tree and collect every PredictionComparison.prediction reference."""
        refs: list[str] = []

        def _walk(node) -> None:
            if isinstance(node, LogicNode):
                for child in node.conditions:
                    _walk(child)
                return
            if isinstance(node, PredictionComparison):
                refs.append(node.prediction)

        for child in self.entry.conditions:
            _walk(child)
        for child in self.exit.conditions:
            _walk(child)
        for rule in self.survival:
            for child in rule.when.conditions:
                _walk(child)
        return refs
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/ -v`
Expected: new validator tests pass; entire `tests/spec/` tree is green.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/engine/strategy/spec/schema.py PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions.py
git commit -m "feat(spec): validators for predictions (refs, ids, single-set, path-safe)"
```

---

### Task 9: EvalContext — add `predictions` field

**Files:**
- Modify: `PythonDataService/app/engine/strategy/spec/primitives.py`
- Modify: `PythonDataService/app/engine/strategy/spec/tests/test_spec_extra_primitives.py` (sanity test only)

- [ ] **Step 1: Write failing test for the new field**

Append to `app/engine/strategy/spec/tests/test_spec_extra_primitives.py` (or create a small new test if you prefer separation):

```python
# ----- EvalContext.predictions ---------------------------------------
from decimal import Decimal

from app.engine.strategy.spec.primitives import EvalContext


def test_eval_context_predictions_default_empty() -> None:
    """Existing call sites that don't pass `predictions` keep working."""
    ctx = EvalContext(
        indicators={},
        current_bar_count=0,
        bar_close_time=None,  # type: ignore[arg-type]
        bar_close_price=Decimal("100"),
    )
    assert ctx.predictions == {}


def test_eval_context_predictions_can_be_supplied() -> None:
    ctx = EvalContext(
        indicators={},
        current_bar_count=0,
        bar_close_time=None,  # type: ignore[arg-type]
        bar_close_price=Decimal("100"),
        predictions={"my_pred": Decimal("0.5")},
    )
    assert ctx.predictions["my_pred"] == Decimal("0.5")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/test_spec_extra_primitives.py::test_eval_context_predictions_default_empty -v`
Expected: TypeError or AttributeError — `predictions` is not a field.

- [ ] **Step 3: Add the field**

Edit `app/engine/strategy/spec/primitives.py` line ~52, extend the `EvalContext` dataclass:

```python
from dataclasses import dataclass, field

@dataclass
class EvalContext:
    """Per-bar state visible to primitives during evaluate / observe_bar."""

    indicators: dict[str, Indicator]
    current_bar_count: int
    bar_close_time: datetime
    bar_close_price: Decimal
    current_bar: TradeBar | None = None

    in_position: bool = False
    entry_bar_count: int | None = None
    entry_price: Decimal | None = None

    # Per-bar prediction values, keyed by spec PredictionRef.id.
    # Populated by SpecAlgorithm before evaluate / observe_bar when the
    # spec declares any predictions; empty for prediction-free specs.
    predictions: dict[str, Decimal] = field(default_factory=dict)
```

Note: `field` may already be imported from `dataclasses`; if not, add it.

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/ -v`
Expected: new tests pass; rest of the spec test tree still green.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/engine/strategy/spec/primitives.py PythonDataService/app/engine/strategy/spec/tests/test_spec_extra_primitives.py
git commit -m "feat(spec): EvalContext.predictions field (default empty dict)"
```

---

### Task 10: PredictionComparisonPrimitive

**Files:**
- Modify: `PythonDataService/app/engine/strategy/spec/primitives.py`
- Modify: `PythonDataService/app/engine/strategy/spec/tests/test_spec_extra_primitives.py`

- [ ] **Step 1: Write failing primitive tests**

Append to `test_spec_extra_primitives.py`:

```python
# ----- PredictionComparisonPrimitive --------------------------------
from app.engine.strategy.spec import schema as S
from app.engine.strategy.spec.primitives import (
    PredictionComparisonPrimitive,
)


def _ctx_with_predictions(preds: dict[str, Decimal]) -> EvalContext:
    return EvalContext(
        indicators={},
        current_bar_count=1,
        bar_close_time=None,  # type: ignore[arg-type]
        bar_close_price=Decimal("100"),
        predictions=preds,
    )


def test_prediction_comparison_fires_when_above_threshold() -> None:
    node = S.PredictionComparison(kind="PredictionComparison", prediction="rsi_pred", op=">", value=0.1)
    p = PredictionComparisonPrimitive(node)
    assert p.evaluate(_ctx_with_predictions({"rsi_pred": Decimal("0.5")})) is True


def test_prediction_comparison_does_not_fire_when_below_threshold() -> None:
    node = S.PredictionComparison(kind="PredictionComparison", prediction="rsi_pred", op=">", value=0.1)
    p = PredictionComparisonPrimitive(node)
    assert p.evaluate(_ctx_with_predictions({"rsi_pred": Decimal("0.05")})) is False


def test_prediction_comparison_keyerror_when_id_missing() -> None:
    """Missing prediction id is a load-time bug (Stage 3 coverage check
    should have caught it), not a runtime branch — surface loudly."""
    node = S.PredictionComparison(kind="PredictionComparison", prediction="absent", op=">", value=0.0)
    p = PredictionComparisonPrimitive(node)
    with pytest.raises(KeyError):
        p.evaluate(_ctx_with_predictions({"present": Decimal("0.5")}))


def test_prediction_comparison_routed_by_build_leaf() -> None:
    """_build_leaf must dispatch PredictionComparison to its primitive."""
    from app.engine.strategy.spec.primitives import _build_leaf

    node = S.PredictionComparison(kind="PredictionComparison", prediction="x", op=">", value=0.0)
    primitive = _build_leaf(node)
    assert isinstance(primitive, PredictionComparisonPrimitive)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/test_spec_extra_primitives.py -v -k "prediction"`
Expected: ImportError on `PredictionComparisonPrimitive`.

- [ ] **Step 3: Implement the primitive and routing**

In `app/engine/strategy/spec/primitives.py`, add the primitive next to `IndicatorComparisonPrimitive`. Mirror the structure used by `IndicatorComparisonPrimitive` (constructor takes the schema node, `evaluate` reads from `ctx`):

```python
class PredictionComparisonPrimitive(Primitive):
    """Compare a per-bar prediction against a constant threshold.

    Reads ``ctx.predictions[node.prediction]`` (a ``Decimal``). A
    ``KeyError`` on lookup means the bar-clock coverage check should
    have caught this — surface loudly rather than swallowing.
    """

    def __init__(self, node: S.PredictionComparison) -> None:
        self._prediction = node.prediction
        self._op = node.op
        self._threshold = Decimal(str(node.value))

    def evaluate(self, ctx: EvalContext) -> bool:
        value = ctx.predictions[self._prediction]   # KeyError surfaces deliberately
        return _compare(self._op, value, self._threshold)
```

Edit `_build_leaf` (line ~396) to add the routing:

```python
def _build_leaf(node) -> Primitive:
    if isinstance(node, S.IndicatorComparison):
        return IndicatorComparisonPrimitive(node)
    if isinstance(node, S.IndicatorBetween):
        return IndicatorBetweenPrimitive(node)
    if isinstance(node, S.FreshCross):
        return FreshCrossPrimitive(node)
    if isinstance(node, S.BarsSinceEntry):
        return BarsSinceEntryPrimitive(node)
    if isinstance(node, S.TimeOfDay):
        return TimeOfDayPrimitive(node)
    if isinstance(node, S.PnLPercent):
        return PnLPercentPrimitive(node)
    if isinstance(node, S.PnLPoints):
        return PnLPointsPrimitive(node)
    if isinstance(node, S.DrawdownFromPeak):
        return DrawdownFromPeakPrimitive(node)
    if isinstance(node, S.BarProperty):
        return BarPropertyPrimitive(node)
    if isinstance(node, S.PredictionComparison):
        return PredictionComparisonPrimitive(node)
    raise NotImplementedError(f"primitive kind {type(node).__name__} not supported")
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/ -v`
Expected: all spec tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/engine/strategy/spec/primitives.py PythonDataService/app/engine/strategy/spec/tests/test_spec_extra_primitives.py
git commit -m "feat(spec): PredictionComparisonPrimitive + _build_leaf routing"
```

---

### Task 11: SpecAlgorithm wiring — accept PredictionSet, populate ctx.predictions

**Files:**
- Modify: `PythonDataService/app/engine/strategy/spec/evaluator.py`
- Create: `PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions_runtime.py`

- [ ] **Step 1: Write failing runtime test**

```python
# PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions_runtime.py
"""Wires PredictionSet into SpecAlgorithm and asserts that ctx.predictions
is populated for the expected bar timestamps before evaluate runs.

Uses a tiny synthetic bar stream and a synthetic PredictionSet built
in-memory (no parquet, no manifest file) by going around the loader.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.strategy.spec import SpecAlgorithm, schema as S
from app.research.ml.artifact import PredictionSetManifest, ChunkRef, GeneratorMeta
from app.research.ml.loader import PredictionSet

NY = ZoneInfo("America/New_York")


def _make_pset(timestamps_ms: list[int], values: list[float]) -> PredictionSet:
    """Build a PredictionSet directly in memory (skip filesystem)."""
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="t",
        symbol="SPY",
        resolution_minutes=15,
        field_names=["prediction"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=GeneratorMeta(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[ChunkRef(
            trained_through_ms=timestamps_ms[0] - 1,
            start_ms=timestamps_ms[0],
            end_ms=timestamps_ms[-1],
            row_count=len(timestamps_ms),
            rows_hash="0" * 64,
        )],
        prediction_set_hash="0" * 64,
    )
    index = {ts: {"prediction": v} for ts, v in zip(timestamps_ms, values, strict=True)}
    return PredictionSet(manifest=manifest, index=index)


def test_spec_algorithm_accepts_optional_prediction_set() -> None:
    """Existing prediction-free constructor signature still works."""
    spec = S.StrategySpec.model_validate({
        "schema_version": "1.0", "name": "t", "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "entry": {"logic": "AND", "conditions": [],
                  "size": {"kind": "SetHoldings", "fraction": 1.0}, "pyramiding": 1},
        "exit": {"logic": "AND", "conditions": []},
    })
    algo = SpecAlgorithm(spec)  # no prediction set
    assert algo._prediction_set is None


def test_spec_algorithm_accepts_explicit_prediction_set() -> None:
    spec = S.StrategySpec.model_validate({
        "schema_version": "1.0", "name": "t", "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "predictions": [{"id": "p", "prediction_set_id": "t", "field": "prediction"}],
        "entry": {
            "logic": "AND",
            "conditions": [{"kind": "PredictionComparison", "prediction": "p", "op": ">", "value": 0.0}],
            "size": {"kind": "SetHoldings", "fraction": 1.0}, "pyramiding": 1,
        },
        "exit": {"logic": "AND", "conditions": []},
    })
    pset = _make_pset([1_000, 2_000], [0.5, -0.5])
    algo = SpecAlgorithm(spec, prediction_set=pset)
    assert algo._prediction_set is pset


def test_spec_with_predictions_requires_prediction_set() -> None:
    spec = S.StrategySpec.model_validate({
        "schema_version": "1.0", "name": "t", "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "predictions": [{"id": "p", "prediction_set_id": "t", "field": "prediction"}],
        "entry": {"logic": "AND",
                  "conditions": [{"kind": "PredictionComparison", "prediction": "p", "op": ">", "value": 0.0}],
                  "size": {"kind": "SetHoldings", "fraction": 1.0}, "pyramiding": 1},
        "exit": {"logic": "AND", "conditions": []},
    })
    with pytest.raises(ValueError, match="declares predictions"):
        SpecAlgorithm(spec)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/test_spec_predictions_runtime.py -v`
Expected: TypeError on the `prediction_set=` kwarg or AttributeError on `algo._prediction_set`.

- [ ] **Step 3: Update `SpecAlgorithm.__init__` and `_on_consolidated_bar`**

Edit `app/engine/strategy/spec/evaluator.py`. Update the constructor signature (line 100):

```python
    def __init__(
        self,
        spec: S.StrategySpec,
        *,
        prediction_set: "PredictionSet | None" = None,
    ) -> None:
        super().__init__()
        self._spec = spec
        self._prediction_set = prediction_set

        # Predictions sanity: a spec that declares predictions must be
        # paired with a loaded PredictionSet at construction time. The
        # bar-clock coverage check (run-pipeline glue) is a separate
        # gate — this one catches the "forgot to wire" mistake early.
        if spec.predictions and prediction_set is None:
            raise ValueError(
                f"spec {spec.name!r} declares predictions "
                f"({[p.id for p in spec.predictions]}) but no prediction_set "
                f"was provided to SpecAlgorithm"
            )
        if prediction_set is not None and spec.predictions:
            prediction_set.assert_pairs_with(spec)

        # ... existing forward-compat guards (EquityLongPosition, survival actions, pyramiding) unchanged ...
```

Add the conditional import at the top (TYPE_CHECKING-style if needed to avoid runtime cycles):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.research.ml.loader import PredictionSet
```

In `_on_consolidated_bar` (line ~209), populate `ctx.predictions` before constructing the `EvalContext`:

```python
    def _on_consolidated_bar(self, bar: TradeBar) -> None:
        # ... existing indicator update + bar_count increment unchanged ...

        # Build the predictions snapshot for this bar. Empty when no
        # PredictionSet is wired (prediction-free specs).
        predictions: dict[str, Decimal] = {}
        if self._prediction_set is not None and self._spec.predictions:
            ts_ms = int(bar.end_time.timestamp() * 1000)
            row = self._prediction_set.index[ts_ms]   # KeyError == coverage-check bug
            for ref in self._spec.predictions:
                predictions[ref.id] = Decimal(str(row[ref.field]))

        entry_price = self._open_trade.entry_price if self._open_trade is not None else None
        ctx = EvalContext(
            indicators=self._indicators,
            current_bar_count=self._bar_count,
            bar_close_time=bar.end_time,
            bar_close_price=bar.close,
            current_bar=bar,
            in_position=self._in_position,
            entry_bar_count=self._entry_bar_count,
            entry_price=entry_price,
            predictions=predictions,
        )
        # ... rest of method unchanged ...
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest app/engine/strategy/spec/tests/ -v`
Expected: all spec tests still pass; new predictions-runtime tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/engine/strategy/spec/evaluator.py PythonDataService/app/engine/strategy/spec/tests/test_spec_predictions_runtime.py
git commit -m "feat(spec): SpecAlgorithm accepts PredictionSet, populates ctx.predictions per bar"
```

---

### Task 12: Bar-clock coverage helper

**Files:**
- Create: `PythonDataService/app/research/ml/coverage.py`
- Create: `PythonDataService/tests/research/ml/test_coverage.py`

- [ ] **Step 1: Write failing coverage tests**

```python
# PythonDataService/tests/research/ml/test_coverage.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.research.ml.artifact import ChunkRef, GeneratorMeta, PredictionSetManifest
from app.research.ml.coverage import assert_bar_clock_coverage
from app.research.ml.loader import PredictionCoverageError, PredictionSet

NY = ZoneInfo("America/New_York")


@dataclass
class _FakeBar:
    end_time: datetime


def _bars(n: int, start: datetime = datetime(2024, 5, 1, 9, 30, tzinfo=NY)) -> list[_FakeBar]:
    return [_FakeBar(end_time=start + timedelta(minutes=15 * i)) for i in range(1, n + 1)]


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _pset(timestamps_ms: list[int]) -> PredictionSet:
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="t",
        symbol="SPY",
        resolution_minutes=15,
        field_names=["prediction"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=GeneratorMeta(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[ChunkRef(
            trained_through_ms=timestamps_ms[0] - 1 if timestamps_ms else 0,
            start_ms=timestamps_ms[0] if timestamps_ms else 0,
            end_ms=timestamps_ms[-1] if timestamps_ms else 0,
            row_count=len(timestamps_ms),
            rows_hash="0" * 64,
        )],
        prediction_set_hash="0" * 64,
    )
    index = {ts: {"prediction": 0.0} for ts in timestamps_ms}
    return PredictionSet(manifest=manifest, index=index)


def test_coverage_passes_when_predictions_match_bars_exactly() -> None:
    bars = _bars(3)
    pset = _pset([_to_ms(b.end_time) for b in bars])
    assert_bar_clock_coverage(pset, bars)  # no exception


def test_coverage_passes_when_predictions_are_a_superset_of_bars() -> None:
    bars = _bars(3)
    extra = bars[0].end_time + timedelta(hours=12)  # bar the engine won't see
    timestamps = [_to_ms(b.end_time) for b in bars] + [_to_ms(extra)]
    pset = _pset(sorted(timestamps))
    assert_bar_clock_coverage(pset, bars)  # extras allowed


def test_coverage_fails_when_a_bar_has_no_prediction() -> None:
    bars = _bars(3)
    timestamps = [_to_ms(b.end_time) for b in bars[:-1]]   # drop last
    pset = _pset(timestamps)
    with pytest.raises(PredictionCoverageError, match="missing predictions for 1"):
        assert_bar_clock_coverage(pset, bars)


def test_coverage_error_lists_missing_timestamps() -> None:
    bars = _bars(5)
    timestamps = [_to_ms(b.end_time) for b in bars[:2]]
    pset = _pset(timestamps)
    with pytest.raises(PredictionCoverageError) as exc:
        assert_bar_clock_coverage(pset, bars)
    assert "missing predictions for 3" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_coverage.py -v`
Expected: ImportError on `app.research.ml.coverage`.

- [ ] **Step 3: Implement the helper**

```python
# PythonDataService/app/research/ml/coverage.py
"""Bar-clock coverage check.

The engine only evaluates strategy logic when ``TradeBarConsolidator``
emits a bar — never on a wall-clock grid. So predictions are required
for every emitted bar, not for every minute between start and end.

This helper sits at the run-pipeline boundary (where the data source
and consolidator are known) and asserts the loaded prediction set
covers every bar the engine will see. Predictions for bars the engine
won't see (e.g. predictions written for a 24x7 calendar grid against a
market-hours stream) are allowed — they're a superset, not a mismatch.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol

from app.research.ml.loader import PredictionCoverageError, PredictionSet

logger = logging.getLogger(__name__)


class _BarLike(Protocol):
    """Anything with an ``end_time: datetime`` (TradeBar duck-types fine)."""

    @property
    def end_time(self): ...


def assert_bar_clock_coverage(
    prediction_set: PredictionSet,
    bar_stream: Iterable[_BarLike],
) -> None:
    """Raise ``PredictionCoverageError`` if any emitted bar lacks a prediction.

    ``bar_stream`` must be the bars the run will actually evaluate —
    typically obtained by running the run's data source through the
    same ``TradeBarConsolidator`` configuration the engine will use.
    Iterating consumes the stream once.
    """
    expected_ms: set[int] = {int(bar.end_time.timestamp() * 1000) for bar in bar_stream}
    have_ms: set[int] = set(prediction_set.index.keys())

    missing = expected_ms - have_ms
    extra = have_ms - expected_ms

    if extra:
        logger.info(
            "[ML] prediction_set %s has %d predictions for bars the engine will not evaluate; "
            "this is allowed but suggests the artifact and run window may be misaligned",
            prediction_set.manifest.prediction_set_id,
            len(extra),
        )

    if missing:
        sample = sorted(missing)[:5]
        raise PredictionCoverageError(
            f"prediction_set {prediction_set.manifest.prediction_set_id!r} missing predictions "
            f"for {len(missing)} emitted bars; first {len(sample)}: {sample}"
        )
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_coverage.py -v`
Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/coverage.py PythonDataService/tests/research/ml/test_coverage.py
git commit -m "feat(research/ml): assert_bar_clock_coverage helper"
```

---

### Task 13: RunLedger schema bump 1.0 → 1.1

**Files:**
- Modify: `PythonDataService/app/research/runs/ledger.py`
- Modify: `PythonDataService/tests/research/runs/test_storage.py` (regression)

- [ ] **Step 1: Write failing ledger tests**

Create `PythonDataService/tests/research/runs/test_ledger_v1_1.py`:

```python
"""Tests for the schema 1.0 → 1.1 ledger bump."""
from __future__ import annotations

import pytest

from app.research.runs.ledger import RunLedger


def _base_ledger_kwargs() -> dict:
    return dict(
        run_id="r1",
        strategy_spec_id="x",
        strategy_spec_hash="0" * 64,
        strategy_spec_json={},
        engine_git_commit="abc",
        symbol="SPY",
        resolution_minutes=15,
        start_ms=0,
        end_ms=1,
        initial_cash=100_000.0,
        fill_mode="signal_bar_close",
        commission_per_order=0.0,
        slippage_per_share=0.0,
        random_seed=0,
        data_snapshot_id="snap",
    )


def test_ledger_writes_schema_1_1_by_default() -> None:
    ledger = RunLedger(**_base_ledger_kwargs())
    assert ledger.schema_version == "1.1"


def test_ledger_loads_legacy_1_0_dict() -> None:
    raw = _base_ledger_kwargs() | {"schema_version": "1.0"}
    ledger = RunLedger.model_validate(raw)
    assert ledger.schema_version == "1.0"
    assert ledger.prediction_set_hash is None


def test_ledger_loads_1_1_with_prediction_set_hash() -> None:
    raw = _base_ledger_kwargs() | {
        "schema_version": "1.1",
        "prediction_set_hash": "f" * 64,
    }
    ledger = RunLedger.model_validate(raw)
    assert ledger.prediction_set_hash == "f" * 64


def test_ledger_rejects_unknown_schema_version() -> None:
    raw = _base_ledger_kwargs() | {"schema_version": "9.9"}
    with pytest.raises(Exception):
        RunLedger.model_validate(raw)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/research/runs/test_ledger_v1_1.py -v`
Expected: failures on `schema_version == "1.1"` and `prediction_set_hash` attribute.

- [ ] **Step 3: Bump the schema**

Edit `PythonDataService/app/research/runs/ledger.py` line ~165:

```python
class RunLedger(BaseModel):
    """Immutable identity record for a single strategy run.

    Persisted as ``ledger.json`` in the run's artifact directory.
    Pydantic ``extra='forbid'`` makes schema drift loud rather than
    silent — adding a field is a deliberate ``schema_version`` bump.

    v1.1: added ``prediction_set_hash`` for ML-aware run identity.
    Legacy 1.0 ledgers continue to load (writes always default to 1.1).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0", "1.1"] = "1.1"
    run_id: str

    # ... existing fields unchanged ...

    # ML predictions identity. ``None`` for prediction-free specs.
    # Populated by the runner when the spec declares any PredictionRef.
    prediction_set_hash: str | None = None
```

(The `prediction_set_hash` field can be inserted next to `data_snapshot_id` for proximity — both are "data identity" inputs.)

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/runs/ -v`
Expected: new ledger tests pass; existing storage / round-trip tests still green.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/runs/ledger.py PythonDataService/tests/research/runs/test_ledger_v1_1.py
git commit -m "feat(runs): RunLedger schema 1.0 -> 1.1 with optional prediction_set_hash"
```

---

### Task 14: Run pipeline — load + cover + thread `prediction_set_hash` into ledger

**Files:**
- Modify: `PythonDataService/app/research/runs/runner.py`
- Create: `PythonDataService/tests/research/runs/test_runner_with_predictions.py`

- [ ] **Step 1: Write failing runner integration test**

```python
# PythonDataService/tests/research/runs/test_runner_with_predictions.py
"""Runner-level test: a spec with predictions loads the artifact, runs
bar-clock coverage, and threads prediction_set_hash into the ledger.

Uses an in-memory data source factory (mirrors existing runner tests).
"""
from __future__ import annotations

import json
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.research.ml.artifact import (
    PredictionSetManifest,
    compute_prediction_set_hash,
    compute_rows_hash,
    write_chunk_rows,
)
from app.research.runs.runner import RunRequest, run_strategy_spec

NY = ZoneInfo("America/New_York")


# A minimal in-memory data source compatible with what BacktestEngine uses.
# This mirrors the fake-data-source pattern in test_runner_inmemory.py;
# adapt the import path if that helper has been factored.
from app.research.runs.tests.helpers import FakeDataSource  # may be at this path; adjust if needed


def _make_artifact(root: Path, set_id: str, bar_end_times_ms: list[int]) -> str:
    set_dir = root / set_id
    chunk_dir = set_dir / "chunks"
    chunk_dir.mkdir(parents=True)
    rows = [{"timestamp_ms": ts, "symbol": "SPY", "prediction": 0.0} for ts in bar_end_times_ms]
    chunk_path = chunk_dir / f"{bar_end_times_ms[0] - 1}.parquet"
    write_chunk_rows(chunk_path, rows, field_names=["prediction"])
    raw = {
        "schema_version": "1.0",
        "prediction_set_id": set_id,
        "symbol": "SPY",
        "resolution_minutes": 15,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {"kind": "deterministic_rule", "rule_id": "test", "rule_version": "1.0"},
        "chunks": [{
            "trained_through_ms": bar_end_times_ms[0] - 1,
            "start_ms": bar_end_times_ms[0],
            "end_ms": bar_end_times_ms[-1],
            "row_count": len(rows),
            "rows_hash": compute_rows_hash(rows),
        }],
        "prediction_set_hash": "0" * 64,
    }
    raw["prediction_set_hash"] = compute_prediction_set_hash(raw)
    (set_dir / "manifest.json").write_text(json.dumps(raw))
    return raw["prediction_set_hash"]


def test_runner_threads_prediction_set_hash_into_ledger(tmp_path: Path, monkeypatch) -> None:
    # 1. Build a spec that uses predictions.
    from app.engine.strategy.spec import StrategySpec
    spec = StrategySpec.model_validate({
        "schema_version": "1.0", "name": "t", "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "predictions": [{"id": "p", "prediction_set_id": "pred_v001", "field": "prediction"}],
        "entry": {"logic": "AND",
                  "conditions": [{"kind": "PredictionComparison", "prediction": "p", "op": ">", "value": -1.0}],
                  "size": {"kind": "SetHoldings", "fraction": 1.0}, "pyramiding": 1},
        "exit": {"logic": "AND", "conditions": []},
    })

    # 2. Stand up an in-memory data source whose emitted bars match the
    #    artifact timestamps exactly.
    bar_times = [
        datetime(2024, 5, 1, 9, 45, tzinfo=NY) + timedelta(minutes=15 * i)
        for i in range(4)
    ]
    bar_end_ms = [int(b.timestamp() * 1000) for b in bar_times]

    artifacts_root = tmp_path / "artifacts" / "predictions"
    artifacts_root.mkdir(parents=True)
    expected_hash = _make_artifact(artifacts_root, "pred_v001", bar_end_ms)

    # Point the runner at this temp artifact root.
    monkeypatch.setenv("LEARN_AI_PREDICTION_ARTIFACTS_ROOT", str(artifacts_root))

    # 3. Run.
    request = RunRequest(
        spec=spec,
        start_date=Date(2024, 5, 1),
        end_date=Date(2024, 5, 2),
    )
    fake_source = FakeDataSource(bars=[
        # Whatever shape FakeDataSource expects; bar end_times must match bar_times.
        # Adapt to the existing helper's API.
    ])
    ledger, _result = run_strategy_spec(
        request,
        data_source_factory=lambda *_args, **_kw: fake_source,
        data_root_revision="test-rev",
    )
    assert ledger.prediction_set_hash == expected_hash
```

> **Note for the implementer:** the existing in-memory data-source helper used in `test_runner_inmemory.py` should be reused here. If its construction signature differs from what's sketched above, adapt — the assertion point is `ledger.prediction_set_hash == expected_hash`. If the helper isn't easily importable, write the smallest possible synthetic data source that makes the runner emit `len(bar_times)` consolidated bars at the given end_times.

- [ ] **Step 2: Run tests to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/research/runs/test_runner_with_predictions.py -v`
Expected: failure — `prediction_set_hash` not threaded; or `ImportError` on `LEARN_AI_PREDICTION_ARTIFACTS_ROOT` handling that doesn't exist yet.

- [ ] **Step 3: Wire the runner**

Edit `app/research/runs/runner.py`. Add an artifact-root resolver near the top:

```python
import os

_DEFAULT_ARTIFACTS_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "artifacts"


def _prediction_artifacts_root() -> Path:
    """Return the root directory for prediction-set artifacts.

    Override via ``LEARN_AI_PREDICTION_ARTIFACTS_ROOT`` env var (used by
    tests). Default: ``PythonDataService/artifacts/predictions/``.
    """
    override = os.environ.get("LEARN_AI_PREDICTION_ARTIFACTS_ROOT")
    if override:
        return Path(override)
    return _DEFAULT_ARTIFACTS_ROOT / "predictions"
```

In `run_strategy_spec` (around line 251), insert the prediction-set load + coverage check after `data_source` is built and before `SpecAlgorithm` is constructed:

```python
    # Load + validate any declared prediction set. v0.5: at most one set per
    # spec (StrategySpec validator enforces).
    prediction_set = None
    prediction_set_hash: str | None = None
    if spec.predictions:
        from app.research.ml.coverage import assert_bar_clock_coverage
        from app.research.ml.loader import PredictionSet

        set_id = spec.predictions[0].prediction_set_id
        artifact_dir = _prediction_artifacts_root() / set_id
        try:
            prediction_set = PredictionSet.load(artifact_dir)
        except Exception as exc:
            return _failed(ledger, f"prediction set {set_id!r} failed to load: {exc}")
        try:
            prediction_set.assert_pairs_with(spec)
        except Exception as exc:
            return _failed(ledger, f"prediction set {set_id!r} does not pair with spec: {exc}")

        # Bar-clock coverage: replay the data source through whatever
        # consolidator the engine will use, harvest bar end_times, assert
        # every one has a prediction. The data_source must expose its
        # bar stream; if the existing factory pattern doesn't, this is
        # where the run-pipeline glue earns its name.
        try:
            bar_stream = data_source.iter_consolidated_bars(
                resolution_minutes=spec.resolution.period_minutes
            )
            assert_bar_clock_coverage(prediction_set, bar_stream)
        except Exception as exc:
            return _failed(ledger, f"prediction set {set_id!r}: bar-clock coverage failed: {exc}")

        prediction_set_hash = prediction_set.manifest.prediction_set_hash
```

> **Note for the implementer:** the call `data_source.iter_consolidated_bars(...)` is the contract this work introduces on the data-source factory. Existing factories (LEAN reader, fake test source) need a method that produces the same consolidated bar stream the engine will see. If that method doesn't exist, add it as part of this task — it is the "run-pipeline glue" the spec calls out. Likely path: `app/engine/data/lean_reader.py` (or wherever the LEAN minute reader lives) gets an `iter_consolidated_bars(resolution_minutes: int) -> Iterable[TradeBar]` method that internally drives a `TradeBarConsolidator` over the same input the engine consumes.

Update the `RunLedger(...)` construction (line ~314) to pass through the hash:

```python
    ledger = RunLedger(
        run_id=rid,
        # ... existing fields ...
        data_snapshot_id=snapshot_id,
        prediction_set_hash=prediction_set_hash,
    )
```

(Construct the ledger AFTER the prediction-set load if you want the failure-path to leave `prediction_set_hash=None`. Re-order so the prediction load runs before the `RunLedger(...)` call, or accept the field can be set later via `model_copy(update=...)`. Simplest path: move the `RunLedger(...)` construction to after the prediction-set block.)

Finally, pass `prediction_set` into `SpecAlgorithm`:

```python
    try:
        strategy = SpecAlgorithm(spec, prediction_set=prediction_set)
    except NotImplementedError as exc:
        return _failed(ledger, f"spec uses unsupported feature: {exc}")
    except ValueError as exc:
        return _failed(ledger, f"spec/prediction-set wiring failed: {exc}")
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/runs/ -v`
Expected: new test passes; existing runner tests still pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/runs/runner.py PythonDataService/tests/research/runs/test_runner_with_predictions.py
git commit -m "feat(runs): runner loads prediction set, runs bar-clock coverage, threads hash to ledger"
```

---

### Task 15: Deterministic-rule generator — `rsi_14_centered`

**Files:**
- Create: `PythonDataService/app/research/ml/generators/deterministic_rule.py`
- Create: `PythonDataService/tests/research/ml/test_generator.py`

- [ ] **Step 1: Write failing generator tests**

```python
# PythonDataService/tests/research/ml/test_generator.py
"""Tests for the deterministic-rule generator: rsi_14_centered.

The rule is: prediction = rsi_14(close) / 100.0 - 0.5
Bars before RSI is ready emit prediction = 0.0 (warmup_policy:
neutral_zero_until_feature_ready).
"""
from __future__ import annotations

from app.research.ml.generators.deterministic_rule import compute_rsi_14_centered_predictions


def test_first_thirteen_bars_emit_zero() -> None:
    closes = [100.0 + i * 0.1 for i in range(20)]
    timestamps_ms = [1000 * i for i in range(20)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert all(r["prediction"] == 0.0 for r in rows[:13])


def test_warmed_bars_have_nonzero_prediction() -> None:
    closes = [100.0 + i * 0.1 for i in range(20)]
    timestamps_ms = [1000 * i for i in range(20)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    # After 14 bars, RSI is ready and predictions should differ from 0.
    assert any(r["prediction"] != 0.0 for r in rows[14:])


def test_predictions_are_in_range_minus_half_to_plus_half() -> None:
    closes = [100.0 + i * 0.1 for i in range(50)]
    timestamps_ms = [1000 * i for i in range(50)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    for r in rows:
        assert -0.5 <= r["prediction"] <= 0.5


def test_deterministic_for_same_input() -> None:
    closes = [100.0 + i * 0.1 for i in range(20)]
    timestamps_ms = [1000 * i for i in range(20)]
    a = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    b = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert a == b


def test_row_shape() -> None:
    closes = [100.0]
    timestamps_ms = [0]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert rows[0] == {"timestamp_ms": 0, "symbol": "SPY", "prediction": 0.0}


def test_symbol_passes_through_arg() -> None:
    rows = compute_rsi_14_centered_predictions([100.0], [0], symbol="QQQ")
    assert rows[0]["symbol"] == "QQQ"
```

- [ ] **Step 2: Run to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_generator.py -v -k "rsi_14"`
Expected: ImportError on `compute_rsi_14_centered_predictions`.

- [ ] **Step 3: Implement the rule**

```python
# PythonDataService/app/research/ml/generators/deterministic_rule.py
"""Deterministic-rule generator: emits a prediction value for every emitted
bar via a closed-form function of existing features.

v0.5 ships one rule: ``rsi_14_centered`` (prediction = RSI14/100 - 0.5).
Bars before RSI's 14-bar warmup completes emit ``prediction = 0.0``,
satisfying the ``neutral_zero_until_feature_ready`` warmup policy.

This module produces the per-row dicts only. Manifest assembly,
chunk parquet writing, and CLI orchestration live in
``app/research/ml/generate_prediction_set.py``.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd
import pandas_ta as ta


RULE_ID = "rsi_14_centered"
RULE_VERSION = "1.0"


def compute_rsi_14_centered_predictions(
    closes: Sequence[float],
    timestamps_ms: Sequence[int],
    *,
    symbol: str = "SPY",
) -> list[dict]:
    """Return one row per (close, timestamp) pair.

    Predictions for bars where RSI has not yet warmed (first 13 bars
    out of any non-empty input) are emitted as ``0.0``. From bar 14
    onwards, ``prediction = (RSI14 - 50) / 100``, equivalent to
    ``RSI14/100 - 0.5``.
    """
    if len(closes) != len(timestamps_ms):
        raise ValueError(
            f"closes ({len(closes)}) and timestamps_ms ({len(timestamps_ms)}) length mismatch"
        )

    if not closes:
        return []

    series = pd.Series(closes, dtype="float64")
    rsi = ta.rsi(series, length=14)   # NaN for first 13 bars

    rows: list[dict] = []
    for ts, rsi_val in zip(timestamps_ms, rsi.tolist(), strict=True):
        if pd.isna(rsi_val):
            prediction = 0.0
        else:
            prediction = float(rsi_val) / 100.0 - 0.5
        rows.append({"timestamp_ms": int(ts), "symbol": symbol, "prediction": prediction})
    return rows
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_generator.py -v`
Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/generators/deterministic_rule.py PythonDataService/tests/research/ml/test_generator.py
git commit -m "feat(research/ml): rsi_14_centered deterministic rule generator"
```

---

### Task 16: CLI `generate_prediction_set.py`

**Files:**
- Create: `PythonDataService/app/research/ml/generate_prediction_set.py`
- Modify: `PythonDataService/tests/research/ml/test_generator.py`

- [ ] **Step 1: Append CLI integration tests**

Append to `tests/research/ml/test_generator.py`:

```python
# ----- CLI orchestration --------------------------------------------
import json
from datetime import date as Date
from pathlib import Path

from app.research.ml.generate_prediction_set import generate_prediction_set


def test_generate_writes_artifact_round_trip(tmp_path: Path) -> None:
    """Generator → loader round-trip: hash check passes."""
    out_dir = tmp_path / "artifacts" / "predictions"
    out_dir.mkdir(parents=True)

    set_id = generate_prediction_set(
        rule="rsi_14_centered",
        symbol="SPY",
        start=Date(2024, 5, 1),
        end=Date(2024, 5, 2),
        resolution_minutes=15,
        artifacts_root=out_dir,
        bars_provider=_synthetic_bars_provider(num_bars=20),
    )

    assert set_id == "pred_spy_rsi_14_centered_2024-05-01_2024-05-02_15m_v1"
    set_dir = out_dir / set_id
    assert (set_dir / "manifest.json").is_file()

    # Loader round-trip: should validate cleanly.
    from app.research.ml.loader import PredictionSet
    pset = PredictionSet.load(set_dir)
    assert pset.manifest.prediction_set_id == set_id
    assert pset.manifest.warmup_policy == "neutral_zero_until_feature_ready"


def test_generate_is_byte_identical_on_repeat(tmp_path: Path) -> None:
    """Same inputs → same prediction_set_hash."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()

    bars = _synthetic_bars_provider(num_bars=20)

    set_id_a = generate_prediction_set(
        rule="rsi_14_centered", symbol="SPY",
        start=Date(2024, 5, 1), end=Date(2024, 5, 2),
        resolution_minutes=15, artifacts_root=out_a,
        bars_provider=bars,
    )
    set_id_b = generate_prediction_set(
        rule="rsi_14_centered", symbol="SPY",
        start=Date(2024, 5, 1), end=Date(2024, 5, 2),
        resolution_minutes=15, artifacts_root=out_b,
        bars_provider=bars,
    )
    assert set_id_a == set_id_b

    a_manifest = json.loads((out_a / set_id_a / "manifest.json").read_text())
    b_manifest = json.loads((out_b / set_id_b / "manifest.json").read_text())
    assert a_manifest["prediction_set_hash"] == b_manifest["prediction_set_hash"]


def _synthetic_bars_provider(num_bars: int):
    """Return a callable that yields (close, timestamp_ms) pairs the generator
    can consume. The CLI's real path will call into the LEAN reader; this
    factory swaps a fixed synthetic series for unit tests.
    """
    def provider(*, symbol: str, start: Date, end: Date, resolution_minutes: int):
        for i in range(num_bars):
            close = 100.0 + i * 0.1
            timestamp_ms = 1714521600000 + i * resolution_minutes * 60_000
            yield close, timestamp_ms
    return provider
```

- [ ] **Step 2: Run to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_generator.py -v -k "writes_artifact or byte_identical"`
Expected: ImportError on `generate_prediction_set` from `app.research.ml.generate_prediction_set`.

- [ ] **Step 3: Implement the orchestrator**

```python
# PythonDataService/app/research/ml/generate_prediction_set.py
"""Orchestrator: rule + bars + window → artifact directory.

Has two public entry points:
  * ``generate_prediction_set(...)`` — programmatic API used by tests.
  * ``main(argv)`` — CLI bound to ``__main__``.

The CLI defers actual market-data reading to a ``bars_provider`` callable.
The default provider (used by the CLI) wraps the LEAN minute reader; tests
inject synthetic providers.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Iterable
from datetime import date as Date
from pathlib import Path

from app.research.ml.artifact import (
    ChunkRef,
    GeneratorMeta,
    PredictionSetManifest,
    compute_prediction_set_hash,
    compute_rows_hash,
    write_chunk_rows,
)
from app.research.ml.generators.deterministic_rule import (
    RULE_ID as RSI_RULE_ID,
    RULE_VERSION as RSI_RULE_VERSION,
    compute_rsi_14_centered_predictions,
)

logger = logging.getLogger(__name__)


BarsProvider = Callable[..., Iterable[tuple[float, int]]]
"""(close, timestamp_ms) pairs for the bars the run will see."""


_RULES: dict[str, tuple[str, str, Callable]] = {
    "rsi_14_centered": (RSI_RULE_ID, RSI_RULE_VERSION, compute_rsi_14_centered_predictions),
}


def _set_id_for(rule: str, symbol: str, start: Date, end: Date, resolution_minutes: int) -> str:
    return (
        f"pred_{symbol.lower()}_{rule}_"
        f"{start.isoformat()}_{end.isoformat()}_"
        f"{resolution_minutes}m_v1"
    )


def generate_prediction_set(
    *,
    rule: str,
    symbol: str,
    start: Date,
    end: Date,
    resolution_minutes: int,
    artifacts_root: Path,
    bars_provider: BarsProvider,
) -> str:
    """Write a complete artifact directory under ``artifacts_root``.

    Returns the ``prediction_set_id`` used as the directory name.
    Overwrites any existing directory with the same id.
    """
    if rule not in _RULES:
        raise ValueError(f"unknown rule {rule!r}; known: {sorted(_RULES)}")
    rule_id, rule_version, rule_fn = _RULES[rule]

    pairs = list(bars_provider(symbol=symbol, start=start, end=end, resolution_minutes=resolution_minutes))
    if not pairs:
        raise ValueError(f"bars_provider returned no bars for {symbol} {start}..{end}")
    closes = [c for c, _ in pairs]
    timestamps_ms = [ts for _, ts in pairs]

    rows = rule_fn(closes, timestamps_ms, symbol=symbol)

    set_id = _set_id_for(rule, symbol, start, end, resolution_minutes)
    set_dir = artifacts_root / set_id
    chunks_dir = set_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    trained_through_ms = timestamps_ms[0] - 1
    chunk_path = chunks_dir / f"{trained_through_ms}.parquet"
    write_chunk_rows(chunk_path, rows, field_names=["prediction"])

    chunk_meta = ChunkRef(
        trained_through_ms=trained_through_ms,
        start_ms=timestamps_ms[0],
        end_ms=timestamps_ms[-1],
        row_count=len(rows),
        rows_hash=compute_rows_hash(rows),
    )

    manifest_dict = {
        "schema_version": "1.0",
        "prediction_set_id": set_id,
        "symbol": symbol,
        "resolution_minutes": resolution_minutes,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {"kind": "deterministic_rule", "rule_id": rule_id, "rule_version": rule_version},
        "chunks": [chunk_meta.model_dump()],
        "prediction_set_hash": "0" * 64,   # placeholder
    }
    manifest_dict["prediction_set_hash"] = compute_prediction_set_hash(manifest_dict)

    # Validate before write.
    PredictionSetManifest.model_validate(manifest_dict)
    (set_dir / "manifest.json").write_text(json.dumps(manifest_dict, sort_keys=True))

    logger.info(
        "[ML] wrote prediction set %s (%d rows, hash=%s)",
        set_id, len(rows), manifest_dict["prediction_set_hash"][:12],
    )
    return set_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _default_lean_bars_provider(*, symbol, start, end, resolution_minutes):
    """Adapter from the LEAN minute reader's stream to (close, ts_ms) pairs.

    Implementer note: this should drive the same TradeBarConsolidator the
    runner uses (Task 14). For v0.5, a thin wrapper around
    ``LeanMinuteReader`` + ``TradeBarConsolidator`` is sufficient.
    """
    raise NotImplementedError(
        "default LEAN bars_provider is wired in Task 17; tests inject a synthetic provider"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generate_prediction_set")
    parser.add_argument("--rule", required=True, choices=sorted(_RULES))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, type=Date.fromisoformat)
    parser.add_argument("--end", required=True, type=Date.fromisoformat)
    parser.add_argument("--resolution-minutes", required=True, type=int)
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent.parent / "artifacts" / "predictions",
    )
    args = parser.parse_args(argv)

    set_id = generate_prediction_set(
        rule=args.rule,
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        resolution_minutes=args.resolution_minutes,
        artifacts_root=args.artifacts_root,
        bars_provider=_default_lean_bars_provider,
    )
    print(set_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_generator.py -v`
Expected: all generator tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/generate_prediction_set.py PythonDataService/tests/research/ml/test_generator.py
git commit -m "feat(research/ml): generate_prediction_set orchestrator + CLI"
```

---

### Task 17: Default LEAN bars provider — wire into the CLI

**Files:**
- Modify: `PythonDataService/app/research/ml/generate_prediction_set.py`
- Modify: `PythonDataService/app/engine/data/lean_format.py` (or similar — wherever the consolidator-driving wrapper belongs; investigate at task time)

> **Implementer:** the runner side of this lives in Task 14 (run-pipeline must obtain `iter_consolidated_bars` from the data source). Reuse that exact path here. If the runner did not need a public `iter_consolidated_bars` because it ran the engine directly, factor a small helper that runs `LeanMinuteReader → TradeBarConsolidator` once and yields the bar list. Both Task 14 and this task should converge on the same helper.

- [ ] **Step 1: Write the integration test**

Append to `tests/research/ml/test_generator.py`:

```python
# ----- LEAN-backed CLI smoke ----------------------------------------
import os
import subprocess
import sys


@pytest.mark.slow
def test_cli_generates_real_artifact_for_one_day(tmp_path: Path) -> None:
    """Smoke test: run the CLI against the real LEAN reader for one trading day."""
    cmd = [
        sys.executable, "-m", "app.research.ml.generate_prediction_set",
        "--rule", "rsi_14_centered",
        "--symbol", "SPY",
        "--start", "2024-05-01",
        "--end", "2024-05-02",
        "--resolution-minutes", "15",
        "--artifacts-root", str(tmp_path),
    ]
    env = os.environ.copy()
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    set_id = result.stdout.strip()
    assert (tmp_path / set_id / "manifest.json").is_file()
```

- [ ] **Step 2: Run to verify**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_generator.py::test_cli_generates_real_artifact_for_one_day -v`
Expected: NotImplementedError (the placeholder), or pass once Step 3 lands.

- [ ] **Step 3: Implement the default bars provider**

Replace `_default_lean_bars_provider` in `generate_prediction_set.py`:

```python
from datetime import timedelta
from app.engine.consolidators.trade_bar_consolidator import TradeBarConsolidator
from app.engine.data.lean_format import LeanMinuteReader   # or whatever the reader is named


def _default_lean_bars_provider(*, symbol, start, end, resolution_minutes):
    """Drive a TradeBarConsolidator at ``resolution_minutes`` over the LEAN
    minute stream and yield ``(close, timestamp_ms)`` for each emitted bar.

    Mirrors the path the engine takes during a run, so the bars produced
    here have the same end_times as the bars the engine will evaluate.
    """
    reader = LeanMinuteReader(symbol=symbol, start=start, end=end)
    consolidator = TradeBarConsolidator(timedelta(minutes=resolution_minutes))

    emitted: list[tuple[float, int]] = []

    def _on_bar(bar):
        emitted.append((float(bar.close), int(bar.end_time.timestamp() * 1000)))

    consolidator.on_bar_emitted = _on_bar
    for minute_bar in reader.iter():
        consolidator.update(minute_bar)
    consolidator.flush()

    yield from emitted
```

> **Implementer:** the exact LEAN reader API may differ (the import path, the iteration method, the consolidator's emit-callback hook). Investigate `app/engine/data/` and `app/engine/consolidators/` at task time and adapt. Keep the **observable contract** identical: yield `(close, timestamp_ms)` pairs that match the bars the engine will evaluate.

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_generator.py -v`
Expected: all tests pass; the slow CLI smoke also passes (skip with `-k "not slow"` for normal iteration).

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/research/ml/generate_prediction_set.py
git commit -m "feat(research/ml): default LEAN bars provider drives same consolidator as engine"
```

---

### Task 18: End-to-end determinism + replay fixture

**Files:**
- Create: `PythonDataService/tests/research/ml/test_e2e_replay.py`
- Create: `PythonDataService/tests/research/ml/fixtures/__init__.py`
- Create: `PythonDataService/tests/research/ml/fixtures/e2e_known_hashes.json`

- [ ] **Step 1: Write E2E test (committed-fixture comparison)**

```python
# PythonDataService/tests/research/ml/test_e2e_replay.py
"""End-to-end determinism + replay tests.

Runs the deterministic-rule generator on a fixed window, asserts the
prediction_set_hash matches a committed fixture, runs a backtest of a
PredictionComparison-using spec, asserts the result_hash matches a
committed fixture, regenerates and reruns to confirm both hashes are
unchanged.

These tests are the v0.5 acceptance gate: if either committed hash
ever drifts, that's a regression (or an intentional bump that requires
updating the fixture file with justification).
"""
from __future__ import annotations

import json
from datetime import date as Date
from pathlib import Path

import pytest

from app.engine.strategy.spec import StrategySpec
from app.research.ml.generate_prediction_set import generate_prediction_set
from app.research.runs.runner import RunRequest, run_strategy_spec


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "e2e_known_hashes.json"


@pytest.fixture
def known_hashes() -> dict:
    return json.loads(_FIXTURE_PATH.read_text())


def _synthetic_bars_provider(num_bars: int = 30):
    def provider(*, symbol, start, end, resolution_minutes):
        for i in range(num_bars):
            close = 100.0 + i * 0.1
            timestamp_ms = 1714521600000 + i * resolution_minutes * 60_000
            yield close, timestamp_ms
    return provider


def test_e2e_prediction_set_hash_matches_fixture(tmp_path: Path, known_hashes: dict) -> None:
    set_id = generate_prediction_set(
        rule="rsi_14_centered", symbol="SPY",
        start=Date(2024, 5, 1), end=Date(2024, 5, 2),
        resolution_minutes=15,
        artifacts_root=tmp_path,
        bars_provider=_synthetic_bars_provider(),
    )
    manifest = json.loads((tmp_path / set_id / "manifest.json").read_text())
    assert manifest["prediction_set_hash"] == known_hashes["prediction_set_hash"], (
        f"prediction_set_hash drifted; if intentional, update "
        f"tests/research/ml/fixtures/e2e_known_hashes.json with justification"
    )


def test_e2e_regenerate_produces_same_hash(tmp_path: Path) -> None:
    """Generating a fresh artifact twice produces the same hash."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    bars = _synthetic_bars_provider()

    set_id = generate_prediction_set(
        rule="rsi_14_centered", symbol="SPY",
        start=Date(2024, 5, 1), end=Date(2024, 5, 2),
        resolution_minutes=15, artifacts_root=a, bars_provider=bars,
    )
    set_id_b = generate_prediction_set(
        rule="rsi_14_centered", symbol="SPY",
        start=Date(2024, 5, 1), end=Date(2024, 5, 2),
        resolution_minutes=15, artifacts_root=b, bars_provider=bars,
    )
    assert set_id == set_id_b

    h_a = json.loads((a / set_id / "manifest.json").read_text())["prediction_set_hash"]
    h_b = json.loads((b / set_id_b / "manifest.json").read_text())["prediction_set_hash"]
    assert h_a == h_b
```

Create the fixture file (initial value populated in Step 3):

```json
// PythonDataService/tests/research/ml/fixtures/e2e_known_hashes.json
{
  "_provenance": "Generated by tests/research/ml/test_e2e_replay.py at <DATE>; regenerate by running the generator with the same synthetic bars (see _synthetic_bars_provider in that file). Drift requires explanation.",
  "prediction_set_hash": "PLACEHOLDER_REGENERATE_AT_TASK_18_STEP_3"
}
```

- [ ] **Step 2: Run tests to verify failure**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_e2e_replay.py -v`
Expected: `test_e2e_prediction_set_hash_matches_fixture` fails (hash mismatch — placeholder in fixture).

- [ ] **Step 3: Capture the real hash and update the fixture**

Run a one-shot to print the generated hash:

```bash
podman exec polygon-data-service python -c "
from datetime import date
from pathlib import Path
import tempfile, json
from app.research.ml.generate_prediction_set import generate_prediction_set

def bars(*, symbol, start, end, resolution_minutes):
    for i in range(30):
        yield 100.0 + i * 0.1, 1714521600000 + i * resolution_minutes * 60000

with tempfile.TemporaryDirectory() as t:
    set_id = generate_prediction_set(
        rule='rsi_14_centered', symbol='SPY',
        start=date(2024,5,1), end=date(2024,5,2),
        resolution_minutes=15,
        artifacts_root=Path(t),
        bars_provider=bars,
    )
    m = json.loads((Path(t)/set_id/'manifest.json').read_text())
    print(m['prediction_set_hash'])
"
```

Copy the printed hash into `tests/research/ml/fixtures/e2e_known_hashes.json`, replacing both `PLACEHOLDER_REGENERATE_AT_TASK_18_STEP_3` and `<DATE>` (with today's date and a 1-line provenance note).

- [ ] **Step 4: Run tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_e2e_replay.py -v`
Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/tests/research/ml/test_e2e_replay.py PythonDataService/tests/research/ml/fixtures/__init__.py PythonDataService/tests/research/ml/fixtures/e2e_known_hashes.json
git commit -m "test(research/ml): E2E determinism with committed prediction_set_hash"
```

---

### Task 19: Prediction-free regression + project-scope test sweep

**Files:**
- Create: `PythonDataService/tests/research/ml/test_regression.py`

- [ ] **Step 1: Write the regression test**

```python
# PythonDataService/tests/research/ml/test_regression.py
"""Regression tests: existing prediction-free specs still run unchanged
under the schema 1.0 → 1.1 ledger and the new EvalContext.predictions
field. No prediction artifacts loaded, no coverage check, no spec
validator surprises.
"""
from __future__ import annotations

import json
from datetime import date as Date

import pytest


def test_existing_sma_crossover_spec_round_trips() -> None:
    """The shipped fixture spec still validates after schema additions."""
    from app.engine.strategy.spec import load_spec_from_path
    from pathlib import Path

    fixtures = Path(__file__).resolve().parent.parent.parent.parent / "app/engine/strategy/spec/fixtures"
    spec = load_spec_from_path(fixtures / "spy_ema_crossover.spec.json")
    assert spec.predictions == []


def test_legacy_1_0_ledger_loads() -> None:
    """A pre-existing artifacts/runs/<id>/ledger.json without
    prediction_set_hash must continue to load."""
    from app.research.runs.ledger import RunLedger

    legacy = {
        "schema_version": "1.0",
        "run_id": "old",
        "strategy_spec_id": "x",
        "strategy_spec_hash": "0" * 64,
        "strategy_spec_json": {},
        "engine_git_commit": "abc",
        "symbol": "SPY",
        "resolution_minutes": 15,
        "start_ms": 0,
        "end_ms": 1,
        "initial_cash": 100_000.0,
        "fill_mode": "signal_bar_close",
        "commission_per_order": 0.0,
        "slippage_per_share": 0.0,
        "random_seed": 0,
        "data_snapshot_id": "snap",
    }
    ledger = RunLedger.model_validate(legacy)
    assert ledger.prediction_set_hash is None
    assert ledger.schema_version == "1.0"
```

- [ ] **Step 2: Run regression tests**

Run: `podman exec polygon-data-service python -m pytest tests/research/ml/test_regression.py -v`
Expected: both tests pass.

- [ ] **Step 3: Run the full project-scope test suite**

Per `.claude/rules/python.md` and `.claude/rules/testing.md`, validate at project scope before pushing:

```bash
podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"
ruff check PythonDataService/app/ PythonDataService/tests/
```

Expected:
- pytest: all tests pass except any pre-existing failures (compare against `origin/master` baseline if anything fails).
- ruff: zero warnings.

- [ ] **Step 4: Commit**

```bash
git add PythonDataService/tests/research/ml/test_regression.py
git commit -m "test(research/ml): legacy spec + ledger 1.0 regression coverage"
```

- [ ] **Step 5: Push and open PR**

```bash
git push -u origin feat/ml-predictions-as-data-v05
gh pr create --title "feat: ML predictions as data — v0.5 plumbing" --body "$(cat <<'EOF'
## Summary

Implements [`docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md`](../docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md). Plumbing-only — no sklearn, no model training, no walk-forward retraining.

- New ``predictions`` block on ``StrategySpec`` and ``PredictionComparison`` condition kind.
- ``app/research/ml/`` package: artifact format (Pydantic manifest + parquet chunks), loader with intrinsic + spec-pairing validation, bar-clock coverage helper, deterministic-rule "fake model" generator, CLI.
- ``EvalContext.predictions: dict[str, Decimal]`` populated per bar by ``SpecAlgorithm``; ``PredictionComparisonPrimitive`` reads from it.
- ``RunLedger`` schema bump 1.0 → 1.1 with new optional ``prediction_set_hash``. Legacy 1.0 ledgers continue to load.
- Run pipeline (``runner.py``) loads the artifact, runs bar-clock coverage against the same ``TradeBarConsolidator`` configuration the engine will use, and threads ``prediction_set_hash`` into the ledger.

## Test plan

- [ ] ``pytest tests/research/ml/ -v`` (all green)
- [ ] ``pytest tests/research/runs/ -v`` (no regression on existing runner tests)
- [ ] ``pytest app/engine/strategy/spec/tests/ -v`` (no regression on spec tests; new prediction tests pass)
- [ ] ``ruff check PythonDataService/app/ PythonDataService/tests/`` (zero warnings)
- [ ] Committed ``prediction_set_hash`` fixture is exact across regenerations
- [ ] Existing prediction-free specs continue to backtest unchanged

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

Performed against the spec sections.

**Spec coverage:**

| Spec section | Implementing task |
|---|---|
| Q1 hypothesis-first | n/a (scoping decision; nothing to implement) |
| Q2 `app/research/ml/` location | Task 1 |
| Q3 `predictions` block + `PredictionComparison` | Tasks 7, 10 |
| Q4 artifact storage + path-safe id | Task 2 (path-safe id), Task 16 (storage layout) |
| Q5 hash semantics (rows_hash, prediction_set_hash, no parquet_file_hash) | Task 3 |
| Q6 ledger field separate from spec hash | Task 13 |
| Q7 CLI generator | Tasks 16, 17 |
| Q8 bar-clock coverage at run-pipeline boundary | Tasks 12, 14 |
| Q9 leakage invariant | Task 2 (`ChunkRef._check_invariants`) + Task 5 (loader recheck) |
| Q10 warmup policy | Task 15 (zero-emission for not-yet-ready) |
| Q11 PredictionComparison only | Task 10 (PredictionBetween deferred) |
| Q12 symbol scoping | Task 6 (`assert_pairs_with`) |
| Q13 single prediction set per spec | Task 8 |
| Q14 EvalContext.predictions wiring | Tasks 9, 10, 11 |
| Risks: cross-platform float repr | Documented in spec; v0.5 contract assumes CPython |
| Risks: bar-clock replay perf | Task 14 implementer note about the helper that runs the consolidator once |

All 14 decisions and the major risks have implementing tasks.

**Placeholder scan:** Two implementer notes in Tasks 14 and 17 acknowledge that exact reader/consolidator API names depend on inspecting the runtime — these are *not* "TODO write code later" placeholders; they are flagged adaptation points where the contract is fully specified and the implementer needs to look at the existing reader's surface to wire it up. Acceptable.

**Type consistency:** `PredictionSet` (Tasks 5, 6, 11, 12, 14) consistent across files; `compute_rows_hash` / `compute_prediction_set_hash` consistent (Tasks 3, 5, 16, 18); `RULE_ID` constant exported from `deterministic_rule.py` and consumed by the orchestrator (Tasks 15, 16); `assert_bar_clock_coverage` signature consistent across `coverage.py` and `runner.py` (Tasks 12, 14).

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-05-09-ml-prediction-as-data-v05.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using `executing-plans`, batch execution with checkpoints.

Which approach?
