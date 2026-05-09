# Build Alpha Functionality Validation Charter

**Mode:** `build-alpha-validation-pending`  
**Target date:** 2026-05-09 morning local time  
**Primary goal:** validate recently added Build Alpha-style alpha-research features by Playwright visual snapshots, API/number tracing, and quant-style interpretation.  
**Prime directive:** accuracy and functional correctness beat breadth.

This charter is for the Claude auto-research tick. It should produce a morning report that says exactly what was tested, with what parameters, what the app displayed, where the Playwright screenshots/snapshots are, whether the numbers agree with Python-owned outputs, and what an architect should fix or build next.

## Inputs To Read First

- `.claude/skills/auto-research-tick/SKILL.md`
- `docs/architecture/build-alpha-style-features-1-8-research-spec.md`
- `docs/audits/auto-research/state.json`
- Relevant implementation files discovered by static search, especially:
  - `Frontend/src/app/components/research-lab/`
  - `Backend/GraphQL/Types/*Research*`
  - `Backend/Models/DTOs/*Research*`
  - `Backend/Services/*Research*`
  - `PythonDataService/app/research/`
  - `PythonDataService/app/engine/strategy/spec/`

Do not assume all eight features are implemented. Discover what exists, then validate the actual behavior.

## Report File

Write the report to:

`docs/audits/auto-research/runs/YYYY-MM-DD-build-alpha-functionality-validation.md`

Use this structure:

1. Executive verdict
2. Environment and services checked
3. Feature coverage matrix
4. Parameter matrix tried
5. Visual inspection notes
6. Playwright screenshots/snapshots
7. Numerical trace and display parity
8. Quant conclusions
9. Architect recommendations
10. Blockers and unvalidated areas
11. Appendix: commands/endpoints/screens inspected

## Validation Method

For each implemented feature:

1. Identify the UI route/component and API endpoint or GraphQL resolver.
2. Run or inspect the feature using the EMA control strategy where applicable.
3. Capture Playwright screenshots/snapshots of what the UI shows: populated charts, tables, cards, warnings, empty states, crashes, inconsistent labels.
4. Trace displayed numbers to Python/API output. UI formatting may round, but Angular must not recompute strategy signals, P&L, statistics, Monte Carlo distributions, OOS folds, null distributions, or sensitivity scores.
5. Decide the verdict using these statuses:
   - `validated`
   - `partially validated`
   - `not implemented`
   - `not run: dependency unavailable`
   - `failed functional correctness`
   - `failed numerical correctness`

Do not count a screen as validated just because it renders. A valid screen has coherent data, credible numbers, and a clear source-of-truth trace.

## Playwright Snapshot Requirement

Use Playwright for visual evidence whenever an existing Playwright runtime or Claude/browser automation tool is available.

Allowed:

- An existing Claude/browser tool backed by Playwright.
- An already-installed local Node package that resolves `playwright` or `@playwright/test`.
- An already-available `playwright` CLI on PATH.

Not allowed:

- Installing Playwright.
- Running package/browser downloads during the audit.
- Restarting containers to make screenshots pass.

If Playwright is not available, the report must say `not run, Playwright unavailable` in the visual evidence section and continue with API/static validation.

Save screenshots under:

`docs/audits/auto-research/snapshots/YYYY-MM-DD/<feature>-<state>.png`

Use desktop viewport `1440x1000` as the primary viewport. Capture full-page screenshots where practical. For each implemented screen, capture at least:

- initial loaded state,
- configured EMA control parameters before run,
- completed result state,
- any error or empty state.

Each screenshot entry in the report must include:

- file path,
- route/URL,
- viewport,
- interaction sequence,
- data state (`loaded`, `running`, `complete`, `error`, `empty`),
- visual conclusion.

## Default Control Strategy

Use the default control strategy unless the implemented surface only supports a narrower configuration. Record every substitution.

| Parameter | Default |
|---|---|
| Symbol | `SPY` |
| Resolution | 15-minute regular-session bars |
| Entry | EMA(5) crosses EMA(10) |
| Optional filter | RSI(14), thresholds 30/70, only if supported |
| Exit | 5-bar hold or implemented declarative hold exit |
| Fill model | documented canonical engine fill model |
| Cost model | zero-cost baseline; plus 1 bps/slippage sensitivity if supported |
| Seeds | 42 for deterministic validations; 43 for stochastic-change sanity check |
| Data window | use the implemented default or the shortest window that produces enough trades; record exact start/end ms |

## Feature-Specific Checks

### Feature 1 - Strategy Spec And Run Ledger

- Same spec, data window, and seed should reproduce the same trades and hashes.
- Changing one strategy parameter should change the spec hash.
- Changing cost/slippage should change only the expected output fields.
- Ledger/provenance should include strategy spec, engine version or commit, symbol, resolution, start/end ms, fill/cost model, seed, result/trade/metric hash if implemented.

### Feature 2 - Signal Catalog

- Catalog exposes EMA(5), EMA(10), RSI(14) if used, crossover primitive, and hold-period exit.
- Metadata includes warmup bars, timestamp alignment, parameter schema, canonical module, reference, and validation status where implemented.
- Unknown signal names should be rejected before running.

### Feature 3 - Backtest Results Workbench

- UI shows exact rule, equity curve, drawdown curve, trade list, metrics, and provenance.
- Headline metrics must come from Python/API output.
- Compare UI-displayed total return, Sharpe, max drawdown, win rate, profit factor, exposure, and trade count to source values. Use explicit display-rounding tolerance.
- Empty cards, NaN, undefined, snake_case/camelCase mapping bugs, or chart/table disagreement are functional failures.

### Feature 4 - Walk-Forward And OOS

- Fold train/test windows must be non-overlapping and use int64 ms UTC.
- Fixed-spec validation should use the same EMA parameters in every test fold.
- If train-optimization is implemented, selected parameters must be frozen before test evaluation.
- Combined OOS curve must be built only from test windows.
- Report OOS retention, mean/median OOS Sharpe, profitable-fold percentage, fold trade counts, and warnings.

### Feature 5 - Monte Carlo Risk Lab

- Same seed reproduces identical simulations.
- Different seed changes sampled paths but not the source trade distribution.
- Reshuffle preserves the multiset of source trade P&Ls.
- Resample permits duplicates.
- Report terminal P&L quantiles, drawdown quantiles, losing-streak quantiles, breach probabilities, and simulation count.
- Classify small simulation counts as smoke evidence, not final statistical confidence.

### Feature 6 - Noise, Shifted-Data, Slippage, And Cost Tests

- Perturbed OHLC bars must preserve `high >= max(open, close)` and `low <= min(open, close)`.
- Timestamps must remain int64 ms UTC and monotonic.
- Report distribution of return, Sharpe, drawdown, and trade count across variants.
- Explain whether original performance is an outlier relative to perturbations.

### Feature 7 - Null Baselines

- Compare EMA strategy against buy-and-hold if implemented.
- Compare against random entries with matched trade count/hold bars if implemented.
- Compare against random EMA parameter pairs if implemented.
- Report empirical percentile and p-value for Sharpe, max drawdown, profit factor, and total return.
- Note multiple-testing risk if many configurations were searched.

### Feature 8 - Parameter Sensitivity And Parsimony

- Sweep fast EMA 3-12 and slow EMA 8-30 if supported.
- Reject invalid `fast >= slow`.
- Report original 5/10 rank, best point, neighboring survival score, plateau score, complexity score, and parsimony-adjusted score if implemented.
- A one-point spike is fragile even if profitable; call this out.

## Quant Interpretation Rules

- Profitability alone is not correctness.
- Low trade count weakens all conclusions; say so plainly.
- A UI value that agrees after rounding can pass display parity, but the source field must still be Python/API-owned.
- Stochastic tests require seed recording. Missing seed provenance is a functional correctness issue.
- OOS and null-baseline results matter more than in-sample return.
- A good architect recommendation names the next smallest change that increases correctness or auditability.

## Completion State Update

After the report is written:

- Set `docs/audits/auto-research/state.json` `mode` to `build-alpha-validation-complete-awaiting-review`.
- Set `last_run` to the current ISO timestamp with offset.
- Set `cursor` to the report path plus a one-line verdict.
- Preserve the baseline finding arrays and baseline timestamps.

If blocked, keep `mode` as `build-alpha-validation-pending`, write the partial report, and put the blocker in `cursor`.
