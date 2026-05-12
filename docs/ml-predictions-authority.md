# ML Predictions — Authority

> **Canonical reference** for how machine-learning predictions enter, flow
> through, and are validated against the engine in learn-ai. Source-of-truth
> implementation snapshot, not a design document — when this page disagrees
> with code, the code is right and this page must be updated in the same PR.
>
> **Sibling docs** (different jobs, do not duplicate):
> - [`superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md`](superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md) — v0.5 design rationale (why predictions enter as a data artifact, not an in-engine model)
> - [`superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md`](superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md) — QC tutorial parity Phase 1 design
> - [`superpowers/specs/2026-05-11-phase3-pnl-parity-design.md`](superpowers/specs/2026-05-11-phase3-pnl-parity-design.md) — Phase 3 trade-level parity design (current)
> - [`references/quantconnect-precomputed-predictions.md`](references/quantconnect-precomputed-predictions.md) — QC fixture capture reference (Phase 1)
> - [`references/qc-aapl-phase3-capture-runbook.md`](references/qc-aapl-phase3-capture-runbook.md) — QC fixture capture runbook (Phase 3)
> - [`references/reconciliations/qc-aapl-phase3.md`](references/reconciliations/qc-aapl-phase3.md) — Phase 3.0 reconciliation report
> - [`handoffs/2026-05-11-phase3-implementation-summary.md`](handoffs/2026-05-11-phase3-implementation-summary.md) — Phase 3 implementation handoff
>
> **Owner:** the engineer editing `PythonDataService/app/research/ml/*`,
> `PythonDataService/app/research/parity/*`, or any `PredictionComparison`
> consumer under `PythonDataService/app/engine/strategy/spec/`. Same-PR
> rule: if you touch those files, update the matching section here and
> bump **Last reviewed**.
>
> **Last reviewed:** 2026-05-12 (post Phase 3.5 Path A merge — single-fill acceptance gate passed; Phase 3.5+ multi-day round-trip P&L deferred pending QC OOS rollover).

---

## Table of contents

- [1. Scope and authority](#1-scope-and-authority)
- [2. Architecture overview](#2-architecture-overview)
- [3. Module surface and canonical files](#3-module-surface-and-canonical-files)
- [4. Prediction set artifact format](#4-prediction-set-artifact-format)
- [5. StrategySpec wiring](#5-strategyspec-wiring)
- [6. QC parity infrastructure](#6-qc-parity-infrastructure)
- [7. Validation status by phase](#7-validation-status-by-phase)
- [8. Fixtures and tests](#8-fixtures-and-tests)
- [9. Frontend surfaces](#9-frontend-surfaces)
- [10. Open issues and next phases](#10-open-issues-and-next-phases)

---

## 1. Scope and authority

"ML predictions" in learn-ai means **precomputed prediction sets** consumed
by `StrategySpec` at backtest time, **not** in-engine model training. The
ML model is trained externally (in QC Cloud, in a separate notebook,
or elsewhere), produces a deterministic per-(symbol, timestamp) numeric
prediction, and that artifact is the engine's input — alongside price bars.

What this means concretely:

| In scope | Out of scope |
|---|---|
| Importing prediction sets from external sources (currently: QC) | Training the model |
| Pinning a prediction set's content via deterministic hash | Live online retraining |
| Pairing a `StrategySpec` with a `prediction_set_id` | In-engine feature engineering producing predictions |
| Verifying bar-clock coverage between a prediction set and the strategy's bar stream | Multi-symbol portfolio construction beyond `SetHoldings` |
| Reconciling our engine's trade log against a reference (QC) backtest that used the same predictions | Live trading on predictions |

The authority of this doc covers everything from "predictions arrive as a
JSON file" through "the engine produces a trade log we compare against a
reference." Live trading on predictions is governed by the IBKR integration
authority — out of scope here.

---

## 2. Architecture overview

The flow at runtime:

```
External notebook / QC Cloud
  │ trains model, emits {date: {symbol: prediction}} JSON
  ▼
import_qc_fixture(qc_export.json) ──► PredictionSet artifact dir
                                       (manifest.json + chunks/<ts>.parquet)
                                       └─ pinned by manifest.prediction_set_hash
  │
  ▼
StrategySpec.predictions[*].prediction_set_id = <id>
PredictionComparison condition consumes prediction values
  │
  ▼
run_strategy_spec(...)
  ├─ PredictionSet.load(<artifact_dir>)
  ├─ prediction_set.assert_pairs_with(spec)
  ├─ assert_bar_clock_coverage(prediction_set, bar_stream)  ← fail-fast
  ├─ BacktestEngine.run(SpecAlgorithm)
  └─ returns RunLedger + BacktestRunResult
         └─ ledger.prediction_set_hash pinned to manifest hash
  │
  ▼
QcReconciler.reconcile_qc_aapl_phase3(...)
  ├─ _parse_qc_orders(qc_orders.json)        ← canonical schema
  ├─ _audit_fixture(...)                      ← resolution-aware
  ├─ _align_fills(...)                        ← seq-aware, no silent drops
  ├─ _classify_divergences(...)               ← 8-category taxonomy
  ├─ _pair_round_trips + _classify_pnl_drift  ← round-trip P&L
  └─ ReconciliationReport (markdown + JSON renderers)
```

### Two and only two boundaries where predictions are "real"

1. **Ingestion** — `import_qc_fixture` parses an external export and writes
   the canonical `PredictionSet` artifact (Parquet chunks + manifest). The
   manifest hash pins content; reimporting the same input always produces
   the same hash.
2. **Engine consumption** — `PredictionSet.load(...)` reads the artifact and
   exposes `next_prediction(timestamp_ms, symbol)` to `SpecAlgorithm`. The
   engine never re-reads the raw export.

Everything between those two boundaries — including the `prediction_set_hash`
recorded in the `RunLedger` — uses the canonical artifact form.

---

## 3. Module surface and canonical files

### Prediction-set lifecycle (`PythonDataService/app/research/ml/`)

| File | Responsibility |
|---|---|
| `loader.py` (`class PredictionSet`) | Load artifact, validate manifest, expose lookups (`next_after()`, `PredictionLookupError`), run `assert_pairs_with(spec)` |
| `artifact.py` | Manifest schema, chunk write/read helpers, hash computation |
| `coverage.py` | `assert_bar_clock_coverage(prediction_set, bar_stream, refs=...)` — fail-fast on missing or extra bars; lookup-aware when `refs` provided |
| `generators/quantconnect_fixture.py` | `import_qc_fixture(...)` — QC `qc_export.json` → canonical artifact, deterministic hash |
| `generators/deterministic_rule.py` | Synthetic prediction generator (testing only — no QC dependency) |
| `generate_prediction_set.py` | CLI entry point for ingestion (used by validation scripts) |

### Engine consumption (`PythonDataService/app/engine/strategy/spec/`)

| File | Responsibility |
|---|---|
| `schema.py` | `StrategySpec.predictions` list, `PredictionRef` (incl. new `lookup` field), `PredictionComparison` Pydantic schema |
| `primitives.py` | `PredictionComparison` evaluator — per-ref lookup dispatch using `PredictionRef.lookup`; `op` ∈ `{<, <=, >, >=, ==, !=}` |
| `engine.py` | `FillMode.NEXT_SESSION_OPEN` — defer-only with NY-trading-date eligibility; fills at first minute-bar open of the next trading session |
| `__init__.py` (`SpecAlgorithm.__init__`) | Takes `prediction_set: PredictionSet \| None` injected from the runner; raises if spec references a set but none is provided |

### Runner integration (`PythonDataService/app/research/runs/`)

| File | Responsibility |
|---|---|
| `runner.py` (`run_strategy_spec`) | Orchestrates prediction-set load + coverage check + engine run; records `prediction_set_hash` on the `RunLedger`; accepts `"next_session_open"` fill_mode string |
| `runner.py` (`_prediction_artifacts_root`) | Artifacts dir resolution — defaults to `PythonDataService/artifacts/predictions/`, overridable via `LEARN_AI_PREDICTION_ARTIFACTS_ROOT` (used by parity tests) |
| `ledger.py` (`RunLedger.prediction_set_hash`) | Pinned hash field — links the run to the exact prediction-set content |

### QC parity infrastructure (`PythonDataService/app/research/parity/`)

Phase 3 trade-level parity machinery — see §6.

| File | Responsibility |
|---|---|
| `fixture_data_reader.py` | CSV-backed `TradeBar` reader; auto-detects daily vs minute resolution; `find_bar_containing(symbol, fill_time_ms)` |
| `ibkr_commission.py` | IBKR equity-tier commission model ($0.005/share, $1 floor, 0.5% cap) — standalone, called reconciler-side |
| `qc_reconciler.py` | `reconcile_qc_aapl_phase3(...)` public entry; 8-category `DivergenceCategory` `StrEnum`; round-trip P&L pairing; markdown + JSON report renderers |

---

## 4. Prediction set artifact format

Lives at `<artifacts_root>/<prediction_set_id>/` after ingestion:

```
<prediction_set_id>/
├── manifest.json         # schema_version, prediction_set_id, symbol, resolution_minutes,
│                         # field_names, warmup_policy, generator, qc_provenance, chunks,
│                         # prediction_set_hash
└── chunks/
    └── <trained_through_ms>.parquet  # one row per (timestamp_ms, prediction_value),
                                      # may include additional field columns
```

**Hash determinism guarantee** — re-importing the same `qc_export.json`
with the same provenance constants produces a byte-identical manifest
and the same `prediction_set_hash`. This is pinned in
`tests/research/ml/fixtures/qc_known_hashes.json`. The
[`test_repeated_import_produces_identical_hash_and_manifest`](../PythonDataService/tests/research/ml/test_quantconnect_fixture_determinism.py)
test enforces it.

**Symbol normalization** — QC stringifies `Symbol` objects with a security-id
suffix (`"AAPL R735QTJ8XC9X"`). The importer's expected input strips that
to bare ticker keys (a notebook post-processing step). `_parse_qc_orders`
in the reconciler also strips the suffix from order payloads.

---

## 5. StrategySpec wiring

A spec consumes a prediction set via:

```python
StrategySpec.model_validate({
  "symbols": ["AAPL"],
  "resolution": {"period_minutes": 1440},
  "predictions": [
    {
      "id": "qc_pred",                              # name used in conditions
      "prediction_set_id": "qc_aapl_gbm_v001",       # artifact dir name
      "field": "prediction"                          # column in the chunk parquet
    }
  ],
  "entry": {
    "conditions": [
      {"kind": "PredictionComparison",
       "prediction": "qc_pred", "op": ">", "value": 0.0}
    ],
    "size": {"kind": "SetHoldings", "fraction": 1.0},
  },
  "exit": {
    "conditions": [
      {"kind": "PredictionComparison",
       "prediction": "qc_pred", "op": "<=", "value": 0.0}
    ],
  },
  ...
})
```

**v0.5 boundary** — exactly one `prediction_set_id` per spec is supported.
Multiple distinct prediction sets in one spec is a planned future
extension; current code enforces the singleton via
`StrategySpec._check_phase1_boundaries`.

**Evaluation timing** — `PredictionComparison` reads the prediction
value indexed by the current bar's timestamp. The bar-clock coverage
check (run before the engine starts) guarantees every bar the engine
will see has a corresponding prediction; runtime lookups are O(1).

**Failure modes** (all surfaced as `RunLedger.status="failed"` rather than
exceptions thrown to the caller):
- Prediction set missing → "prediction set <id> failed to load"
- Spec / set symbol mismatch → "prediction set <id> does not pair with spec"
- Bar-clock coverage gap → "prediction set <id>: bar-clock coverage failed"

---

## 6. QC parity infrastructure

Phase 3 validates that our engine, fed QC's exact inputs (predictions + price
bars), produces a trade log matching QC's recorded backtest. The
infrastructure is general-purpose — extensible to any reference backtester
that produces an orders/events JSON.

### Divergence taxonomy (8 categories)

Encoded as `DivergenceCategory` `StrEnum` in `qc_reconciler.py` and
documented in `.claude/rules/numerical-rigor.md`:

| Category | Meaning | Routes to |
|---|---|---|
| `FIXTURE_INSUFFICIENT` | Captured price history can't explain a reference fill (price outside bar range, or wrong resolution) | Phase 3.5: re-capture / minute-bar promotion |
| `DECISION_MISMATCH` | Only one side has a fill on `(trading_date, side, seq)` | Phase 3 engine / spec bug |
| `DIRECTION_MISMATCH` | Same date+qty, opposite signs | Phase 3 engine bug |
| `QUANTITY_MISMATCH` | Same date+side, different qty | Phase 3 sizing / cash-buffer fix |
| `FILL_PRICE_DRIFT` | Prices differ by more than `fill_price_atol` | Phase 3.5 fill-model port if clustered |
| `COMMISSION_DRIFT` | Reference fee differs from `IbkrEquityCommissionModel` output | Branch A: Phase 3.5; Branch B: diagnostic only |
| `PNL_DRIFT` | Per-round-trip realized P&L diverges beyond propagated atol | Almost always downstream — root-cause upstream first |
| `ORDER_TYPE_MISMATCH` | Reference uses non-`MARKET` order type | Spec doesn't support; investigate reference algorithm |

**Acceptance gate** — `report.status == "passed"` iff zero divergences fall
in the **gating set**: all of the above except `COMMISSION_DRIFT`, plus
`COMMISSION_DRIFT` only when `assert_fees=True` (Branch A).

### Resolution-aware fixture audit

- **Daily fixture**: fill price ≈ daily bar's `open` ± atol (canonical
  `NEXT_BAR_OPEN` semantics)
- **Minute fixture**: fill price within `[low - atol, high + atol]` of the
  minute bar containing the fill time (market fills happen at some price
  inside the bar's range, not at the open)

Auto-detected by `FixtureDataReader.is_minute_resolution`.

### Round-trip P&L pairing

`_pair_round_trips` walks fills sorted by `fill_time_ms`, pairs each buy
with the next sell on a strictly later date. Realized P&L per round-trip:
`(exit_price − entry_price) × shares − entry_fee − exit_fee`. Propagated
tolerance: `(|entry_qty| + |exit_qty|) × per_share_pnl_atol + 2 × commission_atol`.
Phase 3 invariant: single-position-at-a-time long-only — consecutive
same-side fills raise `RoundTripPairingError`.

---

## 7. Validation status by phase

| Phase | Status | What's covered | What blocks closure |
|---|---|---|---|
| **v0.5 plumbing** (PR #207–#210) | ✅ shipped | `PredictionSet` artifact format, manifest-hash determinism, `assert_pairs_with`, `assert_bar_clock_coverage`, runner integration, `RunLedger.prediction_set_hash` | — |
| **QC tutorial parity Phase 1** (PR #211–#215) | ✅ shipped | Captured GBM prediction-set fixture from QC's "Precomputed ML Predictions" tutorial (AAPL anchor, 22-day window); reimport hash pinned at `b8252cfa9a749f5bf592602f3aebc2b3a4ccc6bb0cd41da48a6db7a581342e0e` | — |
| **Phase 3.0 — trade-level parity scaffolding** (PR #218–#220) | ✅ shipped (xfail) | `FixtureDataReader` (daily+minute), `IbkrEquityCommissionModel`, `QcReconciler` (8-category taxonomy), round-trip P&L emission, `_build_our_fills` engine replay; 1-day QC fixture committed | Phase 3.0 acceptance test marked `xfail(strict=True)` — 1-day fixture exposes intrinsic QC-intraday-vs-our-NEXT_BAR_OPEN timing mismatch (documented in [reconciliation summary](references/reconciliations/qc-aapl-phase3.md)) |
| **Phase 3.5 Path A — intraday-trigger fill mode** | ✅ shipped (single-fill scope) | `FillMode.NEXT_SESSION_OPEN` (defer-only with NY-trading-date eligibility), `PredictionRef.lookup="next_after_bar_close"` for data-timing, `PredictionSet.next_after`, lookup-aware bar-clock coverage. Acceptance test passes with 1 pinned aligned fill (2026-02-10 buy, $273.18 vs QC's $273.24 within bid-ask tolerance). | — |
| **Phase 3.5+ — multi-day round-trip P&L** | ⏳ deferred | Gated on QC OOS rollover (~2 months at free tier) or paid-tier upgrade. Requires the full 2026-02-10 → 2026-03-12 fixture window so an exit signal can fire and a closed `LoggedTrade` round-trip emits. | QC account tier OR wait for OOS rollover |
| **Phase 4 — multi-symbol top-N ranking** | ⏳ pending (independent of 3.5) | Currently `SpecAlgorithm` restricts to single symbol; `PortfolioConstruction` extension needed | `StrategySpec` schema change |

### Historical note — Phase 3.0 xfail closed by Phase 3.5 Path A

Phase 3.0 shipped the reconciler infrastructure against a 1-day fixture
with an intentional `xfail(strict=True)`: our engine's `NEXT_BAR_OPEN`
filled one trading day after QC's intraday `set_holdings`, producing a
`DECISION_MISMATCH`. Phase 3.5 closes this via `FillMode.NEXT_SESSION_OPEN`
(defer-only with NY-trading-date eligibility) + `PredictionRef.lookup=
"next_after_bar_close"`. The acceptance test now asserts `status="passed"`
under widened (but justified) tolerances; see the
[reconciliation report](references/reconciliations/qc-aapl-phase3.md)
for the divergence breakdown.

---

## 8. Fixtures and tests

### Reference fixtures

| Path | Phase | Purpose |
|---|---|---|
| `PythonDataService/tests/fixtures/golden/qc-precomputed-predictions/` | Phase 1 | Raw QC GBM prediction-set export (`qc_export.json`) + attribution. Reimport hash pinned. |
| `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/` | Phase 3.0 | 1-day QC backtest fixture: `qc_orders.json` (canonical schema), `qc_price_history.csv` (minute), `qc_equity.json`, `qc_algorithm_screenshot.png`, `attribution.md` |
| `PythonDataService/tests/research/ml/fixtures/qc_known_hashes.json` | Phase 1 | Pinned `prediction_set_hash` for the QC fixture |

### Test inventory

| Test | Validates |
|---|---|
| `tests/research/ml/test_quantconnect_fixture_determinism.py` | Reimporting QC export produces identical manifest + hash |
| `tests/research/parity/test_fixture_data_reader.py` | CSV → `TradeBar`, daily/minute auto-detection, `find_bar_containing` |
| `tests/research/parity/test_ibkr_commission.py` | IBKR fee formula across $1 floor / 0.5% cap edges |
| `tests/research/parity/test_qc_reconciler.py` | Canonical schema parsing, fixture audit (daily + minute), alignment with seq disambiguation, divergence classification (all 8 categories), round-trip P&L drift |
| `tests/research/parity/test_qc_fixture_smoke.py` | QC fixture shape gate; logs `FEE_PRESENCE_BRANCH=A\|B` |
| `tests/research/parity/test_qc_aapl_phase3_trade_parity.py` | End-to-end engine replay → reconciliation (Phase 3.0: `xfail`; Phase 3.5: will assert pass) |

### How to run

```bash
podman exec polygon-data-service python -m pytest /app/tests/research/parity -v
podman exec polygon-data-service python -m pytest /app/tests/research/ml -v
```

Project-scope lint must pass:

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
```

---

## 9. Frontend surfaces

There is **no dedicated ML / predictions UI today.** Prediction sets are
ingested via CLI / notebook and consumed by `StrategySpec` runs. Both
strategy-run UIs surface ML-driven specs identically to indicator-driven
specs:

| FE route | FE component | Backend chain |
|---|---|---|
| `/spec-strategy` | `spec-strategy-runner` | GraphQL `runSpecStrategyBacktest` → `/api/spec-strategy/backtest` → `BacktestEngine` (inline result) |
| `/research-lab` → strategy-runs | `research-lab/strategy-runs` + `run-detail-page` | GraphQL → `/api/research/strategy-runs/*` → `run_strategy_spec` → `BacktestEngine` (RunLedger + RunResult persisted) |

Both paths instantiate the same single `BacktestEngine` class
(`app/engine/engine.py`). Phase 3 parity validates **that** engine — the
validation transfers to both UI paths automatically.

Phase 4 (multi-symbol ranking) will need a dedicated UI surface for
prediction-set browsing and top-N configuration; out of scope here.

---

## 10. Open issues and next phases

### Phase 3.5 — closed via Path A

`FillMode.NEXT_SESSION_OPEN` (defer-only) + `PredictionRef.lookup=
"next_after_bar_close"` shipped 2026-05-12. Single-fill acceptance
gate passed; multi-day round-trip P&L deferred pending QC OOS rollover.
See [reconciliation](references/reconciliations/qc-aapl-phase3.md).

### Phase 4 — multi-symbol ranking

QC's full tutorial algorithm ranks all S&P 500 constituents by prediction
value and takes the top N each day. Our `SpecAlgorithm` currently restricts
to a single symbol. Phase 4 requires:

- `PortfolioConstruction` extension to `StrategySpec` (rank-by-field,
  top-N, equal-weight)
- Multi-symbol bar streaming through the engine
- Multi-symbol prediction-set support in `PredictionSet` (currently
  symbol-keyed at artifact root)

Independent of Phase 3.5 — can start in parallel.

### Other tracked gaps

- **`LoggedTrade` carries no quantity / fee fields.** The Phase 3 `_build_our_fills`
  adapter reconstructs share counts from a running-equity tracker (
  `floor(running_equity / entry_price)`) which approximates QC's
  `SetHoldings(1.0)` to within 1-2 shares. Phase 3.5 should consider
  extending `LoggedTrade` itself with `qty`/`entry_fee`/`exit_fee` so the
  adapter doesn't have to guess.
- **No live retraining hook.** v0.5 is offline-only by design; if live
  retraining ever lands, it'll need a new authority section here.
- **Free-tier QC capture is manual.** The capture runbook
  ([qc-aapl-phase3-capture-runbook.md](references/qc-aapl-phase3-capture-runbook.md))
  documents the workflow but free-tier accounts have no API token, so the
  orders JSON must be hand-extracted from the backtest results blob. Paid
  tier would automate this.

### Don't add without a real reason

- Per-prediction confidence intervals (current contract is point estimate
  only)
- Multi-prediction-set composition in one spec (v0.5 enforces singleton)
- In-engine model training
- Wiring `IbkrEquityCommissionModel` into the engine (currently reconciler-side
  only — keeps fee policy in one auditable file)
