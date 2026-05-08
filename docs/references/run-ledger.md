# Run ledger and reproducibility hashing

**Concept**: A `RunLedger` is the immutable identity record for one execution of a `StrategySpec` through the canonical event-driven engine. Two runs whose identity columns (spec, fill model, cost model, data window, data-root revision, random seed, engine version) are equal are guaranteed by the deterministic engine to produce identical results — so their content hashes (`result_hash`, `trade_log_hash`, `metrics_hash`) must match. The ledger makes that invariant testable and the hashes make it queryable.

**Reference**: RFC 8785 (JSON Canonicalization Scheme) for the sort-keys + tight-separators contract; SHA-256 (FIPS 180-4) as the digest. We do not implement JCS literally — JCS demands `\uXXXX` escaping for non-ASCII and exact float formatting per ECMAScript ToString — because our hashed payloads are closed-vocabulary (Pydantic round-tripped `StrategySpec`, plus typed result fields) and the simpler contract is sufficient without pulling in a JCS dependency. This trade-off is locked in `app/research/runs/hashing.py` and exercised by `tests/research/runs/test_hashing.py` (key-order independence, non-ASCII stability, stable across calls).

**Canonical implementation**: `PythonDataService/app/research/runs/hashing.py` (the canonical-JSON serializer + SHA-256 wrapper), `app/research/runs/ledger.py` (the `RunLedger` schema and identity columns), `app/research/runs/runner.py` (orchestration that assembles the hashes from a real engine run), `app/research/runs/storage.py` (atomic file-backed persistence). HTTP boundary at `app/routers/research_runs.py`. Registry row under § "Backtesting engine and statistics" of `docs/math-sources-of-truth.md`.

## Identity columns

`RunLedger` separates *identity* (the inputs that determine the result) from *result hashes* (the deterministic outputs of those inputs). Identity columns are populated up-front; result hashes are filled after the engine completes.

| Column | Role |
|---|---|
| `strategy_spec_hash` | SHA-256 over `spec.model_dump(mode='json')` after Pydantic round-trip. Field order, default materialization, and Decimal-to-float coercion are stable across calls because Pydantic's dump is deterministic. |
| `data_snapshot_id` | Pipe-joined string `f"{symbol}\|{resolution_minutes}\|{start_ms}\|{end_ms}\|{data_root_revision}"`. **Constraint**: components must not contain `\|` themselves. Symbols are uppercase tickers in this repo (no embedded pipes); `data_root_revision` is a git SHA, an `mtime:<int>` proxy, or `unknown`; the integer fields are unambiguous. If a future symbol family (synthetic baskets, futures spreads) introduces pipes, swap to JSON-array encoding without changing the public function name. **Not** a content hash of bars (see "Why not bar-content addressing" below). |
| `start_ms` / `end_ms` | `int64 ms UTC` for **`America/New_York` midnight** on the requested calendar date. The engine interprets `set_start_date` / `set_end_date` as NY-local — the ledger matches that anchoring so `data_snapshot_id` and `since_ms` filters agree with what the engine actually fetched. |
| `engine_name` / `engine_version` | Hard-coded constants in `ledger.py`. Bump `ENGINE_VERSION` (currently `"0.1.0"`) when the engine's *semantic* output for a given input changes — fill model semantics, drawdown definition, statistics annualization choices, etc. Not for cosmetic refactors. |
| `engine_git_commit` | Captured once per process via `subprocess` of `git rev-parse HEAD`, memoized. Falls back to `"unknown"` outside a git checkout. The ledger field is informational (replay diagnostics across commits); it is **not** part of the deterministic identity contract. |
| `fill_mode`, `commission_per_order`, `slippage_per_share` | Cost model is part of identity. Two runs with the same spec and data window but different cost assumptions are different runs. `slippage_per_share` is an absolute price-points fraction (Decimal-compatible float) applied against the trade direction by `FillModel`; long fills pay the slippage above the bar price, short fills pay below. |
| `random_seed` | Recorded even though the v1 engine has no RNG. Locks Phase D (Monte Carlo) determinism contract before MC code lands so we don't have to migrate ledger schema later. |
| `parent_run_id` / `parent_spec_hash` | Optional lineage columns set on Phase C/D/E child runs (folds, MC simulations, sensitivity sweeps). Cheap to include now; ugly to migrate later. |

## `data_snapshot_id` design

The snapshot id identifies "the bars the engine saw" cheaply. Three options were considered:

| Option | Cost | Fidelity | Verdict |
|---|---|---|---|
| Pipe-joined identity tuple including a coarse `data_root_revision` | O(1) per run | Detects spec / window / root changes; misses silent rewrites of the same parquet file | **Chosen for v1.** Minimal overhead; aligns with how LEAN data is actually managed (vendored, versioned, mostly immutable). |
| SHA-256 of the literal bar payload (concatenate every minute bar, hash) | O(N) per run, ~2 GB read for a year of SPY minutes | Highest possible — any bar mutation changes the id | Rejected for v1; the cost dominates the engine itself for short backtests. Open path for v2 if silent data drift becomes a real concern. |
| Hash of a parquet manifest (filenames + mtimes + sizes) | O(files) per run | Detects file-level changes including in-place rewrites | Reasonable v1.5 if `data_root_revision = git rev-parse HEAD` proves too coarse. |

The chosen `data_root_revision` resolution order in `ledger.py::resolve_data_root_revision`:

1. `$LEAN_DATA_ROOT_REVISION` if explicitly set — lets CI / ops pin a revision string for cross-machine determinism.
2. `git rev-parse HEAD` of the LEAN data root if it's a git working tree.
3. `mtime:<int seconds>` of the data-root directory as a coarse "did this change?" proxy.
4. `"unknown"`.

## On-disk layout

```text
<root>/<run_id>/
├── ledger.json     # RunLedger.model_dump(mode='json')
└── result.json     # BacktestRunResult.model_dump(mode='json')
```

Default root: `<package_root>/artifacts/runs/`, overridable via `LEARN_AI_ARTIFACTS_ROOT`. `PythonDataService/artifacts/` is gitignored. For host persistence across container rebuilds, mount that path in `podman-compose.yml`; without a mount, runs evaporate when the container is recreated.

`spec.json`, `trades.parquet`, `equity.parquet`, and `log.txt` were considered as additional sidecars but dropped from v1 — every field they would carry already lives in `ledger.json` (the spec) or `result.json` (trades, equity curve, log lines). Splitting creates two-sources-of-truth without a real consumer. Phase D's Monte Carlo can read trades from `result.json` directly; if performance becomes a concern, parquet sidecars can be added without breaking the on-disk contract.

## Hashing exclusions

`result_hash` excludes two fields:

- `run_id` — UUID-allocated per run; including it would break the "two runs with the same inputs share a result_hash" property the test suite enforces.
- `log_lines` — human-formatted strings that may carry timing-dependent timestamps. The math-relevant content (equity curve, trades, metrics) is hashed; logs are not.

`trade_log_hash` and `metrics_hash` are sub-hashes over the relevant subtrees so a divergence test can pinpoint *which* part of the result diverged when two runs disagree.

## Failed runs are first-class

When the runner can't complete (data source unavailable, spec uses a Phase-2 feature the evaluator refuses, engine crash), it produces a `status='failed'` ledger paired with a zeroed `BacktestRunResult` that carries the failure reason in `warnings`. Result hashes are still computed over the zeroed payload so two failed runs with the same identity columns share a result hash — useful for the "did this fail before with the same inputs?" lookup. The HTTP endpoint persists failures alongside successes and returns 200 (clients introspect `ledger.status`); this is a deliberate departure from `spec_strategy.py`'s 400-on-NotImplementedError because the research pipeline cares about discoverable failure across many runs.

## Exposure metric

`BacktestMetrics.exposure_pct` is the fraction of base data bars during which the strategy held a position, stored as a 0..1 ratio:

```text
exposure_pct = min(1, max(0, bars_held_total * resolution_minutes / total_bars))
```

`bars_held_total` is measured in the strategy's consolidated resolution bars (for example, 15-minute bars), while `total_bars` is measured from the minute-level equity curve. The multiplication by `resolution_minutes` converts held consolidated bars back to the minute-bar base unit before division. Without that conversion, a 15-minute strategy understates exposure by a factor of 15.

## Validated against

- `PythonDataService/tests/research/runs/test_hashing.py` — canonical-JSON properties (key order, non-ASCII, separators, stability), data-snapshot id formatting, every-field-changes-the-id sweep.
- `tests/research/runs/test_runner_inmemory.py` — replay determinism, parameter / window / fill-mode propagation, exposure unit conversion, failure-path hash stability, lineage round-trip.
- `tests/research/runs/test_storage.py` — atomic-write guarantee, round-trip, every list filter, corrupt-ledger skip-and-warn.
- `tests/research/runs/test_endpoint.py` — HTTP-boundary hash determinism, int64-ms wire-format compliance, error mapping.
- `tests/research/runs/test_ema_acceptance.py` — Phase A acceptance gate against the canonical SPY EMA crossover fixture end-to-end.

## Upgrade path

The file-backed root is the v1 substrate. When run counts grow beyond a few thousand, the upgrade is:

1. **Postgres index, files unchanged** — keep `<root>/<run_id>/{ledger,result}.json` as the truth, add a Postgres table mirroring the ledger fields for fast filter / sort queries. `list_runs` switches backends; `load_run` still reads JSON. Same public function names.
2. **Postgres truth, files as exports** — when read latency becomes the bottleneck, push the full ledger + result into Postgres (jsonb) and treat the file layout as an export format for external tooling. This is a Phase F+ concern.

Schema migration: `RunLedger.schema_version` is currently `"1.0"`. A schema bump means a Pydantic field added or removed; on load, missing fields default and extra fields raise (Pydantic `extra='forbid'`). When that contract breaks, write an explicit migration in `app/research/runs/storage.py` rather than loosening Pydantic's strictness.
