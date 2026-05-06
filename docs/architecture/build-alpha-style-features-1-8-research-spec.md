# Build Alpha-Style Research Pipeline - Features 1-8

**Status:** Research spec, pre-implementation
**Date:** 2026-05-06
**Audience:** Claude / future implementation agent
**Primary test strategy:** SPY EMA crossover
**Scope:** Features 1-8 of the Build Alpha-style roadmap
**Out of scope:** Portfolio mode, automated genetic programming, live trading

This document is a self-contained handoff for researching and planning the
next direction of learn-ai: a Build Alpha-style validation and strategy
research pipeline. It is not a request to clone Build Alpha internals. Treat
Build Alpha as product inspiration from public pages and design the learn-ai
version from this repo's own architecture, numerical rules, and validation
culture.

## Claude Task

Use this document to produce a detailed implementation plan for features 1-8.
Do not jump directly to genetic programming or automated alpha discovery. The
first milestone is a rigorous validation railroad that can judge a simple EMA
crossover strategy before it judges generated strategies.

Your output should include:

1. A phased engineering plan with file/module ownership.
2. Data contracts for requests, responses, stored runs, and validation outputs.
3. API shape recommendations for FastAPI and, only where needed, GraphQL
   passthroughs.
4. Test strategy, golden fixtures, tolerances, and regression gates.
5. UI workflow notes for Engine Lab or a future Research Lab.
6. A concrete EMA crossover acceptance plan.
7. Explicit updates needed to `docs/math-sources-of-truth.md` and
   `docs/architecture/engine-authority-map.md` when math or engine ownership
   changes.

## Non-Negotiable Repo Constraints

- Python owns all numerical math. .NET is transport and persistence. Angular is
  visualization only.
- Do not create a parallel backtesting engine. Extend the canonical Python
  engine and the `StrategySpec` layer.
- Every timestamp crossing a boundary must be `int64 ms UTC`. ISO strings,
  `DateTime`, and browser `Date` objects are not valid wire/storage formats.
- No silent data repair. Duplicate, non-monotonic, or ambiguous timestamps fail
  fast.
- Every new numerical claim needs a fixture, a test, a tolerance, and a
  reference/provenance note.
- Any reference-inspired implementation must be sovereign: reference code can
  inform one-time fixture generation, but runtime cannot depend on the
  reference.
- Do not introduce dependencies without a written alternative and rejection
  reason.
- Do not leave `print`, `console.log`, or `Console.WriteLine` in committed code.

## Existing learn-ai Anchors

Use these as the starting point:

- `PythonDataService/app/engine/` - canonical event-driven backtesting engine.
- `PythonDataService/app/engine/strategy/spec/` - declarative strategy schema
  and `SpecAlgorithm` evaluator.
- `PythonDataService/app/research/signal/` - signal research, walk-forward, and
  graduation machinery.
- `PythonDataService/app/research/validation/` - IC, quantile, and robustness
  validation helpers.
- `docs/math-sources-of-truth.md` - concept-level numerical authority registry.
- `docs/architecture/engine-authority-map.md` - engine-level ownership map.
- `docs/architecture/numerical-authority-migration-plan.md` - current migration
  context.

The current Strategy Spec layer is already a strong foundation: it is
Pydantic-validated, parity-pinned against hand-coded strategies, exposed via
FastAPI, and passed through GraphQL without .NET revalidating the math.

## External Inspiration Baseline

Public Build Alpha pages describe these relevant feature categories:

- Automatic strategy generation and a large signal library:
  https://www.buildalpha.com/
- Output interface, automated workflows, robustness filtering, noise tests,
  randomized OOS, custom signals, parameter optimization, correlation, and
  portfolio tools:
  https://www.buildalpha.com/buildalpha-features/
- Monte Carlo methods including reshuffle, resample, randomized, permutation,
  equity bands, and drawdown analysis:
  https://www.buildalpha.com/monte-carlo-simulation/

Use those as feature inspiration only. The learn-ai design should be grounded
in LEAN parity, canonical Python math, explicit run provenance, and testable
numerical contracts.

## Sequencing Thesis

Build the validation railroad before the alpha factory.

The most tempting feature is automated strategy generation, but generated
strategies are only useful if the app can:

- reproduce the exact run later,
- explain what rules fired,
- separate train from test,
- compare against random baselines,
- survive perturbations,
- quantify drawdown and path uncertainty,
- detect parameter islands,
- and reject complexity that does not buy robustness.

Therefore features 1-8 are the required substrate for later portfolio mode and
automated alpha discovery.

## Primary Golden Path: EMA Crossover

The first complete test case should be:

- Symbol: `SPY`
- Bar resolution: 15-minute regular session bars
- Timezone semantics: exchange time internally, `int64 ms UTC` at boundaries
- Entry: EMA(5) crossing EMA(10), optionally filtered by RSI(14)
- Exit: 5-bar hold or declarative exit rule from `StrategySpec`
- Fill: documented canonical engine fill model
- Costs: explicit zero-cost baseline plus configurable bps/per-share cost

EMA crossover is not meant to prove alpha. It is a control strategy for proving
that the pipeline can run, store, validate, stress, and explain a strategy.

## Feature 1 - Canonical Strategy Spec And Run Ledger

### Goal

Make every strategy research run reproducible, comparable, and auditable.

### Why It Comes First

All later features depend on stable identity. A Monte Carlo test, OOS split, or
noise test is meaningless unless it points back to an exact strategy spec, data
window, engine version, fill model, and random seed.

### Design Intent

Use `StrategySpec` as the only declarative strategy format for this workflow.
The run ledger records the immutable facts of a run, not just its headline
metrics.

### Proposed Run Ledger Fields

- `run_id`
- `strategy_spec_id`
- `strategy_spec_hash`
- `strategy_spec_json`
- `engine_name`
- `engine_version`
- `engine_git_commit`
- `symbol`
- `resolution`
- `start_ms`
- `end_ms`
- `data_source`
- `data_snapshot_id` or equivalent provenance pointer
- `fill_model`
- `commission_model`
- `slippage_model`
- `warmup_policy`
- `random_seed`
- `created_at_ms`
- `result_hash`
- `trade_log_hash`
- `metrics_hash`
- `status`
- `failure_reason`

### Research Questions

- Is the ledger persisted in Postgres immediately, or is v1 a file-backed
  research artifact under `PythonDataService/artifacts/`?
- Should run hashing include the raw input bars hash, or a data snapshot
  identifier plus validation metadata?
- What is the minimum data snapshot contract needed to make "same run later"
  scientifically meaningful?
- Does the existing job/SSE infrastructure already provide enough lifecycle
  semantics for long-running validations?

### Implementation Direction

- Add a run metadata model in Python first.
- Add persistence only after the JSON contract is stable.
- Keep .NET as a passthrough if GraphQL exposure is needed.
- Do not let Angular compute or mutate any run identity fields.

### EMA Acceptance Gate

Given the same EMA crossover spec, data window, and seed:

- repeat runs produce identical trades,
- repeat runs produce identical metric outputs,
- the run ledger hashes match,
- changing one spec parameter changes the spec hash,
- changing one data bar changes the input/data hash or snapshot identity.

### Tests

- Unit test stable canonical JSON serialization for `StrategySpec`.
- Unit test hash changes on parameter change.
- Integration test: run EMA crossover twice and assert identical ledger hashes.
- Regression test: run with one modified cost value and assert only expected
  identity/metric fields change.

## Feature 2 - Signal And Feature Library

### Goal

Build a curated, provenance-aware signal library that `StrategySpec`,
research scoring, and future discovery can all use.

### Why It Comes Second

A strategy generator is only as good as its ingredients. Even without
generation, users need an explicit catalog of safe, tested features that can be
selected, combined, validated, and explained.

### Candidate Signal Families

- Price action: returns, gaps, ranges, breakouts, inside/outside bars.
- Trend: SMA, EMA, MACD, ADX, Supertrend.
- Mean reversion: RSI, Bollinger Band position, z-scores.
- Volatility: ATR, realized volatility, volatility regime z-score.
- Time/session: day of week, month, intraday time window, first/last bar rules.
- Options/IV: IV rank, IV percentile, IV30, skew, term structure where already
  supported by Python options authorities.
- Cross-asset/regime: SPY/QQQ/IWM filters, VIX-like proxy if data exists,
  treasury/credit/sentiment only after data provenance exists.
- Custom Python signals: deferred until the validation and sandbox contract is
  designed.

### Required Signal Metadata

Each signal should declare:

- `name`
- `family`
- `description`
- `input_columns`
- `output_type`: boolean, scalar, categorical, or event
- `warmup_bars`
- `timestamp_alignment`
- `lookahead_safe`: true/false with reason
- `parameter_schema`
- `default_parameters`
- `canonical_module`
- `reference`
- `validated_against`
- `known_limitations`

### Research Questions

- Should the existing `app/research/features/registry.py` and
  `app/engine/strategy/spec/indicators.py` converge into one registry, or
  should one wrap the other?
- How should streaming engine indicators and vectorized research features be
  reconciled when they share names but not execution semantics?
- Which features are LEAN-pinned and which are pandas-ta or internal research
  features?
- How should feature provenance appear in the UI without overwhelming the user?

### Implementation Direction

- Start with a read-only signal catalog endpoint.
- Require every `StrategySpec` primitive to resolve through catalog metadata.
- Add a provenance row to `docs/math-sources-of-truth.md` for new numerical
  concepts.
- Keep vectorized research features and streaming engine indicators distinct
  where their semantics differ.

### EMA Acceptance Gate

The signal catalog must expose:

- EMA(5)
- EMA(10)
- RSI(14), if the filter is included
- crossover primitive
- hold-period exit primitive
- all warmup/alignment metadata used by the EMA strategy

### Tests

- Catalog schema snapshot test.
- Warmup metadata test against EMA/RSI indicator behavior.
- StrategySpec validation test rejects unknown signal names.
- StrategySpec round-trip test preserves signal parameters exactly.

## Feature 3 - Backtest Results Workbench

### Goal

Create the strategy results surface: the place where a researcher can inspect,
compare, sort, and explain runs.

### Why It Comes Third

Before adding robustness tests, the app needs one trustworthy way to display a
plain backtest and its trace.

### Required Result Views

- Equity curve by timestamp.
- Drawdown curve.
- Trade list with entry/exit timestamps, prices, size, P&L, hold bars, reason.
- Metrics panel: total return, CAGR if supported, Sharpe, Sortino if supported,
  max drawdown, win rate, average trade, profit factor, exposure, trade count.
- Rule display: human-readable strategy spec.
- Run provenance: data window, fill/cost models, engine version, spec hash.
- IS/OOS tabs once Feature 4 exists.
- Validation status badges once Features 4-8 exist.

### Research Questions

- Which metrics are already canonical in Python, and which are still legacy or
  pending migration?
- Should result sorting/filtering happen in Python or only on already-returned
  rows in Angular?
- How much run history should the first UI show before persistence is complete?
- What is the minimum result DTO that supports later Monte Carlo, noise, and
  baseline results without schema churn?

### Implementation Direction

- Define a Python `BacktestRunResult` DTO.
- Keep metric calculations in Python only.
- Let Angular render charts and format numbers, but not compute metrics.
- Reuse Engine Lab visual conventions where possible.

### EMA Acceptance Gate

For a completed EMA crossover run, the workbench shows:

- the exact rule,
- the equity curve,
- the drawdown curve,
- the full trade log,
- run provenance,
- and all headline metrics sourced from Python.

### Tests

- DTO serialization test uses `int64 ms UTC` timestamps only.
- Backend/GraphQL passthrough test preserves numeric precision if used.
- Frontend test verifies no metrics are computed in Angular.
- Regression fixture for EMA run result shape.

## Feature 4 - Walk-Forward And OOS Validation

### Goal

Turn a backtest into a train/test experiment with frozen decisions and
transparent OOS retention.

### Why It Comes Fourth

This is the first serious defense against overfit strategies. It also creates
the discipline later automated discovery must obey.

### Required Validation Modes

- Chronological train/test split.
- Rolling walk-forward windows.
- Anchored walk-forward windows, if compatible with existing period splitters.
- Frozen-parameter OOS evaluation.
- OOS retention ratio.
- Fold-level OOS Sharpe, drawdown, return, trade count.
- Alpha-decay slope across folds.

### Research Questions

- Can `PythonDataService/app/research/signal/walk_forward.py` be generalized
  from feature threshold research to full `StrategySpec` backtests?
- Which parameters are allowed to optimize in train? EMA windows? thresholds?
  exit bars? cost assumptions should not be optimized.
- How should the app prevent leakage when selecting the "best" strategy across
  many tested candidates?
- Should the first milestone support optimizing EMA windows, or only
  validating a fixed spec across folds?

### Implementation Direction

Milestone 4A should validate a fixed `StrategySpec` across folds.

Milestone 4B can add parameter-grid selection on train, then freeze the chosen
parameters on test.

Do not implement genetic search here.

### Suggested Output Contract

- `validation_id`
- `parent_run_id`
- `split_policy`
- `folds[]`
  - `fold_index`
  - `train_start_ms`
  - `train_end_ms`
  - `test_start_ms`
  - `test_end_ms`
  - `selected_parameters`
  - `train_metrics`
  - `test_metrics`
  - `test_trade_count`
- `combined_oos_equity_curve`
- `mean_oos_sharpe`
- `median_oos_sharpe`
- `pct_profitable_folds`
- `oos_retention`
- `alpha_decay`
- `warnings`

### EMA Acceptance Gate

Run EMA crossover across rolling folds:

- each fold reports train/test windows with integer ms timestamps,
- the same frozen EMA parameters are used in test for milestone 4A,
- milestone 4B selects EMA windows on train and freezes them on test,
- combined OOS curve is built only from test folds.

### Tests

- Fold boundary tests.
- No-overlap train/test tests.
- Fixed-spec fold replay test.
- Parameter-freeze test for EMA windows.
- Regression test for insufficient folds warning.

## Feature 5 - Monte Carlo Risk Lab

### Goal

Estimate path uncertainty, drawdown ranges, streak risk, and live health bands
from a strategy's trade distribution.

### Why It Comes Fifth

After OOS validation, the next question is not "what was the backtest result?"
It is "what range of future paths would still be normal for this strategy?"

### Required Methods

- Trade reshuffle: same trades, different order.
- Trade resample: sample trades with replacement.
- Forward N-trade projection.
- Drawdown distribution and confidence intervals.
- Equity curve bands, typically 5th/95th percentile.
- Winning and losing streak distribution.
- Probability of breaching a drawdown threshold.

Price-path permutation should be deferred to Feature 6 because it changes the
input data rather than only trade order.

### Research Questions

- Should Monte Carlo run from trade P&L, bar returns, or both?
- How should position sizing affect resampling? Fixed-dollar, fixed-share, or
  percent-equity sizing have different path dependencies.
- What is the default simulation count for local development: 100, 1,000, or
  configurable with warnings?
- How should deterministic seeds be recorded in the run ledger?

### Implementation Direction

- Implement deterministic Python Monte Carlo utilities.
- Require explicit seed and simulation count.
- Return quantiles, not only averages.
- Record `mc_config_hash` and `seed` in validation metadata.

### Suggested Output Contract

- `monte_carlo_id`
- `parent_run_id`
- `method`: reshuffle or resample
- `seed`
- `simulation_count`
- `projection_trade_count`
- `equity_bands`
- `drawdown_quantiles`
- `terminal_pnl_quantiles`
- `max_losing_streak_quantiles`
- `breach_probabilities`
- `warnings`

### EMA Acceptance Gate

Using EMA crossover trades:

- reshuffle preserves the exact set of trade P&Ls,
- resample allows repeats,
- same seed produces identical simulations,
- changing seed changes path samples but not source trade distribution,
- output includes 5th/50th/95th terminal P&L and max drawdown.

### Tests

- Determinism test by seed.
- Reshuffle preserves multiset of trades.
- Resample permits duplicate trades.
- Quantile calculation test against small hand-derived fixture.
- Empty/low-trade-count warning test.

## Feature 6 - Noise, Synthetic, And Shifted-Data Tests

### Goal

Detect strategies that depend on lucky ticks, exact bar boundaries, or one
historical path.

### Why It Comes Sixth

Monte Carlo over trades tests path ordering. Noise and synthetic tests stress
the input market data itself.

### Required Tests

- OHLC jitter/noise test.
- Shifted-bar boundary test.
- Slippage perturbation test.
- Cost perturbation test.
- Optional: volatility-scaled noise regimes.
- Deferred: Monte Carlo price-path permutation, because it needs deeper
  mathematical review and explicit provenance.

### Research Questions

- What is the correct noise model for OHLC bars without violating high/low
  invariants?
- Should noise be applied to log returns, close-to-close returns, or OHLC
  levels?
- How do we preserve `high >= max(open, close)` and
  `low <= min(open, close)` after perturbation?
- What bar shifts are meaningful for 15-minute RTH data: 1 minute, 5 minutes,
  or alternate consolidation anchors?
- What should "pass" mean: positive mean result, bounded degradation, or
  percentile rank vs original?

### Implementation Direction

- Start with small, deterministic perturbation modules in Python.
- Treat synthetic data generators as validation tools, not market data
  replacements.
- Store synthetic-test configs and seeds in the ledger.
- Every generated series must pass bar invariants and timestamp monotonicity.

### Suggested Output Contract

- `robustness_test_id`
- `parent_run_id`
- `test_type`: noise, shifted_bar, slippage, cost
- `seed`
- `series_count`
- `perturbation_config`
- `metric_distribution`
- `pass_rate`
- `original_metric_rank`
- `worst_case_metrics`
- `warnings`

### EMA Acceptance Gate

For EMA crossover:

- run on at least N deterministic jittered series,
- run on at least one shifted-bar variant,
- report distribution of return, Sharpe, drawdown, and trade count,
- show whether original performance is an outlier relative to perturbed runs,
- fail if perturbed bars violate OHLC invariants.

### Tests

- OHLC invariant preservation test.
- Timestamp preservation/monotonicity test.
- Deterministic generation by seed.
- Shifted-bar consolidation boundary test.
- EMA robustness result shape fixture.

## Feature 7 - Null Baselines And Distribution Comparison

### Goal

Answer: did this strategy beat something naive, random, and cheap to produce?

### Why It Comes Seventh

A strategy that passes OOS and Monte Carlo may still be unremarkable if random
rules or buy-and-hold perform similarly. Null baselines provide the empirical
yardstick.

### Required Baselines

- Buy-and-hold for the same symbol/window.
- Random entries with matched trade count and holding period.
- Randomized signal timestamps.
- Random EMA window pairs within a bounded parameter family.
- Random strategy specs from the same primitive set, once safe.
- Cross-symbol naive baseline, where data exists.

### Distribution Comparisons

- Strategy metric percentile vs null distribution.
- Empirical p-value.
- Distribution histogram/violin for Sharpe, return, drawdown, profit factor.
- Edge decay / E-ratio-style favorable vs adverse excursion by bars after
  signal.
- Signal breakdown: which rules appeared before winning vs losing trades.

### Research Questions

- Which null baseline is fair for EMA crossover: random entries, random EMA
  windows, or both?
- Should null baselines preserve trade count exactly?
- Should null entries obey session and position constraints?
- How do we account for multiple testing when comparing many strategy
  candidates to null distributions?
- Can existing feature validation multiple-testing machinery be reused?

### Implementation Direction

- Implement null baselines as Python validation modules.
- Make every null run point back to the same data snapshot and cost/fill model.
- Preserve session rules and no-overlap/position constraints.
- Return empirical ranks rather than only "pass/fail."

### Suggested Output Contract

- `null_test_id`
- `parent_run_id`
- `baseline_type`
- `seed`
- `sample_count`
- `matched_constraints`
- `target_metrics`
- `null_metric_distribution`
- `empirical_percentiles`
- `empirical_p_values`
- `baseline_warnings`

### EMA Acceptance Gate

For EMA crossover:

- compare original EMA run against buy-and-hold,
- compare against random entries with same trade count and hold bars,
- compare against random EMA parameter pairs,
- report percentile rank for Sharpe, max drawdown, profit factor, and return,
- show whether the rule is meaningfully better than its null family.

### Tests

- Random-entry baseline preserves trade count constraints.
- Random EMA baseline samples only allowed window ranges.
- Empirical p-value calculation against hand-derived small sample.
- Determinism by seed.
- Baseline output schema fixture.

## Feature 8 - Parameter Sensitivity And Parsimony Scoring

### Goal

Detect fragile parameter islands and penalize unnecessary complexity.

### Why It Comes Eighth

This is the last major gate before automated discovery. Without sensitivity
and parsimony, a search engine will over-reward clever but brittle rule trees.

### Required Capabilities

- 1D parameter sweep.
- 2D parameter surface, especially EMA fast/slow windows.
- Neighbor survival score.
- Robust plateau detection.
- Complexity score based on rule count, tree depth, parameter count, and
  custom feature count.
- Parsimony-adjusted fitness score.
- Warning for strategies that only work at one narrow parameter value.

### Research Questions

- What complexity score is fair for `StrategySpec`?
- Should parsimony be part of validation only, or part of search fitness later?
- How should parameter surfaces handle invalid combinations, such as
  `fast_ema >= slow_ema`?
- Which metric drives sensitivity: Sharpe, PnL, OOS Sharpe, drawdown-adjusted
  return, or a multi-objective score?
- How many neighboring points are required before calling a plateau robust?

### Implementation Direction

- Start with grid sweeps over named StrategySpec parameters.
- Validate every generated spec through Pydantic before running.
- Cache/reuse run ledger records where possible.
- Compute sensitivity from OOS metrics when Feature 4 is available; otherwise
  label it in-sample only.

### Suggested Output Contract

- `sensitivity_id`
- `parent_spec_hash`
- `parameter_space`
- `metric_name`
- `grid_results`
- `best_point`
- `original_point`
- `neighbor_survival_score`
- `plateau_score`
- `complexity_score`
- `parsimony_adjusted_score`
- `warnings`

### EMA Acceptance Gate

For EMA crossover:

- sweep `fast_ema` from 3 to 12,
- sweep `slow_ema` from 8 to 30,
- reject invalid `fast >= slow` points,
- report original 5/10 point rank,
- report whether performance survives neighboring values,
- produce a heatmap-ready grid result sourced from Python.

### Tests

- Parameter-grid generation test.
- Invalid combination rejection test.
- StrategySpec mutation/round-trip test.
- Plateau score test against a small synthetic grid.
- Complexity score test for one-rule vs multi-rule specs.

## Cross-Feature Graduation Ladder

The end state of features 1-8 should be a strategy graduation score, not a
single "passed" boolean.

Suggested stages:

- Stage 0 - Rejected: missing provenance, insufficient trades, invalid data,
  failed OOS, or clear null-baseline failure.
- Stage 1 - Reproducible: run ledger, deterministic replay, valid result
  surface.
- Stage 2 - Research candidate: OOS retention, Monte Carlo risk, and baseline
  rank are acceptable.
- Stage 3 - Robust candidate: survives noise/shifted data, parameter surface is
  broad, complexity is justified.
- Stage 4 - Ready for portfolio evaluation: eligible for future Feature 9
  portfolio mode.

This ladder should cite the exact failed criteria. Avoid one opaque health
score in v1.

## Suggested Phase Plan

### Phase A - Reproducible EMA Run

Build Feature 1 and enough of Feature 3 to run and inspect EMA crossover.

Exit:

- EMA StrategySpec persists/serializes deterministically.
- Run ledger exists.
- Result workbench DTO exists.
- Repeat runs hash identically.

### Phase B - Feature Catalog And Result Workbench

Build Feature 2 and the full Feature 3 result surface.

Exit:

- Catalog exposes EMA/RSI/crossover/hold primitives.
- EMA result displays equity, drawdown, trades, metrics, provenance.

### Phase C - OOS And Walk-Forward

Build Feature 4 against fixed EMA spec, then optional train-only EMA window
selection.

Exit:

- Folded OOS output exists.
- Combined OOS equity uses only test windows.
- Alpha decay warning exists for insufficient folds.

### Phase D - Monte Carlo And Noise

Build Features 5 and 6.

Exit:

- EMA has deterministic MC bands.
- EMA has deterministic jitter/shifted-bar robustness distribution.

### Phase E - Baselines And Sensitivity

Build Features 7 and 8.

Exit:

- EMA is ranked against random entries, random EMA windows, and buy-and-hold.
- EMA parameter surface exists.
- Parsimony-adjusted score exists.

## Minimal API Sketch

Prefer Python/FastAPI as the source of truth.

Potential endpoints:

- `POST /api/research/strategy-runs`
- `GET /api/research/strategy-runs/{run_id}`
- `POST /api/research/strategy-runs/{run_id}/walk-forward`
- `POST /api/research/strategy-runs/{run_id}/monte-carlo`
- `POST /api/research/strategy-runs/{run_id}/robustness`
- `POST /api/research/strategy-runs/{run_id}/null-baselines`
- `POST /api/research/strategy-specs/sensitivity`
- `GET /api/research/signal-catalog`

GraphQL passthroughs are acceptable only if the frontend route already depends
on GraphQL. They must preserve Python response numbers and not recompute.

## UI Direction

The UI should be a research workbench, not a marketing page.

Primary layout:

- Left rail or top tabs for Run, OOS, Monte Carlo, Robustness, Baselines,
  Sensitivity.
- Main panel for charts/tables.
- Right panel for run provenance and warnings.
- Badges for stage and failed criteria.
- No visible educational text explaining obvious controls.
- Dense, scan-friendly, quiet operational style.

Angular must:

- display charts,
- sort/filter returned result rows,
- format numbers,
- and submit user configs.

Angular must not:

- compute metrics,
- compute drawdowns,
- compute Monte Carlo distributions,
- compute parameter surfaces,
- or transform timestamp semantics.

## Documentation Updates Required Per Feature

For each feature that introduces or moves numerical authority:

- Update `docs/math-sources-of-truth.md`.
- Update `docs/architecture/engine-authority-map.md`.
- Add or update `docs/references/<construct>.md` for reference/provenance.
- Add golden fixtures under
  `PythonDataService/tests/fixtures/golden/<construct-name>/` where numerical
  equivalence or deterministic outputs are claimed.

## Open Decisions For Claude To Resolve

1. File-backed vs database-backed run ledger for v1.
2. Unified feature catalog vs separate engine/research registries.
3. Fixed StrategySpec walk-forward vs parameter-selection walk-forward in the
   first implementation milestone.
4. Exact metric set for the first result DTO.
5. Default Monte Carlo simulation count and warning thresholds.
6. First OHLC noise model and invariant enforcement.
7. Fair null baselines for EMA crossover.
8. Complexity/parsimony formula for StrategySpec.

## Final Direction

The first success state is not "learn-ai discovers alpha." The first success
state is:

> Given a simple EMA crossover, learn-ai can reproduce it, explain it, split it,
> stress it, compare it to nulls, test its parameter neighborhood, and tell the
> user whether the result deserves further research.

Once that is true, portfolio mode and automated strategy discovery can be built
on a disciplined foundation instead of on a pile of attractive backtests.
