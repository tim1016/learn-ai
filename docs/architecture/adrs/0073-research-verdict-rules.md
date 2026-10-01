# ADR 0073 — Research verdicts are fixed rules: a staged graduation ladder, a powered decay test, compounded walk-forward evidence, and a complete-or-nothing run grade

**Status:** Accepted 2026-09-30
**Provenance:** Entry 10 of [#2745](https://github.com/tim1016/learn-ai/issues/2745), from #2742 H. The graduation thresholds and the alpha-decay power guard were adopted from the external methodology review of 2026-04-30 and live in `docs/signal-engine-authority.md` §4–5, which is also served in the app as methodology help. The walk-forward split and curve choices are in `docs/references/walk-forward.md`. The run-verdict contract is decision 6 of the Engine Lab runs 75–76 statistics review (implemented 2026-08-08). Written by [#2749](https://github.com/tim1016/learn-ai/issues/2749).
**Vocabulary:** none owed — graduation stages, folds and readiness scores are research terms, outside the live trading and operator domain that `CONTEXT.md` covers.
**Related:** ADR 0056 D5 (the sweep-native walk-forward *study* verdict, which this ADR does not repeat), ADR 0068 (verdicts are Python-authored math), ADR 0069 (receipts and tolerances), ADR 0072 (run identity).

## Context

A verdict turns a pile of metrics into one answer: "keep researching this", "this decays", "this is ready". Without fixed rules, the answer is easy to game by accident. A missing metric can raise a score. A two-point regression can print a p-value. A rebased curve can hide whether a strategy compounded or only oscillated. Each rule below closes one of those holes. It is fixed in code, versioned, and changed only by a new decision.

## Decision

1. **Signal graduation is a staged ladder with a kill switch first** (`PythonDataService/app/research/signal/graduation.py::evaluate_graduation`). A signal sits at exactly one stage: the lowest stage it fails to advance from.
   - **Stage 0 rejects** if any one of these holds:
     - parameter stability is below 0.25;
     - median out-of-sample Sharpe is 0 or less;
     - fewer than 40% of OOS folds have a positive Sharpe;
     - annual turnover is above 200× **and** net Sharpe is below 0.5.

     A Stage 0 rejection short-circuits interpretation: the deeper panels are suppressed and shown only on explicit request.
   - **Stage 1 advances to Stage 2** when mean OOS Sharpe is above 0.3, stability is above 0.3, and there are at least 4 folds.
   - **Stage 2 advances to Stage 3** when mean OOS Sharpe is above 0.5, stability is above 0.5, and more than 60% of folds are positive.
   - **Stage 3 machinery is deliberately unbuilt:** a Deflated Sharpe above 0.5 on the in-sample grid, cross-asset confirmation, and Hansen SPA / White's Reality Check. No signal has reached Stage 3, so building it would be premature.
   - Stability is `1 − σ/|mean|` of net Sharpe across the threshold grid at a fixed cost (1 bps). A noise-fit signal's Sharpe swings across thresholds, and a real one stays flat.
   - The thresholds are the project defaults adopted from the 2026-04-30 review. A change moves the constants, the methodology doc and `tests/test_graduation.py` together, and is recorded here.
2. **The alpha-decay test needs at least 5 folds** (`app/research/signal/walk_forward.py`, `ALPHA_DECAY_MIN_FOLDS = 5`). The test regresses per-fold OOS Sharpe on fold index, `S_i = β₀ + β₁·i + ε_i`. A negative `β₁` significant at `p < 0.05` is read as decay. With 4 folds or fewer, the regression has at most 2 residual degrees of freedom and its t-test says almost nothing. So the test is marked invalid, and the UI shows that the trend test needs 5 or more folds instead of a misleading p-value.
3. **The spec-path walk-forward uses fixed split semantics and a compounded combined curve** (`app/research/walk_forward/`).
   - **Three split policies** (chronological, rolling, anchored) are validated when built. A degenerate input raises before the runner sees a window, rather than silently producing zero folds.
   - **Fold boundaries are half-open**, `[test_start_ms, test_end_ms)`. The runner turns the exclusive end into an inclusive end date, so a boundary day belongs to the later fold, never to both.
   - **The train window pre-rolls every stateful primitive with entries disabled until the test window starts.** This prevents a cold-start crossover on the first test bars. Metrics stay scoped to the test window.
   - **Positions are flat at test boundaries** (`fold_position_policy = "flat_at_test_boundaries"`), because each fold may select a different spec.
   - **Train-side selection** picks the winner by highest train Sharpe, then highest train return, then earliest declaration order. A candidate needs the minimum train-trade count and a non-null Sharpe. If no candidate is eligible, the analysis fails closed: no default is tried as a fallback.
   - **The combined OOS curve is compounded** (`runner.py::_compound_oos_curve`). Fold N+1 is scaled to start at fold N's terminal equity. Rejected: rebasing each fold to $1. Rebasing hides whether the strategy compounded or merely oscillated, breaks continuity at fold boundaries, and adds nothing that per-fold returns don't already carry. The compounded curve is cumulative OOS evidence, not a continuous brokerage statement, since positions reset at each fold.
4. **The run verdict is a fixed completeness contract: 17 required sub-scores, fixed weights, no reweighting** (`app/services/run_verdict_service.py`, verdict version 2).
   - The 17 required inputs are:
     - Return Quality: Sharpe, Sortino, CAGR, Calmar, annual volatility;
     - Risk Control: max drawdown, recovery, consecutive losses;
     - Trade Edge: profit factor, expectancy, win rate, payoff, fee drag;
     - Statistical Confidence: PSR, sample, skepticism, trade gap.
   - The dimension weights are frozen at 25/85, 20/85, 20/85 and 20/85, preserving the old scorer's relative intent.
   - **A composite, grade and deployment signal exist only when all 17 are present and valid.** Otherwise the verdict is `incomplete` and shows its coverage (for example 16/17). Unknown is never scored as zero, and a bad observed value stays distinct from an unavailable one.
   - **Removing a required metric can never improve a certified grade.** Cross-run comparison needs the same verdict version, the same metric contracts and full coverage.
   - Only platform-canonical statistics feed the score. LEAN-native values are evidence and never back-fill an input.
   - Alpha Calibration stays a visible, ungraded panel until a new verdict version defines it.
   - Why: the former scorer averaged only the sub-scores that were present and renormalized over the dimensions that were present. A run missing expectancy scored *higher* on Trade Edge, and two runs got comparable-looking grades from different denominators.
   - Receipts: the golden `tests/fixtures/golden/run-verdict-v2/` and `tests/services/test_run_verdict_parity.py`.

## Consequences

- A threshold, weight, fold minimum or curve rule changes only through a new decision recorded here, and for the run verdict also through a new verdict version. Persisted verdicts keep their version's meaning.
- The served methodology doc keeps the statistical methodology and the ladder as in-app help. The decisions and their reasons live here.
- Browser code renders these verdicts and never re-derives them (ADR 0068). Where the Frontend still recomputes a research grade or severity, that is non-conformance tracked by #2747's follow-ups.
