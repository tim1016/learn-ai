# Null-baseline analysis

**Concept**: Generate N alternative strategies on the parent run's symbol / window / cost model, run each through the canonical engine, and rank the parent's metric values against the resulting null distribution. Answers "did the parent strategy beat *random*?" — Build Alpha-style Null Baselines (Feature 7 of the architecture spec).

**Reference**: Phipson & Smyth (2010), *Permutation P-values should never be zero: calculating exact P-values when permutations are randomly drawn* — the small-sample p-value formula `(1 + count(null >= parent)) / (N + 1)`. The bias-correction prevents the naive `count / N` estimator from returning a literal zero just because the random sample happens to miss the parent's neighborhood. **Verify the citation on next touch** — the Phipson-Smyth paper is the right one for the formula, but the architecture spec's specific list of baseline methods (buy-and-hold, random EMA window pairs) is repository-internal labeling, not from Phipson-Smyth.

**Canonical implementation**: `PythonDataService/app/research/baselines/generators.py` (spec generators), `runner.py` (orchestration + aggregation), `result.py` (DTOs), `storage.py` (file-backed persistence), `app/routers/baselines.py` (HTTP boundary). Registry row in `docs/architecture/engine-authority-map.md` § "Null-baseline analysis". Phase E1 of the build-alpha-style research pipeline.

**Validated against**: `PythonDataService/tests/research/baselines/test_*.py` — 52 tests + 1 informational skip covering generator correctness (B&H tautology shape, EMA-pair invariants, parameter-range validation), runner orchestration (B&H equity metrics, random-EMA generates N runs, null-distribution coverage, percentile and p-value invariants), storage round-trip with path-traversal defense, and HTTP boundary including route-clash regression.

## Two baseline methods

| Method | What it samples | When to use | Output count |
|---|---|---|---|
| **`buy_and_hold`** | Single deterministic spec — enter on bar 1 via a `BarProperty: range >= 0` tautology, hold through end-of-algorithm flush. | "Did this strategy beat just holding the market?" | `sample_count` (typically 1; >1 only for engine-determinism sanity-checking) |
| **`random_ema_windows`** | `(fast, slow)` EMA period pairs from a bounded family (default `fast ∈ [3, 12]`, `slow ∈ [10, 30]`, `slow > fast`); each pair becomes a SPY-EMA-style spec on the parent's symbol. | "Did the parent's specific EMA(5,10) choice beat a random pair from the same family?" | `sample_count` |

Both are deterministic given a `random_seed` (stored on `BaselineConfig`) — same seed → identical sampled parameter list, same baseline runs, same null distribution. Pinned by `test_random_ema_windows_same_seed_produces_identical_parameters`.

## v1 deferred (architecture spec called for these but they don't ship today)

* **`random_entries` / `random_signal_timestamps`** — fire on a pre-computed list of bar indices. Needs a new spec primitive (`BarIndex` or fixed-time-list firing) or an engine-bypass strategy class. Out of scope until a real consumer drives the spec change.
* **`random_strategy_specs`** — random-spec generation across the whole primitive set. That's the Build Alpha automated-discovery feature, not the null-baseline feature.
* **`cross_symbol`** — needs multi-symbol data wiring.

## The buy-and-hold tautology

`StrategySpec` has no "always true" primitive. To run a single-trade buy-and-hold without modifying the schema, the generator builds an entry condition `BarProperty: property=range, op=">=", value=0.0` — tautologically true because the OHLC invariant `high >= low` (validated at engine ingestion) makes `range >= 0` always satisfied. The exit uses `BarsSinceEntry: op=">=" value=999_999`, an unreachable threshold. Result: enter on bar 1, hold through the engine's `on_end_of_algorithm` flush.

**Known limitation:** the engine's `on_end_of_algorithm` calls `ctx.liquidate(symbol)` which submits a pending order, but the main bar loop has already exited so the closing fill is never drained. The position is correctly tracked through equity (`RunMetrics.total_return_pct` and `max_drawdown_pct` are computed from the real equity curve), but `RunLedger.trade_log` ends up empty for buy-and-hold. `RunMetrics.total_trades = 0` and `exposure_pct = 0.0` are artefacts of this. Null-distribution aggregation works on the equity-derived metrics, which are correct, so the baseline still answers the right question. Fixing the engine flush is tracked as a follow-up; not blocking for null-baseline research.

## Null-distribution aggregation

For each target metric (default coverage: `sharpe_ratio`, `total_return_pct`, `max_drawdown_pct`, `profit_factor`, `win_rate`, `expectancy_pct`):

| Field | Definition |
|---|---|
| `parent_value` | Value from the parent run's `RunMetrics`. `None` if the parent's metric is `None` (e.g., zero-trade `win_rate`). |
| `null_values` | Array of metric values across **successful** baseline runs. Failed baselines and `None`-valued metrics are excluded. |
| `empirical_percentile` | Fraction of `null_values` strictly less than `parent_value`. Higher percentile = parent did better than the null *for higher-is-better metrics*. For lower-is-better metrics (max drawdown), higher percentile = parent did *worse*. The user reads percentile knowing each metric's directionality. |
| `empirical_p_value` | `(1 + count(null >= parent)) / (N + 1)` — the Phipson-Smyth small-sample one-sided p-value for "parent is anomalously high vs null". Symmetric form for "anomalously low" is `1 - empirical_p_value`, computed by the client. The lower bound `1 / (N + 1)` is what you get when no null beats the parent. |

Skips `None` values in the null sample so a single zero-trade baseline doesn't poison the per-metric distribution.

## On-disk layout

```text
<root>/baselines/<baseline_id>/
├── config.json    # BaselineConfig — inputs that produced this run
└── result.json    # BaselineResult — baselines list + null distributions
```

Sibling layout to `<root>/walk-forward/<wf_id>/` (Phase C) and `<root>/monte-carlo/<mc_id>/` (Phase D), parallel to Phase A's `<root>/<run_id>/`. Same regex (`^[0-9a-f]{32}$`) and resolved-path containment defense as the other storage layers.

The per-baseline child runs are **not** persisted under `<baselines>/...` — they're normal Phase A `RunLedger`s under `<root>/<baseline_run_id>/` with `parent_run_id` set to the **baselines run id** (not the parent run). This means `list_runs(parent_run_id=baseline_id)` enumerates the children for any given baselines analysis. The user can drill from baselines list → individual fold's run-detail page.

## Failure semantics

* **Missing parent run** → `status='failed'` with reason. Persisted normally.
* **Malformed `parent_run_id`** (Phase A's regex rejects it) → `status='failed'`.
* **Generator failure** (e.g., unsatisfiable EMA constraints, count <= 0, negative seed) → `status='failed'` before any baseline runs, no child runs created.
* **Per-baseline failure** (engine refuses an unsupported spec, infrastructure error) → that baseline's `BaselineRunRecord.status='failed'` with `failure_reason`, excluded from null-distribution aggregation. The overall analysis stays `status='completed'`.

This matches the Phase A/C/D "failed runs are first-class research records" contract — persist failures so they're discoverable, don't raise from the runner.

## Server-side caps

* **`sample_count` ≤ 200** at the router. Each baseline is a full backtest, so 200 × backtest-time bounds latency. For interactive workflows 30-50 is a good range; the architecture spec recommends ≥30 for stable null distributions.
* **`random_seed` ≥ 0** — Pydantic 422 at the wire boundary plus a defensive check in the runner (same belt-and-suspenders as Phase D, since `numpy.random.default_rng` raises for negative seeds).

## HTTP boundary

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/research/strategy-runs/baselines` | Run + persist + return `(config, result)` |
| `GET` | `/api/research/strategy-runs/baselines/{baseline_id}` | Load persisted baseline |
| `GET` | `/api/research/strategy-runs/baselines` | List, filtered by `parent_run_id` / `method` / `since_ms`, newest-first |

Mounted **before** `research_runs` in `app/main.py` so the literal `/baselines` segment wins against the parameterised `GET /{run_id}` route on the parent. Validated by `test_baselines_path_does_not_clash_with_run_id_route`.

## Upgrade path

1. **`random_entries` / `random_signal_timestamps`** baselines: requires either a `BarIndex` spec primitive (fire on a pre-computed list of bar indices) or a parallel-engine strategy class that bypasses spec. Spec extension is cleaner; tracked as a future PR.
2. **Cross-symbol naive baseline**: needs multi-symbol data wiring + a way to derive a target-symbol from the parent's symbol (e.g., "if parent is on SPY, baseline against QQQ + IWM").
3. **Random spec generation**: pulls from the spec primitive set with a complexity budget. This is part of Build Alpha's automated-discovery feature, not the baseline feature — separate phase.
4. **Phase E1-frontend**: section on the run-detail page mirroring Phase C/D-frontend's pattern + a new `/research-lab/baselines/:baseline_id` detail route showing per-metric null distribution charts + the parent's empirical position.
