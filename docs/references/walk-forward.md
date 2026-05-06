# Walk-forward analysis

**Concept**: Split a date window into train/test folds, run the same `StrategySpec` over each fold's test window through the canonical engine, and aggregate fold-level metrics into a single out-of-sample (OOS) view. Distinguishes "looks great in-sample" from "still holds up on bars the strategy didn't see during fitting."

**Reference**: López de Prado, *Advances in Financial Machine Learning* (2018), §7 — "Cross-Validation in Finance" — establishes walk-forward as the standard CV protocol for time-series strategies, where standard k-fold is invalid because of look-ahead leakage. **Verify on next touch** — the citation is approximate; §7 covers walk-forward conceptually but the specific split-policy taxonomy (chronological / rolling / anchored) is repository-internal.

**Canonical implementation**: `PythonDataService/app/research/walk_forward/` (`splits.py`, `result.py`, `runner.py`, `storage.py`) + `app/routers/walk_forward.py`. Registry row in `docs/architecture/engine-authority-map.md` § "Walk-forward analysis". Phase C of the build-alpha-style research pipeline.

**Validated against**: `PythonDataService/tests/research/walk_forward/test_*.py` — 55 tests covering split-policy correctness, runner orchestration (fold count, parent linkage, persistence, aggregation math), storage round-trip with path-traversal defense, and HTTP boundary (request validation, 404/400/422 mapping, list filtering).

## Milestone scope

**Phase 4A — fixed spec across folds (this implementation).** Each fold's test window runs through `run_strategy_spec` with the same spec; the train window is *recorded* in the fold record but not *executed*, since there's no parameter fitting happening. The combined OOS curve is the concatenation of fold test-window equity curves, compounded.

**Phase 4B — train-side parameter selection (deferred).** Each fold optimizes a parameter grid on the train window, freezes the chosen parameters, then runs them on test. Plugs into the same fold list with a non-empty `selected_parameters` field. Belongs behind Feature 8 (sensitivity sweeps), which provides the parameter-search machinery 4B reuses.

## Three split policies

| Policy | Train side | Test side | When to use |
|---|---|---|---|
| **Chronological** | First `train_pct` of the window | Remainder | Single train/test cut. Quickest "did this overfit?" check. |
| **Rolling** | Fixed-size window slides by `step_days` | Same, immediately after train | Standard walk-forward in the LMDP / López de Prado sense — old history drops off as the window moves. |
| **Anchored** | Window from `start` to a moving cut-off (grows each fold) | Fixed-size window after train | "Longer training is strictly better" matches the model. |

All three operate on `int64 ms UTC` boundaries anchored at NY-local midnight to match the engine's session semantics. Each policy validates its parameters at construction (negative window, train > total, etc.) — degenerate inputs raise `ValueError` before the runner ever sees the windows, rather than silently emitting a zero-fold result.

## Fold-boundary semantics

Split policies emit fold boundaries as **half-open ms intervals** `[test_start_ms, test_end_ms)`. Fold N+1's `test_start_ms` equals fold N's `test_end_ms` (no gap, no overlap).

The engine's data filter is **inclusive on both ends** (`start <= bar.date() <= end`). To prevent the boundary day from appearing in two adjacent folds, the runner converts each fold's exclusive `test_end_ms` to an **inclusive end date** by subtracting one day before formatting (`_ms_to_inclusive_end_date`). Fold N tests `[Jan 12, Jan 16]` and fold N+1 tests `[Jan 17, Jan 21]` — boundary day belongs to N+1.

## Combined OOS curve — compounded, not rebased

When a strategy is profitable on fold N, the next fold's compounded equity should reflect that. The runner therefore concatenates fold equity curves with **multiplicative compounding**: fold N+1's start equity equals fold N's terminal equity, achieved by scaling fold N+1's curve by `(fold_N_terminal / fold_N+1_initial)`.

This produces the *investor experience curve* — what someone holding the strategy across all folds would actually see on their statement.

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
| `pct_profitable_folds` | Fraction of folds with `total_return_pct > 0` | No folds (split policy emitted zero) |
| `oos_retention` | `(mean OOS sharpe) / (parent run's full-window sharpe)` | No `parent_run_id` supplied — router-level concern, not currently auto-resolved |
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
* **Per-fold failure** (spec uses an unsupported feature, infrastructure error) → fold's `RunLedger.status = "failed"` (Phase A contract), fold appears in the WF result with zeroed metrics, WF status stays `"completed"` and the fold contributes to the failure count via aggregation skipping.
* **Persistence failure** for a single fold → logged + appended to `WalkForwardResult.warnings`; the WF continues. Persistence failure for the WF itself → 500 from the endpoint (the analysis ran; we couldn't durably record it).

This is the same "failed runs are first-class research records" contract Phase A established — discoverability across many runs matters more than fail-fast strictness when the failure is the *result* the researcher wanted to know about.

## HTTP boundary

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/research/strategy-runs/walk-forward` | Run + persist + return `(config, result)` |
| `GET` | `/api/research/strategy-runs/walk-forward/{wf_id}` | Load persisted WF |
| `GET` | `/api/research/strategy-runs/walk-forward` | List, filtered by `parent_run_id`/`spec_hash`/`since_ms`, newest-first |

The walk-forward router is mounted **before** `research_runs` in `app/main.py` so the literal `/walk-forward` segment wins against the parameterised `GET /{run_id}` route on the parent. Validated by `test_walk_forward_path_does_not_clash_with_run_id_route`.

GraphQL passthrough is intentionally not implemented — Phase B's UI consumed FastAPI directly via `HttpClient`, and the walk-forward UI (deferred Phase C-frontend) follows the same pattern.

## Upgrade path

1. **Per-fold parallelism**: folds are independent; the runner currently executes them serially. A future change could `concurrent.futures` the fold loop. Sequential is fine for v1 — a 10-fold WF over a synthetic year completes in seconds.
2. **`oos_retention` auto-resolution**: when `parent_run_id` is supplied, the runner could load the parent run's `BacktestRunResult` and fill `oos_retention` automatically. Currently left None; client computes if needed.
3. **Phase 4B (parameter selection)**: requires the sensitivity-sweep machinery from Feature 8. The fold list has `selected_parameters: dict` ready for non-empty values; the runner just needs the inner search loop.
