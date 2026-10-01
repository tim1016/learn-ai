# ML Predictions — Authority

> What the code cannot say about machine-learning predictions in learn-ai:
> their scope (§1), how an operator produces a prediction set (§3), and what
> has and has not been validated against a reference (§7). The code is
> `PythonDataService/app/research/ml/` (artifact, loader, coverage, CLI) and
> the spec `prediction` primitive in `PythonDataService/app/engine/strategy/spec/`.
> Sections 2, 4–6 and 8–10 restated that code and were cut; Git history has them.
>
> **Sibling docs** (different jobs, do not duplicate):
> - [ADR 0072](architecture/adrs/0072-research-run-identity-and-sealed-run-inputs.md) Decision 6 — why predictions enter as a content-hashed data artifact, not an in-engine model
> - [`references/quantconnect-precomputed-predictions.md`](references/quantconnect-precomputed-predictions.md) — QC fixture capture reference (Phase 1)
> - [`references/reconciliations/qc-aapl-phase3.md`](references/reconciliations/qc-aapl-phase3.md) — Phase 3.0 reconciliation report and the Phase 3 QC capture runbook
>
> **Last reviewed:** 2026-05-12 (post Phase 3.5 Path A merge — **single-fill** acceptance gate passed; multi-trade engine validation **does not exist** end-to-end against real QC output; decision not to pursue paid-tier QC, minute-data trailing-window workarounds, or shifted-window re-capture).

---

## Table of contents

- [1. Scope and authority](#1-scope-and-authority)
- [3. Module surface and canonical files](#3-module-surface-and-canonical-files)
- [7. Validation status by phase](#7-validation-status-by-phase)

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
reference." Live trading on predictions is out of scope here.

---

## 3. Module surface and canonical files

### Producing a prediction set

`generate_prediction_set` is the operator CLI that writes the prediction-set
artifact the spec `prediction` primitive reads. It reads minute bars from the
LEAN data roots (`LEAN_DATA_ROOT` or `LEAN_DATA_CACHE` must be set) and
refuses with the missing session ranges when coverage is incomplete:

```bash
cd PythonDataService && .venv/bin/python -m app.research.ml.generate_prediction_set \
  --rule rsi_14_centered --symbol <SYMBOL> --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
  --resolution-minutes <N>
```

It prints the new `prediction_set_id` and writes
`<artifacts-root>/<prediction_set_id>/`. The default `--artifacts-root` is
`PythonDataService/artifacts/predictions/`, the root `run_strategy_spec` reads
(`LEARN_AI_PREDICTION_ARTIFACTS_ROOT` overrides the runner's root). A QC export
is imported with `import_qc_fixture` (`app/research/ml/generators/quantconnect_fixture.py`)
instead.

---

## 7. Validation status by phase

| Phase | Status | What's covered | What blocks closure |
|---|---|---|---|
| **v0.5 plumbing** (PR #207–#210) | ✅ shipped | `PredictionSet` artifact format, manifest-hash determinism, `assert_pairs_with`, `assert_bar_clock_coverage`, runner integration, `RunLedger.prediction_set_hash` | — |
| **QC tutorial parity Phase 1** (PR #211–#215) | ✅ shipped | Captured GBM prediction-set fixture from QC's "Precomputed ML Predictions" tutorial (AAPL anchor, 22-day window); reimport hash pinned at `b8252cfa9a749f5bf592602f3aebc2b3a4ccc6bb0cd41da48a6db7a581342e0e` | — |
| **Phase 3.0 — trade-level parity scaffolding** (PR #218–#220) | ✅ shipped (xfail) | `FixtureDataReader` (daily+minute), `IbkrEquityCommissionModel`, `QcReconciler` (8-category taxonomy), round-trip P&L emission, `_build_our_fills` engine replay; 1-day QC fixture committed | Phase 3.0 acceptance test marked `xfail(strict=True)` — 1-day fixture exposes intrinsic QC-intraday-vs-our-NEXT_BAR_OPEN timing mismatch (documented in [reconciliation summary](references/reconciliations/qc-aapl-phase3.md)) |
| **Phase 3.5 Path A — intraday-trigger fill mode** | ✅ shipped (single-fill scope) | `FillMode.NEXT_SESSION_OPEN` (defer-only with NY-trading-date eligibility), `PredictionRef.lookup="next_after_bar_close"` for data-timing, `PredictionSet.next_after`, lookup-aware bar-clock coverage. Acceptance test passes with 1 pinned aligned fill (2026-02-10 buy, $273.18 vs QC's $273.24 within bid-ask tolerance). | — |
| **Phase 3.5+ — multi-day round-trip P&L** | 🛑 not pursued (accepted limitation, **no empirical multi-trade engine validation**) | The QC tutorial algorithm uses `Resolution.MINUTE`, and QC free tier provides only a short trailing window of minute-resolution data ([QC forum](https://www.quantconnect.com/forum/discussion/19781/getting-data-with-free-plan/): "Hourly and Daily history is available broadly"; minute/second/tick have "shorter trailing windows"). Empirically that minute-data trailing window is ~90 calendar days, which truncated the captured backtest to the entry day only — no exit was simulated by QC. End-to-end validation is **single-fill only**; reconciler's multi-fill / round-trip logic is unit-tested with synthetic fixtures, **not** against real QC output. We have no empirical evidence the engine handles multi-trade sequences correctly under a real truth set. Decision 2026-05-12: not buying the Researcher Seat (~$10/mo); not re-capturing at daily resolution (would validate a different engine fill-mode code path, `NEXT_BAR_OPEN`, instead of the `NEXT_SESSION_OPEN` path Phase 3.5 Path A actually validates). | Cheapest unblock at full fidelity: paid-tier QC for longer minute-data history. Cheaper but lower fidelity: re-capture at daily resolution, accept that it validates a different engine code path. Or use a different reference backtester. |
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
