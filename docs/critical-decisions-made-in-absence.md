# Critical Decisions Made in Absence

## SPY EMA normalized-gap walk-forward research

**Prepared:** 2026-08-15

**Status:** Implemented for review

**Source discussion:** [SPY EMA research conversation](https://chatgpt.com/c/6a7ff340-8f44-83ea-9ccf-bb8b13ca2eee)

**Product route:** `/research-lab/backtests/spy-ema-walk-forward`

## Executive summary

I completed the normalized-gap walk-forward path without waiting for decisions that could be made reversibly and recorded precisely. The implementation keeps every comparable number in Python, preserves the existing absolute-gap strategy as a control, selects a normalized threshold only on trailing data, freezes it before each test month, and persists the complete candidate lineage.

The highest-value review questions are:

1. Should the candidate objective remain train Sharpe, or become a constrained/multi-metric score?
2. Is five training trades enough evidence for an eligible candidate?
3. Should the baseline remain zero-cost next-bar-open, or should the primary presentation include a cost-stressed companion?
4. Is the explicit flat-at-each-TEST-boundary position policy the intended economic interpretation?

None of these decisions is hidden in Angular. The persisted walk-forward receipt exposes the exact policy and inputs used.

## Decisions

| # | Decision made | Why this choice | Consequence | Review trigger | Priority |
|---|---|---|---|---|---|
| 1 | Python remains the sole numerical and selection authority. | Repository policy requires one answer for formula, candidate selection, compounding, and metrics. | FastAPI returns complete receipts; Angular only maps, formats, and charts them. | Any client begins deriving a threshold, return, or statistic. | Must keep |
| 2 | Added a typed `DifferenceBps` operand instead of a general multiply/divide expression language. | It expresses the research concept directly and avoids expanding the executable AST beyond the required domain operation. | The spec is auditable and schema-valid; unrelated arithmetic remains unsupported. | A second legitimate strategy requires reusable ratio arithmetic. | Review later |
| 3 | The `$0.20` EMA-gap strategy is a full-window control, not a candidate. | It answers whether normalization changes behavior without letting the existing rule contaminate threshold selection. | Its run is persisted first and becomes the walk-forward parent. | The team wants the absolute threshold to compete inside every fold. | Review tomorrow |
| 4 | Normalized gap is `10,000 * (EMA5 - EMA10) / EMA10`. | Basis points remove price-level dependence while retaining direction and scale. | A zero denominator is rejected; indicator warmup yields no comparison. | A different denominator convention is desired, such as midpoint or EMA5. | Must review |
| 5 | Candidate grid is `1, 2, 3, 4, 5, 7.5, 10` bps in ascending order. | It brackets the `$0.20` control neighborhood near a `$500` SPY price while keeping the search small. | Final metric ties choose the lower threshold through declaration order. | Results cluster at either grid boundary or between 5 and 10 bps. | Review tomorrow |
| 6 | Rolling windows use 180 calendar days train, 30 calendar days test, stepped 30 calendar days. | This matches the six-month learn / one-month prove design and produces non-overlapping OOS months. | The default 2024-08-01 through 2026-08-01 window yields 18 folds. Train windows overlap; test windows do not. | The strategy trades too sparsely or regime turnover suggests shorter memory. | Must review |
| 7 | Winner = highest eligible train Sharpe, then total return, then declaration order. | It is deterministic, simple to explain, and avoids peeking at test results. | Null Sharpe cannot silently switch the objective. The selected exact spec is frozen on test. | A pre-registered multi-objective or downside-risk objective is approved. | Must review |
| 8 | Eligibility requires at least five training trades and non-null Sharpe. | This rejects obviously degenerate candidates while keeping the initial protocol usable. | A fold fails closed if no candidate qualifies; no default threshold is substituted. | Actual train counts show five is statistically too permissive or too restrictive. | Must review |
| 9 | Every train candidate and test fold is persisted as a child run. | Selection cannot be audited from the winning metric alone. | The receipt contains every assignment, spec hash, train metric, eligibility reason, selected value, and OOS run ID. | Storage volume becomes material; audit fidelity must not be reduced silently. | Must keep |
| 10 | Training-candidate persistence failure blocks OOS testing. | A winner chosen from an incomplete receipt set is not independently verifiable. | The analysis fails closed instead of returning apparently valid OOS evidence. | A transactional artifact bundle replaces individual child writes. | Must keep |
| 11 | Default execution is next-bar-open with zero commission and slippage. | Next-bar-open avoids same-close execution optimism; zero costs preserve a clean signal baseline consistent with the research discussion. | The headline is not a deployability claim and may overstate implementable returns. | Before any promotion or capital decision, add registered cost-stress runs. | Must review |
| 12 | SPY bars use the default regular-session LEAN reader. | The protocol says RTH, and the default reader filters using the NYSE calendar, including half-days. | Extended-hours bars never warm indicators or fire signals in the production data path. | A data-source factory bypasses the canonical reader or extended-hours research is requested. | Must keep |
| 13 | The 145-run protocol executes sequentially behind the existing background jobs boundary. | Sequential order keeps receipts deterministic; the jobs boundary prevents a long HTTP request and supplies progress, cancellation, result retrieval, and page-navigation survival. | The public job type is `spy_ema_walk_forward`; progress counts all 145 engine runs, and Python checks cancellation immediately before every candidate and TEST run. | Measured wall time justifies bounded parallelism without changing selection order or artifact identity. | Must keep for launch |
| 14 | Pipeline persistence is intentionally not globally transactional. | The existing artifact store is per-run atomic; introducing a new database transaction would be a separate architectural change. | A crash/cancellation can leave valid control or child receipts before the aggregate is written. They remain forensic evidence but may be orphaned from list views. | Orphan frequency becomes non-negligible or mid-run resumability is approved. | Review later |
| 15 | The new route presents protocol before performance and renders failure evidence explicitly. | A research surface should make the experiment legible before showing a favorable or unfavorable curve. | Users see the candidate lattice, formula, job progress/cancel action, control metrics, selected threshold per fold, failure/warning evidence, and OOS equity. A failed WF never falls back to the control curve. | Researchers need candidate-surface diagnostics beyond the fold strip. | Must keep |
| 16 | TEST indicator state pre-rolls from the fold's training start, while positions start and end flat at every TEST boundary. | Cold-starting EMA/RSI/`FreshCross` at TEST creates artificial boundary signals; carrying positions across folds is incoherent when the selected threshold can change. | Each TEST ledger records `warmup_start_ms`; entries are disabled before TEST; reported evidence excludes warmup; `fold_position_policy="flat_at_test_boundaries"` is persisted. The combined curve compounds independently-flat fold returns, not a literal continuous position history. | The research question changes to deployment continuity under a single evolving model and specifies a position handoff rule. | Must review |
| 17 | Parameter-search retention is the equal-weight mean of fold-local `test_sharpe / selected_train_sharpe`. | The absolute `$0.20` control is a different strategy and cannot be a valid denominator; a ratio of aggregate means also answers a different question. | Every fold persists selected train Sharpe and its ratio; the aggregate persists `oos_retention_basis="mean_fold_test_to_selected_train"`. | A weighted retention definition is pre-registered before viewing results. | Must keep |
| 18 | V1 inputs are server-owned and protocol identity/version are persisted. | A mutable API labeled “V1” could create custom evidence that the UI mislabels as canonical. | The job accepts only its generated `job_id`; dates, grid, split, costs, and fill mode are frozen in Python. UI discovery first finds an exact protocol ID/version receipt, then follows its persisted parent to the exact control. | A V2 protocol is approved; publish a new version rather than mutating V1. | Must keep |
| 19 | Child receipt persistence fails closed. | Metrics with a dead or missing receipt cannot be independently audited. | Train receipt failure blocks selection. TEST receipt failure produces a failed fold with null `test_run_id`, stops the protocol, preserves partial evidence, and excludes the fold from all aggregates. | Artifact writes become transactionally bundled with a verifiable commit receipt. | Must keep |

## Deliberate non-decisions

- No genetic search, EMA-period search, exit-bar optimization, or cost optimization.
- No test-window metric participates in candidate selection.
- No claim of statistical significance or correction for repeated candidate testing.
- No live-trading, broker-control, or deployment integration.
- No GraphQL passthrough; generic reads use FastAPI and the long run uses the existing .NET jobs transport.
- No client-side metric fallback if the Python evidence is missing.

## Known limitations

- The default window is fixed. A missing local SPY data slice will surface as a failed research receipt or request error, not be silently shortened.
- Five trades is an operational floor, not a proof of Sharpe stability.
- Zero transaction costs are optimistic. Treat the result as signal research until cost stress is registered and persisted.
- The seven-point grid introduces selection bias. The result is OOS by fold but is not a multiple-hypothesis-adjusted discovery claim.
- Sequential job execution is cancellable between engine runs but is not resumable mid-fold after a process crash.
- A crash after child persistence and before aggregate persistence can leave orphan child receipts.
- A TEST fold with zero reported-window bars is failed and excluded; if every TEST fold fails, the aggregate fails rather than presenting degenerate headline metrics.

## Validation receipts at handoff

- Exact-Decimal golden fixture for `DifferenceBps`, including warmup and zero-denominator boundaries.
- Deterministic selection tests covering Sharpe, return, declaration-order ties, ineligible candidates, and fail-closed behavior.
- Runner tests proving every train/test child receipt, frozen spec hashes, state pre-roll, flat fold boundaries, fold-local retention, partial failure evidence, persistence failure handling, and parent lineage.
- Versioned pipeline test proving the default window creates 18 folds.
- HTTP tests covering strict discriminated split validation and non-finite numeric rejection.
- Jobs and pipeline tests proving the request accepts no V1 overrides, uses the frozen protocol, reports all 145 engine runs, and checks cancellation at every candidate/TEST boundary.
- Angular tests covering protocol-first linked-control discovery, background execution/result retrieval, cancellation state, selected-threshold rendering, failed evidence, dead-link suppression, and no control-curve fallback.

## Suggested review order

1. Confirm decisions 4–8 (formula, grid, folds, objective, eligibility).
2. Confirm decision 11 (execution/cost baseline).
3. Confirm decisions 16–18 (fold state, retention meaning, immutable V1 identity).
4. Inspect the route, job progress, and failure receipts for research usability.
5. Only then interpret performance from a run against the intended pinned data snapshot.
