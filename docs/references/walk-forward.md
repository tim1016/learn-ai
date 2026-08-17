# Walk-forward analysis

**Concept**: Split a date window into train/test folds and aggregate fold-level metrics into a single out-of-sample (OOS) view. A fixed-spec run replays one `StrategySpec` on every test window. A parameter-search run executes fully materialized candidates on train, freezes one deterministic winner, and evaluates only that winner on test. Both paths use the canonical engine.

**Reference**: López de Prado, *Advances in Financial Machine Learning* (2018), §7 — "Cross-Validation in Finance" — establishes walk-forward as the standard CV protocol for time-series strategies, where standard k-fold is invalid because of look-ahead leakage. **Verify on next touch** — the citation is approximate; §7 covers walk-forward conceptually but the specific split-policy taxonomy (chronological / rolling / anchored) is repository-internal.

**Canonical implementation**: `PythonDataService/app/research/walk_forward/` (`splits.py`, `result.py`, `runner.py`, `storage.py`) + `app/routers/walk_forward.py`. Long versioned protocols enter through `Backend/Jobs/JobsApi.cs` and `PythonDataService/app/routers/jobs.py`; Python still owns every numerical result. Registry row in `docs/architecture/engine-authority-map.md` § "Walk-forward analysis". Phase C of the build-alpha-style research pipeline.

**Validated against**: `PythonDataService/tests/research/walk_forward/test_*.py` — tests covering split-policy correctness, fixed and train-selected orchestration, deterministic selection, candidate/test receipt lineage, aggregation math, storage round-trip with path-traversal defense, and HTTP boundary behavior.

## Milestone scope

**Phase 4A — fixed spec across folds.** Each fold's test window runs through `run_strategy_spec` with the same spec. The train window is not scored, but its bars pre-roll indicators and stateful primitives before TEST entries are enabled. The combined OOS curve is the concatenation of fold test-window equity curves, compounded.

**Phase 4B — train-side parameter selection (shipped 2026-08-15).** Each fold runs every fully materialized candidate on train and persists each candidate as a child `RunLedger`. The winner is the eligible candidate with the highest train Sharpe, then highest train total return, then earliest declaration order. Eligibility requires the configured minimum train-trade count and a non-null Sharpe. No eligible candidate means the analysis fails closed; no default threshold is tested as a fallback. The winner's exact spec is frozen for the test window, and `selected_parameters` plus all `training_candidates` are persisted on the fold.

## SPY EMA normalized-gap protocol

The frozen job type `POST /api/jobs/spy_ema_walk_forward` implements protocol `spy-ema-normalized-gap` version `1.0`, captured in `docs/references/spy-ema-normalized-gap-walk-forward.md`. The public .NET jobs boundary mints the job identity, exposes progress/result/cancellation, and dispatches to Python's internal `/api/jobs-internal/spy-ema-walk-forward` worker. The worker runs an absolute `$0.20` full-window control, then evaluates the relative EMA-gap candidate grid through rolling 180-day train / 30-day test windows stepped every 30 days. The immutable 2024-08-01 through 2026-08-01 window produces 18 OOS folds.

The linked `spy-ema-exhaustive-run` V1.0 protocol can subsequently retain up
to five TRAIN candidates per fold, deduplicate their exact specs, and compare
each unique gap through both an explicitly selection-biased full two-year run
and a fixed-gap replay of all 18 OOS folds. Its formulas, recency definition,
lineage, and read endpoints are documented in
`docs/references/spy-ema-exhaustive-run.md`.

## Three split policies

| Policy | Train side | Test side | When to use |
|---|---|---|---|
| **Chronological** | First `train_pct` of the window | Remainder | Single train/test cut. Quickest "did this overfit?" check. |
| **Rolling** | Fixed-size window slides by `step_days` | Same, immediately after train | Standard walk-forward in the LMDP / López de Prado sense — old history drops off as the window moves. |
| **Anchored** | Window from `start` to a moving cut-off (grows each fold) | Fixed-size window after train | "Longer training is strictly better" matches the model. |

All three operate on `int64 ms UTC` boundaries anchored at NY-local midnight to match the engine's session semantics. Each policy validates its parameters at construction (negative window, train > total, etc.) — degenerate inputs raise `ValueError` before the runner ever sees the windows, rather than silently emitting a zero-fold result.

## Fold-boundary semantics

Split policies emit fold boundaries as **half-open ms intervals** `[test_start_ms, test_end_ms)`. A rolling/anchored caller may deliberately set `step_days` below or above `test_days`, creating overlap or gaps. The canonical SPY V1 protocol pins `step_days == test_days == 30`, so only that protocol guarantees contiguous, non-overlapping OOS months.

The engine's data filter is **inclusive on both ends** (`start <= bar.date() <= end`). To prevent the boundary day from appearing in two adjacent folds, the runner converts each fold's exclusive `test_end_ms` to an **inclusive end date** by subtracting one day before formatting (`_ms_to_inclusive_end_date`). Fold N tests `[Jan 12, Jan 16]` and fold N+1 tests `[Jan 17, Jan 21]` — boundary day belongs to N+1.

### Indicator and position state at a fold boundary

Every TEST child loads bars beginning at its `train_start_ms`. Entries remain disabled until `test_start_ms`, while EMA, RSI, `FreshCross`, and every other stateful primitive observe the pre-roll bars. This prevents a cold-start crossover at the first TEST bars. The child ledger records `warmup_start_ms`, while metrics, trades, equity points, and `bars_consumed` remain scoped to TEST.

Positions do **not** carry across fold boundaries. `fold_position_policy = "flat_at_test_boundaries"` is persisted in the config: each TEST child starts flat and the engine forces it flat at the child end. This is deliberate because each fold may select a different specification.

## Combined OOS curve — compounded fold returns

When a strategy is profitable on fold N, the next fold's compounded equity should reflect that. The runner therefore concatenates fold equity curves with **multiplicative compounding**: fold N+1's start equity equals fold N's terminal equity, achieved by scaling fold N+1's curve by `(fold_N_terminal / fold_N+1_initial)`.

This produces a compounded sequence of independently-flat fold returns. It is useful for visualizing cumulative OOS evidence, but it is **not** a literal continuous-position brokerage statement because positions are reset at each fold boundary.

**Rebased-per-fold** (each fold starts at $1) is rejected for v1 because:
1. It loses the "did the strategy compound or just oscillate?" signal — flat-but-volatile fold sequences look indistinguishable from genuinely-compounding ones on a rebased plot.
2. It breaks visual continuity across fold boundaries.
3. It doesn't add any information that fold-level `total_return_pct` doesn't already carry.

If a future caller wants the rebased view, it's an additive switch on the runner — current `_compound_oos_curve` semantics are not load-bearing.

## Aggregation metrics

| Metric | Definition | None when |
|---|---|---|
| `mean_oos_sharpe` | Arithmetic mean of fold `test_metrics.sharpe_ratio` | Every fold's sharpe is None |
| `median_oos_sharpe` | Median of fold sharpes | Every fold's sharpe is None |
| `pct_profitable_folds` | Fraction of auditable completed folds with `total_return_pct > 0` | No completed folds |
| `oos_retention` for fixed-spec WF | `(mean OOS Sharpe) / (same-spec parent full-window Sharpe)`; basis `mean_oos_to_parent` | Parent/mean is null or parent Sharpe is 0 |
| `oos_retention` for parameter search | Equal-weight arithmetic mean of each eligible fold's `(test Sharpe / selected winner train Sharpe)`; basis `mean_fold_test_to_selected_train` | Every eligible fold ratio is null (for example selected train Sharpe is 0 or TEST Sharpe is null) |
| `alpha_decay` | OLS slope of fold sharpe vs `fold_index`. Negative = decay; positive = strategy still working | Fewer than 2 folds with non-None sharpe |

`alpha_decay` is **directional, not a pass/fail gate**. The point is to surface "this strategy was good in 2022 but stopped working in 2024" — interpretation belongs to the researcher.

## On-disk layout

```text
<root>/walk-forward/<wf_id>/
├── config.json    # WalkForwardConfig — inputs that produced this WF
└── result.json    # WalkForwardResult — folds + aggregated metrics
```

Each fold's individual run lives at `<root>/<fold_run_id>/{ledger,result}.json` (Phase A storage), with `parent_run_id = wf_id` so `list_runs(parent_run_id=wf_id)` finds them. The walk-forward layout is a **sibling** of the runs layout, not nested — a single `<root>` directory cleanly holds both shapes.

`wf_id` follows the same regex (`^[0-9a-f]{32}$`) and same path-traversal defense (resolved-path containment check) as `run_id`. Malformed IDs raise `ValueError` before any path concatenation.

## Failure semantics

* **Split-policy failure** (window too short, invalid params) → `WalkForwardResult.status = "failed"`, `failure_reason` populated, no folds executed. Persisted normally so the listing surfaces the failure.
* **Train-side selection failure** (every candidate failed, missed the minimum trade count, or has null Sharpe) → `WalkForwardResult.status = "failed"`; no test run is executed with a fallback candidate. `selection_failures[]` retains the fold window and every training-candidate receipt already produced.
* **Per-fold test failure** (unsupported feature, infrastructure error, or zero TEST bars after pre-roll is removed) → the fold appears with `status = "failed"` and aggregation skips it. A persisted empty-data child remains linkable evidence, but cannot count as OOS. The WF stays `"completed"` only when at least one auditable TEST fold completed; if every TEST fold fails, the aggregate is `"failed"` with no headline metrics.
* **Persistence failure** for a train candidate → selection fails closed, because testing a winner without the full comparison set would create unverifiable evidence. Persistence failure for a test receipt also fails the overall WF closed; the fold carries `status="failed"`, a null `test_run_id`, and is excluded from its curve and all aggregates. Persistence failure for the WF aggregate itself fails the calling endpoint/job (the analysis ran; its aggregate could not be durably recorded).

This is the same "failed runs are first-class research records" contract Phase A established — discoverability across many runs matters more than fail-fast strictness when the failure is the *result* the researcher wanted to know about.

## HTTP boundary

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/research/strategy-runs/walk-forward` | Run + persist + return `(config, result)` |
| `POST` | `/api/jobs/spy_ema_walk_forward` | Public asynchronous start for the immutable V1 SPY EMA protocol; request body must be empty |
| `POST` | `/api/jobs-internal/spy-ema-walk-forward` | .NET-to-Python dispatch carrying only the minted `job_id` |
| `GET` | `/api/research/strategy-runs/walk-forward/{wf_id}` | Load persisted WF |
| `GET` | `/api/research/strategy-runs/walk-forward` | List, filtered by parent/spec/protocol identity/creation time, newest-first |

The walk-forward router is mounted **before** `research_runs` in `app/main.py` so the literal `/walk-forward` segment wins against the parameterised `GET /{run_id}` route on the parent. Validated by `test_walk_forward_path_does_not_clash_with_run_id_route`.

When `parent_run_id` is supplied on `POST`, the router loads the parent strategy run from the same artifacts root and passes the parent run's Sharpe ratio into the walk-forward runner so `oos_retention` is filled. If the parent id is malformed, missing, or points to an unreadable/corrupt strategy run, the endpoint returns HTTP 400 instead of persisting a child analysis with unverifiable lineage.

GraphQL passthrough is intentionally not implemented. Generic walk-forward reads use FastAPI directly; the long SPY research protocol uses the existing .NET jobs transport for progress, cancellation, and result retrieval.

## Upgrade path

1. **Per-fold parallelism**: folds are independent; the runner currently executes them serially. A future change could `concurrent.futures` the fold loop. Sequential is fine for v1 — a 10-fold WF over a synthetic year completes in seconds.
2. **Resumability**: the jobs boundary is cancellable and survives page navigation, but it does not resume mid-fold after a process crash. A resumable implementation must reuse persisted child receipts only after verifying the pinned protocol, spec hashes, and data revision.
