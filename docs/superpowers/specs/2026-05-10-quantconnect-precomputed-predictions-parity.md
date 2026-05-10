# QuantConnect precomputed-predictions parity — Phase 1 design

**Status:** approved-in-conversation, plan pending
**Date:** 2026-05-10
**Author:** Claude (with Tim) — supersedes the Keras-tutorial framing in the prior handoff
**Predecessors:**
- `docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md` — v0.5 plumbing spec (merged in #207, #208, #209)
- `docs/superpowers/specs/2026-05-10-quantconnect-tutorial-parity-handoff.md` — first framing of the parity study; kept on disk because its Hard Problems / Open Questions sections are still useful for Phase 2

## Why this study (Tim's belief, refined)

> "First we need to exactly reproduce what QuantConnect does as they show in their tutorials. So we can have confidence that what we have built is actually matching another implementation."

The original framing picked QuantConnect's Keras "ML key concepts" tutorial, which trains a Sequential MLP and saves it to `qb.ObjectStore` for runtime model loading. Two problems with that as the *first* parity validation:

1. **Runtime path doesn't match v0.5.** v0.5's contract is "predictions as data, generated out-of-band, consumed by `StrategySpec` via `PredictionComparison`." QC's Keras tutorial loads the model in the algorithm and predicts at runtime. The prior handoff (lines 87–91) explicitly recommends deviating from QC's runtime path to fit v0.5's design — meaning the parity study would need to apologize for its own deviation on the first pass.
2. **TF/Keras non-determinism.** Even with all seeds and op-order flags set, training output drifts across TF builds. The prior handoff (lines 56–63) acknowledges Regime A (bit-exact) is "technically infeasible without running on QC's cloud and capturing their exact outputs as fixtures" — i.e., the only honest path is to capture QC outputs as golden fixtures anyway.

QuantConnect publishes a **separate** tutorial — "Precomputed ML Predictions" — that natively matches v0.5's design: predictions are computed offline, exported, and streamed into a backtest. Choosing this tutorial for Phase 1 collapses both problems:

- No runtime deviation (their pattern == ours).
- No training non-determinism in our code path (we import their predictions; we don't retrain).
- The "capture QC fixtures" plan from the prior handoff *is* the work, not a fallback.

Phase 2 — Keras model save/load parity — is deferred. It validates a v1 capability (declarative `ModelSpec` + model-as-data runtime), not v0.5 plumbing.

## Goal

Validate that learn-ai's v0.5 prediction-as-data pipeline reproduces QuantConnect's precomputed-predictions tutorial output exactly at three levels:

1. **Data parity** — given the same input window and symbol, the features and predictions we ingest from QC's export match QC's published values within explicit tolerance.
2. **Artifact parity** — the imported prediction-set artifact has a stable, pinned `prediction_set_hash`.
3. **Runtime parity** — a `StrategySpec` using `PredictionComparison` against the imported artifact loads, passes bar-clock coverage, runs to completion, and produces a stable `RunLedger.prediction_set_hash` and `result_hash`.

P&L parity against QC's backtest is **not** a Phase 1 claim. QC's fill model, commission schedule, and slippage configuration are not reproduced in v0.5. The first runtime claim is signal/prediction ingestion parity — that the same predictions arrive at the same bar timestamps and produce the same comparison-condition outcomes, not that the resulting trades have the same fills.

## Non-goals (deferred)

| Deferred to | What |
|---|---|
| Phase 2 | QC Keras tutorial parity (model save/load, runtime inference); `ModelSpec` schema; bit-exact-vs-behavioral disambiguation for trained-model output |
| Phase 3 | P&L-level parity (requires reproducing QC fills, commissions, slippage); multi-symbol custom-universe parity (requires `prediction_set_hashes: dict[str, str]` at ledger schema 1.2) |
| Out of project | Adding TensorFlow / Keras as runtime dependencies; running QC notebooks locally |

## The five Open Questions from the prior handoff — answered for Phase 1

| # | Open question | Phase 1 answer |
|---|---|---|
| Q1 | QuantConnect account / can we run their notebook? | **Required.** The fixture capture step (Workflow §B below) needs Tim to run QC's published precomputed-predictions tutorial in QC Cloud with pinned dates and export the artifacts listed in §B. Without this, Phase 1 has no ground truth and reduces to "we re-implemented their pattern; our outputs are the new baseline" — which is documented but not parity. **Gate before fixture-dependent tasks.** |
| Q2 | Bit-exact or behavioral parity? | **Strict-float for data and artifact parity.** `atol=1e-9, rtol=0` for feature and prediction comparisons — the prediction values come from a deterministic export, not from training, so strict float is achievable. Bit-exact for shapes/timestamps/symbol. Behavioral parity questions defer to Phase 2 (where actual training enters the picture). |
| Q3 | TensorFlow + Keras as heavy deps? | **Not in Phase 1.** No model is trained or loaded in our code path. The QC export is JSON; the importer reads JSON. Sklearn / TF stay out of `requirements-heavy.txt` until Phase 2 forces the issue. |
| Q4 | Data source — LEAN cache, Polygon, or QC? | **QC.** The export *is* our data source for predictions. The underlying price history needed for the bar-clock coverage check is whatever the run requests (LEAN minute → consolidator, or Polygon daily); the prediction artifact is decoupled from that. The Phase 1 fixture pins one calendar window, so the coverage check uses that exact window. |
| Q5 | Runtime path — precompute (a) or model-load (b)? | **(a) precompute.** QC's *precomputed-predictions* tutorial *is* path (a). No deviation from the reference. |
| Q6 | Calendar window | **Pinned in the fixture.** Tim picks the window when running the QC notebook (Workflow §B) and we lock those exact dates. No `datetime.now()` anywhere. |
| Q7 | Success delivery | **A new reference doc** at `docs/references/quantconnect-precomputed-predictions.md` describing the fixture and the parity claim, plus parity tests under `PythonDataService/tests/research/ml/test_quantconnect_fixture_parity.py`. |

## Decisions

| # | Question | Decision | Rationale |
|---|---|---|---|
| D1 | Where does the QC fixture importer live? | **`PythonDataService/app/research/ml/generators/quantconnect_fixture.py`** as a new generator module. Imports a JSON file and emits a manifest + parquet chunk to the same `artifacts/predictions/<id>/` layout as the deterministic-rule generator. | Mirrors `generators/deterministic_rule.py`. Keeps the artifact format the single source of truth for what an "imported" prediction set looks like. |
| D2 | Does the manifest schema bump from 1.0 to 1.1? | **No.** Stay at `schema_version: "1.0"`. Add `quantconnect_precomputed_fixture` as a second variant in a discriminated `GeneratorMeta` union. All QC-specific provenance (fixture export date, calendar window, sklearn / LEAN versions, raw QC dataset id, source URL) lives inside that variant — no manifest-level fields change. | The user's revision proposal suggested "schema 1.0 \| 1.1" for backward-compat. With a discriminated `GeneratorMeta` union, *both* old `deterministic_rule` manifests and new `quantconnect_precomputed_fixture` manifests validate at `schema_version: "1.0"`. A bump is unnecessary — and bumps that aren't necessary clutter the upgrade path the v0.5 spec reserves for v1.2 multi-set ledgers. |
| D3 | Where does the raw QC export live? | **`PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/`** — the canonical golden-fixture location per `.claude/rules/numerical-rigor.md` § Golden fixtures. Contents: `qc_export.json` (raw QC output), `qc_price_history.csv` (the SPY/symbol bars QC saw), `attribution.md` (pinned dates, versions, screenshot or log of the QC run, command to regenerate). | The artifact directory `artifacts/predictions/<id>/` is gitignored and regenerable; the QC export is the immutable ground truth and must be checked in. Standard repo pattern. |
| D4 | Symbol scope | **Single-symbol.** Use whichever symbol QC's tutorial actually publishes the precomputed predictions for. If the tutorial is multi-symbol, filter to one symbol at fixture-import time and document the filter. Multi-symbol parity defers to Phase 3. | v0.5's `prediction_set.symbol == spec.symbols[0]` invariant is single-symbol. Forcing multi-symbol now means rewriting the run pipeline before validating the existing one. |
| D5 | Timestamp policy at the import boundary | **Two-step conversion at the importer.** (1) Parse QC's date strings into a `pandas.Timestamp` localized to QC's documented tz, (2) convert to `int64 ms UTC` immediately. The raw QC date string is preserved in `attribution.md` for audit; it does **not** enter the manifest or any parquet row. Production rows are canonical `int64 ms UTC` only. | Fully matches `.claude/rules/numerical-rigor.md` § Timestamp rigor: "Two and only two conversion boundaries → External-API ingestion." The QC export *is* an external-API boundary. |
| D6 | Can the fixture importer be re-run? | **Yes, deterministically.** Given the same `qc_export.json`, the importer must produce a byte-identical manifest + parquet (modulo pyarrow metadata, which never enters `prediction_set_hash`). Tested by regenerating in CI and asserting a pinned hash. | Reproducibility is the point of the parity study. If the importer's output drifts, neither the artifact hash nor the runtime ledger hash is meaningful. |
| D7 | What does a Phase 1 success look like, concretely? | **Three pinned hashes** committed to a fixture file: (a) `qc_fixture_prediction_set_hash` — the hash of the importer's output for the committed `qc_export.json`; (b) `qc_fixture_run_ledger_prediction_set_hash` — the value the `RunLedger` writes when a `StrategySpec` consumes the imported artifact; (c) `qc_fixture_result_hash` — the run's overall result hash. Plus a parity test asserting QC's published prediction values equal the importer's output within `atol=1e-9`. | Concrete, automatable, and matches the pattern v0.5 already uses (`tests/research/ml/fixtures/e2e_known_hashes.json`). |
| D8 | What if QC's published values don't match within `atol=1e-9`? | **Stop and classify the divergence using the `reconcile-backtest` taxonomy.** Do not loosen the tolerance. The likely categories at this layer are (i) timestamp — QC dates parsed wrong, (ii) symbol filter — wrong symbol selected, (iii) precision — QC published predictions to fewer digits than 1e-9. Case (iii) is the only legitimate reason to loosen, and only after the value is documented in `docs/references/quantconnect-precomputed-predictions.md` with the QC source line that establishes their precision. | Same numerical-rigor rule the rest of the repo follows. |

## Architecture (extending v0.5, not redesigning it)

Three layers touched. **None** of v0.5's runtime invariants change.

1. **Schema** — `app/research/ml/artifact.py`: `GeneratorMeta` becomes a discriminated union (`deterministic_rule | quantconnect_precomputed_fixture`).
2. **Importer** — `app/research/ml/generators/quantconnect_fixture.py` (new): reads `qc_export.json`, applies the symbol filter, converts timestamps, computes `rows_hash` + `prediction_set_hash`, writes manifest + chunk.
3. **Tests + fixtures** — golden fixture skeleton at `tests/fixtures/golden/qc-precomputed-predictions/` plus parity tests at `tests/research/ml/test_quantconnect_fixture_*.py`.

Loader, runner, ledger, evaluator: **unchanged**. The whole point of this study is that v0.5's plumbing already works; we're proving it by feeding it an external reference's predictions.

### `GeneratorMeta` discriminated union

Current shape (one model):

```python
class GeneratorMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["deterministic_rule"]
    rule_id: str
    rule_version: str
```

New shape (discriminated union):

```python
class DeterministicRuleGenerator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["deterministic_rule"]
    rule_id: str
    rule_version: str


class QuantConnectPrecomputedFixtureGenerator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["quantconnect_precomputed_fixture"]
    qc_tutorial_url: str                     # e.g. https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions
    qc_exported_at_ms: int                   # int64 ms UTC — when the QC notebook ran (converted at fixture-capture time)
    qc_calendar_window_start_ms: int         # the pinned [start, end) window QC ran on
    qc_calendar_window_end_ms: int
    qc_symbol_filter: str                    # which symbol from the QC export was selected (e.g. "SPY")
    qc_dataset_id: str                       # QC dataset identifier the export is anchored to
    qc_versions: dict[str, str]              # {"sklearn": "1.5.0", "lean": "...", "numpy": "..."} — verbatim from QC env
    qc_daily_anchor_tz: str                  # IANA tz used to convert QC's date-only strings (typically "America/New_York")
    qc_daily_anchor_hhmm: str                # "HH:MM" wall-clock anchor for date-only -> int64 ms UTC (typically "16:00" market close)


GeneratorMeta = Annotated[
    DeterministicRuleGenerator | QuantConnectPrecomputedFixtureGenerator,
    Field(discriminator="kind"),
]


class PredictionSetManifest(BaseModel):
    # ... unchanged except that `generator: GeneratorMeta` now resolves to the union type alias above ...
```

Existing `deterministic_rule` manifests on disk continue to load. New QC manifests round-trip through the same code path. `extra='forbid'` still applies per variant.

### QC's documented export shape

The QuantConnect tutorial saves predictions as a **bare JSON list** (no envelope) of daily records:

```json
[
  {"date": "YYYY-MM-DD", "prediction_by_symbol": {"SPY": 0.0123, "QQQ": -0.011}},
  {"date": "YYYY-MM-DD", "prediction_by_symbol": {"SPY": -0.0045, "QQQ":  0.022}},
  ...
]
```

Per QC's tutorial code: `predictions_json = [{'date': date, 'prediction_by_symbol': group.set_index('symbol')['prediction'].to_dict()} for date, group in df.groupby('time')]` with `df['time'] = df['time'].dt.strftime('%Y-%m-%d')`. The file is saved as `'research-to-backtest-factors.json'` in QC's ObjectStore.

**Date is date-only** (`'%Y-%m-%d'`), not tz-aware. The importer pairs each date with a caller-supplied `qc_daily_anchor_tz` + `qc_daily_anchor_hhmm` (typically `("America/New_York", "16:00")` to anchor at NYSE close) and converts to `int64 ms UTC` at the ingestion boundary — a permitted conversion per `numerical-rigor.md` § Timestamp rigor.

**No provenance in QC's emitted file.** Tutorial URL, dataset id, sklearn/LEAN versions, calendar window, and exported-at timestamp must be captured separately at fixture-capture time (in `attribution.md`) and passed to the importer as explicit arguments.

### Importer behavior (`quantconnect_fixture.py`)

Inputs:

- `qc_export_path: Path` — pointer to `qc_export.json` (QC's bare list).
- `prediction_set_id: str` — artifact directory name; validated path-safe.
- `output_root: Path` — target artifact root (default `PythonDataService/artifacts/predictions/`).
- `symbol: str` — which symbol to extract from each daily record's `prediction_by_symbol` map.
- Provenance kwargs: `qc_tutorial_url`, `qc_exported_at_ms`, `qc_calendar_window_start_ms`, `qc_calendar_window_end_ms`, `qc_dataset_id`, `qc_versions`, `qc_daily_anchor_tz` (default `"America/New_York"`), `qc_daily_anchor_hhmm` (default `"16:00"`).

Steps (every step has a test in the plan):

1. Parse `qc_export.json` as a closed Pydantic `RootModel[list[QcPredictionRecord]]`. Each record is `extra='forbid'` with a non-empty `prediction_by_symbol` map.
2. For each daily record, look up `symbol` in `prediction_by_symbol`. **Days where the symbol is absent are silently skipped** — QC's universe filter may legitimately exclude a symbol on some days. The import only fails if the symbol is absent from **every** record.
3. Convert each record's `"YYYY-MM-DD"` date + `qc_daily_anchor_tz` + `qc_daily_anchor_hhmm` to `int64 ms UTC`. Reject any date string that doesn't strictly match `^\d{4}-\d{2}-\d{2}$` — a tz-aware ISO 8601 string would be ambiguous (which tz applies, the embedded one or the anchor?) and the caller must rewrite it before reaching the importer.
4. Validate strictly-increasing timestamps after sort. Duplicate dates fail.
5. Build the row list `[{"timestamp_ms": int, "symbol": str, "prediction": float}, ...]` and compute `rows_hash` via the existing `compute_rows_hash` helper.
6. Build the manifest dict with `generator.kind == "quantconnect_precomputed_fixture"` and the provenance fields populated, run `compute_prediction_set_hash`, write `manifest.json` and `chunks/<trained_through_ms>.parquet`.
7. Set `trained_through_ms = start_ms - 1` — same convention as the deterministic-rule generator. Documented inline.

### Validation workflow

This is the runbook for actually capturing the fixture and locking the parity claim. Some steps require Tim (QC Cloud access); the importer code can be developed before Tim runs the QC notebook, using a hand-crafted `qc_export.json` for unit tests.

**§A — Build the importer with synthetic fixtures (no QC account needed).**

1. Hand-craft a small `qc_export.json` representative of QC's documented JSON shape (per the QC docs URL in `attribution.md` of the fixture skeleton).
2. Implement the importer + tests against the synthetic JSON.
3. Land schema/importer/test code with synthetic-fixture coverage. **No** real-data parity claim yet.

**§B — Capture the real QC fixture (gated on QC Cloud access).**

In QuantConnect Cloud, run the precomputed-ML-predictions tutorial pinned to a deterministic window. Export:

- `qc_export.json` — the predictions the tutorial published (for the chosen symbol).
- `qc_price_history.csv` — the OHLCV history QC saw, so we can verify any feature recomputation if the tutorial computes intermediate features.
- `attribution.md` — pinned start/end dates, sklearn / LEAN / numpy versions, QC dataset id, the exact tutorial URL and commit/version (if QC versions tutorials), a screenshot of the QC notebook output for visual audit.
- A log of the QC run (timestamps, any warnings) saved alongside the fixture.

**§C — Lock the parity claim.**

1. Run the importer against the captured `qc_export.json`. Capture the resulting `prediction_set_hash`.
2. Build a `StrategySpec` referencing the imported artifact via `PredictionComparison`. Run the backtest. Capture `RunLedger.prediction_set_hash` and `result_hash`.
3. Pin all three hashes in `tests/research/ml/fixtures/qc_known_hashes.json`.
4. Write the parity test that asserts: (a) re-running the importer reproduces the pinned `prediction_set_hash`; (b) re-running the backtest reproduces the pinned `result_hash`; (c) for every prediction row the importer emits, the value equals QC's published value within `atol=1e-9, rtol=0`.

## Test list

`PythonDataService/tests/research/ml/`:

**Schema tests** (`test_artifact_generator_meta.py`):
- `DeterministicRuleGenerator` round-trips (existing 1.0 manifests still load).
- `QuantConnectPrecomputedFixtureGenerator` round-trips with all required fields populated.
- Discriminated-union dispatch: a manifest with `kind: "quantconnect_precomputed_fixture"` and a `rule_id` field rejects (`extra='forbid'` enforced per variant).
- A manifest with `kind: "deterministic_rule"` and a `qc_versions` field rejects symmetrically.
- Unknown `kind` value rejects.

**Importer unit tests** (`test_quantconnect_fixture_importer.py`):
- Synthetic 5-row export → importer produces a manifest + chunk that loads via `PredictionSet.load(...)`.
- Multi-symbol export with `symbol="SPY"` filter → only SPY rows in output.
- Symbol absent from export → descriptive error.
- Duplicate date for the same symbol → error (per `numerical-rigor.md` "fail fast on duplicates").
- Non-monotonic dates → error.
- Naive date string with no tz → error (cites the `numerical-rigor.md` rule).
- Closed JSON model: extra top-level field in `qc_export.json` → error.
- Path-unsafe `prediction_set_id` → error (reuses `is_path_safe_id`).

**Importer determinism test** (`test_quantconnect_fixture_determinism.py`):
- Run the importer twice on the synthetic fixture; assert byte-identical `manifest.json` and identical `prediction_set_hash`.
- Run on two different machines / pyarrow versions: ensured indirectly because `prediction_set_hash` is a function of canonical row JSON, not parquet bytes (existing v0.5 invariant).

**Real-fixture parity test** (`test_quantconnect_fixture_parity.py`) — **gated on §B fixture capture**:
- Skip with a clear `pytest.skip("QC fixture not yet captured")` until `qc_export.json` is committed under `tests/fixtures/golden/qc-precomputed-predictions/`.
- When unskipped: assert the importer's output matches each pinned hash in `qc_known_hashes.json`; assert per-row prediction equals QC's published value within `atol=1e-9, rtol=0`.

**Runtime parity test** (`test_quantconnect_fixture_runtime.py`) — **gated on §B**:
- Skip until fixture captured.
- When unskipped: build a `StrategySpec` with `PredictionComparison` against the imported artifact, run via `run_strategy_spec`, assert `RunLedger.prediction_set_hash` and `result_hash` match the pinned values.

The skip-until-captured pattern lets §A land independently of QC Cloud access while keeping the §C parity claim mechanically enforced once the fixture exists.

## Out of scope for Phase 1

- Phase 2's Keras tutorial — model training, model save/load, runtime inference, sklearn / TF in `requirements-heavy.txt`.
- Phase 3's P&L parity — fill / commission / slippage matching against QC's backtester.
- `ModelSpec` schema. Phase 1 imports predictions; it doesn't describe the model that produced them. The model lives in QC; we keep the URL and version metadata, not the model code.
- Multi-symbol or custom-universe parity. Single-symbol invariant from v0.5 holds.
- Walk-forward retraining. Phase 1 imports one chunk's worth of predictions for a fixed window.
- New routes / Angular UI / GraphQL passthrough. The artifact and ledger surface that already exists is enough.

## Risks and open questions

- **R1 — QC's published JSON shape.** Confirmed via QC's documented tutorial code (see "QC's documented export shape" above): bare list of `{date: "YYYY-MM-DD", prediction_by_symbol: {symbol: float}}`. The importer's `RootModel[list[QcPredictionRecord]]` matches this shape exactly with `extra='forbid'`. Risk remaining: QC may version the tutorial and change the shape without notice — §B should diff the captured `qc_export.json` against this spec's documented shape and surface any drift before unskipping the parity tests.
- **R2 — QC tz semantics.** QC documents `algorithm.Time` as exchange-aware. Whether the predictions JSON serializes timestamps as UTC, local-with-offset, or naive-with-implied-NY is a property of QC's exporter and must be confirmed from the captured fixture before the importer's tz handling is finalized. Documented in the importer module docstring after §B.
- **R3 — QC dataset-id stability.** If QC silently re-versions the dataset (e.g., dividend adjustment policy change), our pinned hash would still pass for the captured `qc_export.json` but the underlying QC reality would have drifted. Mitigation: `attribution.md` records the QC dataset id and run timestamp; periodic re-export and diff is a follow-up, not Phase 1 scope.
- **R4 — Symbol the QC tutorial actually uses.** The original handoff assumed SPY based on the Keras tutorial; the precomputed-predictions tutorial may publish a different symbol or a multi-symbol export. **D4** + the importer's `symbol` parameter handle either case, but Tim should confirm before §B so the fixture and the spec match.
- **R5 — Phase 1 still requires QC Cloud access.** §A unblocks importer code, but §C is gated on a real export. If Tim never runs the QC notebook, Phase 1 ships with synthetic-fixture coverage only and the real-fixture parity tests stay skipped — documented but not actually proven against QC.

## What's pinned vs. what's still flexible

Pinned by this spec:
- The Phase 1 / Phase 2 / Phase 3 split.
- The schema-1.0-stays-1.0 + discriminated `GeneratorMeta` union approach.
- The `int64 ms UTC` boundary policy at the importer.
- The three hash claims in §C.
- The skip-until-captured test pattern.

Flexible (decided by the captured fixture):
- The exact `QuantConnectPrecomputedFixtureGenerator` field set — we expect the seven fields above to cover real QC exports, but the field list is finalized when §B lands.
- The fixture's symbol, calendar window, and version metadata.
- Whether the QC export contains intermediate features (and if so, whether we validate feature parity in addition to prediction parity).

## Suggested session opener for the implementation pass

> Read this spec (`docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md`) and its sibling plan (`docs/superpowers/plans/2026-05-10-quantconnect-precomputed-predictions-parity.md`). Implement §A end-to-end (schema extension + importer + synthetic-fixture tests). Stop at the §B gate; do not write the real-fixture parity test until Tim has captured the QC export.
