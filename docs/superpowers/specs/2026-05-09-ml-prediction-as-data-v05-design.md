# ML predictions as data — v0.5 plumbing design

**Status:** approved (brainstorm), pending implementation plan
**Date:** 2026-05-09
**Author:** Claude (with Tim)
**Predecessors:** none. First step on the QuantConnect-style precomputed-ML-predictions path.

## Goal

Land the architectural plumbing that lets a `StrategySpec` consume precomputed predictions as data, **without** introducing sklearn, training, walk-forward retraining, or any model nondeterminism. The first PR ships the artifact format, the spec extension, the engine integration, the run-ledger extension, and a deterministic-rule "fake model" generator that proves the pipe end to end.

The deliberate move is plumbing-before-model. By making the v0.5 generator a pure function of existing features (e.g. `prediction = rsi_14 / 100 - 0.5`), every artifact is bit-reproducible and every backtest hash is stable, so the architectural seams can be validated under the repo's strict-equivalence contract before the contract is loosened for sklearn output in v1.

## Non-goals (deferred)

| Deferred to | What |
|---|---|
| v1 | sklearn / any real model training; `ModelSpec` schema; FastAPI generation endpoint; behavioral equivalence contract for sklearn output |
| v2 | walk-forward retraining (multiple chunks per set); `prediction_set_hashes: dict[str, str]` for multi-set specs |
| v3+ | Angular ML Research tab; feature-importance / drift dashboards; live/paper inference path; permissions and artifact GC |

The artifact format is forward-compatible with walk-forward (chunk array). The ledger field name is forward-compatible with multi-set specs (rename `prediction_set_hash` → `prediction_set_hashes` at schema 1.2).

## Decisions

| # | Question | Decision | Rationale |
|---|---|---|---|
| Q1 | Hypothesis-first or platform-first | **Hypothesis-first.** v0.5 is plumbing only; first real ML hypothesis lands on top of proven plumbing. | Avoids 8–12 weeks of platform with no validated use case. Defers loosening the math contract until pressure exists. |
| Q2 | Where do ML pipelines live | **`PythonDataService/app/research/ml/`** for artifact-producing pipelines. **`PythonDataService/app/ml/`** stays as reusable toolbox (currently `protocols.py`, `preprocessing/stationarity.py`). | Mirrors `app/research/{runs,baselines,walk_forward}` — research that produces artifacts is under `research/`. Toolbox stays toolbox. |
| Q3 | Spec extension shape | **Top-level `predictions: list[PredictionRef]`** parallel to `indicators`, plus a new `PredictionComparison` condition kind. | Same structural pattern as the indicator block; spec authors validate prediction-id references at load time exactly as indicator ids are validated today (`_iter_indicator_refs`). |
| Q4 | Artifact storage | **`PythonDataService/artifacts/predictions/<prediction_set_id>/`** with `manifest.json` + `chunks/<trained_through_ms>.parquet`. `prediction_set_id` is validated as path-safe (regex `^[a-zA-Z0-9_\-.]+$`, no slashes, no `..`). | `PythonDataService/artifacts/` is the gitignored research-artifact root used by `app/research/runs/storage.py` (today it lazily creates `artifacts/runs/`; on this dev machine `artifacts/live_runs/` and `artifacts/fixture-validation/` are present). Predictions get a peer subdirectory. Predictions are independent of any single run, so they live outside `runs/`. Path-safe id closes the obvious traversal foot-gun. |
| Q5 | Hash semantics | **Reuse `app/research/runs/hashing.py::hash_payload`** (sha256 over `json.dumps(payload, sort_keys=True, separators=(",",":"), ensure_ascii=False)`, returning bare 64-char hex). `rows_hash` = `hash_payload(rows_list)` where rows are sorted by `timestamp_ms` and `prediction` is left as a `float` (Python's default JSON float repr is the shortest round-trippable, equivalent to `repr(float)`). `prediction_set_hash` = `hash_payload(manifest_dict)` with `rows_hash` populated per chunk and the `prediction_set_hash` field itself excluded. **No `parquet_file_hash` in the manifest.** A sidecar `chunks/<trained_through_ms>.parquet.sha256` may be written next to the parquet for corruption detection; it is recomputed on read but never enters the manifest or the ledger. | Hashing parquet bytes lets pyarrow version / metadata / compression drift change identity for semantically-identical content. Hashing canonical row records pins the **content** the engine sees. Reusing `hash_payload` keeps the format (bare 64-char hex) consistent with `data_snapshot_id` and `strategy_spec_hash`. Keeping `parquet_file_hash` out of the manifest prevents it from quietly contaminating `prediction_set_hash`. |
| Q6 | Run-ledger integration | **New top-level field `prediction_set_hash: str \| None`** on `RunLedger`, schema bump `1.0 → 1.1`. **Not** folded into `strategy_spec_hash`. | Spec hash stays a function of what the user authored. Prediction *content* is parallel to market data (`data_snapshot_id`). Naming reserves a clean upgrade path to `prediction_set_hashes: dict[str, str]` at 1.2 if multi-set specs ever land. |
| Q7 | Generator surface | **CLI script** at `app/research/ml/generate_prediction_set.py`, no FastAPI endpoint in v0.5. | Mirrors `app/engine/tests/fixtures/golden/<n>/regenerate.py`. Generation is out-of-band from runtime. UI question deferred to v1. |
| Q8 | Engine integration | **Bar-clock strict coverage, hard-fail at session start.** Engine builds the expected timestamp set by replaying the same data source through the same `TradeBarConsolidator` the run will use, harvesting the emitted `TradeBar.end_time` values. The loader then asserts every emitted bar has a prediction. Per-bar lookup at evaluation time is `O(1)` dict access. **`StrategySpec` does not own `start`/`end`** — the run request/router does — so the coverage check necessarily lives at the run-pipeline boundary, not in spec validation. | Markets are not a wall-clock grid. Predictions are required for every bar the engine actually evaluates — not for nights, weekends, holidays, missing-data days, or trailing partials the consolidator doesn't flush. Walking a wall-clock grid would over-require and produce false-fail loads. Right model: market data defines the clock; predictions decorate it. |
| Q9 | Leakage invariant | **For every chunk:** `chunk.start_ms > trained_through_ms`, AND every row's `timestamp_ms ∈ [chunk.start_ms, chunk.end_ms]`. Engine refuses to load on violation. | The original wording (`min(timestamps) <= trained_through_ms` as fail) was logically equivalent but read as "fail when prediction is *after* training" — confusing. Restated explicitly: predictions must lie strictly after the training window. |
| Q10 | Warmup policy | **`warmup_policy: "neutral_zero_until_feature_ready"`** declared in the manifest. The v0.5 generator emits `prediction = 0.0` for bars where the underlying feature (e.g. RSI's first 13 bars) is not yet warmed. | Strict coverage requires a prediction for every emitted bar, including bars before the feature is ready. Encoding the policy in the manifest documents the intent and lets future consumers gate semantically (e.g. "treat zero predictions as no-signal"). Future policies (`forward_fill`, `nan_drop`) require explicit migration. |
| Q11 | Condition coverage | **Ship `PredictionComparison` only.** Defer `PredictionBetween`. | YAGNI. The `IndicatorBetween` precedent makes Between a 5-minute add when a real spec demands it. |
| Q12 | Symbol scoping | **`prediction_set.symbol` must equal `spec.symbols[0]`.** | Phase-1 single-symbol invariant carries through. Engine validates at load. |
| Q13 | Multi-set in v0.5 | **At most one unique `prediction_set_id` per spec.** The schema admits multiple `PredictionRef` entries (so future field-named handles like `prediction` and `confidence` from the same set are allowed) but a `model_validator` rejects more than one distinct `prediction_set_id`. | The ledger field `prediction_set_hash` is singular at v1.1; rejecting multi-set specs at schema-load avoids a "ledger can't represent the run" silent failure. Multi-set is a v1.2 schema change (`prediction_set_hashes: dict[str, str]`). |
| Q14 | Primitive ↔ engine wiring | **`EvalContext` gains `predictions: dict[str, Decimal]`.** `SpecAlgorithm` converts each emitted `bar.end_time` to `int64 ms UTC`, looks up the row in the loaded `PredictionSet`, and populates `ctx.predictions[ref.id] = Decimal(str(value))` for every declared `PredictionRef` before evaluating any block. `PredictionComparisonPrimitive.evaluate` reads from `ctx.predictions[node.prediction]`. | Mirrors how `IndicatorComparisonPrimitive` reads from `ctx.indicators`. Primitive is decoupled from engine internals (no `bar.timestamp_ms` reach-around — `TradeBar` carries `end_time: datetime`, not an int). `Decimal` matches the type used by every other comparison primitive (see `evaluate_operand` returning `Decimal`). |

## Architecture

Four layers touched, in order of dependency:

1. **Schema / artifact format** — `app/research/ml/` (new), `app/engine/strategy/spec/schema.py` (extended).
2. **Loader** — `app/research/ml/loader.py` (new): manifest validation, hash verification, leakage-invariant checks, builds the `{ts_ms: {field: value}}` index.
3. **Run-pipeline glue** — wherever the spec runner today resolves the data source / consolidator (e.g. `app/routers/spec_strategy.py` or its delegate). Runs the bar-clock coverage check, populates the algorithm with the loaded `PredictionSet`.
4. **Engine integration** — `app/engine/strategy/spec/primitives.py` (new `EvalContext.predictions` field, new `PredictionComparisonPrimitive`), `app/engine/strategy/spec/evaluator.py` (`SpecAlgorithm` populates `ctx.predictions` per bar), `app/research/runs/ledger.py` (schema `1.0 → 1.1`, new `prediction_set_hash` field).

Frontend / .NET / GraphQL untouched in v0.5.

### Directory layout

```
PythonDataService/
├── app/
│   ├── ml/                                   # toolbox (existing) — unchanged in v0.5
│   │   ├── protocols.py
│   │   └── preprocessing/stationarity.py
│   └── research/
│       └── ml/                               # NEW — artifact-producing pipelines
│           ├── __init__.py
│           ├── artifact.py                   # PredictionSet model, manifest I/O, hashing, validation
│           ├── loader.py                     # load + validate artifact, build {timestamp_ms: prediction} index
│           ├── generators/
│           │   ├── __init__.py
│           │   └── deterministic_rule.py     # v0.5 "fake model": rule → artifact
│           └── generate_prediction_set.py    # CLI entrypoint
└── artifacts/
    └── predictions/                          # NEW
        └── <prediction_set_id>/
            ├── manifest.json
            └── chunks/
                └── <trained_through_ms>.parquet
```

### Artifact format

**`manifest.json`** (canonical JSON, `extra='forbid'` Pydantic):

```json
{
  "schema_version": "1.0",
  "prediction_set_id": "pred_spy_rsi_rule_v001",
  "symbol": "SPY",
  "resolution_minutes": 15,
  "field_names": ["prediction"],
  "warmup_policy": "neutral_zero_until_feature_ready",
  "generator": {
    "kind": "deterministic_rule",
    "rule_id": "rsi_14_centered",
    "rule_version": "1.0"
  },
  "chunks": [
    {
      "trained_through_ms": 1714521600000,
      "start_ms": 1714608000000,
      "end_ms": 1717199999000,
      "row_count": 173,
      "rows_hash": "<64-char hex>"
    }
  ],
  "prediction_set_hash": "<64-char hex>"
}
```

`warmup_policy` is the only declared policy in v0.5. Future values (`forward_fill`, `nan_drop`, `caller_supplied`) are explicit schema additions, not free-form strings.

**Chunk parquet schema** (per row):

| column | type | notes |
|---|---|---|
| `timestamp_ms` | `int64` | Bar-close, UTC ms (per `numerical-rigor.md` canonical format). Strictly increasing within and across chunks. |
| `symbol` | `string` | Equals manifest `symbol`; column exists for forward-compat with multi-symbol v2+. |
| `prediction` | `float64` | Single scalar for v0.5. |

Additional float columns are admitted by the format (`field_names` lists them) but v0.5 consumers reference `field: "prediction"` only.

**Hash policy** (reuses `app/research/runs/hashing.py::hash_payload`):

- `rows_hash` for a chunk = `hash_payload(rows_list)` where `rows_list = [{"timestamp_ms": int, "symbol": str, "prediction": float}, ...]` sorted ascending by `timestamp_ms`. `hash_payload` is `sha256(json.dumps(payload, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")).hexdigest()` — bare 64-char hex. Python's default JSON float serialization is the shortest round-trippable representation (equivalent to `repr(float)`), so float identity round-trips exactly within CPython.
- `prediction_set_hash` = `hash_payload(manifest_dict)` with `rows_hash` populated per chunk, the `prediction_set_hash` field removed before hashing (chicken-and-egg).
- **Parquet file bytes are not hashed into ledger identity and not present in the manifest.** An optional `chunks/<trained_through_ms>.parquet.sha256` sidecar may be written for corruption detection on read; failing this check is a "regenerate the artifact" error, not a "regenerate the world" identity drift.

This way: regenerating an artifact with a different pyarrow version produces identical `rows_hash` / `prediction_set_hash` (and identical ledger identity for any run that consumed it) as long as the row content is identical.

### `StrategySpec` extension

Add to `app/engine/strategy/spec/schema.py`:

```python
class PredictionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str                                    # spec-local handle, e.g. "rsi_rule_pred"
    prediction_set_id: str                     # artifact directory name
    field: str = "prediction"                  # which column in the artifact rows


class PredictionComparison(_ConditionBase):
    kind: Literal["PredictionComparison"]
    prediction: str                            # PredictionRef.id
    op: ComparisonOp
    value: float


# Append to Condition union:
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
    | PredictionComparison,                    # NEW
    Field(discriminator="kind"),
]


# Add to StrategySpec:
class StrategySpec(BaseModel):
    # ... existing fields ...
    predictions: list[PredictionRef] = Field(default_factory=list)
    # ...
```

Validation extends `_check_phase1_boundaries`:

- `PredictionRef.id` uniqueness within `predictions`.
- Every `PredictionComparison.prediction` references a declared `PredictionRef.id` (parallel to the indicator-id walk).
- `_iter_indicator_refs` gets a sibling `_iter_prediction_refs` that walks the same logic tree.
- **At most one unique `prediction_set_id` across all `PredictionRef` entries.** Multiple `PredictionRef` rows are allowed (e.g. binding `prediction` and `confidence` field handles to the same set) but they must point at one set. v1.2 (`prediction_set_hashes: dict[str, str]`) lifts this restriction.

### `RunLedger` extension

`app/research/runs/ledger.py`:

- `schema_version: Literal["1.0", "1.1"] = "1.1"` — write-1.1, accept-1.0-or-1.1.
- New optional field: `prediction_set_hash: str | None = None`.
- Populated by the run pipeline when the spec declares any `PredictionRef`. Ledgers for prediction-free specs leave it `None`.
- Field is hashed into nothing; it stands beside `data_snapshot_id` as a parallel identity input. Two ledgers agreeing on `(strategy_spec_hash, data_snapshot_id, prediction_set_hash, engine_version, ...)` must produce the same `result_hash`.

**Migration:** `artifacts/runs/` is gitignored. The accept-1.0-or-1.1 union lets pre-existing 1.0 ledgers continue to load (with `prediction_set_hash = None`); newly-written ledgers carry 1.1. No regeneration required. When `prediction_set_hashes: dict[str, str]` lands at v1.2, the same union widens to `Literal["1.0", "1.1", "1.2"]` and the field migrates by reading 1.1's singular value into a one-key dict.

The acceptance gate that compares result hashes for replay testing extends to include `prediction_set_hash` in its identity tuple.

### Engine integration

A new `app/research/ml/loader.py`:

```python
class PredictionSet:
    manifest: PredictionSetManifest             # parsed manifest.json
    index: dict[int, dict[str, float]]          # timestamp_ms (UTC ms) -> {field: value}

    @classmethod
    def load(cls, root: Path, prediction_set_id: str) -> PredictionSet: ...
```

**Stage 1 — `PredictionSet.load(...)`** (intrinsic validation, no engine state):

1. Read `manifest.json`, validate against Pydantic model.
2. For each chunk file: read parquet, recompute `rows_hash`, assert match.
3. Recompute `prediction_set_hash` from manifest + chunk rows; assert match.
4. Assert `chunk.start_ms > chunk.trained_through_ms` (leakage invariant).
5. Assert every row in chunk has `chunk.start_ms <= timestamp_ms <= chunk.end_ms`.
6. Assert timestamps strictly increasing (no duplicates within or across chunks).
7. Build the `{timestamp_ms: {field: value}}` index.

**Stage 2 — pairing with a `StrategySpec`** (still no engine state):

8. Assert `manifest.symbol == spec.symbols[0]`.
9. Assert `manifest.resolution_minutes == spec.resolution.period_minutes`.
10. Assert at most one `prediction_set_id` referenced by the spec equals `manifest.prediction_set_id`.

**Stage 3 — bar-clock coverage check at run-pipeline boundary** (where the run request supplies start/end and the data source is known):

The run pipeline replays the run's data source through the same `TradeBarConsolidator` configuration the engine will use, and harvests the emitted bars' `end_time` values. `StrategySpec` does not own start/end — the run request does — so this check necessarily lives in the run-pipeline glue, not in `PredictionSet.load`. Sketch:

```python
def assert_bar_clock_coverage(
    pset: PredictionSet,
    bar_stream: Iterable[TradeBar],   # same source + consolidator the engine will use
) -> None:
    expected_ms = {to_int_ms_utc(bar.end_time) for bar in bar_stream}
    have_ms = pset.index.keys()
    missing = expected_ms - have_ms
    extra = have_ms - expected_ms     # not fatal but logged
    if missing:
        raise PredictionCoverageError(
            f"prediction_set {pset.manifest.prediction_set_id} missing predictions for "
            f"{len(missing)} emitted bars; first 5: {sorted(missing)[:5]}"
        )
```

Bars the engine will see are the only bars that need predictions — nights, weekends, holidays, missing-data days, and trailing partials the consolidator never emits are *correctly* absent from both sets. `extra` (predictions for bars the engine won't see) is logged but accepted; predictions are allowed to be a superset of the bar stream as long as no required bar is missing.

**Stage 4 — per-bar evaluation.** `EvalContext` gains `predictions: dict[str, Decimal] = field(default_factory=dict)`. `SpecAlgorithm` populates it before each `evaluate` / `observe_bar` cycle:

```python
# SpecAlgorithm.on_consolidated_bar (sketch)
ts_ms = to_int_ms_utc(bar.end_time)
ctx_predictions: dict[str, Decimal] = {}
for ref in self._spec.predictions:
    raw = self._prediction_set.index[ts_ms][ref.field]   # KeyError == bar-clock bug
    ctx_predictions[ref.id] = Decimal(str(raw))
ctx = EvalContext(..., predictions=ctx_predictions)
```

`PredictionComparisonPrimitive.evaluate(ctx)` reads `ctx.predictions[node.prediction]`, compares against `Decimal(str(node.value))`, returns the bool. Mirrors `IndicatorComparisonPrimitive`'s shape exactly. Any `KeyError` in the `index[ts_ms]` lookup is a load-time bug (Stage 3 should have caught it), not a runtime branch.

### v0.5 deterministic-rule generator

`app/research/ml/generators/deterministic_rule.py`:

- Inputs: symbol, `[start_ms, end_ms]`, resolution_minutes, rule id (e.g. `"rsi_14_centered"`), rule params.
- Pulls bar data via existing engine paths (no new data sources) and runs them through the same `TradeBarConsolidator` configuration the run will use, so the emitted bars match what the engine will see.
- Computes the rule (e.g. `prediction = rsi_14(close) / 100.0 - 0.5`).
- For each emitted bar, emits a row. **Bars where the underlying feature is not yet ready (e.g. RSI's first 13 bars) emit `prediction = 0.0`** — this is the `neutral_zero_until_feature_ready` warmup policy the manifest declares. There are no missing rows; strict bar-clock coverage holds from the first emitted bar.
- Sets `trained_through_ms` to `start_ms - 1` (the rule has no training; this is the conventional "trained through one ms before the first prediction" placeholder that satisfies the leakage invariant).
- Writes manifest + single chunk file.

CLI:

```
python -m app.research.ml.generate_prediction_set \
    --rule rsi_14_centered \
    --symbol SPY \
    --start 2024-05-01 --end 2024-05-31 \
    --resolution-minutes 15 \
    --out pred_spy_rsi_rule_v001
```

The CLI is the only generation surface in v0.5. No FastAPI endpoint, no Angular trigger.

## Test list

Lives under `PythonDataService/tests/research/ml/`.

**Schema tests** (`test_spec_predictions.py`):
- `PredictionRef` block loads, round-trips, rejects extras.
- `PredictionComparison` condition loads, round-trips.
- Spec with undeclared `PredictionComparison.prediction` id → load error (parallel to existing indicator-id check).
- Duplicate `PredictionRef.id` → load error.
- Spec referencing two distinct `prediction_set_id` values → load error (Q13 multi-set rejection).
- Path-unsafe `prediction_set_id` (contains `/`, `\`, `..`, leading dot) → load error (Q4).

**Artifact tests** (`test_artifact.py`):
- Manifest round-trip via `extra='forbid'` model.
- `rows_hash` is stable across pandas/pyarrow version pins (synthetic test using fixed-seed row content; assert exact 64-char hex hash).
- `prediction_set_hash` excludes the `prediction_set_hash` field from its own input (assert: insert any value into the field before hashing → unchanged result).
- Tampering a chunk's row content → `rows_hash` mismatch → load error.
- Tampering manifest content (e.g. flip a timestamp) → `prediction_set_hash` mismatch → load error.
- Adding a `parquet_file_hash` field to the manifest → schema error (the field lives in a sidecar, not the manifest).

**Loader tests** (`test_loader.py`) — `PredictionSet.load(...)`:
- Chunk with `start_ms <= trained_through_ms` → load error with descriptive message (leakage invariant).
- Chunk with a row outside `[start_ms, end_ms]` → load error.
- Duplicate timestamp within a chunk → load error.
- Duplicate timestamp across chunks → load error.
- Resolution mismatch (manifest 15-min, spec 5-min) → load error.
- Symbol mismatch (manifest `SPY`, spec `QQQ`) → load error.

**Bar-clock coverage tests** (`test_coverage.py`) — `assert_bar_clock_coverage(...)`:
- Synthetic bar stream with one missing bar's matching prediction → coverage error naming the missing timestamp.
- Predictions covering bars the engine won't emit (e.g. wall-clock midnight rows in a market-hours stream) → accepted, logged.
- Real-data smoke: replay a known SPY 15-min day through the consolidator, generate predictions for exactly the emitted bars, coverage check passes.
- Real-data smoke: same setup but predictions for an artificial wall-clock 24×7 grid → coverage check passes (extras allowed); replace one true bar's prediction with a wrong timestamp → coverage check fails.

**Engine-integration tests** (`test_eval_context_predictions.py`):
- `EvalContext.predictions` populated before `evaluate` / `observe_bar`.
- `PredictionComparisonPrimitive` reads from `ctx.predictions`, not from a global / bar field.
- Decimal conversion: artifact `float64 = 0.123456789012345` round-trips into `Decimal(str(value))` exactly; comparisons against `Decimal(str(node.value))` produce the expected bool.

**Warmup tests** (`test_warmup.py`):
- Generator with `rsi_14_centered` rule on a 30-bar window emits 30 rows: first 13 are `0.0`, remainder are non-zero (assert via fixture).
- Manifest declares `warmup_policy: "neutral_zero_until_feature_ready"`.
- Other policy values reject at manifest load (closed enum).

**End-to-end / replay tests** (`test_e2e_replay.py`):
- Run the deterministic-rule generator on a fixed `[start, end]` with a known SPY day. Assert resulting `prediction_set_hash` equals a committed-fixture string.
- Run a backtest of a `PredictionComparison`-using spec against the artifact. Assert `result_hash` equals a committed-fixture string.
- Regenerate the artifact (delete + rerun CLI). Assert `prediction_set_hash` unchanged.
- Rerun the backtest. Assert `result_hash` unchanged.
- Run a prediction-free spec (legacy path). Assert `RunLedger.prediction_set_hash is None` and `result_hash` matches an existing fixture (no regression on schema 1.0 → 1.1 migration).
- Across-version: pin a future pyarrow upgrade in CI; same fixture must produce same `prediction_set_hash`. (Tracked as a follow-up if pyarrow upgrades land mid-implementation.)

## Out of scope for v0.5

- Sklearn, any real model.
- `ModelSpec` schema (the JSON Tim sketched in the original proposal — `model_type`, `features`, `label`, etc.). The CLI takes a rule id, not a model spec, in v0.5.
- Walk-forward retraining (one chunk per set is enough; format already supports many).
- FastAPI endpoint to generate predictions.
- Angular UI to trigger generation or inspect predictions. Existing run-explorer UI already shows ledger fields, so `prediction_set_hash` will surface via the existing diff path with no UI work.
- GraphQL passthrough.
- Behavioral / statistical equivalence contract for nondeterministic models.
- Multi-set specs (`prediction_set_hashes: dict[str, str]`).
- `PredictionBetween`, `FreshPredictionCross`, prediction operands inside `Subtract`.

## Risks and open questions

- **Cross-platform row hashing under `repr(float)`.** Python's `repr(float)` is platform-stable for IEEE 754 doubles in CPython 3.11+, but a CI matrix that ever ran a non-CPython interpreter (PyPy, etc.) could break determinism. Risk is theoretical for this repo (CPython only). Documented but not mitigated.
- **Bar-clock coverage check requires replaying the data source twice** — once for the coverage check, once for the actual run. For long 1-minute backtests this is ~10⁶ bars × 2. Acceptable; if it becomes a hot path, the run pipeline can persist the harvested timestamp set and reuse it for the engine pass instead of replaying. Flagged for future profiling.
- **Run-pipeline glue is the new layer.** Where the bar-clock check sits — e.g. inside `app/routers/spec_strategy.py`, in a delegate, or in a new `app/research/ml/coverage.py` helper called by the router — is an implementation choice the writing-plans step should pin down by reading the current `spec-strategy` runner shape.
- **`trained_through_ms = start_ms - 1` for the rule generator** is a convention, not a semantic truth (the rule has no training). Documented in the generator's module docstring; revisit when the first real model lands and `trained_through_ms` becomes meaningful.
- **Float-equality in row comparisons.** `prediction` is `float64`; the canonical-JSON serialization uses `repr` which round-trips exactly. Tests assert exact equality of hashes, not numerical tolerance. Consistent with the deterministic-by-design v0.5 contract.
