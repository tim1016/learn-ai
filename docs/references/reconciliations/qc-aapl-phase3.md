# Reconciliation — QC AAPL Phase 3 trade-level parity

**Status:** Phase 3.0 — infrastructure validated end-to-end; full parity gate deferred to Phase 3.5.
**Date:** 2026-05-12
**Reference:** [Phase 3 design spec](../../superpowers/specs/2026-05-11-phase3-pnl-parity-design.md), [capture runbook](../qc-aapl-phase3-capture-runbook.md)
**Fixture:** `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/` (PR #219)
**Captured QC backtest:** "Hipster Yellow Bat" — project 31452310, algorithm id `748c9f9f400b777443a57289ba4468b7`

## What was reconciled

Our engine's `StrategySpec` for single-symbol AAPL (PredictionComparison entry/exit, `SetHoldings(1.0)`, `fill_mode="next_bar_open"`) running against QC's captured minute-bar price history, with predictions from PR #215's `qc_export.json`. Window: 2026-02-09 → 2026-02-12.

QC's backtest produced **1 fill**: BUY 365 AAPL @ $273.238 on 2026-02-10 14:31 UTC (09:31 ET), fee $1.83.

Our engine produced **0 closed round-trips** in `LoggedTrade`. The engine signals on 2026-02-10's daily close and queues a `NEXT_BAR_OPEN` fill for 2026-02-11; the position never closes inside the 4-day window, so no `LoggedTrade` is emitted (the engine logs only completed round-trips). The `OurFill` adapter therefore returns an empty list.

## Divergence report

| Category | Count | Note |
|---|---|---|
| `DECISION_MISMATCH` | 1 | (buy, 2026-02-10): QC=yes, ours=no |
| `FIXTURE_INSUFFICIENT` | 0 | Minute audit clean — QC's fill price ($273.24) falls within the 09:31 minute bar's range [$273.05, $275.11] |
| All other gating categories | 0 | — |

**Propagated PnL atol:** $3.66 (one round-trip × ~$0.01 per share × ~365 shares × 2 fills, plus 2 × $0.01 fee atol)

## Why this is "infrastructure-validated, not parity-passed"

This is a known, documented mismatch baked into the design spec (§7 risk #5 and the Phase 3.5 escalation section):

1. **QC's algorithm uses `set_holdings @ 9:31 ET` with `Resolution.MINUTE`** — fills at minute T's bar inside the same trading day a prediction is for.
2. **Our engine's canonical fill mode is `NEXT_BAR_OPEN` on `Resolution.DAILY`** — signal at daily close of T → fill at daily open of T+1.

These two semantics differ by exactly one trading day. No fixture tweak can close this on our side without changing the engine's fill model. Phase 3.5 takes one of two paths:

- **Path A — Add an intraday-trigger fill mode** (e.g. `INTRADAY_OPEN_FILL`) that signals on daily-close prediction and fills at the same trading day's minute-bar open. Closer match to QC's actual semantics, more engine code, requires minute-resolution data subscription support.
- **Path B — Document the one-day fill-date offset as an accepted divergence** and reconcile P&L against `(QC's day-T fill, our day-(T+1) fill)` with explicit timing-offset tolerance. Cheaper, less satisfying.

Phase 3.0 ships the reconciler infrastructure validated against a real captured QC fixture; the acceptance gate flips to "passed" when Phase 3.5 lands. The acceptance test is marked `xfail(strict=True)` so a future engine change that produces a real pass surfaces as `XPASS` and forces explicit removal of the `xfail` mark.

## What the 1-day fixture *does* validate

| Component | Validated by |
|---|---|
| `FixtureDataReader` (minute resolution) | `is_minute_resolution=True` detected; minute bars parsed and indexed correctly |
| `FixtureDataReader.find_bar_containing` | QC fill at 2026-02-10 09:31 ET correctly resolves to the 09:31 minute bar |
| `_audit_fixture` minute-mode branch | Fill price $273.238 falls within [$273.048, $275.106] → no `FIXTURE_INSUFFICIENT` |
| `_parse_qc_orders` (canonical schema) | Hand-derived qc_orders.json parses cleanly |
| `_align_fills` + `_classify_divergences` | Emits the predicted `DECISION_MISMATCH` for the lone unmatched QC fill |
| `IbkrEquityCommissionModel` | Reconciler computes IBKR fee for the captured fill (diagnostics) |
| End-to-end report rendering | `qc-aapl-phase3-latest.md` written to `artifacts/reconciliations/` per design §3.4 |
| Prediction-set import + spec wiring | `import_qc_fixture` + `run_strategy_spec` integrate cleanly via temp `LEARN_AI_PREDICTION_ARTIFACTS_ROOT` |

## Tolerances accepted

All defaults from `Tolerances.phase3_default()` — no loosening.

## Open follow-ups

- **Phase 3.5**: capture multi-day fixture (2026-02-10 → 2026-03-12 matching PR #215 prediction-set window). Choose Path A or Path B per the rationale above.
- **Branch A vs B**: This fixture is Branch A (`orderFeeAmount = 1.83` non-zero). `assert_fees=True` is wired in the test.
- **Phase 4**: multi-symbol top-N ranking. Cleanly orthogonal to Phase 3.5.

## How to re-run

```bash
podman exec polygon-data-service python -m pytest \
  /app/tests/research/parity/test_qc_aapl_phase3_trade_parity.py -v -s \
  --write-recon-report
```

The report lands at `PythonDataService/artifacts/reconciliations/qc-aapl-phase3-latest.md` (gitignored). The committed summary here is the human-authored interpretation.
