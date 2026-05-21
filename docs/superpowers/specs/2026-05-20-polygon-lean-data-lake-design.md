# Polygon → LEAN Data Lake — Design Spec

**Status:** Approved (brainstorming complete; ready for implementation planning)
**Date:** 2026-05-20
**Authors:** Tim (architectural review) + Claude (drafting)
**Supersedes parts of:** `docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md` (workspace data-staging), `docs/superpowers/specs/2026-05-19-lean-engine-polygon-parity-design.md` (per-run Polygon-canonical fetch)

---

## Executive summary

A **canonical, read-only data lake** at `${LEAN_DATA_VOLUME_HOST_PATH}/lake` becomes the single source of truth for market data consumed by both the Python engine and the LEAN sidecar. Polygon is the upstream provider; the lake is the local mirror in LEAN's on-disk format. A Postgres catalog (`data_lake_artifacts`) maintains the manifest of what is on disk, with atomic claim-and-complete semantics. Every engine run is preceded by `ensure_data(run_spec)`, which fetches only the missing minute days, materializes corp-action artifacts, and returns a `DataAvailabilityResult` containing a byte-level fingerprint.

The architectural inversion: **engines never call Polygon.** They mount the lake read-only and read pre-staged files. Backend (.NET) orchestrates the run lifecycle; Python is a data/math worker; the LEAN launcher is a narrow runtime adapter.

Load-bearing decisions:

- **One canonical data root.** Configured per deploy as a host-bind directory with `lake/` and `staging/` subdirectories.
- **Postgres catalog** (not SQLite). EF Core migrations from Backend; runtime writes from Python via asyncpg.
- **LEAN-format on ingest.** Polygon responses are transformed into LEAN's deci-cent zip layout at write time. No alternate storage formats.
- **Raw bars only in v1.** Adjustment lives in factor / map files; pre-adjusted variants deferred.
- **Both engines read the same bytes.** Python uses `LeanMinuteDataReader`; LEAN uses its own reader. Same files, same paths, same hashes.
- **Bar-stream equivalence before P&L equivalence.** The Slice 3 ladder gates each rung on the previous passing.

---

## 1. Motivation and non-goals

### Motivation

The pre-data-lake pipeline staged Polygon-fetched bars into per-run workspaces. Two engines (Python in-house + LEAN sidecar) each fetched and staged their own copies of the same data. There was no manifest of "what we have on disk"; every run hit Polygon. Worse, the two engines could end up consuming subtly different bytes for the same nominal `(symbol, date)` because their fetch parameters diverged.

The audit `docs/audits/computational-fidelity-2026-04-22.md` documented downstream symptoms: timestamp inconsistencies, partial reconciliation gaps, and a path where "looks staged but trades wrong" bugs could hide.

### Goals

1. Fetched once, reused forever. A backtest spanning a previously-cached window must not re-fetch from Polygon.
2. Delta fetching. Only the missing minute days for the requested window are fetched.
3. Single physical artifact per `(symbol, date, resolution, data_type, provider, adjustment_mode)`. Both engines consume that artifact.
4. Inspectable cache. Coverage queryable at row granularity from Postgres.
5. Byte-level reproducibility. Two backtests on the same logical inputs produce identical `manifest_sha256`.
6. Engine equivalence is a provable claim, encoded as passing tests at every layer from bar stream to per-trade P&L.

### Non-goals (v1)

- Pre-adjusted bar variants. v1 stores raw + factor files; downstream consumers apply adjustments.
- Hour resolution as a first-class catalog kind. Hour data is consolidated in-algorithm from minute bars; preflight rejects direct `Resolution.Hour` subscriptions in v1.
- True LEAN-vendor daily / hour parity fixtures. The derived daily we ship is repo-internal consistency only; vendor parity is a separately scoped future proof path.
- Multi-provider support. v1 is Polygon-only; the `provider` column is reserved for extensibility.
- Object-storage backing tier. The lake is local filesystem in v1.
- LRU eviction. The lake grows monotonically in v1.

---

## 2. Architecture

### 2.1 Service roles

| Service | Role |
|---|---|
| Backend (.NET) | Workflow orchestrator. Records run requests, calls Python `ensure_data` + `prepare_run`, calls the engine entry point or the LEAN launcher, records outputs. Owns Postgres migrations (EF Core). |
| Python data service | Data/math worker. Hosts `app/data_lake/` (the only writer to the lake). Hosts the Python engine (in-process backtest runner). Hosts `LeanMinuteDataReader` for read paths. No top-level orchestration. |
| LEAN sidecar launcher | Narrow execution adapter. Accepts `data_lake_run_id` + `lean_image_digest`, mounts the workspace + lake under deploy-time-configured roots, runs the LEAN container, returns result metadata. |
| Postgres | Catalog + audit. Knows what artifacts exist and whether they are valid. Never stores bar bytes. |

### 2.2 Volume layout (host-bind)

Explicit host directory; not a podman-managed `_data` path.

```
LEAN_DATA_VOLUME_HOST_PATH=/var/lib/learn_ai_lean_data    (configured per deploy)
  ├─ ${LEAN_DATA_VOLUME_HOST_PATH}/lake          ← the only subtree engines see
  └─ ${LEAN_DATA_VOLUME_HOST_PATH}/staging       ← writer-private
```

`lake/` and `staging/` share the same filesystem (same `stat.st_dev`), so POSIX `rename(2)` is atomic across the boundary. A startup guard asserts this; never silently falls back to copy + unlink.

Container mount table:

| Mount source | Container | Container target | Mode |
|---|---|---|---|
| `${VOLUME}` (full) | Python data service writer | `/lean-data-writer` | rw |
| `${VOLUME}/lake` (subpath) | Python data service reader | `/lean-data` | ro |
| `${VOLUME}/lake` (subpath) | LEAN container | `/lean-run/data` | ro |
| `<workspace>/algorithm` | engine container | `/lean-run/algorithm` | ro |
| `<workspace>/config` | engine container | `/lean-run/config` | ro |
| `<workspace>/manifest.json` | engine container | `/lean-run/manifest.json` | ro |
| `<workspace>/logs` | engine container | `/lean-run/logs` | rw |
| `<workspace>/results` | engine container | `/lean-run/results` | rw |
| `<workspace>/tmp` | engine container | `/tmp/lean-run` | rw |

Two env vars distinguish writer access from reader access within the Python data service:

- `LEAN_DATA_WRITE_ROOT=/lean-data-writer` — only `app/data_lake/` references this.
- `LEAN_DATA_ROOT=/lean-data` — everything else (including `LeanMinuteDataReader`) uses this.

Capability separation is path-disciplined within the container today; container-boundary-enforced tomorrow (Option 1 extraction needs only to repoint `LEAN_DATA_WRITE_ROOT` to a different process).

### 2.3 Control flow

```
Frontend (Angular)
  └─→ Backend (.NET, GraphQL mutation)
       ├─ INSERT data_lake_runs row in Postgres (audit; requested_at_ms, run_spec)
       ├─ POST python-service:8000/api/data-lake/ensure-data
       │    └─→ Python ensure_data:
       │         ├─ Expand session calendar; non-sessions go to skipped_non_sessions
       │         ├─ catalog_client: query coverage per (artifact_kind × identity)
       │         ├─ For each missing artifact:
       │         │    polygon_fetcher → staging/ → validate → sha256 → atomic rename → UPDATE row complete
       │         ├─ factor_files + map_files: refresh if Polygon corp-action revision moved
       │         └─ Return DataAvailabilityResult (paths, hashes, per-artifact status)
       ├─ Backend evaluates partial-coverage policy on the result
       ├─ POST python-service:8000/api/data-lake/prepare-run
       │    └─→ Python prepare_run: materialize workspace + manifest.json + manifest_sha256
       ├─ Backend launches the engine — choice based on run_type:
       │    ├─ python_lab: POST python-service:8000/api/engine/run-python-lab
       │    └─ lean_lab:   POST launcher:8090/launch (with data_lake_run_id + lean_image_digest)
       └─ Backend updates data_lake_runs as engine_status progresses
```

Backend orchestrates; Python prepares; the launcher executes. LEAN-format knowledge (config rendering, manifest hashing, path rules, data policy) stays Python-side.

### 2.4 Failure modes

| Failure | Behavior |
|---|---|
| `ensure_data` partial failure | `DataAvailabilityResult.overall_status='partial'`; Backend evaluates per-`failure.reason` policy |
| `ensure_data` total failure / timeout | Backend reports user-facing error; no engine launches |
| `prepare_run` workspace conflict | Backend idempotency catches; if it slips, prepare_run fails fast with `workspace_already_exists` |
| Atomic rename across filesystems (EXDEV) | Writer startup guard catches at init; prepare_run never reaches this case |
| Engine crashes mid-run | No lake mutation possible (RO mount enforces at the OS level) |
| Concurrent ensure_data for the same artifact | Loser polls on Postgres advisory transition; both return the same `complete` row when winner finishes |

---

## 3. Catalog schema

### 3.1 `data_lake_artifacts`

Single table for all artifact kinds. EF Core migration from Backend; runtime writes from Python `app/data_lake/catalog_client.py` via parametrized asyncpg SQL.

```sql
CREATE TABLE data_lake_artifacts (
  id                     bigserial PRIMARY KEY,
  artifact_kind          text NOT NULL CHECK (artifact_kind IN
                            ('time_series_bars','factor_file','map_file','metadata')),
  market                 text,                       -- nullable; required for non-metadata
  symbol                 text,                       -- nullable; required for non-metadata
  trading_date           date,                       -- nullable; required for time_series_bars
  resolution             text CHECK (resolution IN ('minute','hour','daily')),
  data_type              text CHECK (data_type IN ('trade','quote')),
  provider               text NOT NULL,
  provider_params        jsonb NOT NULL,
  price_adjustment_mode  text CHECK (price_adjustment_mode IN
                            ('raw','polygon_split_adjusted','lean_adjusted')),
  data_contract_hash     char(64) NOT NULL,
  row_count              int,
  first_bar_start_ms     bigint,                     -- start-of-bar (Polygon/LEAN convention)
  last_bar_start_ms      bigint,
  corp_action_revision   char(64),                   -- factor/map only
  file_path              text NOT NULL,              -- relative to lake/
  file_size_bytes        bigint,
  file_sha256            char(64),
  status                 text NOT NULL CHECK (status IN
                            ('fetching','complete','stale','failed')),
  lease_owner            text,
  lease_expires_at_ms    bigint,
  attempt_count          int NOT NULL DEFAULT 0,
  last_error             text,
  error_message          text,
  fetched_at_ms          bigint NOT NULL,
  completed_at_ms        bigint,

  CONSTRAINT artifact_kind_fields CHECK (
    (artifact_kind = 'time_series_bars'
       AND market IS NOT NULL AND symbol IS NOT NULL
       AND resolution IS NOT NULL AND data_type IS NOT NULL
       AND price_adjustment_mode IS NOT NULL
       AND ((resolution = 'minute' AND trading_date IS NOT NULL)
            OR (resolution IN ('hour','daily') AND trading_date IS NULL)))
    OR (artifact_kind IN ('factor_file','map_file')
       AND market IS NOT NULL AND symbol IS NOT NULL
       AND price_adjustment_mode IS NOT NULL
       AND trading_date IS NULL AND resolution IS NULL AND data_type IS NULL)
    OR (artifact_kind = 'metadata'
       AND trading_date IS NULL AND resolution IS NULL AND data_type IS NULL)
  ),

  -- v1 only: single canonical root → raw bars only.
  -- Relaxed in v2 by adding a data_root_id column and dropping this constraint.
  CONSTRAINT raw_only_for_canonical_data_root CHECK (
    artifact_kind = 'metadata' OR price_adjustment_mode = 'raw'
  )
);

-- Partial unique indexes — one identity scheme per artifact kind.
-- Minute bars are keyed per (symbol, trading_date); hour/daily bars are one zip
-- per ticker so they're keyed per (symbol) without trading_date.

CREATE UNIQUE INDEX uq_data_lake_artifacts_minute_bars
  ON data_lake_artifacts (market, symbol, trading_date, data_type,
                          provider, price_adjustment_mode)
  WHERE artifact_kind = 'time_series_bars' AND resolution = 'minute';

CREATE UNIQUE INDEX uq_data_lake_artifacts_aggregated_bars
  ON data_lake_artifacts (market, symbol, resolution, data_type,
                          provider, price_adjustment_mode)
  WHERE artifact_kind = 'time_series_bars' AND resolution IN ('hour','daily');

CREATE UNIQUE INDEX uq_data_lake_artifacts_corp_actions
  ON data_lake_artifacts (market, symbol, artifact_kind, provider, price_adjustment_mode)
  WHERE artifact_kind IN ('factor_file','map_file');

CREATE UNIQUE INDEX uq_data_lake_artifacts_metadata
  ON data_lake_artifacts (data_contract_hash)
  WHERE artifact_kind = 'metadata';

-- Hot-path lookups.
CREATE INDEX ix_data_lake_artifacts_coverage
  ON data_lake_artifacts (market, symbol, resolution, data_type, trading_date)
  WHERE artifact_kind = 'time_series_bars';

CREATE INDEX ix_data_lake_artifacts_corp_action_lookup
  ON data_lake_artifacts (symbol, artifact_kind)
  WHERE artifact_kind IN ('factor_file','map_file');

CREATE INDEX ix_data_lake_artifacts_incomplete
  ON data_lake_artifacts (status, lease_expires_at_ms)
  WHERE status <> 'complete';
```

Notes:

- All timestamps are `bigint *_ms` (UTC ms), per `.claude/rules/numerical-rigor.md`. Only `trading_date` is a `date`, with explicit exchange-local semantics.
- `text + CHECK` constraints over native Postgres enum types — cross-language enum migrations are fussy.
- `file_path` is relative to `lake/`. Engines mount `lake/` at some container path (e.g. `/lean-run/data`) and see `<mount>/<file_path>`.
- `data_contract_hash = sha256` over canonicalized `{provider, provider_params, price_adjustment_mode, session_policy='full' (storage constant), lean_format_version}`. Identifies "interchangeable consumer" fingerprint.
- `corp_action_revision = sha256` over canonicalized JSON of Polygon's normalized splits / dividends / ticker-events. Recomputed by the refresh job; mismatch flips `complete → stale`.

### 3.2 `data_lake_runs`

Sibling table to `StrategyExecution`. Records data-lake-side run audit, joins to existing persistence.

```sql
CREATE TABLE data_lake_runs (
  id                       uuid PRIMARY KEY,
  strategy_execution_id    integer REFERENCES "StrategyExecutions"("Id"),  -- nullable; populated when engine row exists
  engine_run_id            text,                                            -- LEAN run-id slug or Python engine run id
  run_type                 text NOT NULL CHECK (run_type IN ('python_lab','lean_lab')),
  run_spec                 jsonb NOT NULL,
  workspace_path           text,
  manifest_sha256          char(64),
  data_availability_hash   char(64),
  ensure_data_status       text CHECK (ensure_data_status IN
                              ('pending','complete','partial','failed')),
  ensure_data_response     jsonb,
  engine_status            text CHECK (engine_status IN
                              ('not_started','running','complete','failed')),
  requested_at_ms          bigint NOT NULL,
  started_at_ms            bigint,
  completed_at_ms          bigint
);

CREATE INDEX ix_data_lake_runs_strategy_execution
  ON data_lake_runs (strategy_execution_id);
```

### 3.3 Schema ownership + drift defense

- Backend `Backend/Migrations/` owns the EF Core migrations.
- Python `app/data_lake/catalog_schema.py` declares the expected columns / constraints / partial indexes as typed literals.
- `tests/integration/data_lake/test_schema_drift.py` introspects the live DB via `pg_catalog`, asserts equality with the Python expectation, fails CI on mismatch.

---

## 4. `ensure_data` contract

### 4.1 Input — `DataRunSpec`

```python
class DataRunSpec(BaseModel):
    request_id: UUID                              # Backend-supplied; trace correlation + idempotency
    run_type: Literal['python_lab', 'lean_lab']
    requester: str | None = None
    strategy_execution_id: int | None = None

    market: Literal['usa'] = 'usa'
    symbols: list[str]                            # uppercase, min 1
    start_trading_date: date                      # inclusive, exchange-local
    end_trading_date: date                        # inclusive, exchange-local

    resolution: Literal['minute'] = 'minute'      # v1 floor
    data_types: list[Literal['trade','quote']] = ['trade']
    price_adjustment_mode: Literal['raw'] = 'raw'
    provider: Literal['polygon'] = 'polygon'

    include_factor_files: bool = True
    include_map_files: bool = True
    include_lean_metadata: bool = True
    lean_image_digest: str | None = None          # required iff include_lean_metadata

    force_refresh: bool = False
    fetch_timeout_seconds: int = 600
```

Boundary validation in the FastAPI route: symbols match `^[A-Z][A-Z0-9.]*$`, `start_trading_date <= end_trading_date`, range capped (default 5 years) to prevent runaway calls, `lean_image_digest` required iff `include_lean_metadata`.

### 4.2 Output — `DataAvailabilityResult`

```python
class ArtifactRecord(BaseModel):
    id: int
    artifact_kind: str
    market: str | None
    symbol: str | None
    trading_date: date | None
    resolution: str | None
    data_type: str | None
    provider: str                          # 'polygon' for trade; 'learn_ai_derived' for quote (v1)
    price_adjustment_mode: str | None
    data_contract_hash: str
    file_path: str                         # relative to lake/
    file_sha256: str
    row_count: int | None
    first_bar_start_ms: int | None
    last_bar_start_ms: int | None

class ArtifactFailure(BaseModel):
    artifact_kind: str
    symbol: str | None
    trading_date: date | None
    data_type: str | None
    reason: Literal[
        'provider_auth_error',
        'provider_entitlement_error',
        'provider_rate_limited',
        'provider_api_error',
        'provider_no_data',                # expected session; provider returned empty
        'unknown_symbol',
        'validation_failed',
        'io_error',
        'lease_timeout',
        'fetch_timeout',
        'unsupported_resolution',          # e.g., Resolution.Hour in v1
        'internal_error',
    ]
    detail: str | None
    provider_status_code: int | None       # set for provider_*_error
    attempt_count: int

class NonSessionRecord(BaseModel):
    market: str
    trading_date: date
    reason: Literal['weekend', 'market_holiday']

class DataAvailabilityResult(BaseModel):
    request_id: UUID
    overall_status: Literal['complete', 'partial', 'failed']
    lean_data_root_path: str               # absolute host path consumers mount RO
    data_availability_hash: str            # sha256 over byte-AND-contract tuples
    artifacts: list[ArtifactRecord]
    failures: list[ArtifactFailure]
    skipped_non_sessions: list[NonSessionRecord]  # FYI; never failures
    fetched_artifact_count: int
    reused_artifact_count: int
    refreshed_artifact_count: int          # complete → fetching → complete with new bytes
    completed_at_ms: int
    duration_ms: int
```

`data_availability_hash = sha256` over sorted tuples of `(artifact_kind, market, symbol, trading_date, data_type, file_path, file_sha256, row_count, first_bar_start_ms, last_bar_start_ms)`. Includes byte hash and contract — proves same bytes consumed, not just same contract requested.

### 4.3 HTTP boundary

```
POST /api/data-lake/ensure-data
Authorization: Bearer <internal-service-token>
Content-Type: application/json

Body: DataRunSpec → Response: DataAvailabilityResult

200 → overall_status in {complete, partial}
408 → fetch_timeout exceeded with no usable result
422 → DataRunSpec validation failed
500 → unrecoverable writer error
```

Route lives in `app/routers/data_lake.py`. Thin wrapper around in-process `data_lake.ensure_data(spec)`. Future Option 1 swaps in-process call for remote call to the extracted writer service; the contract is unchanged.

### 4.4 Concurrency primitives — three atomic transitions

```sql
-- (1) Claim a new artifact
INSERT INTO data_lake_artifacts (..., status='fetching',
        lease_owner=$me, lease_expires_at_ms=$now_ms + 300_000, attempt_count=1)
ON CONFLICT (per partial unique index) DO NOTHING
RETURNING id;

-- (2) Steal an expired lease OR retry a previous failure
UPDATE data_lake_artifacts
   SET status='fetching',
       lease_owner=$me,
       lease_expires_at_ms=$now_ms + 300_000,
       attempt_count = attempt_count + 1,
       last_error = NULL
 WHERE id = $existing_id
   AND ( (status='fetching' AND lease_expires_at_ms < $now_ms)
      OR (status='failed' AND attempt_count < $max_retries) )
RETURNING id, file_path, file_sha256;

-- (3) Refresh a complete artifact (force_refresh)
UPDATE data_lake_artifacts
   SET status='fetching',
       lease_owner=$me,
       lease_expires_at_ms=$now_ms + 300_000,
       attempt_count = attempt_count + 1
 WHERE id = $existing_complete_id
   AND status = 'complete'
RETURNING id, file_path AS prior_file_path, file_sha256 AS prior_file_sha256;
```

**No advisory locks across network calls.** Claim is a single INSERT; fetch happens with no DB transaction open; completion is a single UPDATE. Lease TTL = 5 minutes; long batched fetches refresh via heartbeats every 60 s.

**Non-destructive refresh.** For transition (3), the file on disk is untouched until the new bytes pass validation. Concurrent engines reading from `lean_data_root_path` during the refresh window see the old, consistent bytes. On validation failure, the row UPDATEs back to `status='complete'` with the *prior* `file_sha256` preserved — no data hole. On success, atomic-rename overwrites the file and the row UPDATEs to the new bytes' metadata.

**Background sweep** (`app/data_lake/sweep.py`) reclaims rows with expired leases via atomic UPDATE to `status='failed', last_error='lease_expired'`.

### 4.5 Session-aware artifact expansion

```python
def expand_required_artifacts(spec: DataRunSpec) -> tuple[list[ArtifactIdentity], list[NonSessionRecord]]:
    """Filter the date range to real exchange sessions BEFORE creating artifact rows.

    Weekends and market holidays never become artifact requirements — they go to
    skipped_non_sessions instead. Half-day early closes ARE sessions (full minute
    coverage for the truncated window).
    """
    sessions, non_sessions = trading_sessions_for(
        spec.market, spec.start_trading_date, spec.end_trading_date
    )
    # trading_sessions_for reads the LEAN market-hours-database — the same
    # metadata artifact ensure_data stages as artifact_kind='metadata'.

    required = []
    for symbol in spec.symbols:
        for trading_date in sessions:
            for data_type in spec.data_types:
                provider = 'polygon' if data_type == 'trade' else 'learn_ai_derived'
                required.append(ArtifactIdentity(
                    artifact_kind='time_series_bars',
                    market=spec.market, symbol=symbol,
                    trading_date=trading_date,
                    resolution='minute', data_type=data_type,
                    provider=provider, price_adjustment_mode='raw',
                ))
        if spec.include_factor_files:
            required.append(ArtifactIdentity(artifact_kind='factor_file', symbol=symbol, ...))
        if spec.include_map_files:
            required.append(ArtifactIdentity(artifact_kind='map_file', symbol=symbol, ...))
        # Daily-trade derived artifact: one per symbol; materialization deferred
        # until all source minute-trade artifacts for this symbol complete.
        if 'trade' in spec.data_types:
            required.append(ArtifactIdentity(
                artifact_kind='time_series_bars',
                market=spec.market, symbol=symbol,
                trading_date=None,                       # daily is per-symbol, not per-date
                resolution='daily', data_type='trade',
                provider='learn_ai_derived',
                price_adjustment_mode='raw',
            ))
    if spec.include_lean_metadata:
        required.append(ArtifactIdentity(artifact_kind='metadata', ...))  # market-hours
        required.append(ArtifactIdentity(artifact_kind='metadata', ...))  # symbol-properties
    return required, non_sessions
```

### 4.6 Batched Polygon fetch

After claiming, group successfully-claimed artifacts by `(symbol, data_type)` and find contiguous trading-date runs. Per run: one paginated Polygon `/v2/aggs/ticker/{sym}/range/1/minute/{start}/{end}?adjusted=false` call. Partition the response by exchange-local trading date; per-day buckets become per-row completions (stage → validate → hash → rename → UPDATE complete).

Quote artifacts (v1): no Polygon call. Synthesize per-day from the same-day trade artifact's bytes; record provenance in `provider_params={source_trade_artifact_id, source_file_sha256}`.

Daily artifacts (v1): no Polygon call. Aggregate from same-symbol minute-trade bars; record provenance in `provider_params={source_minute_artifact_ids, source_file_sha256s}`. **No QC parity claim** for derived daily — used for cross-engine consistency within this repo, not for vendor-equivalent validation.

**Daily materialization timing**: claimed upfront alongside minute artifacts (Section 4.5), but the actual aggregation runs as a post-step within the same `ensure_data` invocation, after all source minute-trade artifacts for `(symbol)` within the request's window reach `status='complete'`. The implementation pattern: `ensure_data` processes claims in two passes — (1) Polygon-sourced + factor/map/metadata artifacts in the batched fetch loop, (2) derived artifacts (daily) after their dependencies complete.

**Daily refresh on incremental ensure_data**: when a later `ensure_data` call adds new complete minute-trade artifacts for a symbol whose daily artifact already exists, the existing daily row is force-refreshed via the Section 4.4 transition (3) — old daily.zip stays usable until new derivation passes validation, then is atomically replaced. See Appendix A for the still-open lifecycle question.

Batch error handling:
- If the batched call fails (rate limit, transient) → all artifacts in that batch marked failed with the same `reason`. Retry counter incremented per row.
- If the call succeeds but a specific date has no bars → only that day fails with `provider_no_data`.

### 4.7 Retry and partial-coverage policy

- Per-artifact retries: `max_retries = 3`, exponential backoff (1 s, 4 s, 16 s), consumed against the remaining `fetch_timeout_seconds` budget.
- Partial-coverage policy lives in **Backend, not Python**. Python reports `overall_status='partial'` whenever `failures` is non-empty. Backend's policy table maps `run_type` × `failure.reason` → proceed / fail. Recommended defaults:
  - `provider_no_data` on a confirmed market holiday → tolerable (but holidays should have been filtered upstream by session expansion)
  - `provider_no_data` on a non-holiday session → fatal (real gap in the data)
  - Everything else → fatal

### 4.8 Cancellation / timeout

- HTTP client cancels → Python catches `asyncio.CancelledError`, cancels in-flight `httpx` Polygon calls, rolls back open DB transactions. Lease rows survive; the sweep reclaims them after `lease_expires_at_ms`.
- `fetch_timeout_seconds` exhausted → Python returns 408 with `overall_status='failed'` and a `fetch_timeout` failure per unresolved artifact.
- Staging files from cancelled fetches are isolated under `staging/<request_id>/` and reclaimed by the sweep — never reachable from the canonical lake because the atomic rename never ran.

### 4.9 Idempotency

A retried POST with the same `request_id` is safe. Python doesn't write `data_lake_runs` (Backend owns that table). For `data_lake_artifacts`, the ON CONFLICT DO NOTHING claim + per-artifact identity tuple means re-runs of the same `DataRunSpec` converge to the same final state regardless of how many times Backend retries.

Backend persists `(request_id, spec_hash)` on its side to dedupe at the orchestration layer.

### 4.10 What `ensure_data` does NOT do

- Does not stage LEAN config or per-run workspaces — `prepare_run` does that (Section 5.5).
- Does not launch any engine.
- Does not consolidate higher resolutions; the engine reader derives them from minute bars at read time.
- Does not refresh `corp_action_revision` on the hot path. A nightly sweep recomputes for symbols touched recently; mismatch flips `complete → stale`, triggering refetch on the next `ensure_data` that needs it.

---

## 5. File layout, atomic write protocol, workspace contract

### 5.1 Volume internal layout

```
${LEAN_DATA_VOLUME_HOST_PATH}            ← single backing filesystem
├── lake/                                ← the only subtree any engine sees
│   ├── equity/usa/
│   │   ├── minute/<sym_lower>/yyyymmdd_{trade,quote}.zip
│   │   ├── daily/<sym_lower>.zip                    (learn_ai_derived; no QC parity claim)
│   │   ├── factor_files/<sym_lower>.csv
│   │   └── map_files/<sym_lower>.csv
│   ├── market-hours/market-hours-database.json
│   └── symbol-properties/symbol-properties-database.csv
└── staging/                              ← writer-private; never mounted into engines
    └── <request_id>/<worker_id>/attempt_<n>/...mirrored .tmp paths
```

Catalog `file_path` is stored **relative to `lake/`** (e.g. `equity/usa/minute/spy/20240520_trade.zip`). Engines see this path under their own mount target.

### 5.2 Atomic rename protocol

POSIX `rename(2)` is atomic only within a single filesystem. Placing `staging/` inside `${LEAN_DATA_VOLUME_HOST_PATH}` alongside `lake/` guarantees src and dst share `stat.st_dev`.

```python
def _assert_atomic_rename_safe() -> None:
    lake_dev    = (LEAN_DATA_VOLUME_HOST_PATH / "lake").stat().st_dev
    staging_dev = (LEAN_DATA_VOLUME_HOST_PATH / "staging").stat().st_dev
    if lake_dev != staging_dev:
        raise RuntimeError(
            "staging/ and lake/ on different filesystems — atomic rename impossible. "
            "Refusing to start the writer. Mount staging/ on the same filesystem as lake/."
        )
```

Fatal at writer init; never silently fall back to copy + unlink.

Per-attempt scoping: `<staging>/<request_id>/<worker_id>/attempt_<n>/<rel_lake_path>.tmp` makes retry collisions structurally impossible. Backend's idempotency handles `request_id` reuse; `worker_id` distinguishes parallel workers; `attempt_n` distinguishes in-worker retries.

Sweep: staging directories older than 24 h with no corresponding `fetching`/`failed` catalog row are reclaimed.

### 5.3 `path_policy.py` — typed LEAN paths

No string concatenation outside this module. Lint rule (`tests/test_no_lean_paths_outside_policy.py`) forbids the substrings `equity/usa/`, `market-hours/`, `symbol-properties/` anywhere outside `app/data_lake/path_policy.py` and its tests.

```python
@dataclass(frozen=True)
class LeanMinuteBarPath:
    market: Literal['usa']
    symbol: str
    trading_date: date
    data_type: Literal['trade', 'quote']
    def relative_path(self) -> Path:
        return (Path('equity') / self.market / 'minute' / self.symbol.lower()
                / f"{self.trading_date.strftime('%Y%m%d')}_{self.data_type}.zip")

@dataclass(frozen=True)
class LeanDailyBarPath:
    market: Literal['usa']
    symbol: str
    def relative_path(self) -> Path:
        return Path('equity') / self.market / 'daily' / f"{self.symbol.lower()}.zip"

@dataclass(frozen=True)
class LeanFactorFilePath:
    market: Literal['usa']; symbol: str
    def relative_path(self) -> Path:
        return Path('equity') / self.market / 'factor_files' / f"{self.symbol.lower()}.csv"

@dataclass(frozen=True)
class LeanMapFilePath:
    market: Literal['usa']; symbol: str
    def relative_path(self) -> Path:
        return Path('equity') / self.market / 'map_files' / f"{self.symbol.lower()}.csv"

@dataclass(frozen=True)
class LeanMetadataPath:
    kind: Literal['market_hours', 'symbol_properties']
    def relative_path(self) -> Path:
        return {
            'market_hours':       Path('market-hours') / 'market-hours-database.json',
            'symbol_properties':  Path('symbol-properties') / 'symbol-properties-database.csv',
        }[self.kind]

def relative_path_for_artifact(identity: ArtifactIdentity) -> Path:
    """Single dispatch; every artifact_kind has exactly one resolver."""
    ...

def staging_path_for(rel: Path, request_id: UUID, worker_id: str, attempt: int) -> Path:
    return (Path('staging') / str(request_id) / worker_id / f"attempt_{attempt}"
            / rel.with_suffix(rel.suffix + '.tmp'))
```

### 5.4 Per-run workspace

Separate from the data lake; lives at `PythonDataService/artifacts/runs/<data_lake_runs.id>/`.

```
artifacts/runs/<uuid>/
├── algorithm/
│   └── main.py                          # strategy source
├── config/
│   └── config.json                      # LEAN config; data-folder → mounted lake path
├── manifest.json                        # SHA256s + run identity + lean_data_root_path
├── logs/                                # engine stdout/stderr
├── results/                             # results.json, tradeLogs, etc.
└── tmp/                                 # engine-internal scratch
```

Workspaces keep: algorithm files, config, logs, results, runtime output.
Workspaces do NOT keep: market data payloads (mounted RO from the lake).

Atomic materialization: written to `artifacts/runs/<uuid>.tmp/`, fsynced, then atomic-renamed to the final path. Backend never observes a partial workspace.

### 5.5 `prepare_run` and `RunHandoff`

```python
async def prepare_run(spec: RunSpec, availability: DataAvailabilityResult) -> RunHandoff:
    """Materialize a per-run workspace given ensure_data's result.

    Preconditions: Backend has gated on availability.overall_status and its own
    partial-coverage policy. prepare_run does NOT call Polygon, does NOT consult
    the lake catalog, does NOT launch any engine.

    Steps (transactional via .tmp + atomic rename):
      1. Allocate workspace at artifacts/runs/<spec.data_lake_run_id>.tmp/
      2. Write algorithm source → algorithm/main.py (fsync)
      3. Render LEAN config.json (fsync) — data-folder=/lean-run/data,
         results-destination=/lean-run/results
      4. Compute manifest_sha256 over canonical-JSON of the manifest content (5.6)
      5. Write manifest.json (fsync)
      6. Atomic rename .tmp → final
      7. Return RunHandoff
    """
```

```python
class RunSpec(BaseModel):
    request_id: UUID
    data_lake_run_id: UUID                       # = data_lake_runs.id
    strategy_execution_id: int | None
    run_type: Literal['python_lab', 'lean_lab']
    algorithm_source: str
    config_overrides: dict[str, Any]
    parameters: dict[str, Any]                   # GetParameter values for LEAN
    starting_cash: Decimal
    start_date: date
    end_date: date
    symbols: list[str]
    lean_image_digest: str | None                # required when run_type='lean_lab'

class RunHandoff(BaseModel):
    request_id: UUID
    data_lake_run_id: UUID
    run_type: Literal['python_lab', 'lean_lab']
    workspace_path: str                          # absolute host path (Backend audit only)
    manifest_sha256: str
    data_availability_hash: str                  # echoed for audit linkage
    lean_data_root_path: str                     # host path (Backend audit only)
    lean_image_digest: str | None
```

### 5.6 `manifest_sha256` content

Canonical JSON of:

```python
{
    'manifest_schema_version': 1,

    'algorithm_sha256':        sha256(algorithm_source),
    'config_sha256':           sha256(rendered_config_json),
    'parameters':              canonical(parameters),

    'run_spec': {
        'symbols':       sorted(symbols),
        'start_date':    start_date.isoformat(),
        'end_date':      end_date.isoformat(),
        'starting_cash': str(starting_cash),
        'run_type':      run_type,
    },

    'engine_version': {
        'lean_image_digest':         lean_image_digest_or_None,           # for lean_lab
        'python_engine_git_rev':     git_rev_or_None,                     # python_lab — intent
        'python_engine_code_sha256': code_sha256_or_None,                 # python_lab — bytes actually ran
    },

    'execution_policy_hash': sha256(canonical({
        'fill_model':         ...,         # e.g. 'signal_bar_close','next_bar_open'
        'fee_model':          ...,         # e.g. 'ibkr_tier_1','zero'
        'brokerage':          ...,         # e.g. 'interactive_brokers','qc_paper'
        'normalization_mode': 'raw',
        'fill_forward':       ...,         # bool
    })),

    'data_availability_hash': availability.data_availability_hash,
}
```

Excluded by design (vary across identical reproductions): `requester`, `requested_at_ms`, `workspace_path`, `lean_data_root_path`, `strategy_execution_id`.

**`python_engine_code_sha256` declared file set** (versioned itself as `python_engine_code_sha256_set_v1`; new file → bump version):

```
PythonDataService/app/engine/**
PythonDataService/app/data_lake/**
PythonDataService/app/lean_sidecar/launcher/**       # launcher client + config only
PythonDataService/app/lean_sidecar/manifest.py
# Excluded: tests/, __pycache__/, *.pyc, artifacts/, logs/, generated runtime files
```

**Schema-version bump policy**: `manifest_schema_version` increments whenever a field is added/removed/renamed. Old manifests stay verifiable; new code computes the right hash based on `manifest_schema_version`.

### 5.7 Launcher path-under-root contract

```python
# Launcher deploy-time config (env vars; NOT per-request)
LAUNCHER_LEAN_DATA_ROOT              = "/var/lib/learn_ai_lean_data/lake"
LAUNCHER_WORKSPACE_ROOT              = "/var/lib/learn_ai_workspaces"
LAUNCHER_LEAN_IMAGE_DIGEST_ALLOWLIST = ["sha256:97884667…"]

class LaunchRequest(BaseModel):
    data_lake_run_id: UUID                       # → workspace dir under LAUNCHER_WORKSPACE_ROOT
    lean_image_digest: str                       # validated against allowlist
    cpu_limit: float = 2.0
    memory_limit_mb: int = 4096

# Launcher resolves paths internally; Backend cannot override.
workspace_path = LAUNCHER_WORKSPACE_ROOT / str(req.data_lake_run_id)
data_root      = LAUNCHER_LEAN_DATA_ROOT
# Refuses if workspace_path doesn't exist OR escapes LAUNCHER_WORKSPACE_ROOT
# (resolve symlinks; assert resolved path startswith resolved root).
```

`RunHandoff.workspace_path` / `lean_data_root_path` survive in the response for Backend's audit logs, but the launcher's mount logic ignores them.

### 5.8 Failure modes

| Failure | Behavior |
|---|---|
| Workspace dir already exists at final path | Backend idempotency catches; if it slips through, prepare_run fails with `workspace_already_exists` |
| Atomic rename across filesystems (EXDEV) | Startup guard catches at writer init; prepare_run never reaches this case |
| Disk full during workspace write | Cleanup `.tmp` workspace; 507 |
| `lean_image_digest` missing for `lean_lab` | 422 from prepare_run validation |
| Partial workspace observed (sweep) | `.tmp/` dir older than 1 h with no `data_lake_runs.workspace_path` set → reclaimed |

---

## 6. Slice sequence

### 6.1 Phased Slice 1 — Foundation + LEAN-lab cutover

| Phase | Deliverable | Approx size |
|---|---|---|
| **1a** | EF Core migrations (`data_lake_artifacts` + `data_lake_runs`); `app/data_lake/{path_policy,catalog_schema,catalog_client}.py`; schema-drift test; `ensure_data` skeleton with fixture-backed Polygon (canned `DataAvailabilityResult` for known inputs). Gated behind a feature flag; no production path. | ~800 LOC |
| **1b** | Real `polygon_fetcher.py` for minute trade aggregates; `lean_writer.py` deci-cent zips; full claim/steal/refresh/retry with leases; atomic-write protocol; byte hash + `data_availability_hash` over byte tuples; sweep skeleton (not yet scheduled). | ~1000 LOC |
| **1c** | `derived.py` (quote + daily, `provider='learn_ai_derived'`); `factor_files.py` + `map_files.py` from Polygon `/v3/reference/*`; LEAN-image metadata extraction migrated into `data_lake/`. | ~800 LOC |
| **1d** | `prepare_run.py` workspace materialization; Backend GraphQL `runBacktest` orchestration; launcher honours `data_lake_run_id` + deploy-time roots; `lean_sidecar_service.run_trusted_sample` retired/shrunk; smoke test SPY 2024-05-20 → 2026-05-20 reproduces existing EMA-crossover trade log. | ~900 LOC |

Each phase is a stacked PR / logical commit series; each passes its own integration tests; production paths unchanged until 1d.

### 6.2 Slice 2 — Python engine cutover

- `app/engine/data/lean_minute_reader.py` — reads deci-cent zips, factor files, map files; emits `TradeBar` streams
- Wire Python engine's backtest path to `LeanMinuteDataReader`; `polygon_capture` fixture loader retained for unit tests only
- `POST /api/engine/run-python-lab` accepts `RunHandoff`
- Bar-stream equivalence test (E.bar): SPY 2024-05-20 → 2026-05-20 via Python reader vs LEAN container reader; assert byte-equal OHLCV. The LEAN side captures its OnData stream via an instrumented algorithm writing `state.csv` (pattern present in `trusted_samples/ema_crossover.py`).

Size: ~800 LOC + integration test.

### 6.3 Slice 3 — Numerical equivalence ladder

```
1. Bar-stream      (lifted from Slice 2; pin tolerances in a real test)
2. Indicator       (EMA5, EMA10, RSI14 on the same bars)
3. Signal          (per-bar ENTER/EXIT/HOLD labels)
4. Order           (orders submitted: side, qty, time)
5. Fill            (fill price + time; LEAN ImmediateFillModel vs in-house SIGNAL_BAR_CLOSE)
6. Fee/commission  (LEAN IBKR-tier vs in-house IbkrEquityCommissionModel)
7. P&L             (per-trade + cumulative)
```

Each rung gates the next. Reconciliation report `docs/references/reconciliations/spy_ema_crossover.md` records tolerances + any accepted divergences.

Size: ~600 LOC of test code + reconciliation report.

### 6.4 Slice 4 — Operational hardening (parallelisable with Slice 3)

Early Slice 4 deliverables (must land before any long Slice 3 ladder run becomes routine):
- Lease-expiry sweep scheduled
- `GET /api/data-lake/coverage` inspection endpoint (Backend GraphQL resolver)

Remaining Slice 4:
- Orphan staging cleanup + `.tmp` workspace cleanup scheduled
- `corp_action_revision` nightly recompute → flips affected factor/map artifacts to `stale`
- Frontend coverage panel (small Angular component)
- Structured log hooks + basic metrics (artifact rows by status, sweep counters, leases reclaimed)
- Authority doc `docs/architecture/data-lake.md` listed in `docs/doc-authority.md`

### 6.5 Slice 5 — Future, deliberately deferred

- Pre-adjusted variants (`data_root_id` column; relax the v1 raw-only CHECK)
- Hour resolution as a first-class catalog kind
- Multi-provider support (Databento, IBKR, etc.)
- True LEAN-vendor daily / hour parity fixtures (separate proof path from the v1 derived-from-minute daily)
- Object-storage backup tier (S3/Azure Blob as cold archive; warm cache stays local-filesystem)
- LRU eviction if disk pressure becomes real

### 6.6 Dependency graph

```
                                ┌─ Slice 4 (operational; parallel with 3,
                                │    but sweep + inspection land before
                                │    Slice 3 routinely runs)
                                │
Slice 1a → 1b → 1c → 1d → Slice 2 ─┴─→ Slice 3 (full ladder)
                                              │
                                              └─→ Slice 5 (deferred, post-ship)
```

---

## 7. Testing strategy

### 7.1 Test taxonomy

| Cat | Scope | Where | Runtime budget |
|---|---|---|---|
| **U** unit | Pure functions in `app/data_lake/*`. No DB, no network, no filesystem beyond `tmp_path`. | `tests/unit/data_lake/` | < 5 s total |
| **I** integration | `ensure_data` against real Postgres + `respx`-mocked Polygon. Concurrency, leases, refresh, retry, failure. | `tests/integration/data_lake/` | < 60 s total |
| **A** atomic-write / filesystem | Rename atomicity, same-device guard, staging isolation, sweep cleanup. Real filesystem under `tmp_path`. | `tests/integration/data_lake/fs/` | < 30 s total |
| **D** schema drift | Introspect live Postgres via `pg_catalog`; assert equality with `catalog_schema.py`. | `tests/integration/data_lake/test_schema_drift.py` | < 5 s |
| **E** equivalence ladder | One file per rung (bar/indicator/signal/order/fill/fee/pnl); each gates the previous. | `tests/integration/parity/ladder/` | 30 s – 5 min each |
| **S** smoke / e2e | Full pipeline through ensure_data → prepare_run → launcher → LEAN container → results. | `tests/e2e/data_lake/` | 5 – 30 min |

### 7.2 Infrastructure decisions

**Postgres**:
- Unit tests: no DB.
- Integration tests: dedicated `learn_ai_test` database; schema dropped + migrated per session; per-test cleanup via `TRUNCATE ... RESTART IDENTITY CASCADE` in a function-scoped fixture.
- CI: `services: postgres` in GitHub Actions; shares the EF Core migration entry point with dev.

**Polygon mocking**:
- `respx` for happy-path + standard error codes.
- Recorded-response fixtures in `tests/fixtures/polygon_capture_v2/` (multi-endpoint: aggregates + splits + dividends + tickers/events).
- `--use-real-polygon` pytest flag is **manual and slow**. CI without this flag never touches the real Polygon API. Regeneration must update `attribution.md`; **never silently rewrites fixtures**.

**LEAN container**:
- Pinned image digest (Section 5.7 allowlist).
- Equivalence-ladder runs cache `state.csv` / trade log / `results.json` under `tests/fixtures/lean_runs/<manifest_sha256>/`. Re-running reads from cache when `manifest_sha256` matches.
- Smoke + parity LEAN container runs assert `--network=none` on the container; prevents hidden Polygon calls.
- Smoke tests do fresh LEAN runs; marked `@pytest.mark.slow`.

### 7.3 Fixture budget rules

- v1 commits fixtures to git (not LFS).
- New committed fixtures > 10 MB require an explicit review note in the PR.
- > 50 MB triggers an LFS / S3 / out-of-tree decision; not silently committed.
- Commit narrow-window `polygon_capture_v2/` for integration and ladder tests.
- Commit LEAN reference outputs per ladder rung, not entire workspaces.
- Large 2-year full-workspace artifacts are generated nightly or stored outside git.

### 7.4 PR smoke + nightly smoke

- **PR smoke**: one symbol, short window (1 month), pinned fixture / mocked Polygon, full orchestration. Required before merge for Slice 1d+ changes. Opt-in CI job in environments without LEAN; required gate where LEAN is available.
- **Nightly smoke**: SPY 2024-05-20 → 2026-05-20 (or the canonical 2-year window). Blocks merge to main if previous night was red.

### 7.5 Fixture freshness / live-gate

- `--use-real-polygon` is manual / slow / opt-in.
- A live regeneration updates `attribution.md` (date, command, parameters); never silent.
- CI never runs with `--use-real-polygon`.

### 7.6 No-network LEAN gate

- Smoke and parity tests assert the LEAN container is launched with `--network=none`.
- This is a test-level assertion (inspect launcher invocation arguments before container start) AND a runtime assertion (the launcher always passes `--network=none` for these test classes).
- Prevents a regression where an algorithm under test silently makes outbound calls.

### 7.7 CI gates per slice

| Slice | Required test categories |
|---|---|
| 1a | U (path_policy, catalog_schema) + D |
| 1b | U (lean_writer) + I (claim / lease / retry / refresh) + A (atomic rename, staging isolation) |
| 1c | U (derived, factor_files, map_files) + I (multi-artifact-kind ensure_data) |
| 1d | I (Backend orchestration via test-double posting to routes) + S (full smoke test) |
| 2 | I (LeanMinuteDataReader against canonical zips) + E.bar |
| 3 | E.indicator → E.signal → E.order → E.fill → E.fee → E.pnl, each gating the next |
| 4 | I (sweep: lease expiry, orphan staging, .tmp workspace cleanup) + I (coverage endpoint) |

Per-commit: U + D. Per-PR: U + D + I + A + applicable E rungs + PR smoke (1d+). Nightly: full S.

### 7.8 Test naming convention

Per `.claude/rules/python.md` and `.claude/rules/testing.md`:
- `test_<function>_<scenario>` for unit/integration
- Ladder tests use the rung name explicitly: `test_bar_stream_equivalence_spy_ema_crossover_2024`, `test_indicator_equivalence_ema5_spy_ema_crossover_2024`, `test_pnl_equivalence_spy_ema_crossover_2024`
- Drift: `test_postgres_schema_matches_catalog_schema_module`

### 7.9 Project-level acceptance criteria

"Complete equivalence between the Python engine and the LEAN engine" is provable when:

1. Slice 1 (1a–1d) merged. LEAN sidecar runs end-to-end against the lake; smoke test green.
2. Slice 2 merged. Python engine reads from the same files; E.bar green.
3. Slice 3 ladder fully green on SPY EMA crossover 2024-05-20 → 2026-05-20.
4. Reconciliation report published at `docs/references/reconciliations/spy_ema_crossover.md` with tolerances + any accepted divergences.
5. Slice 4 sweep + inspection endpoint deployed.
6. Slice 5 items deliberately deferred and documented.

---

## 8. Authority and references

### Authority

This document is the authority for the Polygon → LEAN data-lake pipeline. On merge, add to `docs/doc-authority.md`:
```
- docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md
```
After Slice 1d ships, a companion `docs/architecture/data-lake.md` operational doc will be added (Slice 4 deliverable).

### Supersedes
- `docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md` § 4.4 (workspace data-staging is retired in favour of mounting the lake RO)
- `docs/superpowers/specs/2026-05-19-lean-engine-polygon-parity-design.md` (per-run Polygon-canonical fetch is retired in favour of the lake catalog)

### References

- `.claude/rules/numerical-rigor.md` (timestamp policy, tolerances, reconciliation taxonomy)
- `.claude/rules/python.md` (FastAPI conventions, asyncpg, ruff scope)
- `.claude/rules/dotnet.md` (EF Core, Hot Chocolate v15)
- `.claude/rules/testing.md` (cross-stack testing standards)
- `docs/architecture/lean-sidecar-lab.md` (launcher invariants; pre-data-lake topology)
- `docs/audits/computational-fidelity-2026-04-22.md` (timestamp-format incident that motivates the `int64 ms UTC` rule)
- QuantConnect data format reference: https://www.quantconnect.com/docs/v2/lean-engine/key-concepts/data-format
- QuantConnect data storage layout: https://www.quantconnect.com/docs/v2/lean-engine/key-concepts/format-and-storage

---

## Appendix A — Open implementation questions

These are smaller decisions deferred to the implementation plan; they do not change the design above:

- Sweep job scheduler: cron via the existing `auto-research-tick` machinery, or a separate `asyncio` background task in the Python service? Decide in Slice 4.
- Coverage endpoint shape: GraphQL query returning per-symbol gap arrays, or REST with date-range query params? Decide in Slice 4.
- Backend retry/backoff for `ensure_data` 408 timeouts: exponential, with what cap? Decide in Slice 1d.
- LeanMinuteDataReader's daily-aggregation seam: derive on read each time, or cache the derivation? Decide in Slice 2 against an A/B benchmark.
- Daily-artifact refresh trigger: re-derive on every `ensure_data` call that completes new minute-trade artifacts for the symbol (simplest, may be wasteful when the new dates don't extend the window), or only when the union of source artifact IDs changes (precise, but requires diffing `provider_params`). Decide in Slice 1c.
