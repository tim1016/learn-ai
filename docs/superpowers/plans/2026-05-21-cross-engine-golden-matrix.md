# Cross-Engine Golden-Fixture Matrix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand the LEAN-Sidecar-vs-Engine-Lab parity check from one cell to a 4-ticker × 3-window = 12-cell matrix; each cell pins LEAN orders.json + state.csv + observations.csv as a golden fixture; Engine Lab runs live at test time and must pass three gates (observations, per-bar state, trade-level reconciler).

**Architecture:** New package `app/lean_sidecar/parity_matrix/` holds the matrix definition, manifest schema, observations comparator, state comparator, and per-cell orchestration. A new CLI script `scripts/regenerate_cross_engine_study.py` invokes per-cell regeneration. A new parameterized pytest file runs Engine Lab live against pinned LEAN outputs for all 12 cells, with `cross_engine_smoke` marker on the four W6mo cells (normal CI) and `slow` marker on the eight W12mo+W24mo cells (pre-push / on-demand).

**Tech Stack:** Python 3.11+, Pydantic v2, pytest, `Decimal` arithmetic for full precision, existing `app/lean_sidecar/cross_reconciler.py` + `cross_runner.py` integration surface, existing `app/engine/data/lean_format.py` for LEAN data reader.

**Authoritative spec:** `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md`

---

## File structure (locked before tasks)

```
PythonDataService/
├── app/lean_sidecar/parity_matrix/         (NEW package)
│   ├── __init__.py
│   ├── matrix.py                            # Cell IDs + ticker/window matrix data
│   ├── manifest.py                          # Pydantic v2 manifest + sha256 helpers
│   ├── observations_parity.py               # Gate 1 comparator (pure function)
│   ├── state_parity.py                      # Gate 2 comparator (pure function)
│   └── cell_runner.py                       # Orchestrates LEAN run + Engine run + 3 gates
│
├── app/engine/strategy/algorithms/spy_ema_crossover.py   (MODIFY if needed)
│   # Must emit observations.csv + state.csv in same schema as LEAN trusted sample.
│
├── scripts/
│   └── regenerate_cross_engine_study.py     (NEW) CLI: --cell / --ticker / --all
│
├── tests/lean_sidecar/parity_matrix/         (NEW)
│   ├── __init__.py
│   ├── test_matrix.py                       # Matrix data invariants
│   ├── test_manifest.py                     # Schema round-trip + sha256 stability
│   ├── test_observations_parity.py          # Gate 1 unit tests
│   ├── test_state_parity.py                 # Gate 2 unit tests
│   └── test_cell_runner.py                  # Mocked end-to-end orchestration
│
├── tests/research/parity/
│   └── test_cross_engine_study.py           (NEW) Live parameterized test, 12 cells
│
└── tests/fixtures/golden/cross-engine-studies/   (NEW, populated by regen script)
    ├── README.md
    ├── _lean_data_capture/{SPY,QQQ,AAPL,TSLA}/
    └── cells/{TICKER}_{WINDOW}_{START}_to_{END}/
        ├── attribution.md
        ├── manifest.json
        ├── lean/{orders.json, state.csv, observations.csv}
        └── reconciliation_pinned.json
```

---

## Task 1: Bootstrap `parity_matrix` package + matrix definition

**Files:**
- Create: `PythonDataService/app/lean_sidecar/parity_matrix/__init__.py`
- Create: `PythonDataService/app/lean_sidecar/parity_matrix/matrix.py`
- Create: `PythonDataService/tests/lean_sidecar/parity_matrix/__init__.py`
- Create: `PythonDataService/tests/lean_sidecar/parity_matrix/test_matrix.py`

- [ ] **Step 1: Write the failing tests** at `tests/lean_sidecar/parity_matrix/test_matrix.py`

```python
"""Matrix invariants — 12 cells, 4 tickers × 3 nested windows."""
from __future__ import annotations

from datetime import date

from app.lean_sidecar.parity_matrix.matrix import (
    CELLS,
    Cell,
    WindowLabel,
    cell_by_id,
)


def test_cells_total_count() -> None:
    assert len(CELLS) == 12


def test_cells_have_four_distinct_tickers() -> None:
    assert {c.ticker for c in CELLS} == {"SPY", "QQQ", "AAPL", "TSLA"}


def test_each_ticker_has_three_windows() -> None:
    for ticker in ("SPY", "QQQ", "AAPL", "TSLA"):
        labels = {c.window_label for c in CELLS if c.ticker == ticker}
        assert labels == {WindowLabel.W6MO, WindowLabel.W12MO, WindowLabel.W24MO}


def test_all_cells_share_end_date() -> None:
    assert {c.end_date for c in CELLS} == {date(2026, 4, 30)}


def test_window_start_dates_match_spec() -> None:
    starts = {(c.window_label, c.start_date) for c in CELLS}
    assert (WindowLabel.W6MO, date(2025, 11, 3)) in starts
    assert (WindowLabel.W12MO, date(2025, 5, 1)) in starts
    assert (WindowLabel.W24MO, date(2024, 6, 3)) in starts


def test_cell_id_format() -> None:
    spy_w24 = cell_by_id("SPY_W24mo_2024-06-03_to_2026-04-30")
    assert spy_w24.ticker == "SPY"
    assert spy_w24.window_label == WindowLabel.W24MO
    assert spy_w24.start_date == date(2024, 6, 3)
    assert spy_w24.end_date == date(2026, 4, 30)


def test_cell_by_id_unknown_raises() -> None:
    import pytest
    with pytest.raises(KeyError):
        cell_by_id("UNKNOWN_W6mo_2025-11-03_to_2026-04-30")
```

- [ ] **Step 2: Run tests to verify failure**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_matrix.py -v
```
Expected: ImportError or ModuleNotFoundError.

- [ ] **Step 3: Create the package `__init__.py` files**

`PythonDataService/app/lean_sidecar/parity_matrix/__init__.py` and `PythonDataService/tests/lean_sidecar/parity_matrix/__init__.py` — both empty files.

- [ ] **Step 4: Write `matrix.py`**

```python
"""Cross-engine parity matrix — 4 tickers × 3 nested windows = 12 cells.

Reference: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

TICKERS: Final[tuple[str, ...]] = ("SPY", "QQQ", "AAPL", "TSLA")
END_DATE: Final[date] = date(2026, 4, 30)


class WindowLabel(StrEnum):
    W6MO = "W6mo"
    W12MO = "W12mo"
    W24MO = "W24mo"


_WINDOW_STARTS: Final[dict[WindowLabel, date]] = {
    WindowLabel.W6MO: date(2025, 11, 3),
    WindowLabel.W12MO: date(2025, 5, 1),
    WindowLabel.W24MO: date(2024, 6, 3),
}


@dataclass(frozen=True)
class Cell:
    ticker: str
    window_label: WindowLabel
    start_date: date
    end_date: date

    @property
    def cell_id(self) -> str:
        return (
            f"{self.ticker}_{self.window_label.value}_"
            f"{self.start_date.isoformat()}_to_{self.end_date.isoformat()}"
        )


def _build_cells() -> tuple[Cell, ...]:
    out: list[Cell] = []
    for ticker in TICKERS:
        for label, start in _WINDOW_STARTS.items():
            out.append(Cell(ticker=ticker, window_label=label,
                            start_date=start, end_date=END_DATE))
    return tuple(out)


CELLS: Final[tuple[Cell, ...]] = _build_cells()
_CELL_INDEX: Final[dict[str, Cell]] = {c.cell_id: c for c in CELLS}


def cell_by_id(cell_id: str) -> Cell:
    """Return cell by canonical id; raise KeyError if unknown."""
    return _CELL_INDEX[cell_id]
```

- [ ] **Step 5: Run tests to verify pass**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_matrix.py -v
```
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/lean_sidecar/parity_matrix/__init__.py \
        PythonDataService/app/lean_sidecar/parity_matrix/matrix.py \
        PythonDataService/tests/lean_sidecar/parity_matrix/__init__.py \
        PythonDataService/tests/lean_sidecar/parity_matrix/test_matrix.py
git commit -m "feat(parity-matrix): cell IDs + 4×3 nested window definitions"
```

---

## Task 2: Manifest Pydantic schema + sha256 helpers

**Files:**
- Create: `PythonDataService/app/lean_sidecar/parity_matrix/manifest.py`
- Create: `PythonDataService/tests/lean_sidecar/parity_matrix/test_manifest.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Manifest schema round-trip + sha256 helpers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.lean_sidecar.parity_matrix.manifest import (
    BrokerSpec,
    CellManifest,
    DataSpec,
    LeanRuntimeSpec,
    PinnedArtifactHashes,
    StateCsvSchema,
    StrategySpec,
    WindowSpec,
    sha256_of_file,
    sha256_of_text,
)


def test_sha256_of_text_stable() -> None:
    h = sha256_of_text("hello")
    assert h == hashlib.sha256(b"hello").hexdigest()
    assert len(h) == 64


def test_sha256_of_file(tmp_path: Path) -> None:
    p = tmp_path / "x.txt"
    p.write_text("hello", encoding="utf-8")
    assert sha256_of_file(p) == hashlib.sha256(b"hello").hexdigest()


def test_manifest_round_trip() -> None:
    m = CellManifest(
        schema_version=1,
        cell_id="SPY_W6mo_2025-11-03_to_2026-04-30",
        ticker="SPY",
        window=WindowSpec(
            label="W6mo",
            start_date="2025-11-03",
            end_date="2026-04-30",
            session="regular",
            trading_days_expected=125,
        ),
        strategy=StrategySpec(
            trusted_sample="ema_crossover",
            trusted_sample_source_sha256="a" * 64,
            parameters_constants={
                "FAST_PERIOD": 5, "SLOW_PERIOD": 10, "RSI_PERIOD": 14,
                "EXIT_BARS": 5, "GAP_MIN": 0.20, "RSI_LO": 50, "RSI_HI": 70,
            },
            runtime_parameters={
                "bar_minutes": 15, "adjustment": "raw", "starting_cash": 100000,
            },
        ),
        data=DataSpec(
            lean_data_capture_ref="_lean_data_capture/SPY",
            data_contract_hash="b" * 64,
        ),
        broker=BrokerSpec(
            brokerage_model="InteractiveBrokersBrokerage",
            account_type="Margin",
            fill_model="ImmediateFillModel",
            fee_model="IbkrEquityCommissionModel",
        ),
        lean_runtime=LeanRuntimeSpec(
            container_image_digest="docker.io/quantconnect/lean@sha256:" + "c" * 64,
        ),
        artifacts=PinnedArtifactHashes(
            orders_sha256="d" * 64, state_sha256="e" * 64,
            observations_sha256="f" * 64, reconciliation_sha256="0" * 64,
        ),
        state_csv_schema=StateCsvSchema(
            columns=["ts_ms_utc", "close", "ema_fast", "ema_slow",
                     "rsi", "cross_state", "signal"],
            column_types={
                "ts_ms_utc": "int64",
                "close": "decimal_string",
                "ema_fast": "decimal_string",
                "ema_slow": "decimal_string",
                "rsi": "decimal_string",
                "cross_state": "string_enum:above|below|equal",
                "signal": "string_enum:HOLD|ENTER|EXIT",
            },
        ),
        timezone="America/New_York",
        timestamp_convention="int64_ms_utc",
        fixture_git_commit="1" * 40,
        python_data_service_commit="1" * 40,
        generator_script_sha256="2" * 64,
        captured_by="Tester",
        captured_at_ms_utc=1779849600000,
    )
    serialized = m.model_dump_json()
    reloaded = CellManifest.model_validate_json(serialized)
    assert reloaded == m


def test_manifest_rejects_unknown_session() -> None:
    with pytest.raises(ValidationError):
        WindowSpec(
            label="W6mo", start_date="2025-11-03", end_date="2026-04-30",
            session="weird", trading_days_expected=125,
        )


def test_state_csv_schema_columns_match_keys() -> None:
    s = StateCsvSchema(
        columns=["a", "b"],
        column_types={"a": "int64", "b": "int64"},
    )
    assert set(s.columns) == set(s.column_types.keys())


def test_state_csv_schema_rejects_mismatched_keys() -> None:
    with pytest.raises(ValidationError):
        StateCsvSchema(
            columns=["a", "b"],
            column_types={"a": "int64"},  # missing "b"
        )
```

- [ ] **Step 2: Run tests to verify failure**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_manifest.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write `manifest.py`**

```python
"""Cell manifest schema + sha256 helpers.

Schema is `schema_version=1`. Any non-additive change MUST bump
schema_version and document the migration in the cell's attribution.md.

Reference: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
            § "Cell manifest.json schema (v1)"
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
_CHUNK = 1 << 16


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class WindowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["W6mo", "W12mo", "W24mo"]
    start_date: str
    end_date: str
    session: Literal["regular", "extended"]
    trading_days_expected: int = Field(..., gt=0)


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trusted_sample: str
    trusted_sample_source_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    parameters_constants: dict[str, float]
    runtime_parameters: dict[str, str | int | float]


class DataSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lean_data_capture_ref: str
    data_contract_hash: str = Field(..., pattern=r"^[a-f0-9]{64}$")


class BrokerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brokerage_model: str
    account_type: str
    fill_model: str
    fee_model: str


class LeanRuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container_image_digest: str = Field(
        ..., pattern=r"^docker\.io/quantconnect/lean@sha256:[a-f0-9]{64}$"
    )


class PinnedArtifactHashes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orders_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    state_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    observations_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    reconciliation_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")


class StateCsvSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: list[str]
    column_types: dict[str, str]

    @model_validator(mode="after")
    def columns_match_column_types(self) -> StateCsvSchema:
        if set(self.columns) != set(self.column_types.keys()):
            raise ValueError(
                "state_csv_schema.columns must match column_types keys exactly"
            )
        return self


class CellManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    cell_id: str
    ticker: str
    window: WindowSpec
    strategy: StrategySpec
    data: DataSpec
    broker: BrokerSpec
    lean_runtime: LeanRuntimeSpec
    artifacts: PinnedArtifactHashes
    state_csv_schema: StateCsvSchema
    timezone: Literal["America/New_York"]
    timestamp_convention: Literal["int64_ms_utc"]
    fixture_git_commit: str = Field(..., pattern=r"^[a-f0-9]{7,40}$")
    python_data_service_commit: str = Field(..., pattern=r"^[a-f0-9]{7,40}$")
    generator_script_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    captured_by: str
    captured_at_ms_utc: int = Field(..., gt=0)

    @field_validator("cell_id")
    @classmethod
    def cell_id_format(cls, v: str) -> str:
        # Format: <TICKER>_<W6mo|W12mo|W24mo>_<YYYY-MM-DD>_to_<YYYY-MM-DD>
        import re
        if not re.fullmatch(
            r"[A-Z]+_W(6|12|24)mo_\d{4}-\d{2}-\d{2}_to_\d{4}-\d{2}-\d{2}", v
        ):
            raise ValueError(f"cell_id has wrong shape: {v!r}")
        return v
```

- [ ] **Step 4: Run tests to verify pass**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_manifest.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/lean_sidecar/parity_matrix/manifest.py \
        PythonDataService/tests/lean_sidecar/parity_matrix/test_manifest.py
git commit -m "feat(parity-matrix): pydantic manifest schema + sha256 helpers"
```

---

## Task 3: Observations parity comparator (Gate 1)

Pure-function comparator for `observations.csv` (per-minute bar consumption proof). Compares as `Decimal` parsed from string for OHLCV, exact int for timestamp, exact row count and order.

**Files:**
- Create: `PythonDataService/app/lean_sidecar/parity_matrix/observations_parity.py`
- Create: `PythonDataService/tests/lean_sidecar/parity_matrix/test_observations_parity.py`

- [ ] **Step 1: Write failing tests**

```python
"""Gate 1 — observations.csv exact-equality comparator."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.lean_sidecar.parity_matrix.observations_parity import (
    ObservationsParityResult,
    compare_observations,
)


def _write(path: Path, rows: list[str]) -> None:
    path.write_text("ms_utc,open,high,low,close,volume\n" + "\n".join(rows) + "\n",
                    encoding="utf-8")


def test_identical_passes(tmp_path: Path) -> None:
    rows = [
        "1700000000000,100.50,101.00,100.25,100.75,1000",
        "1700000060000,100.75,101.50,100.50,101.25,1500",
    ]
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write(a, rows); _write(b, rows)
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is True
    assert r.row_count == 2
    assert r.failures == []


def test_row_count_mismatch_fails(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write(a, ["1700000000000,100.50,101.00,100.25,100.75,1000"])
    _write(b, [])
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is False
    assert any("row_count" in f.reason for f in r.failures)


def test_timestamp_mismatch_localized(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write(a, ["1700000000000,100.50,101.00,100.25,100.75,1000"])
    _write(b, ["1700000060000,100.50,101.00,100.25,100.75,1000"])
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is False
    assert r.failures[0].row_index == 0
    assert r.failures[0].field == "ms_utc"


def test_close_decimal_drift_fails(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write(a, ["1700000000000,100.50,101.00,100.25,100.75,1000"])
    # Trailing zero differs in source but Decimal-equal: 100.750 == 100.75.
    _write(b, ["1700000000000,100.50,101.00,100.25,100.750,1000"])
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is True  # Decimal equality, not string equality

    _write(b, ["1700000000000,100.50,101.00,100.25,100.7500001,1000"])
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is False
    assert r.failures[0].field == "close"


def test_schema_header_drift_fails(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("ms_utc,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8")
    b.write_text("ms_utc,o,h,l,c,v\n1,1,1,1,1,1\n", encoding="utf-8")
    r = compare_observations(reference=a, candidate=b)
    assert r.passed is False
    assert any("schema" in f.reason for f in r.failures)
```

- [ ] **Step 2: Run tests, verify failure**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_observations_parity.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write `observations_parity.py`**

```python
"""Gate 1 — observations.csv exact-equality comparator.

Compares per-minute bar consumption between LEAN (pinned) and Engine
Lab (live). Exact equality:
  * ms_utc as int
  * OHLCV as Decimal parsed from string
  * row count and order
  * header exactly ["ms_utc","open","high","low","close","volume"]
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

EXPECTED_HEADER: tuple[str, ...] = (
    "ms_utc", "open", "high", "low", "close", "volume",
)


@dataclass(frozen=True)
class ObservationsFailure:
    row_index: int  # 0 = first data row; -1 for schema/structural failures
    field: str      # field name or "schema" / "row_count"
    reason: str


@dataclass(frozen=True)
class ObservationsParityResult:
    passed: bool
    row_count: int
    failures: list[ObservationsFailure] = field(default_factory=list)


def _load(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = [r for r in reader]
    return header, rows


def compare_observations(
    *, reference: Path, candidate: Path
) -> ObservationsParityResult:
    ref_h, ref_rows = _load(reference)
    cand_h, cand_rows = _load(candidate)
    failures: list[ObservationsFailure] = []

    if tuple(ref_h) != EXPECTED_HEADER:
        failures.append(ObservationsFailure(
            row_index=-1, field="schema",
            reason=f"reference header {ref_h!r} != expected {list(EXPECTED_HEADER)!r}",
        ))
    if tuple(cand_h) != EXPECTED_HEADER:
        failures.append(ObservationsFailure(
            row_index=-1, field="schema",
            reason=f"candidate header {cand_h!r} != expected {list(EXPECTED_HEADER)!r}",
        ))
    if failures:
        return ObservationsParityResult(passed=False, row_count=0, failures=failures)

    if len(ref_rows) != len(cand_rows):
        failures.append(ObservationsFailure(
            row_index=-1, field="row_count",
            reason=f"reference has {len(ref_rows)} rows; candidate has {len(cand_rows)}",
        ))
        return ObservationsParityResult(
            passed=False, row_count=min(len(ref_rows), len(cand_rows)),
            failures=failures,
        )

    for i, (r, c) in enumerate(zip(ref_rows, cand_rows, strict=True)):
        # ms_utc int
        try:
            if int(r[0]) != int(c[0]):
                failures.append(ObservationsFailure(
                    row_index=i, field="ms_utc",
                    reason=f"{r[0]} != {c[0]}",
                ))
                continue
        except ValueError as e:
            failures.append(ObservationsFailure(
                row_index=i, field="ms_utc", reason=f"unparseable ({e})",
            ))
            continue
        # OHLCV Decimal
        for idx, name in enumerate(("open", "high", "low", "close", "volume"), start=1):
            try:
                if Decimal(r[idx]) != Decimal(c[idx]):
                    failures.append(ObservationsFailure(
                        row_index=i, field=name,
                        reason=f"{r[idx]} != {c[idx]}",
                    ))
            except InvalidOperation as e:
                failures.append(ObservationsFailure(
                    row_index=i, field=name, reason=f"unparseable ({e})",
                ))

    return ObservationsParityResult(
        passed=not failures, row_count=len(ref_rows), failures=failures,
    )
```

- [ ] **Step 4: Run tests, verify pass**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_observations_parity.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/lean_sidecar/parity_matrix/observations_parity.py \
        PythonDataService/tests/lean_sidecar/parity_matrix/test_observations_parity.py
git commit -m "feat(parity-matrix): Gate 1 — observations.csv exact-equality comparator"
```

---

## Task 4: State parity comparator (Gate 2)

Pure-function comparator for `state.csv`. Per-bar agreement: ts/close/cross_state/signal exact; ema_fast/ema_slow/rsi within `atol=1e-9, rtol=0`.

**Files:**
- Create: `PythonDataService/app/lean_sidecar/parity_matrix/state_parity.py`
- Create: `PythonDataService/tests/lean_sidecar/parity_matrix/test_state_parity.py`

- [ ] **Step 1: Write failing tests**

```python
"""Gate 2 — per-bar state.csv parity comparator."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.lean_sidecar.parity_matrix.state_parity import (
    StateParityResult,
    compare_state,
    DEFAULT_INDICATOR_ATOL,
)

HEADER = "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\n"


def _write_state(path: Path, rows: list[str]) -> None:
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")


def test_identical_passes(tmp_path: Path) -> None:
    rows = [
        "1700000000000,100.5,99.1,98.7,55.2,above,HOLD",
        "1700000900000,101.2,99.8,99.0,57.1,above,ENTER",
    ]
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, rows); _write_state(b, rows)
    r = compare_state(reference=a, candidate=b)
    assert r.passed is True
    assert r.row_count == 2


def test_indicator_within_atol_passes(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.5,99.100000000001,98.7,55.2,above,HOLD"])
    r = compare_state(reference=a, candidate=b)
    # Default atol=1e-9: 1e-12 drift is within tolerance.
    assert r.passed is True


def test_indicator_exceeds_atol_fails(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.5,99.10001,98.7,55.2,above,HOLD"])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert r.failures[0].field == "ema_fast"


def test_close_must_be_exact(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.50001,99.1,98.7,55.2,above,HOLD"])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert r.failures[0].field == "close"


def test_signal_enum_exact(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.5,99.1,98.7,55.2,above,ENTER"])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert r.failures[0].field == "signal"


def test_cross_state_enum_exact(tmp_path: Path) -> None:
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write_state(a, ["1700000000000,100.5,99.1,98.7,55.2,above,HOLD"])
    _write_state(b, ["1700000000000,100.5,99.1,98.7,55.2,below,HOLD"])
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert r.failures[0].field == "cross_state"


def test_schema_drift_fails(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"; b = tmp_path / "b.csv"
    a.write_text(HEADER + "1700000000000,100.5,99.1,98.7,55.2,above,HOLD\n",
                 encoding="utf-8")
    b.write_text(
        "ts_ms_utc,close,ema_fast,ema_slow,rsi,signal\n"  # missing cross_state
        "1700000000000,100.5,99.1,98.7,55.2,HOLD\n",
        encoding="utf-8",
    )
    r = compare_state(reference=a, candidate=b)
    assert r.passed is False
    assert any("schema" in f.reason for f in r.failures)


def test_default_indicator_atol_is_1e_minus_9() -> None:
    assert DEFAULT_INDICATOR_ATOL == Decimal("1e-9")
```

- [ ] **Step 2: Run tests, verify failure**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_state_parity.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write `state_parity.py`**

```python
"""Gate 2 — per-bar state.csv parity comparator.

Per-bar agreement:
  * ts_ms_utc, close, cross_state, signal — exact equality
  * ema_fast, ema_slow, rsi — Decimal abs-diff within DEFAULT_INDICATOR_ATOL

State files MUST emit full-precision Decimal strings; this comparator
parses them with Decimal arithmetic so atol=1e-9 holds without float drift.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

EXPECTED_COLUMNS: tuple[str, ...] = (
    "ts_ms_utc", "close", "ema_fast", "ema_slow", "rsi", "cross_state", "signal",
)
DEFAULT_INDICATOR_ATOL: Decimal = Decimal("1e-9")
_INDICATOR_FIELDS: tuple[str, ...] = ("ema_fast", "ema_slow", "rsi")
_VALID_CROSS_STATES: frozenset[str] = frozenset({"above", "below", "equal"})
_VALID_SIGNALS: frozenset[str] = frozenset({"HOLD", "ENTER", "EXIT"})


@dataclass(frozen=True)
class StateFailure:
    row_index: int
    field: str
    reason: str


@dataclass(frozen=True)
class StateParityResult:
    passed: bool
    row_count: int
    failures: list[StateFailure] = field(default_factory=list)


def _load(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return header, rows


def compare_state(
    *,
    reference: Path,
    candidate: Path,
    indicator_atol: Decimal = DEFAULT_INDICATOR_ATOL,
) -> StateParityResult:
    ref_h, ref_rows = _load(reference)
    cand_h, cand_rows = _load(candidate)
    failures: list[StateFailure] = []

    if tuple(ref_h) != EXPECTED_COLUMNS:
        failures.append(StateFailure(
            row_index=-1, field="schema",
            reason=f"reference header {ref_h!r} != expected {list(EXPECTED_COLUMNS)!r}",
        ))
    if tuple(cand_h) != EXPECTED_COLUMNS:
        failures.append(StateFailure(
            row_index=-1, field="schema",
            reason=f"candidate header {cand_h!r} != expected {list(EXPECTED_COLUMNS)!r}",
        ))
    if failures:
        return StateParityResult(passed=False, row_count=0, failures=failures)

    if len(ref_rows) != len(cand_rows):
        failures.append(StateFailure(
            row_index=-1, field="row_count",
            reason=f"reference has {len(ref_rows)} rows; candidate has {len(cand_rows)}",
        ))
        return StateParityResult(
            passed=False, row_count=min(len(ref_rows), len(cand_rows)),
            failures=failures,
        )

    for i, (r, c) in enumerate(zip(ref_rows, cand_rows, strict=True)):
        # Exact equality for ts, close, cross_state, signal.
        if r["ts_ms_utc"] != c["ts_ms_utc"]:
            failures.append(StateFailure(
                row_index=i, field="ts_ms_utc",
                reason=f"{r['ts_ms_utc']} != {c['ts_ms_utc']}",
            ))
            continue
        try:
            if Decimal(r["close"]) != Decimal(c["close"]):
                failures.append(StateFailure(
                    row_index=i, field="close",
                    reason=f"{r['close']} != {c['close']}",
                ))
        except InvalidOperation as e:
            failures.append(StateFailure(
                row_index=i, field="close", reason=f"unparseable ({e})",
            ))
        for name in _INDICATOR_FIELDS:
            try:
                diff = abs(Decimal(r[name]) - Decimal(c[name]))
                if diff > indicator_atol:
                    failures.append(StateFailure(
                        row_index=i, field=name,
                        reason=(f"abs_diff={diff} > atol={indicator_atol} "
                                f"({r[name]} vs {c[name]})"),
                    ))
            except InvalidOperation as e:
                failures.append(StateFailure(
                    row_index=i, field=name, reason=f"unparseable ({e})",
                ))
        if r["cross_state"] != c["cross_state"]:
            failures.append(StateFailure(
                row_index=i, field="cross_state",
                reason=f"{r['cross_state']} != {c['cross_state']}",
            ))
        if r["signal"] != c["signal"]:
            failures.append(StateFailure(
                row_index=i, field="signal",
                reason=f"{r['signal']} != {c['signal']}",
            ))

    return StateParityResult(
        passed=not failures, row_count=len(ref_rows), failures=failures,
    )
```

- [ ] **Step 4: Run tests, verify pass**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_state_parity.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Project-scope lint**

```
ruff check PythonDataService/app/ PythonDataService/tests/
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/lean_sidecar/parity_matrix/state_parity.py \
        PythonDataService/tests/lean_sidecar/parity_matrix/test_state_parity.py
git commit -m "feat(parity-matrix): Gate 2 — per-bar state.csv comparator (atol=1e-9)"
```

---

## Task 5: Align Engine Lab EMA-crossover strategy to emit `observations.csv` + `state.csv`

The Engine Lab strategy at `app/engine/strategy/algorithms/spy_ema_crossover.py` must emit `observations.csv` and `state.csv` with the **exact same schema** as the LEAN trusted sample at `app/lean_sidecar/trusted_samples/ema_crossover.py`. The cross-runner already wires Engine Lab to the same data folder; we just need the algorithm to write the two CSVs in a deterministic location the cell runner can read.

**Files:**
- Read: `PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py`
- Modify: same file (add observations + state emitters)
- Modify: `PythonDataService/tests/engine/strategy/algorithms/test_spy_ema_crossover.py` (or create if missing)

- [ ] **Step 1: Read the existing strategy and its base class**

```
cat PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py
cat PythonDataService/app/engine/strategy/base.py | head -200
```

Identify:
1. Where the strategy receives consolidated 15-min bars.
2. Where it writes any existing output files (if any).
3. The bar timestamp convention (verify it produces `int64 ms UTC` at the boundary).
4. The constants: `FAST_PERIOD=5, SLOW_PERIOD=10, RSI_PERIOD=14, EXIT_BARS=5, GAP_MIN=0.20, RSI_LO=50, RSI_HI=70`. These MUST match the LEAN trusted sample.

If any constant disagrees with `EMA_CROSSOVER_SOURCE` in `app/lean_sidecar/trusted_samples/ema_crossover.py`, **stop and surface the discrepancy** — do not edit. Either side is wrong, and that needs a deliberate decision before fixtures are generated.

- [ ] **Step 2: Write the failing test**

Path: `PythonDataService/tests/engine/strategy/algorithms/test_spy_ema_crossover_emitters.py`

```python
"""Engine-Lab EMA crossover MUST emit observations.csv + state.csv with
the same column schema as the LEAN trusted sample."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossover

# These imports are speculative — replace with the actual public surface.
# The test EXPECTS the strategy to accept a `output_dir: Path | None` kwarg
# at construction so the cell runner can capture per-cell outputs.


def test_strategy_accepts_output_dir_kwarg(tmp_path: Path) -> None:
    s = SpyEmaCrossover(symbol="SPY", output_dir=tmp_path)
    assert s is not None


def test_strategy_emits_observations_with_correct_header(tmp_path: Path) -> None:
    s = SpyEmaCrossover(symbol="SPY", output_dir=tmp_path)
    s._open_emitters()  # Internal hook; replace with whatever lifecycle method exists.
    with (tmp_path / "observations.csv").open("r", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == ["ms_utc", "open", "high", "low", "close", "volume"]


def test_strategy_emits_state_with_correct_header(tmp_path: Path) -> None:
    s = SpyEmaCrossover(symbol="SPY", output_dir=tmp_path)
    s._open_emitters()
    with (tmp_path / "state.csv").open("r", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == ["ts_ms_utc", "close", "ema_fast", "ema_slow",
                      "rsi", "cross_state", "signal"]


def test_emitters_use_full_decimal_precision(tmp_path: Path) -> None:
    s = SpyEmaCrossover(symbol="SPY", output_dir=tmp_path)
    s._open_emitters()
    s._emit_state_row(
        ts_ms_utc=1700000000000,
        close="100.500000000001",
        ema_fast="99.123456789012",
        ema_slow="98.987654321098",
        rsi="55.234567890123",
        cross_state="above",
        signal="HOLD",
    )
    with (tmp_path / "state.csv").open("r", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[1] == ["1700000000000", "100.500000000001",
                       "99.123456789012", "98.987654321098",
                       "55.234567890123", "above", "HOLD"]
```

- [ ] **Step 3: Run the test, verify failure**

```
podman exec polygon-data-service python -m pytest \
    tests/engine/strategy/algorithms/test_spy_ema_crossover_emitters.py -v
```
Expected: failures around missing `output_dir`, `_open_emitters`, or `_emit_state_row`.

- [ ] **Step 4: Implement the emitter API on `SpyEmaCrossover`**

Add to `app/engine/strategy/algorithms/spy_ema_crossover.py`:

```python
# Inside the class:
def __init__(self, *, symbol: str, output_dir: Path | None = None, **kwargs):
    super().__init__(...)  # whatever the existing super signature is
    self._output_dir = output_dir
    self._observations_writer = None
    self._state_writer = None
    self._observations_fp = None
    self._state_fp = None

def _open_emitters(self) -> None:
    if self._output_dir is None:
        return
    self._output_dir.mkdir(parents=True, exist_ok=True)
    self._observations_fp = (self._output_dir / "observations.csv").open(
        "w", encoding="utf-8", newline="")
    self._observations_writer = csv.writer(self._observations_fp)
    self._observations_writer.writerow(
        ["ms_utc", "open", "high", "low", "close", "volume"])
    self._state_fp = (self._output_dir / "state.csv").open(
        "w", encoding="utf-8", newline="")
    self._state_writer = csv.writer(self._state_fp)
    self._state_writer.writerow(
        ["ts_ms_utc", "close", "ema_fast", "ema_slow", "rsi",
         "cross_state", "signal"])

def _emit_observation_row(
    self, *, ms_utc: int, open_: str, high: str, low: str,
    close: str, volume: str,
) -> None:
    if self._observations_writer is not None:
        self._observations_writer.writerow(
            [str(ms_utc), open_, high, low, close, volume])

def _emit_state_row(
    self, *, ts_ms_utc: int, close: str, ema_fast: str, ema_slow: str,
    rsi: str, cross_state: str, signal: str,
) -> None:
    if self._state_writer is not None:
        self._state_writer.writerow(
            [str(ts_ms_utc), close, ema_fast, ema_slow, rsi,
             cross_state, signal])

def _close_emitters(self) -> None:
    for fp in (self._observations_fp, self._state_fp):
        if fp is not None:
            fp.close()
```

Wire `_open_emitters()` into the strategy's lifecycle (e.g., `on_init` / `setup` — match the base class). Wire `_emit_observation_row` into the minute-bar consumption hook. Wire `_emit_state_row` into the consolidated-bar handler, after both EMAs and RSI report `is_ready=True`. Wire `_close_emitters()` into the teardown hook.

**Values must be written as full-precision Decimal strings.** Use `str(Decimal(value))` — do NOT format with `f"{x:.6f}"`. The Decimal arithmetic in `app/engine/indicators/` already operates in full precision; the emitter must preserve it.

- [ ] **Step 5: Run the test, verify pass**

```
podman exec polygon-data-service python -m pytest \
    tests/engine/strategy/algorithms/test_spy_ema_crossover_emitters.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Project-scope lint + full algo test suite**

```
ruff check PythonDataService/app/ PythonDataService/tests/
podman exec polygon-data-service python -m pytest tests/engine/strategy/algorithms/ -v
```
Expected: clean lint, no regression in existing strategy tests.

- [ ] **Step 7: Commit**

```bash
git add PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py \
        PythonDataService/tests/engine/strategy/algorithms/test_spy_ema_crossover_emitters.py
git commit -m "feat(engine): SpyEmaCrossover emits observations.csv + state.csv at full Decimal precision"
```

---

## Task 6: Cell runner — orchestrate LEAN + Engine + 3 gates

The cell runner is the single function called per cell. It does NOT spawn LEAN itself (the regen script wraps that); it operates on already-staged outputs and runs the three gates in order.

**Files:**
- Create: `PythonDataService/app/lean_sidecar/parity_matrix/cell_runner.py`
- Create: `PythonDataService/tests/lean_sidecar/parity_matrix/test_cell_runner.py`

- [ ] **Step 1: Write failing tests**

```python
"""Cell runner — three-gate orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.lean_sidecar.parity_matrix.cell_runner import (
    CellGateResult,
    CellRunReport,
    run_cell_gates,
)


def _make_minimal_pinned_cell(d: Path) -> Path:
    """Create a minimal cell directory with valid pinned LEAN outputs."""
    cell = d / "cell"; cell.mkdir()
    lean = cell / "lean"; lean.mkdir()
    (lean / "observations.csv").write_text(
        "ms_utc,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8")
    (lean / "state.csv").write_text(
        "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\n"
        "1,1,1,1,1,above,HOLD\n", encoding="utf-8")
    (lean / "orders.json").write_text('{"orders": []}', encoding="utf-8")
    return cell


def _make_matching_engine_outputs(d: Path) -> Path:
    eng = d / "engine"; eng.mkdir()
    (eng / "observations.csv").write_text(
        "ms_utc,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8")
    (eng / "state.csv").write_text(
        "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\n"
        "1,1,1,1,1,above,HOLD\n", encoding="utf-8")
    return eng


def test_all_gates_pass(tmp_path: Path) -> None:
    pinned = _make_minimal_pinned_cell(tmp_path)
    eng = _make_matching_engine_outputs(tmp_path)
    # No fills on either side — Gate 3 (cross-reconciler) sees an empty
    # alignment, returns passed.
    eng_orders: list = []
    report = run_cell_gates(
        pinned_lean_dir=pinned / "lean",
        engine_output_dir=eng,
        engine_normalized_orders=eng_orders,
    )
    assert report.overall_passed is True
    assert report.observations.passed is True
    assert report.state.passed is True
    assert report.trade.passed is True


def test_gate1_failure_short_circuits(tmp_path: Path) -> None:
    pinned = _make_minimal_pinned_cell(tmp_path)
    eng = _make_matching_engine_outputs(tmp_path)
    # Break observations on engine side.
    (eng / "observations.csv").write_text(
        "ms_utc,open,high,low,close,volume\n2,1,1,1,1,1\n", encoding="utf-8")
    report = run_cell_gates(
        pinned_lean_dir=pinned / "lean",
        engine_output_dir=eng,
        engine_normalized_orders=[],
    )
    assert report.overall_passed is False
    assert report.observations.passed is False
    # Gate 2 and 3 not evaluated.
    assert report.state is None
    assert report.trade is None


def test_gate2_failure_skips_gate3(tmp_path: Path) -> None:
    pinned = _make_minimal_pinned_cell(tmp_path)
    eng = _make_matching_engine_outputs(tmp_path)
    # Identical observations, divergent state.
    (eng / "state.csv").write_text(
        "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\n"
        "1,1,1,1,2,above,HOLD\n",  # rsi=2 vs pinned rsi=1
        encoding="utf-8")
    report = run_cell_gates(
        pinned_lean_dir=pinned / "lean",
        engine_output_dir=eng,
        engine_normalized_orders=[],
    )
    assert report.overall_passed is False
    assert report.observations.passed is True
    assert report.state.passed is False
    assert report.trade is None
```

- [ ] **Step 2: Run test, verify failure**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_cell_runner.py -v
```
Expected: ImportError.

- [ ] **Step 3: Write `cell_runner.py`**

```python
"""Cell runner — three-gate orchestration.

Operates on already-staged outputs (LEAN pinned, Engine live). LEAN
container invocation lives in the regeneration script; this module is
pure orchestration so it's exercisable in tests without LEAN running.

Gate order — short-circuit on failure:
  Gate 1: observations.csv exact equality
  Gate 2: state.csv per-bar parity within atol=1e-9
  Gate 3: trade-level cross-reconciler (8-category taxonomy)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.lean_sidecar.cross_reconciler import (
    CrossReconciliationTolerances,
    reconcile_cross_engine,  # if it's named differently, adjust import here
)
from app.lean_sidecar.normalized_parser import (
    NormalizedOrderEvent,
    parse_orders,  # adjust to actual exported name
)
from app.lean_sidecar.parity_matrix.observations_parity import (
    ObservationsParityResult,
    compare_observations,
)
from app.lean_sidecar.parity_matrix.state_parity import (
    StateParityResult,
    compare_state,
)

# When Gate 3 has no fills on either side, the cross-reconciler returns
# an empty-but-passed report. Same shape used here.


@dataclass(frozen=True)
class CellGateResult:
    """Wraps an underlying gate's report for the cell-level summary."""
    name: str
    passed: bool
    detail: dict  # serializable representation of the inner report


@dataclass(frozen=True)
class CellRunReport:
    overall_passed: bool
    observations: ObservationsParityResult
    state: StateParityResult | None
    trade: CellGateResult | None


def run_cell_gates(
    *,
    pinned_lean_dir: Path,
    engine_output_dir: Path,
    engine_normalized_orders: list,
    trade_tolerances: CrossReconciliationTolerances | None = None,
    assert_fees: bool = True,
) -> CellRunReport:
    obs = compare_observations(
        reference=pinned_lean_dir / "observations.csv",
        candidate=engine_output_dir / "observations.csv",
    )
    if not obs.passed:
        return CellRunReport(overall_passed=False, observations=obs,
                             state=None, trade=None)

    state = compare_state(
        reference=pinned_lean_dir / "state.csv",
        candidate=engine_output_dir / "state.csv",
    )
    if not state.passed:
        return CellRunReport(overall_passed=False, observations=obs,
                             state=state, trade=None)

    # Gate 3 — load pinned LEAN orders, parse to NormalizedOrderEvent list,
    # run cross-reconciler against the engine's normalized orders.
    with (pinned_lean_dir / "orders.json").open("r", encoding="utf-8") as f:
        pinned_orders_payload = json.load(f)
    pinned_lean_events: list[NormalizedOrderEvent] = parse_orders(
        pinned_orders_payload
    )
    tol = trade_tolerances or CrossReconciliationTolerances.default()
    cross = reconcile_cross_engine(
        lean_events=pinned_lean_events,
        engine_events=engine_normalized_orders,
        tolerances=tol,
        assert_fees=assert_fees,
    )
    trade_result = CellGateResult(
        name="trade",
        passed=(cross.status == "passed"),
        detail=cross.to_dict() if hasattr(cross, "to_dict") else {},
    )
    return CellRunReport(
        overall_passed=trade_result.passed,
        observations=obs, state=state, trade=trade_result,
    )
```

**Note for the implementer:** the exact symbols `reconcile_cross_engine`, `parse_orders`, and `CrossReconciliationTolerances.default()` may have slightly different names in the existing modules. Before writing `cell_runner.py`, grep for the actual entrypoints:

```
grep -n "^def " PythonDataService/app/lean_sidecar/cross_reconciler.py
grep -n "^def " PythonDataService/app/lean_sidecar/normalized_parser.py
```

and adjust the imports to match. Don't add adapters — call the canonical API directly.

- [ ] **Step 4: Run tests, verify pass**

```
podman exec polygon-data-service python -m pytest tests/lean_sidecar/parity_matrix/test_cell_runner.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/lean_sidecar/parity_matrix/cell_runner.py \
        PythonDataService/tests/lean_sidecar/parity_matrix/test_cell_runner.py
git commit -m "feat(parity-matrix): three-gate cell runner (short-circuit on failure)"
```

---

## Task 7: Regeneration CLI script + fixture directory bootstrap

This script is what an operator runs to (re)build the pinned LEAN side of one or more cells. It does NOT touch the committed fixture directory unless all three gates pass for the cell.

**Files:**
- Create: `PythonDataService/scripts/regenerate_cross_engine_study.py`
- Create: `PythonDataService/tests/scripts/test_regenerate_cross_engine_study.py`
- Create: `PythonDataService/tests/fixtures/golden/cross-engine-studies/README.md`

- [ ] **Step 1: Write the fixture-directory README**

`PythonDataService/tests/fixtures/golden/cross-engine-studies/README.md`:

```markdown
# Cross-Engine Golden-Fixture Matrix

12 cells = 4 tickers (SPY, QQQ, AAPL, TSLA) × 3 nested windows (W6mo / W12mo / W24mo, all ending 2026-04-30). Each cell pins LEAN orders.json + state.csv + observations.csv as the reference; Engine Lab runs live at test time.

Authoritative design: `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md`.

## Layout

- `_lean_data_capture/<TICKER>/` — shared 24mo minute capture per ticker (LEAN deci-cent zips). Three cells per ticker read from this single capture.
- `cells/<CELL_ID>/` — one directory per (ticker, window). Contains `manifest.json`, `attribution.md`, `lean/`, `reconciliation_pinned.json`.

## Regeneration

Triggers (only these):
1. LEAN container image digest changes.
2. Trusted-sample source changes.
3. Deliberate refresh after a parity audit changed the contract.

No quarterly regen. Freshness checks belong in a separate canary job, not here.

Workflow: `python scripts/regenerate_cross_engine_study.py --cell <id> | --ticker <T> | --all`. The script refuses to write a cell directory unless all three gates pass.

## Tests

- Smoke (every PR): `pytest -m cross_engine_smoke` — runs the four W6mo cells.
- Full (pre-push / nightly): `pytest -m slow tests/research/parity/test_cross_engine_study.py` — runs all 12.
```

- [ ] **Step 2: Write the failing test for the regen script**

`PythonDataService/tests/scripts/test_regenerate_cross_engine_study.py`:

```python
"""Regen script — argument parsing + cell-selection logic.

LEAN container invocation is mocked; we test the orchestration shape only.
"""
from __future__ import annotations

import argparse

import pytest

from PythonDataService.scripts import regenerate_cross_engine_study as regen


def test_parse_args_all() -> None:
    ns = regen._parse_args(["--all"])
    assert ns.all is True
    assert ns.cell is None
    assert ns.ticker is None


def test_parse_args_one_cell() -> None:
    ns = regen._parse_args(["--cell", "SPY_W6mo_2025-11-03_to_2026-04-30"])
    assert ns.cell == "SPY_W6mo_2025-11-03_to_2026-04-30"


def test_parse_args_one_ticker() -> None:
    ns = regen._parse_args(["--ticker", "SPY"])
    assert ns.ticker == "SPY"


def test_parse_args_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        regen._parse_args(["--all", "--cell", "SPY_W6mo_2025-11-03_to_2026-04-30"])


def test_resolve_target_cells_all() -> None:
    cells = regen._resolve_target_cells(
        argparse.Namespace(all=True, cell=None, ticker=None))
    assert len(cells) == 12


def test_resolve_target_cells_ticker() -> None:
    cells = regen._resolve_target_cells(
        argparse.Namespace(all=False, cell=None, ticker="SPY"))
    assert len(cells) == 3
    assert all(c.ticker == "SPY" for c in cells)


def test_resolve_target_cells_single() -> None:
    cells = regen._resolve_target_cells(
        argparse.Namespace(
            all=False,
            cell="SPY_W6mo_2025-11-03_to_2026-04-30",
            ticker=None))
    assert len(cells) == 1
    assert cells[0].cell_id == "SPY_W6mo_2025-11-03_to_2026-04-30"
```

- [ ] **Step 3: Run test, verify failure**

```
podman exec polygon-data-service python -m pytest tests/scripts/test_regenerate_cross_engine_study.py -v
```
Expected: ImportError.

- [ ] **Step 4: Write the regen script**

`PythonDataService/scripts/regenerate_cross_engine_study.py`:

```python
"""Regenerate cross-engine golden-fixture cells.

Per-cell sequence:
  1. Pre-flight: verify _lean_data_capture/<TICKER>/ exists and its
     data_contract_hash matches the capture manifest.
  2. Stage LEAN sidecar run for (ticker, window) into a temp dir.
  3. Run Engine Lab live against the same capture, into another temp dir.
  4. Run all three gates (observations, state, trade-level).
  5. On pass: replace the committed cell directory atomically; write
     manifest.json with fresh hashes.
  6. On fail: emit failure report to a sibling .failed/ dir; exit
     non-zero; leave committed cell directory untouched.

Reference: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
            § "Regeneration workflow"
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.lean_sidecar.parity_matrix.cell_runner import (
    CellRunReport,
    run_cell_gates,
)
from app.lean_sidecar.parity_matrix.manifest import (
    CellManifest,
    sha256_of_file,
    sha256_of_text,
)
from app.lean_sidecar.parity_matrix.matrix import CELLS, Cell, cell_by_id

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = (
    REPO_ROOT / "PythonDataService" / "tests" / "fixtures" / "golden"
    / "cross-engine-studies"
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Regenerate cross-engine golden-fixture cells.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true",
                       help="Regenerate all 12 cells.")
    group.add_argument("--cell", type=str,
                       help="Regenerate one cell by cell_id.")
    group.add_argument("--ticker", type=str,
                       help="Regenerate all three cells for one ticker.")
    return p.parse_args(argv)


def _resolve_target_cells(ns: argparse.Namespace) -> list[Cell]:
    if ns.all:
        return list(CELLS)
    if ns.cell:
        return [cell_by_id(ns.cell)]
    if ns.ticker:
        return [c for c in CELLS if c.ticker == ns.ticker]
    raise SystemExit("no target specified")


def _stage_lean_run(cell: Cell, output_dir: Path) -> None:
    """Invoke LEAN sidecar for one cell, writing orders.json / state.csv /
    observations.csv into output_dir/lean/.

    This is the integration boundary with the existing
    app/lean_sidecar/runner.py + workspace machinery. Implementation
    detail: bind the workspace's data folder to the shared
    _lean_data_capture/<ticker>/ directory, pass the EMA_CROSSOVER_SOURCE
    trusted sample, set parameters {symbol, start_date, end_date,
    starting_cash, bar_minutes=15, session=regular, adjustment=raw},
    run, then copy state.csv + observations.csv from the LEAN
    ObjectStore into output_dir/lean/, and copy the parsed orders
    payload to orders.json.
    """
    raise NotImplementedError("wire to existing LEAN sidecar runner")


def _run_engine_live(cell: Cell, output_dir: Path) -> list:
    """Run Engine Lab via cross_runner.run_engine_lab_on_workspace; write
    observations.csv + state.csv into output_dir/engine/; return the
    normalized order events for Gate 3."""
    raise NotImplementedError("wire to cross_runner.run_engine_lab_on_workspace")


def _write_cell_atomically(
    *, cell: Cell, staged_lean_dir: Path, reconciliation: dict,
) -> None:
    """Replace the committed cell directory in one os.replace; write
    manifest.json + attribution.md + reconciliation_pinned.json."""
    target = FIXTURE_ROOT / "cells" / cell.cell_id
    staging = FIXTURE_ROOT / "cells" / f".{cell.cell_id}.new"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    # Copy lean/ outputs
    shutil.copytree(staged_lean_dir, staging / "lean")
    # Write reconciliation_pinned.json
    (staging / "reconciliation_pinned.json").write_text(
        json.dumps(reconciliation, indent=2, sort_keys=True),
        encoding="utf-8")
    # Build manifest
    manifest = _build_manifest(cell, staging)
    (staging / "manifest.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8")
    # Attribution stub
    (staging / "attribution.md").write_text(
        f"# {cell.cell_id} attribution\n\nRegenerated by "
        f"`regenerate_cross_engine_study.py`. See manifest.json for the "
        f"full provenance block.\n",
        encoding="utf-8")
    # Atomic replace
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)


def _build_manifest(cell: Cell, staging: Path) -> CellManifest:
    """Build the CellManifest with fresh artifact hashes."""
    # Implementation detail — fill in trusted_sample_source_sha256 by
    # hashing EMA_CROSSOVER_SOURCE; resolve container_image_digest from
    # the LEAN sidecar config's ALLOWED_IMAGE_DIGESTS pin; resolve
    # fixture_git_commit via `git rev-parse HEAD`.
    raise NotImplementedError("complete the manifest builder")


def _emit_failure_report(cell: Cell, report: CellRunReport, root: Path) -> None:
    failure_dir = root / ".failed" / cell.cell_id
    failure_dir.mkdir(parents=True, exist_ok=True)
    (failure_dir / "report.json").write_text(
        json.dumps({
            "cell_id": cell.cell_id,
            "overall_passed": report.overall_passed,
            "observations_passed": report.observations.passed,
            "state_passed": (report.state.passed
                             if report.state is not None else None),
            "trade_passed": (report.trade.passed
                             if report.trade is not None else None),
            "observations_failures": [
                f.__dict__ for f in report.observations.failures
            ],
            "state_failures": (
                [f.__dict__ for f in report.state.failures]
                if report.state is not None else None),
        }, indent=2, sort_keys=True), encoding="utf-8")


def regenerate_one_cell(cell: Cell) -> bool:
    """Regenerate one cell. Returns True on success, False on failure."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        lean_out = tmp_root / "lean_staged"; lean_out.mkdir()
        eng_out = tmp_root / "engine_staged"; eng_out.mkdir()
        _stage_lean_run(cell, lean_out)
        engine_orders = _run_engine_live(cell, eng_out)
        report = run_cell_gates(
            pinned_lean_dir=lean_out / "lean",
            engine_output_dir=eng_out / "engine",
            engine_normalized_orders=engine_orders,
        )
        if not report.overall_passed:
            _emit_failure_report(cell, report, FIXTURE_ROOT)
            return False
        # Build reconciliation_pinned.json from the trade gate's detail.
        reconciliation = {
            "status": "passed",
            "trade_gate": report.trade.detail if report.trade else {},
            "captured_at_ms_utc": int(
                datetime.now(timezone.utc).timestamp() * 1000),
        }
        _write_cell_atomically(
            cell=cell, staged_lean_dir=lean_out / "lean",
            reconciliation=reconciliation,
        )
        return True


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])
    cells = _resolve_target_cells(ns)
    print(f"Regenerating {len(cells)} cell(s): {[c.cell_id for c in cells]}")
    failures: list[str] = []
    for c in cells:
        print(f"--- {c.cell_id} ---")
        if not regenerate_one_cell(c):
            failures.append(c.cell_id)
            print(f"  FAILED — see .failed/{c.cell_id}/report.json")
        else:
            print(f"  passed")
    if failures:
        print(f"\n{len(failures)} cell(s) failed: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

The `_stage_lean_run`, `_run_engine_live`, and `_build_manifest` functions are intentionally left as `NotImplementedError` stubs at this task — they are wired in Task 9 (after the data capture exists). The args-parsing + orchestration shape is what this task verifies.

- [ ] **Step 5: Run tests, verify pass**

```
podman exec polygon-data-service python -m pytest tests/scripts/test_regenerate_cross_engine_study.py -v
```
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/scripts/regenerate_cross_engine_study.py \
        PythonDataService/tests/scripts/test_regenerate_cross_engine_study.py \
        PythonDataService/tests/fixtures/golden/cross-engine-studies/README.md
git commit -m "feat(parity-matrix): regen script skeleton + fixture README"
```

---

## Task 8: Live parameterized pytest for the 12-cell matrix

The pytest file that runs Engine Lab live against pinned LEAN fixtures. Marked with `cross_engine_smoke` on the four W6mo cells (every-PR CI) and `slow` on the eight W12mo+W24mo cells (pre-push / on-demand).

Tests are written **before** fixtures exist. Until the regen script generates fixtures, the test will skip with a clear "fixture missing" message — that's the intended state during early development.

**Files:**
- Create: `PythonDataService/tests/research/parity/test_cross_engine_study.py`
- Modify: `PythonDataService/pytest.ini` to register the new marker

- [ ] **Step 1: Register the marker in `pytest.ini`**

Add to `[pytest]` markers section:

```ini
cross_engine_smoke: 6-month cells run on every PR (subset of cross_engine_study)
```

- [ ] **Step 2: Write the parameterized test**

```python
"""Cross-engine matrix parity test — Engine Lab live vs pinned LEAN.

Each cell loads pinned LEAN orders.json + state.csv + observations.csv,
runs Engine Lab live against the shared _lean_data_capture/<TICKER>/
data folder, and asserts all three gates pass.

Markers:
  * `cross_engine_smoke` — applied to W6mo cells (4 of 12); runs on every PR.
  * `slow` — applied to W12mo and W24mo cells (8 of 12); run pre-push / on-demand.

Reference: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.lean_sidecar.parity_matrix.cell_runner import run_cell_gates
from app.lean_sidecar.parity_matrix.matrix import CELLS, Cell, WindowLabel

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "golden" / "cross-engine-studies"
)


def _id_for(cell: Cell) -> str:
    return cell.cell_id


def _markers_for(cell: Cell) -> list:
    if cell.window_label is WindowLabel.W6MO:
        return [pytest.mark.cross_engine_smoke]
    return [pytest.mark.slow]


def _parametrize_cells() -> list:
    return [
        pytest.param(c, id=_id_for(c), marks=_markers_for(c)) for c in CELLS
    ]


@pytest.mark.parametrize("cell", _parametrize_cells())
def test_cross_engine_cell(cell: Cell, tmp_path: Path) -> None:
    cell_dir = FIXTURE_ROOT / "cells" / cell.cell_id
    if not cell_dir.is_dir():
        pytest.skip(
            f"fixture missing — run `python scripts/regenerate_cross_engine_study.py "
            f"--cell {cell.cell_id}` to generate"
        )

    pinned_lean_dir = cell_dir / "lean"
    assert pinned_lean_dir.is_dir(), f"pinned lean/ missing in {cell_dir}"

    # Run Engine Lab live against the shared capture.
    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    engine_orders = _run_engine_for_cell(cell, engine_dir)

    report = run_cell_gates(
        pinned_lean_dir=pinned_lean_dir,
        engine_output_dir=engine_dir,
        engine_normalized_orders=engine_orders,
    )

    if not report.overall_passed:
        # Emit a structured failure summary into the test report.
        msg = [f"Cell {cell.cell_id} failed parity:"]
        if not report.observations.passed:
            msg.append(f"  Gate 1 (observations): {len(report.observations.failures)} failures")
            for f in report.observations.failures[:5]:
                msg.append(f"    row={f.row_index} field={f.field}: {f.reason}")
        elif report.state and not report.state.passed:
            msg.append(f"  Gate 2 (state): {len(report.state.failures)} failures")
            for f in report.state.failures[:5]:
                msg.append(f"    row={f.row_index} field={f.field}: {f.reason}")
        elif report.trade and not report.trade.passed:
            msg.append(f"  Gate 3 (trade): {report.trade.detail}")
        pytest.fail("\n".join(msg))


def _run_engine_for_cell(cell: Cell, output_dir: Path) -> list:
    """Run Engine Lab for one cell.

    Wires app.lean_sidecar.cross_runner.run_engine_lab_on_workspace to
    point at the shared _lean_data_capture/<TICKER>/ directory, with
    the SpyEmaCrossover strategy resolved by class name.
    """
    from datetime import datetime, time
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from app.lean_sidecar.cross_runner import run_engine_lab_on_workspace

    # The cross-runner reads ``<workspace_path>/data/equity/usa/minute/<symbol>/``
    # so we point workspace_path at _lean_data_capture/<TICKER>/'s parent
    # if the layout matches; otherwise we build a workspace pointer
    # whose `data/` is the capture root.
    capture = FIXTURE_ROOT / "_lean_data_capture" / cell.ticker
    if not capture.is_dir():
        pytest.skip(
            f"capture missing — run capture step to populate "
            f"{capture}"
        )

    result = run_engine_lab_on_workspace(
        workspace_path=capture,  # cross_runner reads <ws>/data/equity/usa/...
        strategy_class_name="SpyEmaCrossover",
        symbol=cell.ticker,
        start_date=cell.start_date,
        end_date=cell.end_date,
        initial_cash=Decimal(100000),
    )
    # The strategy emitted state.csv + observations.csv into its own
    # output_dir; cross_runner needs to be configured to pass that
    # output_dir to the strategy. If the existing cross_runner doesn't
    # surface this kwarg, extend it in Task 5 / Task 8.
    return result.order_events
```

- [ ] **Step 3: Run the test — expect all cells to skip (fixtures don't exist yet)**

```
podman exec polygon-data-service python -m pytest \
    tests/research/parity/test_cross_engine_study.py -v
```
Expected: 12 skipped with "fixture missing" messages.

- [ ] **Step 4: Run smoke selector**

```
podman exec polygon-data-service python -m pytest \
    tests/research/parity/test_cross_engine_study.py -v -m cross_engine_smoke
```
Expected: 4 selected, 4 skipped (the W6mo cells).

- [ ] **Step 5: Verify default `not slow` test run does not collect the 8 slow cells**

```
podman exec polygon-data-service python -m pytest \
    tests/research/parity/test_cross_engine_study.py -v -k "not slow" --co
```
Expected: only the 4 W6mo cells collected.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/tests/research/parity/test_cross_engine_study.py \
        PythonDataService/pytest.ini
git commit -m "test(parity-matrix): parameterized 12-cell live test with smoke/slow markers"
```

---

## Task 9: Polygon capture — 24mo minute data for all four tickers

Populate `_lean_data_capture/{SPY,QQQ,AAPL,TSLA}/equity/usa/minute/<ticker>/*_trade.zip` using the existing data-lake `ensure_data` pipeline (Slice 1c). Each capture is the shared input for the ticker's three cells.

**Files:**
- Modify: existing data-lake `ensure_data` invocation — no new code, but a CLI invocation per ticker.
- Create: capture manifests at `_lean_data_capture/<TICKER>/manifest.json` documenting the source.

- [ ] **Step 1: Verify Polygon API key is configured**

```
podman exec polygon-data-service env | grep POLYGON_API_KEY
```

If unset, abort and surface to the user. This is a runtime requirement; do not silently skip.

- [ ] **Step 2: Identify the `ensure_data` invocation surface**

```
grep -rn "ensure_data" PythonDataService/app/ --include="*.py" | head -30
```

Find the function or endpoint that fetches a (symbol, start, end) minute range and writes LEAN deci-cent zips to `LEAN_DATA_WRITE_ROOT`. Recent commits (Slice 1c PR-F) wired this; locate the entrypoint and confirm it accepts the parameters needed.

- [ ] **Step 3: Capture SPY 24mo minute data**

For each ticker, invoke `ensure_data` (or its CLI equivalent) for the W24mo span:

```
podman exec polygon-data-service python -c "
import asyncio
from datetime import date
from app.data_lake.ensure import ensure_data  # adjust import to actual entrypoint

async def run():
    await ensure_data(
        symbol='SPY',
        start=date(2024, 6, 3),
        end=date(2026, 4, 30),
        resolution='minute',
        adjustment='raw',
    )

asyncio.run(run())
"
```

Adjust the import and function signature to match what Slice 1c PR-F actually exposed.

- [ ] **Step 4: Copy / link zips into `_lean_data_capture/SPY/`**

The data lake writes to `LEAN_DATA_WRITE_ROOT`; the fixture directory needs the same layout under `_lean_data_capture/SPY/equity/usa/minute/spy/`. Either symlink (preferred for dev) or copy (for committed fixtures).

```
mkdir -p PythonDataService/tests/fixtures/golden/cross-engine-studies/_lean_data_capture/SPY/equity/usa/minute/spy/
# Copy the zips from $LEAN_DATA_WRITE_ROOT/equity/usa/minute/spy/ for dates 2024-06-03 to 2026-04-30.
```

- [ ] **Step 5: Write the capture manifest**

`PythonDataService/tests/fixtures/golden/cross-engine-studies/_lean_data_capture/SPY/manifest.json`:

```json
{
  "schema_version": 1,
  "ticker": "SPY",
  "source": "polygon",
  "resolution": "minute",
  "adjustment": "raw",
  "start_date": "2024-06-03",
  "end_date": "2026-04-30",
  "captured_at_ms_utc": <fill in via `date +%s%3N`>,
  "captured_by": "<git user.name>",
  "data_contract_hash": "<sha256 over the sorted list of zip filenames + their sha256s>"
}
```

- [ ] **Step 6: Repeat for QQQ, AAPL, TSLA**

Three more invocations of Steps 3–5, one per ticker.

- [ ] **Step 7: Commit the captures**

```bash
git add PythonDataService/tests/fixtures/golden/cross-engine-studies/_lean_data_capture/
git commit -m "fixture(parity-matrix): 24mo minute captures — SPY, QQQ, AAPL, TSLA"
```

Note: this commit is large (~40 MB across four tickers). That's acceptable; the regen policy treats this directory as immutable except on the same triggers that regenerate cells.

---

## Task 10: Wire `_stage_lean_run` + `_run_engine_live` + `_build_manifest` in the regen script

Now that captures exist, fill in the three `NotImplementedError` stubs in `regenerate_cross_engine_study.py` and prove the wiring on the smallest cell (`SPY_W6mo_…`).

**Files:**
- Modify: `PythonDataService/scripts/regenerate_cross_engine_study.py`
- Modify: `PythonDataService/tests/scripts/test_regenerate_cross_engine_study.py` (add a smoke-level integration test)

- [ ] **Step 1: Read the LEAN sidecar runner entrypoint**

```
cat PythonDataService/app/lean_sidecar/runner.py
cat PythonDataService/app/lean_sidecar/workspace.py | head -80
```

Identify the function that (a) builds a workspace from a trusted-sample source string + parameters, (b) launches LEAN via podman, (c) returns a result handle pointing at the output directory.

- [ ] **Step 2: Implement `_stage_lean_run`**

Replace the `NotImplementedError` stub with a function that:

1. Builds a workspace, copying `EMA_CROSSOVER_SOURCE` into `<workspace>/project/main.py`.
2. Mounts `_lean_data_capture/<TICKER>/` as the workspace's `data/` folder (read-only).
3. Sets parameters: `symbol`, `start_date`, `end_date`, `bar_minutes=15`, `session=regular`, `adjustment=raw`, `starting_cash=100000`.
4. Invokes the LEAN sidecar runner.
5. After the run, reads `state.csv` and `observations.csv` from the workspace's `output/` (or LEAN ObjectStore path — confirm with `workspace.py`) and copies them into `output_dir/lean/`.
6. Parses LEAN's `result.json` orders into the `orders.json` shape this study expects (use `normalized_parser` and serialize the result).

- [ ] **Step 3: Implement `_run_engine_live`**

Replace the stub with a function that calls `cross_runner.run_engine_lab_on_workspace`. The strategy is `SpyEmaCrossover`. The capture serves as the workspace `data/` folder. The strategy's `output_dir` (added in Task 5) is `output_dir/engine/`.

Return the normalized order events from `result.order_events` (or whatever the cross-runner result exposes).

- [ ] **Step 4: Implement `_build_manifest`**

```python
def _build_manifest(cell: Cell, staging: Path) -> CellManifest:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
    from app.lean_sidecar.config import LEAN_IMAGE_DIGEST  # whatever the pinned digest constant is

    capture_manifest_path = (
        FIXTURE_ROOT / "_lean_data_capture" / cell.ticker / "manifest.json"
    )
    capture_manifest = json.loads(capture_manifest_path.read_text(encoding="utf-8"))

    git_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT
    ).decode().strip()
    git_user = subprocess.check_output(
        ["git", "config", "user.name"], cwd=REPO_ROOT
    ).decode().strip()
    script_sha = sha256_of_file(Path(__file__))

    return CellManifest(
        schema_version=1,
        cell_id=cell.cell_id,
        ticker=cell.ticker,
        window=WindowSpec(
            label=cell.window_label.value,
            start_date=cell.start_date.isoformat(),
            end_date=cell.end_date.isoformat(),
            session="regular",
            trading_days_expected=_expected_trading_days(cell),
        ),
        strategy=StrategySpec(
            trusted_sample="ema_crossover",
            trusted_sample_source_sha256=sha256_of_text(EMA_CROSSOVER_SOURCE),
            parameters_constants={
                "FAST_PERIOD": 5, "SLOW_PERIOD": 10, "RSI_PERIOD": 14,
                "EXIT_BARS": 5, "GAP_MIN": 0.20, "RSI_LO": 50, "RSI_HI": 70,
            },
            runtime_parameters={
                "bar_minutes": 15, "adjustment": "raw", "starting_cash": 100000,
            },
        ),
        data=DataSpec(
            lean_data_capture_ref=f"_lean_data_capture/{cell.ticker}",
            data_contract_hash=capture_manifest["data_contract_hash"],
        ),
        broker=BrokerSpec(
            brokerage_model="InteractiveBrokersBrokerage",
            account_type="Margin",
            fill_model="ImmediateFillModel",
            fee_model="IbkrEquityCommissionModel",
        ),
        lean_runtime=LeanRuntimeSpec(
            container_image_digest=LEAN_IMAGE_DIGEST,
        ),
        artifacts=PinnedArtifactHashes(
            orders_sha256=sha256_of_file(staging / "lean" / "orders.json"),
            state_sha256=sha256_of_file(staging / "lean" / "state.csv"),
            observations_sha256=sha256_of_file(staging / "lean" / "observations.csv"),
            reconciliation_sha256=sha256_of_file(staging / "reconciliation_pinned.json"),
        ),
        state_csv_schema=StateCsvSchema(
            columns=["ts_ms_utc", "close", "ema_fast", "ema_slow",
                     "rsi", "cross_state", "signal"],
            column_types={
                "ts_ms_utc": "int64",
                "close": "decimal_string",
                "ema_fast": "decimal_string",
                "ema_slow": "decimal_string",
                "rsi": "decimal_string",
                "cross_state": "string_enum:above|below|equal",
                "signal": "string_enum:HOLD|ENTER|EXIT",
            },
        ),
        timezone="America/New_York",
        timestamp_convention="int64_ms_utc",
        fixture_git_commit=git_head,
        python_data_service_commit=git_head,
        generator_script_sha256=script_sha,
        captured_by=git_user,
        captured_at_ms_utc=int(datetime.now(timezone.utc).timestamp() * 1000),
    )


def _expected_trading_days(cell: Cell) -> int:
    # Approximate; the regenerator can compute the real count from the
    # data folder if higher precision is wanted.
    from datetime import timedelta
    days = (cell.end_date - cell.start_date).days
    return int(days * 252 / 365)
```

- [ ] **Step 5: Smoke-run the smallest cell first**

```
podman exec polygon-data-service python scripts/regenerate_cross_engine_study.py \
    --cell SPY_W6mo_2025-11-03_to_2026-04-30
```

Expected: ~5–10 min of LEAN runtime, ~30s of Engine runtime, all three gates pass, cell directory written under `tests/fixtures/golden/cross-engine-studies/cells/SPY_W6mo_…/`.

**If any gate fails:** stop and investigate before generating other cells. Surface the root cause (likely one of the "implementation verification" items from the spec § "Implementation verification"). Do not loosen tolerances; do not regenerate the failing cell to mask the failure. Fix the root cause, then retry.

- [ ] **Step 6: Commit the smoke cell once it passes**

```bash
git add PythonDataService/scripts/regenerate_cross_engine_study.py \
        PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/SPY_W6mo_2025-11-03_to_2026-04-30/
git commit -m "feat(parity-matrix): wire LEAN+Engine staging in regen script + first cell"
```

---

## Task 11: Regenerate the remaining 11 cells and verify the full matrix

- [ ] **Step 1: Regenerate the remaining 3 SPY cells (well, 2 more — W12mo and W24mo)**

```
podman exec polygon-data-service python scripts/regenerate_cross_engine_study.py \
    --ticker SPY
```

This re-runs all 3 SPY cells. The W6mo will be re-generated; that's expected and produces no diff if nothing changed. If a re-run produces a different hash, investigate before continuing.

- [ ] **Step 2: Regenerate all four tickers**

```
podman exec polygon-data-service python scripts/regenerate_cross_engine_study.py --all
```

Expected runtime: ~1–3 hours total (~10–15 min per cell × 12). Background it and monitor `.failed/` for any failures.

- [ ] **Step 3: Smoke pytest — the four W6mo cells**

```
podman exec polygon-data-service python -m pytest \
    tests/research/parity/test_cross_engine_study.py -v -m cross_engine_smoke
```
Expected: 4 passed.

- [ ] **Step 4: Full pytest — all 12 cells**

```
podman exec polygon-data-service python -m pytest \
    tests/research/parity/test_cross_engine_study.py -v
```
Expected: 12 passed.

- [ ] **Step 5: Project-scope tests + lint (pre-push hygiene)**

```
ruff check PythonDataService/app/ PythonDataService/tests/
podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"
```
Expected: clean lint; tests pass (existing failures, if any, are documented in PR description per `.claude/rules/testing.md` § "Pre-push test-suite hygiene").

- [ ] **Step 6: Commit the remaining 11 cells**

```bash
git add PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/
git commit -m "fixture(parity-matrix): pin remaining 11 cells (3 tickers × 3 windows + SPY × 2)"
```

- [ ] **Step 7: Push and open PR**

```bash
git push -u origin feat/cross-engine-golden-matrix
gh pr create --title "feat(parity-matrix): 12-cell cross-engine golden fixtures" --body "$(cat <<'EOF'
## Summary
- New `app/lean_sidecar/parity_matrix/` package: 12-cell matrix definition, manifest schema, observations + state parity comparators, three-gate cell runner.
- `SpyEmaCrossover` strategy now emits `observations.csv` + `state.csv` at full Decimal precision, matching the LEAN trusted sample schema.
- New CLI `scripts/regenerate_cross_engine_study.py` regenerates pinned LEAN outputs per cell; refuses to write a cell unless all three gates pass.
- New parameterized pytest at `tests/research/parity/test_cross_engine_study.py` — `cross_engine_smoke` on four W6mo cells, `slow` on eight W12mo+W24mo cells.
- 12 pinned cells generated under `tests/fixtures/golden/cross-engine-studies/cells/`; one shared 24mo minute capture per ticker under `_lean_data_capture/`.

## Test plan
- [x] `pytest tests/lean_sidecar/parity_matrix/` — unit tests (matrix, manifest, observations, state, cell_runner)
- [x] `pytest tests/scripts/test_regenerate_cross_engine_study.py` — regen CLI tests
- [x] `pytest -m cross_engine_smoke` — 4 W6mo cells live
- [x] `pytest tests/research/parity/test_cross_engine_study.py` — full 12 cells live
- [x] `ruff check PythonDataService/app/ PythonDataService/tests/` — clean

Design: `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review (already applied)

- **Spec coverage** — every spec requirement maps to a task:
  - Matrix definition (4×3=12) → Task 1
  - Manifest schema → Task 2
  - Gate 1 / Gate 2 / Gate 3 → Tasks 3, 4, 6
  - Three independent LEAN runs (b1) → Tasks 7, 10, 11
  - Shared `_lean_data_capture/` per ticker → Task 9
  - Regeneration script + strict-trigger policy → Tasks 7, 10
  - "Write `reconciliation_pinned.json` only if both gates pass" — `_write_cell_atomically` runs only on success path → Task 7
  - Engine emitter alignment → Task 5
  - Smoke + slow markers → Task 8
  - Pre-push test hygiene → Task 11

- **Type consistency** — `Cell`, `WindowLabel`, `CellManifest`, `ObservationsParityResult`, `StateParityResult`, `CellRunReport` referenced consistently across all tasks.

- **Placeholder scan** — three intentional `NotImplementedError` stubs in Task 7 are filled in Task 10; flagged in-doc. No `TBD` / `TODO` / "fill in later" elsewhere.

- **Known integration unknowns flagged in-task, not skipped**:
  - Task 5: Engine Lab strategy constants must match LEAN trusted sample — surface discrepancy, do not silently align.
  - Task 6: cross-reconciler entrypoint name to be confirmed by grep before import.
  - Task 9: data-lake `ensure_data` entrypoint to be located by grep.
  - Task 10: LEAN ObjectStore location of `state.csv` / `observations.csv` to be confirmed from `workspace.py`.
