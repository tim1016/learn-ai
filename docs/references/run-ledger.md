# Run ledger and reproducibility hashing

**Concept**: A `RunLedger` is the immutable identity record for one execution of a `StrategySpec` through the canonical event-driven engine. Two runs whose identity columns are equal are guaranteed by the deterministic engine to produce identical results — so their content hashes (`result_hash`, `trade_log_hash`, `metrics_hash`) must match.

**Decisions**: [ADR 0072](../architecture/adrs/0072-research-run-identity-and-sealed-run-inputs.md) holds the canonical-JSON hashing contract, the identity columns, the hashing exclusions, the `data_snapshot_id` design and the failed-run identity rule.

**Canonical implementation**: `PythonDataService/app/research/runs/` (`hashing.py`, `ledger.py`, `runner.py`, `storage.py`) and `app/routers/research_runs.py`.

## `data_snapshot_id` design

The id is pipe-joined: `f"{symbol}\|{resolution_minutes}\|{start_ms}\|{end_ms}\|{data_root_revision}"`. **Constraint**: components must not contain `\|` themselves. Symbols are uppercase tickers in this repo (no embedded pipes); the `data_root_revision` forms ADR 0072 decision 2 lists carry no pipe (an operator-set `$LEAN_DATA_ROOT_REVISION` must not add one); the integer fields are unambiguous. If a future symbol family (synthetic baskets, futures spreads) introduces pipes, swap to JSON-array encoding without changing the public function name.
