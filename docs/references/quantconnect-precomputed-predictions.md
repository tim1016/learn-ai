# QuantConnect precomputed-predictions parity (Phase 1)

**Reference source:** QuantConnect "Precomputed ML Predictions" tutorial — `https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions` (URL pinned in the captured fixture's `attribution.md`).

**Spec:** `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md`

**Plan:** `docs/superpowers/plans/2026-05-10-quantconnect-precomputed-predictions-parity.md`

## Status

- §A — schema extension + importer + synthetic-fixture tests: **landed** (PR #211).
- §B — real QC fixture capture: **landed** (this PR).
- §C — data parity (per-row tolerance + `prediction_set_hash` pin): **landed** (this PR).
- §C runtime — `RunLedger.prediction_set_hash` + `result_hash` pinned via a backtest run on synthetic SPY daily bars: **landed** (this PR).

## Tolerances

| Comparison | Tolerance | Justification |
|---|---|---|
| QC published prediction value vs. importer output value | `atol=1e-9, rtol=0` | QC's export is deterministic; predictions are static numbers. Anything looser is a smell — see spec D8. |
| `prediction_set_hash` reproduction | bit-exact | Hash is a function of canonical row JSON; pyarrow / pandas drift cannot affect it (v0.5 invariant). |
| `RunLedger.prediction_set_hash`, `result_hash` | bit-exact | Same reasoning. |

## What §A established

- `GeneratorMeta` is a discriminated union (`deterministic_rule | quantconnect_precomputed_fixture`); manifest schema stays at `1.0`.
- `app/research/ml/generators/quantconnect_fixture.py` reads QC's documented bare-list export shape `[{date: "YYYY-MM-DD", prediction_by_symbol: {symbol: float}}, ...]`, picks the requested symbol from each daily record's map, converts each date-only string + a caller-supplied `(daily_anchor_tz, daily_anchor_hhmm)` to `int64 ms UTC` at the ingestion boundary, and emits a v0.5-compliant `manifest.json` plus a single chunk parquet.
- Provenance (tutorial URL, dataset id, exported_at_ms, calendar window, sklearn/LEAN versions) is **not** in QC's emitted file — captured separately in `attribution.md` at fixture-capture time and passed to the importer as explicit kwargs.
- Determinism is enforced by a re-run test: same input must produce byte-identical manifest and identical `prediction_set_hash`.

The §A test fixtures use synthetic data crafted to match QC's documented shape. §B captures real values; if QC has versioned the tutorial since this writing, the §B step diffs the captured shape against the documented one and surfaces any drift before unskipping the parity tests.

## Pinned decisions

| # | Decision | Value | Notes |
|---|---|---|---|
| 1 | Symbol to anchor parity on | **AAPL** | Long-tenured S&P 500 constituent; present in `qb.universe.etf(spy)` continuously across any reasonable validation window. Changed from SPY because SPY itself is **not** a constituent of its own ETF universe — QC's published tutorial uses `qb.universe.etf(spy)` which returns SP500 stocks, so SPY is absent from the export. |
| 2 | Daily anchor `(tz, HH:MM)` for date-only → `int64 ms UTC` | `("America/New_York", "16:00")` (defaults; NYSE close) | |
| 3 | `qc_dataset_id` convention | Use QC's labeled string for the data source (e.g. `"QuantConnect/USEquity-Daily"`); record the verbatim label in `attribution.md`. | |
| 4 | QC `Symbol` key normalization | Strip the security-id suffix at fixture-capture time via `str(s).split(' ', 1)[0]` so saved JSON has bare ticker keys (e.g. `"AAPL"`, not `"AAPL R735QTJ8XC9X"`). | QC stringifies `Symbol` objects with security identifiers. The importer reads bare tickers; the normalization happens in the notebook before save. |

## Captured fixture provenance

(See `PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/attribution.md` for the full record.)

- **QC tutorial URL**: <https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions>
- **QC dataset id**: `QuantConnect/USEquity-Daily` (verbatim placeholder; QC Cloud doesn't expose a stable internal id at the notebook level)
- **Validation window**: `2026-02-10` → `2026-03-12` at NYSE close (`1770757200000` → `1773345600000` ms UTC) — the `[validation_start, validation_end]` QC's tutorial predicts on
- **Train window**: 90 trading days preceding `validation_start` (QC tutorial default)
- **Symbol** (parity anchor): AAPL — extracted from a full `qb.universe.etf(spy)` SP500-constituents export (~500 symbols per record)
- **Model**: `sklearn.ensemble.GradientBoostingRegressor(n_estimators=100, learning_rate=0.1, max_depth=3, random_state=42)` — QC's published precomputed-ML-predictions tutorial code, verbatim. Features: 10-day momentum, 20-day daily-return volatility, relative volume. Label: open-to-open return from `T+1` to `T+2`.
- **Versions**: sklearn 1.6.1, numpy 1.26.4, pandas 2.3.3 (lean: record from QC Cloud "About" footer next time)
- **Exported at (UTC ms)**: `1778469503771` (≈ 2026-05-10)
- **Row count**: 22 daily AAPL predictions (one per trading day in the validation window)
- **Pinned `prediction_set_hash`**: `b8252cfa9a749f5bf592602f3aebc2b3a4ccc6bb0cd41da48a6db7a581342e0e` (in `tests/research/ml/fixtures/qc_known_hashes.json`)
- **Pinned `RunLedger.prediction_set_hash`**: `b8252cfa9a749f5bf592602f3aebc2b3a4ccc6bb0cd41da48a6db7a581342e0e` — equals the manifest hash because the runner threads it through unchanged
- **Pinned `result_hash`**: `2585aadb0e9a22e0b7da4b0b62d4b027acdbaf97695bdff452cc7aa1b23f8446` — covers the (artifact, spec, synthetic AAPL daily bars, engine config) tuple end-to-end

## §C runtime test setup

Lives at `PythonDataService/tests/research/ml/test_quantconnect_fixture_runtime.py`.

- **Spec**: minimal AAPL-daily, no indicators, single `PredictionComparison(prediction="qc_pred", op=">", value=0.0)` entry, 1-bar `BarsSinceEntry` exit. Reduces inputs to `result_hash` to a deterministic function of (imported artifact, synthetic bars, engine defaults).
- **Synthetic data**: one minute bar per QC prediction date with `time = 09:30 ET` and `end_time = 16:00 ET`, plus a sentinel bar 4 days after the last prediction to flush the last day's consolidated bar. Daily consolidation (`period_minutes = 1440`) emits one bar per prediction date with `end_time` matching QC's anchor convention exactly.
- **Why synthetic, not real Polygon/LEAN bars**: the parity claim is about *prediction-pipeline determinism*, not about price-action realism. Decoupling from a live data source keeps the test reproducible offline and across CI re-pulls of master.

## Parity claim (current)

> Captured QC export `qc_export.json` — QC's published `GradientBoostingRegressor(random_state=42)` tutorial run against the SP500-constituents universe, AAPL anchor — feeds into our importer, which reproduces `prediction_set_hash = b8252cfa…342e0e` deterministically across re-runs, and every per-row AAPL prediction value the importer emits equals the source JSON value within `atol=1e-9, rtol=0`. The runtime backtest produces a stable `result_hash = 2585aadb…f8446`.

This **is** literal-QC-published-value parity at the plumbing level: the captured JSON contains QC's actual GBM tutorial output for SP500 constituents over the pinned validation window, and our importer + runner reproduce hashes deterministically against that JSON. The runtime test uses synthetic flat AAPL daily bars (open=high=low=close), not real market data — `result_hash` validates pipeline determinism, not P&L realism (Phase 3 work).

**Superseded fixture**: A prior `prediction_set_hash = 5807a23f…f0188` (LinearRegression on SPY, 2025-01-13 → 2025-12-30) is preserved in git history at PR #213. That fixture validated the importer plumbing with a deterministic-but-non-QC-published model; this fixture replaces it with QC's actual tutorial output.

---

## §B / §C runbook — step by step

This is the operational runbook for capturing the QC fixture and locking the parity claim. Spec lives at `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md`. §A landed in PR #211.

### Part 1 — In QuantConnect Cloud (§B)

#### Step 1. Pick the calendar window

Pin two dates in advance and write them down. Examples: start `2024-01-02`, end `2024-12-31`.

**Critical:** these must be hardcoded in the notebook. The QC tutorial defaults to a sliding window from `datetime.now()`; replace that with literal dates so the export is reproducible across runs.

#### Step 2. Open the QC research notebook

1. Log into `quantconnect.com` → **Research** workspace.
2. Open the **Precomputed ML Predictions** tutorial: <https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions>
3. Anchor on **SPY** (per pinned decision #1).

#### Step 3. Run the notebook with pinned dates

In the notebook, ensure:
- The data-history call uses the pinned start/end from Step 1, not relative offsets.
- The `predictions_json` save line is unchanged: `qb.object_store.save('research-to-backtest-factors.json', json.dumps(predictions_json))`.
- Run all cells.

#### Step 4. Capture provenance metadata

Before leaving the notebook, run a cell that prints versions and the export-time:

```python
import sklearn, numpy, pandas as pd
print("sklearn:", sklearn.__version__)
print("numpy:", numpy.__version__)
print("exported_at_ms:", int(pd.Timestamp.utcnow().value // 1_000_000))
# QC's LEAN version is shown on the Research workspace's "About" / footer.
```

Write down (or paste into a scratch file):
- The exact `sklearn`, `numpy`, `lean` versions.
- The `exported_at_ms` int.
- QC's labeled string for the dataset (visible in the data-history call).
- The pinned start/end from Step 1, converted to `int64 ms UTC` at the daily anchor `(America/New_York, 16:00)`.

Take a **screenshot** of the predicted-vs-actual plot and the printed version cell — this is your audit trail.

#### Step 5. Download the artifacts

Pull these files out of QC Cloud:
- `research-to-backtest-factors.json` from ObjectStore (this is QC's bare-list export).
- `qc_price_history.csv` — the OHLCV history the tutorial used (right-click on the DataFrame → save as CSV, or write to ObjectStore and download).
- The screenshot from Step 4.

### Part 2 — In the repo (§C)

#### Step 6. Drop the fixture into place

Create the captured-fixture files under the existing fixture skeleton dir:

```text
PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/
├── qc_export.json               ← rename from research-to-backtest-factors.json
├── qc_price_history.csv         ← from Step 5
├── qc_notebook_screenshot.png   ← from Step 4
└── attribution.md               ← see template below
```

**`attribution.md` template** (replace the `<...>` values):

```markdown
# QC precomputed-predictions fixture — attribution

- **Tutorial URL**: https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions
- **Captured by**: Tim (QC Cloud account)
- **Captured at (UTC ms)**: <int from Step 4 — qc_exported_at_ms>
- **Calendar window start (UTC ms, anchor 16:00 America/New_York)**: <int>
- **Calendar window end (UTC ms, anchor 16:00 America/New_York)**: <int>
- **QC dataset id**: <verbatim string from Step 4>
- **Symbol**: SPY
- **QC versions**:
  - sklearn: <version>
  - numpy: <version>
  - lean: <version>
- **Notebook screenshot**: see `qc_notebook_screenshot.png` in this directory.
- **Notes**: <any deviations from the tutorial code, special handling, etc.>
```

#### Step 7. Run the importer once to capture the prediction-set hash

From the repo root:

```bash
podman exec -it polygon-data-service python -c "
from pathlib import Path
from app.research.ml.generators.quantconnect_fixture import import_qc_fixture

manifest = import_qc_fixture(
    qc_export_path=Path('/app/tests/fixtures/golden/qc-precomputed-predictions/qc_export.json'),
    prediction_set_id='qc_spy_precomputed_v001',
    output_root=Path('/app/artifacts/predictions'),
    symbol='SPY',
    qc_tutorial_url='https://www.quantconnect.com/docs/v2/writing-algorithms/importing-data/streaming-data/precomputed-ml-predictions',
    qc_exported_at_ms=<int from attribution.md>,
    qc_calendar_window_start_ms=<int>,
    qc_calendar_window_end_ms=<int>,
    qc_dataset_id='<verbatim string>',
    qc_versions={'sklearn': '<v>', 'numpy': '<v>', 'lean': '<v>'},
    # qc_daily_anchor_tz / qc_daily_anchor_hhmm default to ('America/New_York', '16:00') — pinned decision #2
)
print('prediction_set_hash:', manifest.prediction_set_hash)
"
```

Save the printed `prediction_set_hash`.

#### Step 8. Build a `StrategySpec` and run it once to capture two more hashes

Write a minimal SpecAlgorithm run that consumes the imported artifact (mirror `app/engine/strategy/spec/tests/test_spec_predictions_runtime.py` for shape). Run via `run_strategy_spec`. From the resulting `RunLedger`, capture:
- `RunLedger.prediction_set_hash`
- `RunLedger.result_hash`

#### Step 9. Pin all three hashes

Create `PythonDataService/tests/research/ml/fixtures/qc_known_hashes.json`:

```json
{
  "prediction_set_hash": "<from Step 7>",
  "run_ledger_prediction_set_hash": "<from Step 8>",
  "result_hash": "<from Step 8>"
}
```

#### Step 10. Parity tests — what they assert (already implemented)

Phase 1 §B + §C landed real assertions in both parity files; this step is a description of what they enforce, not pending work. If you re-capture the fixture in a future cycle, these tests guard the regression surface automatically.

- `test_qc_fixture_parity_per_row_predictions_match` (parity file) — loads `qc_export.json` directly, runs the importer, walks the importer's row index, and asserts `math.isclose(imported_value, qc_published_value, abs_tol=1e-9, rel_tol=0)` for every row. Also asserts the importer's NY-date set equals the QC export's NY-date set (one-to-one coverage; rejects duplicates and missing rows).
- `test_qc_fixture_prediction_set_hash_pinned` (parity file) — asserts importer's output `prediction_set_hash` equals `qc_known_hashes.json["prediction_set_hash"]`.
- `test_qc_fixture_strategy_spec_run_ledger_hash_pinned` (runtime file) — runs the StrategySpec, asserts `RunLedger.prediction_set_hash` equals the pinned value.
- `test_qc_fixture_strategy_spec_result_hash_pinned` (runtime file) — asserts `RunLedger.result_hash` equals the pinned value.

Keep the per-row tolerance at `atol=1e-9, rtol=0` — do not loosen unless you're documenting an explicit reason in this doc (see spec D8).

#### Step 11. Verify

```bash
ruff check PythonDataService/app/ PythonDataService/tests/   # from repo root, NOT via podman exec
podman exec polygon-data-service python -m pytest tests/research/ml/ -v
```

Expected: all four previously-skipped tests now pass; nothing else regresses.

#### Step 12. Commit + PR

```bash
git switch -c feat/qc-precomputed-predictions-parity-phase-1c
git add PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/ \
        PythonDataService/tests/research/ml/fixtures/qc_known_hashes.json \
        PythonDataService/tests/research/ml/test_quantconnect_fixture_parity.py \
        PythonDataService/tests/research/ml/test_quantconnect_fixture_runtime.py \
        docs/references/quantconnect-precomputed-predictions.md
git commit -m "feat(ml-parity): QC precomputed-predictions Phase 1 §C — pin hashes, activate parity tests"
git push -u origin feat/qc-precomputed-predictions-parity-phase-1c
gh pr create --title "feat(ml-parity): QC precomputed-predictions Phase 1 §C" --body "..."
```

Don't forget to fill in the "Captured fixture provenance" section at the top of this file with the real values.

---

## Future steps (after §C lands)

> ⚠️ Out of Phase 1 scope — recorded here so they don't get lost.

- **Phase 2 — Keras-tutorial parity.** Original framing in `docs/superpowers/specs/2026-05-10-quantconnect-tutorial-parity-handoff.md` (marked superseded). Validates QC's Keras "ML key concepts" tutorial: trains a Sequential MLP locally, saves model to `qb.ObjectStore`, reads back at runtime. Hard problems flagged in that doc (TF/Keras non-determinism, runtime model-load path) still apply. Adds sklearn / TensorFlow / Keras to `requirements-heavy.txt`. Deferred until Phase 1 §C is shipped.
- **Phase 3 — P&L parity.** Phase 1 only validates **signal/prediction ingestion** parity, not P&L. Reproducing QC's fill model, commission schedule, and slippage configuration in our engine is significant work. Defer until a real use case demands it.
- **Multi-symbol custom universes.** Phase 1 invariant: `prediction_set.symbol == spec.symbols[0]` (single symbol). Multi-symbol parity requires bumping the run-ledger schema to `1.2` with `prediction_set_hashes: dict[str, str]` and updating the spec validator. Tracked in the v0.5 spec's "Non-goals" table; not scheduled.
- **Quarterly re-export hygiene.** QC may version dataset semantics silently (e.g., dividend-adjustment changes). Periodically re-run the §B notebook against the same calendar window and diff the new `qc_export.json` against the committed one. If they differ, the parity claim is stale — refresh deliberately and document why in `attribution.md`.
