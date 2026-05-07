# Monte Carlo trade-path simulation

**Concept**: Take the trade list from a persisted `RunLedger`, simulate N alternate paths over the per-trade `pnl_pct` array, and aggregate into equity bands + drawdown/streak/terminal-PnL quantiles + drawdown-breach probabilities. Answers "what range of paths are normal for this strategy's distribution?" — Build Alpha-style Monte Carlo Risk Lab (Feature 5 of the architecture spec).

**Reference**: Standard non-parametric bootstrap, Efron (1979) "Bootstrap Methods: Another Look at the Jackknife"; reshuffle/permutation testing in trading is well-established and documented in López de Prado, *Advances in Financial Machine Learning* (2018) §7. **Verify both citations on next touch** — the specific method names (reshuffle / resample / forward projection) are repository-internal labels, not direct quotes from either reference.

**Canonical implementation**: `PythonDataService/app/research/monte_carlo/methods.py` (path-generation primitives), `runner.py` (orchestration + aggregation), `result.py` (DTOs), `storage.py` (file-backed persistence), `app/routers/monte_carlo.py` (HTTP boundary). Registry row in `docs/architecture/engine-authority-map.md` § "Monte Carlo risk analysis". Phase D of the build-alpha-style research pipeline.

**Validated against**: `PythonDataService/tests/research/monte_carlo/test_*.py` — 69 tests covering primitive correctness (multiset preservation, sample-with-replacement semantics), runner orchestration (quantile invariants, determinism by seed, reshuffle terminal-equity invariance, failure paths), storage round-trip with path-traversal defense, and HTTP boundary including route-clash regression with `/{run_id}`.

## Two simulation methods

| Method | What it does | When to use | Output length |
|---|---|---|---|
| **Reshuffle** | Permute the input returns array. Same multiset, different order. | Test path dependence: if the strategy's edge is real, the order shouldn't matter much. | Always equals input length. |
| **Resample** | Sample the input returns with replacement. May produce duplicates of any input return. | Test sample sensitivity (standard bootstrap when `size = len(returns)`); forward projection (`size > len(returns)`) under an IID-returns assumption. | Caller-controlled (`projection_trade_count`). |

Both are deterministic given a `random_seed` — same seed → identical simulations across machines via `numpy.random.default_rng(seed)`.

**Reshuffle terminal-equity invariance.** Because reshuffle preserves the multiset and equity compounding is multiplicative, every reshuffle simulation lands at the *same* terminal equity (commutativity of multiplication). This is a real mathematical fact, not a quirk: `prod(1 + r_pi(i))` for any permutation π is the same value. The test suite pins this with `test_reshuffle_terminal_equity_is_constant_across_sims`, which asserts `equity_bands[-1].p5 == p50 == p95` for reshuffle output.

## Aggregation

| Aggregate | Definition |
|---|---|
| `equity_bands[i]` | At each trade index `i` (0..N inclusive), the 5th / 50th / 95th percentile of equity across simulations. Index 0 = `initial_cash` for every simulation; index N = terminal. |
| `drawdown_quantiles` | `{p5, p50, p95}` of the per-simulation max-drawdown fraction (peak-to-trough, in `[0, 1]`). |
| `terminal_pnl_quantiles` | `{p5, p50, p95}` of `terminal_equity − initial_cash`. |
| `max_losing_streak_quantiles` | `{p5, p50, p95}` of the longest run of consecutive losing trades per simulation. |
| `breach_probabilities[t]` | Sample fraction of simulations whose realised max-drawdown ≥ `t`, for each client-supplied threshold `t ∈ [0, 1]`. |

Quantiles are computed via `numpy.percentile` with the default linear-interpolation rule.

**Equity-curve compounding** matches `app/engine/results/statistics.py::_equity_curve_from_trades` exactly: `equity[i+1] = equity[i] * (1 + return[i])`. Simulated curves are therefore on the same scale as the parent run's reported `equity_curve` and `max_drawdown_pct`.

## On-disk layout

```text
<root>/monte-carlo/<mc_id>/
├── config.json    # MonteCarloConfig — inputs that produced this MC
└── result.json    # MonteCarloResult — bands + quantiles + breach probabilities
```

Sibling layout to `<root>/walk-forward/<wf_id>/` (Phase C) and parallel to `<root>/<run_id>/` (Phase A). Same regex (`^[0-9a-f]{32}$`) and resolved-path containment defense as the other storage layers — malformed `mc_id` raises `ValueError` before any path concatenation.

The MC layer **does not** persist or re-run the parent's engine. It loads the parent's `RunLedger` + `BacktestRunResult` from the Phase A storage directory and operates on the trade list. The `parent_trade_log_hash` is recorded in `MonteCarloConfig` so a future drift check can detect "the parent run's trade list has been re-persisted with different content but the same run_id" (shouldn't happen, but the field exists for the audit).

## Failure semantics

* **Missing parent run** → `status='failed'` with `failure_reason="parent run not found: ..."`. Persisted normally so it shows up in listings.
* **Malformed `parent_run_id`** (rejected by Phase A's `_run_dir` regex) → `status='failed'` with the underlying `ValueError` message. Same persistence rule.
* **Empty parent trade list** → `status='failed'` with a clear message. MC over zero trades isn't meaningful; better to surface than to invent.
* **Reshuffle with mismatched `projection_trade_count`** → `status='failed'`. Reshuffle is a permutation; only `projection_trade_count == 0` (use parent length) or `== parent length` is sensible. Other values are user errors.
* **Invalid `breach_thresholds`** (outside `[0, 1]`) → `status='failed'`.

This matches the Phase A "failed runs are first-class research records" contract — persist failures so they're discoverable, don't raise from the runner.

## Server-side caps and warnings

* **`simulation_count`** is capped at **10,000** server-side (`_MAX_SIMULATION_COUNT` in `app/routers/monte_carlo.py`). Above-cap requests get 422 from Pydantic. Higher values are usually a mis-typed config rather than a real research need; 10k of a 200-trade run finishes in well under a second.
* **Low-trade-count warning** (`< 30` parent trades) — Monte Carlo quantiles over a small sample have wide intervals that aren't statistically meaningful. We don't *block* the run (the user may genuinely want to look) but we surface a warning so the UI can flag the result as illustrative.
* **Long-projection warning** (resample with `projection_trade_count > 4 × parent_trade_count`) — the IID-returns assumption gets weaker the further you project past the historical sample.
* **Low-simulation warning** (`simulation_count < 100`) — tail quantiles (P5 / P95) are noisy at small batch sizes.

## HTTP boundary

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/research/strategy-runs/monte-carlo` | Run + persist + return `(config, result)` |
| `GET` | `/api/research/strategy-runs/monte-carlo/{mc_id}` | Load persisted MC |
| `GET` | `/api/research/strategy-runs/monte-carlo` | List, filtered by `parent_run_id`/`method`/`since_ms`, newest-first |

Mounted **before** `research_runs` in `app/main.py` so the literal `/monte-carlo` segment wins against the parameterised `GET /{run_id}` route on the parent. Validated by `test_monte_carlo_path_does_not_clash_with_run_id_route`.

GraphQL passthrough is intentionally not implemented — same precedent as `walk_forward.py` and `research_runs.py`. The Phase D-frontend PR (deferred) will consume FastAPI directly via Angular's `HttpClient`.

## What's NOT in Phase D

* **Feature 6 — OHLC noise / shifted-bar / synthetic-data tests.** Needs synthetic-data generators that preserve OHLC invariants (`high ≥ max(open, close)`, `low ≤ min(open, close)`) and timestamp monotonicity. That's a separate engineering project worth its own PR; deferred to a future `app/research/robustness/` module.
* **Frontend integration.** Backend is fully consumable via HTTP today. The Phase D-frontend PR will add a sub-section on the run-detail page (next to the walk-forward section) plus a new `/research-lab/monte-carlo/:mc_id` detail route mirroring Phase C-frontend's pattern.
* **Block bootstrap.** Pure resample assumes IID returns. Block bootstrap (Politis & Romano 1994) preserves short-range autocorrelation in the return sequence; not in v1, but `methods.py` is the natural place to add a `block_bootstrap_trades` primitive when needed.

## Upgrade path

1. **Block bootstrap** (preserves autocorrelation): add `block_bootstrap_trades(returns, *, block_len, size, rng)` to `methods.py`; thread a new `MonteCarloMethod = "block_bootstrap"` enum value through. Storage and aggregation are method-agnostic — only the path-generation primitive changes.
2. **Per-fold resampling** (Phase D × Phase C): when a parent is a walk-forward result, resample within each fold separately rather than across the merged trade list. Useful when fold returns are themselves heteroskedastic.
3. **Forward N-trade projection with cost perturbation**: combine resample with a slippage / commission shift to answer "what if the next 100 trades come with 2× the costs?" — straightforward extension of the runner once Feature 6's cost-perturbation module lands.
