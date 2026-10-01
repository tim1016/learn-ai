# ADR 0072 — A research run is identified by a canonical hash of its inputs; parameter cells, trade evidence and model predictions are hashed inputs too

**Status:** Accepted 2026-09-30
**Provenance:** Entry 9 of [#2745](https://github.com/tim1016/learn-ai/issues/2745), merged from #2742 G and F and #2741 kept why 3 (artifact-seam decisions 1–2). Recorded here so that later clean-up slices can delete their only other homes: the Recency Chart design spec's D11 and D16 (PRD [#1577](https://github.com/tim1016/learn-ai/issues/1577), 2026-08-16), the ML "predictions as data" v0.5 design (2026-05-09), and the research artifact-seam decisions. Written by [#2749](https://github.com/tim1016/learn-ai/issues/2749).
**Vocabulary:** none owed — run ledger, `params_hash`, evidence fingerprint and prediction set are research terms, outside the live trading and operator domain that `CONTEXT.md` covers.
**Related:** ADR 0069 (strict equivalence — the reason identical inputs must reproduce identical hashes), ADR 0055 (Python-owned research tables), ADR 0056 D3 (a walk-forward study freezes one data snapshot and one code identity), ADR 0057 D5 (Recency redelivery is answered by `params_hash`), ADR 0061 (a validated configuration is an identity), ADR 0022 (`int64 ms UTC`).

## Context

Research answers are only worth keeping if they can be found again and reproduced. "Did I already run this?", "why do these two runs disagree?" and "is this trade the same evidence as that one?" each need an identity that changes exactly when the inputs that decide the result change, and not otherwise. The run ledger and prediction sets use the canonical-JSON SHA-256 in `PythonDataService/app/research/runs/hashing.py::hash_payload`: `sort_keys`, tight separators, `ensure_ascii=False`, bare 64-character hex. It is a relaxed RFC 8785. The hashed payloads are closed-vocabulary Pydantic round-trips, so literal JCS escaping and float formatting buy nothing and would add a dependency. Two older identities, `params_hash` and the trade fingerprint, use their own frozen encoders (decisions 4 and 5).

## Decision

1. **A run's identity is its input columns, and equal inputs must give equal result hashes.**
   - `RunLedger` (`app/research/runs/ledger.py`) is the immutable identity record of one `StrategySpec` execution. There is no single identity hash: the identity is the set of input columns the ledger records:
     - `strategy_spec_hash`: the spec after a Pydantic round-trip;
     - `engine_name` / `ENGINE_VERSION`;
     - `symbol`, `resolution_minutes`;
     - the report window `start_ms` / `end_ms`, plus the data pre-roll start `warmup_start_ms`;
     - `initial_cash`;
     - `fill_mode`, `commission_per_order`, `slippage_per_share`;
     - `warmup_policy`, `random_seed`;
     - `data_source`, `data_snapshot_id`, `prediction_set_hash`;
     - the parent lineage.

     `data_snapshot_id` starts at the pre-roll start, not at the report start. Two runs with the same `data_snapshot_id` can therefore differ in `start_ms`, and only the full column set identifies a run.
   - Runs that agree on all of these columns must produce the same `result_hash`, `trade_log_hash` and `metrics_hash`. The deterministic engine guarantees it, and the run tests enforce it. The two sub-hashes exist to show *which* part of a result diverged.
   - `result_hash` excludes `run_id` (a per-run UUID would break the equal-inputs property) and `log_lines` (timing-dependent text).
   - `engine_git_commit` is informational, not identity.
   - **Bump `ENGINE_VERSION` when the engine's semantic output for a given input changes:** fill semantics, the drawdown definition, the annualization choice. Do not bump it for a cosmetic refactor.
   - A failed run is first-class: a `status='failed'` ledger with a zeroed result whose hashes are still computed, so the same failing inputs share an identity.
   - The run window's dates are anchored at `America/New_York` midnight, the engine's own date semantics. This is a surface-stated anchor under ADR 0022 (a).
2. **`data_snapshot_id` names the bars cheaply. It is not a hash of their content.**
   - The format is `symbol|resolution_minutes|start_ms|end_ms|data_root_revision`.
   - A lake-backed run (#2446) sets the revision to `lake:<data_availability_hash>` after admission. That hash covers the admitted lake state.
   - Otherwise it resolves in this order (`runs/ledger.py::resolve_data_root_revision`):
     1. `$LEAN_DATA_ROOT_REVISION`;
     2. `files:<16 hex>`, a hash over the modification times of the window's minute zips, taken in the reader's root precedence;
     3. the data root's git HEAD;
     4. `files:none` when a window was given, or `unknown` when none was.
   - Rejected: hashing every bar, an O(N) read that costs more than a short backtest.
3. **The run ledger is immutable and hash-addressed; other research phases persist mutable configs; hashing is opt-in per phase** (artifact-seam decisions 1–2).
   - `runs/` keeps `ledger.json` + `result.json`. Monte Carlo, baselines and walk-forward keep `config.json`, through one descriptor-bound store (`app/research/artifact/`).
   - The descriptor's `hash_payload` hook is optional: a phase with no hook is not hashed. So existing replay addresses stay byte-stable, and a phase opts in only when it becomes replay-addressable.
   - `runs/hashing.py` stays the ledger's canonical-JSON SHA-256.
   - Collapsing the layouts would have erased the line between an immutable identity and a mutable input, and forced a migration for nothing.
4. **`params_hash` is the one cell identity of every parameter sweep, and the grid has rails, not a cap** (Recency Chart D11; PRD [#1926](https://github.com/tim1016/learn-ai/issues/1926)).
   - `params_hash` (`app/research/sweep/grid.py`) is a stable, key-order-independent hash of one strategy's parameter assignment. It is SHA-256 over `json.dumps({"strategy_key", "params"}, sort_keys=True)` with the default separators. That encoder is frozen. Recency, Grid Search and the Walk-Forward Study all use it as the cell identity, and ADR 0057 D5 answers redelivery with it.
   - The grid language has **no product cap on run count, only engineering rails**:
     - expansion is lazy (a generator; a large but legitimate sweep never fully materializes);
     - a cheap eager size check rejects a pathological or malformed grid above the `MAX_GRID_SIZE` sanity ceiling (2,000,000) before anything runs;
     - concurrency is bounded.
   - Why: a cap would be a product limit nobody asked for, but a fat-fingered step must fail at once rather than exhaust the service.
5. **A trade's evidence fingerprint covers everything that can change the trade, never the parameters alone** (Recency Chart D16).
   - `app/research/recency/fingerprint.py::trade_fingerprint` is SHA-256 over `json.dumps(..., sort_keys=True)` with the default separators (a frozen encoder) of: symbol, strategy key, **strategy code version**, `params_hash`, **data policy** (adjustment, session, resolution), **fill model**, **commissions**, entry ms and exit ms.
   - Why: two runs with the same `params_hash` but a different fill model must never collapse into one piece of evidence. Deduplicating on parameters alone destroyed scientific provenance, as code review found.
   - Deduplication is deterministic. One fingerprint may back several runs. The representative is the newest live run, and the membership set is returned (ADR 0057 D6).
6. **A model's output enters a run only as a precomputed, content-hashed prediction set** ("predictions as data"). Inside a run there is no training and no inference. A model trained elsewhere emits per-(symbol, timestamp) values, and `app/research/ml/` turns them into a canonical artifact that the spec `prediction` primitive reads (`app/engine/strategy/spec/primitives.py`).
   - **Two boundaries only.** Import writes the canonical artifact (`manifest.json` plus parquet chunks). The engine loads that artifact and never re-reads the raw export.
   - **Content, not bytes, is hashed** (`app/research/ml/artifact.py`). `rows_hash` covers the canonical row records sorted by timestamp. `prediction_set_hash` covers the manifest minus its own field. A parquet file hash is never in the manifest, because pyarrow version, metadata or compression drift must not change the identity of identical content.
   - **The ledger carries `prediction_set_hash` as its own identity field.** It is not folded into `strategy_spec_hash`: the spec hash stays a function of what the user authored, and predictions sit beside market data, like `data_snapshot_id`. A spec references at most one prediction set.
   - **Loading fails fast** (`app/research/ml/loader.py`, `coverage.py`). It recomputes the hashes and enforces the leakage invariant: every chunk starts strictly after the end of its training window, and every row lies inside its chunk. Timestamps must be strictly increasing, and the symbol and resolution must pair with the spec. Coverage is checked against the **bar clock**: the run's own data replayed through the same consolidator. Every bar the engine will evaluate needs a prediction. Nights, holidays and unflushed partials need none. Market data defines the clock; predictions decorate it.
   - **Warmup is declared in the manifest** (`neutral_zero_until_feature_ready`), not inferred.
   - Why: plumbing before model. A deterministic generator keeps every artifact bit-reproducible and every backtest hash stable under strict equivalence (ADR 0069). Loosening that contract for real model output (behavioral equivalence) is a later decision. It does not happen silently.

## Consequences

- Any new input that can change a result must join the identity: a ledger column, the fingerprint, or a new hashed artifact. A schema change is a `schema_version` bump. Old ledgers keep loading, and no existing hash is rewritten.
- New identities use `runs/hashing.py::hash_payload`. The two frozen encoders, `params_hash` and `trade_fingerprint`, must not be "unified" with it without a migration. Changing either one changes every persisted `params_hash` and fingerprint, and breaks ADR 0057 D5's redelivery matching.
- `docs/references/run-ledger.md` keeps only the `data_snapshot_id` delimiter constraint. The decisions, identity columns and exclusions are here.
