# ADR 0049 — The data lake is the market-data authority: files canonical, Postgres catalog and coordination only

**Status:** Proposed 2026-08-27
**Provenance:** Decision ticket [#1831](https://github.com/tim1016/learn-ai/issues/1831), child of the Data Lake Enablement PRD [#1825](https://github.com/tim1016/learn-ai/issues/1825). HITL: standing requires human acceptance; this ADR must not be marked `Accepted` without the user.
**Decision drivers:** The platform fetches historical market data three separate ways with no shared authority — the backtest engines' policy-keyed zip cache re-downloads a full requested range whenever a single day is missing (one symbol's recorded provenance shows 43 overlapping fetches, per the PRD), the chart/indicator routers hit the Polygon API fresh behind only a 15-minute cache, and a fully designed, built, and tested Postgres-catalogued LEAN-format data lake sits dark behind `DATA_LAKE_ENABLED=False` (`PythonDataService/app/config.py:100`) with its final wiring never completed. This PRD finishes and enables the lake as the single authority; this ADR gives that standing a decision record instead of an implied one.
**Related:** ADR 0001 (control-plane substrate: files + hash sidecars canonical, Postgres a future projection, never a source of truth — this ADR's scoping baseline), ADR 0022 (temporal authority: every wire/storage surface above the lake's vendored files is `int64 ms UTC` — Decision 1a scopes the one deliberate exception), ADR 0039 (an ADR's Status states decision standing, not code conformance — load-bearing here, since parallel PRD slices are still wiring engines onto the lake as this is written), ADR 0048 (the fencing-generation precedent Decision 1b names as the shape a future fix to the expired-lease race would take, not a fix this ADR makes).
**Vocabulary:** Owed on acceptance: `CONTEXT.md` has no entry today for *data lake*, *artifact*, or *catalog* in the market-data sense — its "Custody log and fold" and "Execution ledger" entries are the broker-custody domain, a different authority than the one this ADR names. Per ADR 0040 Decision 4.

## Context

### What the lake already is in the tree

`PythonDataService/app/data_lake/` holds a complete, working implementation: `ensure_data.py` (the delta-fetch entry point, `async def ensure_data(spec: DataRunSpec) -> DataAvailabilityResult`), `catalog_client.py` (an `asyncpg` pool against Postgres — claim/lease/coverage queries, no bar bytes), `catalog_schema.py` (the Python mirror of the EF Core migration, drift-tested against `pg_catalog`), `path_policy.py`, `lean_writer.py`, `factor_files.py`, `map_files.py`, `sweep.py` (lease-expiry reclaim), and the Polygon-side fetchers. `catalog_schema.DATA_LAKE_ARTIFACTS` has no bytes/blob column — only `FilePath`, `FileSha256`, `FileSizeBytes`, row counts, timestamps, and lease/claim state. Postgres was built to hold metadata about the files, never the files' contents. `Backend/Migrations/20260521033222_AddDataLakeArtifactsAndRuns.cs` is the EF Core migration that owns the schema, per the design's service-role split (Backend owns Postgres migrations; Python owns the only writer).

The lake is currently reachable in-process (`app/routers/data_lake.py` exposes `ensure_data` over a thin `POST /api/data-lake/ensure-data` used by the observatory/backfill surface this PRD's later slices add) but is not yet the engines' or charts' read path — `DATA_LAKE_ENABLED` defaults `False`, the Python engine and LEAN sidecar still resolve data through the older policy-store cache (`app/engine/data/policy_store.py`), and no Backend code path calls `ensure-data` or `prepare-run` today (grep confirms only the EF migration references those table names). This ADR names the target authority; wiring the remaining consumers onto it is the rest of the PRD's slices, several of which are landing in parallel with this record. Per ADR 0039 Decision 1, the `Proposed` status below states standing, not that every consumer has been re-pointed yet.

### The original design authority

The lake's design is `docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md`, pruned from the tree but recovered from git history with:

```shell
git show 8441f4f6^:docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md
```

The lake's live source cites that spec by section number throughout (e.g. `catalog_schema.py`'s docstring points at "§ 3", `sweep.py`'s at "§ 4.4"); this ADR does the same, and any successor reading a `§ N` citation in the code should recover the spec with the command above before assuming the section renumbered.

The spec's **§2.1 service roles** table draws the authority split this ADR ratifies: Python `app/data_lake/` is "the only writer to the lake" and hosts the reader; Postgres is "Catalog + audit. Knows what artifacts exist and whether they are valid. **Never stores bar bytes.**" Its **§2.2 volume layout / mount table** enforces that split at the mount boundary — a writer-only `LEAN_DATA_WRITE_ROOT` mount (`/lean-data-writer`, rw) that only `app/data_lake/` ever references, versus a `LEAN_DATA_ROOT` mount (`/lean-data`, ro) for every reader (`LeanMinuteDataReader`, the LEAN sidecar's own `/lean-run/data` mount), with `lake/` and `staging/` sharing one filesystem specifically so the atomic `rename(2)` publish is real, not copy-then-unlink. Its **§2.3 control flow** is the flow Decision 3 below deliberately departs from.

## Decision

### 1. The lake is the single authority for historical bar data; files are canonical, Postgres is catalog and coordination only

Hash-receipted LEAN-format artifact files under `lake/` are the canonical bytes for every historical bar the platform consumes — both engines and, per this PRD, the chart/indicator read path for completed sessions. Postgres's `data_lake_artifacts` / `data_lake_runs` tables hold metadata about those files (path, hash, size, status, lease/claim state, provenance) and coordinate concurrent writers; they never hold a bar. Deleting every row and rebuilding the catalog by re-hashing `lake/` would lose zero bar data — that is the operational test for "coordination, not authority," and the schema (no bytes column, confirmed above) satisfies it structurally, not by convention.

### 1a. Temporal representation inside the canonical files is scoped, not silent — a deliberate deviation from ADR 0022

The on-disk LEAN format is not `int64 ms UTC`. A minute artifact's row is `ms_since_midnight,open,high,low,close,volume` with the trading date carried only in the file path (`{trading_date:%Y%m%d}_{data_type}.zip`, `path_policy.py`); a daily artifact's row is the literal string `"YYYYMMDD 00:00"` (`app/engine/data/lean_format.py`, `app/data_lake/derived_daily.py`). The catalog's own `TradingDate` column is a Postgres `date`, not a millisecond value. None of this is `int64 ms UTC`, and ADR 0022 is unambiguous that every wire/storage temporal value should be.

This ADR scopes that gap deliberately rather than leaving it silent. The LEAN on-disk encoding is a **vendored, parity-mandated serialization**: byte-for-byte compatibility between the Python engine's reader and the LEAN sidecar's own C# reader is the entire reason the lake exists in this format — it is QuantConnect's on-disk format, not this platform's choice to redesign, and diverging from it would break the two-engine parity claim the lake is chartered to prove. ADR 0022 governs what is *in flight, at rest, or on the wire* for this platform's own surfaces; the LEAN file is a vendored on-disk format the read boundary owns, not a wire payload this platform authors. The reconstruction back to a real instant happens exactly at that read boundary, through the mechanism ADR 0022 requires: `ensure_data.py` and `app/engine/data/lean_format.py` both reconstruct the ET wall-clock via `ZoneInfo("America/New_York")` (confirmed: `_ET = ZoneInfo("America/New_York")` in `ensure_data.py`, `EASTERN = ZoneInfo("America/New_York")` in `lean_format.py`) — never a fixed offset — and `lean_format.py` converts the result to `int64 ms UTC` through the shared `app.utils.timestamps` helpers (`datetime_at_ms`, `to_ms_utc`) before it leaves the reader. Every surface above the files — `DataAvailabilityResult`, the catalog's `FetchedAtMs`/`CompletedAtMs`/`LeaseExpiresAtMs`/`FirstBarStartMs`/`LastBarStartMs` columns, and every API response — carries `int64 ms UTC` per ADR 0022, without exception. The vendored format's `TradingDate`-as-Postgres-`date` column is the one place this ADR does not extend that rule to the catalog itself, and it is named here rather than left for a future audit to rediscover.

### 1b. Integrity is hash-receipted and catalog-verified, not filesystem-immutable

Publishing a file is not append-only. `atomic_write_and_promote` (`app/data_lake/atomic.py`) promotes via `os.replace(staged, final)` to a **fixed identity path** derived from artifact identity (symbol, date, kind) — not a content-addressed path — so nothing at the filesystem level prevents a second write to the same path. What actually backs the "canonical bytes" claim is that every publish computes a SHA-256 of what it wrote and the catalog records that hash (`catalog_client.complete_artifact`) as the receipt a consumer trusts: integrity is catalog-verified, not filesystem-enforced.

Deliberate replacement is a supported, catalog-mediated protocol, not a violation of that claim: `refresh_complete_minute_bar` explicitly transitions a row `'complete' → 'fetching'` for an operator-triggered day refresh (correcting a provider revision), and `steal_or_retry_minute_bar` reclaims a `'fetching'` row whose lease expired, or a `'failed'` row within its retry budget. Both go through the catalog first, and both update the recorded hash atomically with the replacement.

**A known hazard, named rather than fixed here.** `complete_artifact`'s guard is `WHERE "Status" = 'fetching'` — it does not check lease ownership. A writer whose lease has already expired and been stolen by a second worker (`steal_or_retry_minute_bar`) can still be alive and mid-fetch; its own later `os.replace()` call is unguarded by any check against the current lease owner or a fencing generation, so it can silently overwrite the file the new owner already published — *after* the catalog row has already been marked `'complete'` under the new owner's hash. The catalog's `Status='fetching'` guard stops the zombie writer's metadata update from clobbering the winner's row, but nothing stops the zombie's file write from clobbering the winner's bytes, leaving the recorded hash pointing at bytes no longer on disk. This ADR does not resolve that race — it is a protocol-level gap, not a decision this record is making — and names it so a future reader does not mistake this ADR's silence for "handled."

### 1c. v1 read-path scope: which consumers adopt the lake, and which remain direct

Adoption in this PRD is exactly two read paths: the backtest engines (the Python engine and the LEAN sidecar, #1833/#1834) and the chart/indicator routers (`app/routers/aggregates.py`, `app/routers/indicators.py`, split at the session boundary per the PRD). Every other direct-Polygon consumer in the tree is explicitly **out of scope** and keeps calling `PolygonClientService.fetch_aggregates` directly — named here so the boundary is a decision, not an oversight discovered later: `app/routers/jobs.py` (rule-based backtest, Feature Research, and Signal Engine jobs), `app/services/dataset_service.py`, `app/research/batch_runner.py`, `app/research/options/iv_builder.py`, `app/services/options_companion_service.py`, `app/volatility/data_loader.py`, and `app/research/divergence/ingest/polygon_ingest.py`. Bringing any of these onto the lake is a future adoption decision, not implied by this one.

### 2. Scoping against ADR 0001

ADR 0001 decided "files + Parquet + hash sidecars canonical; no Postgres in the live-runtime control plane," scoped explicitly to that plane — the run ledger, decision/execution/trade Parquet, halt flags. The market-data lake is a different authority domain: it governs historical bar acquisition and storage, not live-run state, so it does not fall inside ADR 0001's original scope statement by name.

It honors ADR 0001's doctrine anyway, by choice rather than by accident. The lake's Postgres usage is structurally the same shape ADR 0001's own amendments later sanctioned for `clerk_transactions` and the IBKR lifecycle projector: a rebuildable read-model derived from a canonical substrate, never a second custody of the substrate's content. The one real difference — the catalog row is written concurrently with the file it describes (claim → fetch → atomic rename → mark complete), not tailed after the fact from a journal — changes *when* the row is written, not *what it is for*: arbitrating concurrent writers and answering "what do we have," never answering "what are the bytes." A lake whose catalog stored bar bytes would conflict with ADR 0001; this one's catalog does not. Deliberate, catalog-mediated file replacement (Decision 1b) is not a conflict with that doctrine either — the catalog's hash column is exactly the audit record ADR 0001's file-plus-hash-sidecar doctrine asks for, and it is rewritten atomically with each mediated replacement. The *unmediated* race Decision 1b names is a known protocol hazard, not a considered design choice, and this ADR calls it out rather than implying a blanket immutability that the code does not actually enforce.

### 3. Deliberate deviation from the 2026-05-20 spec's §2.3 Backend-orchestrated flow

The spec's §2.3 control flow routes every run through the .NET Backend: insert a `data_lake_runs` audit row, `POST ensure-data`, evaluate partial-coverage policy, `POST prepare-run`, then launch the engine and update the run row. That flow was never built — the only piece of §2.3 the Backend ships today is the EF Core migration for the two Postgres tables; no Backend code calls `ensure-data` or `prepare-run`.

The decision instead is **in-process Python orchestration at run materialization**: rather than the Backend driving `ensure-data`/`prepare-run` over HTTP, the Python engine and the LEAN sidecar are to call `ensure_data()` directly, as a function call in the same process, at the point where the retiring policy-store export currently sits. That wiring is #1833 (Python engine) and #1834 (LEAN sidecar), both in flight in parallel with this ADR — as Context above states, `DATA_LAKE_ENABLED` is still `False` today and both engines still read the policy-store cache. What already exists is the shape the decision requires, proven rather than assumed: `ensure_data()` is a plain in-process `async def` with no HTTP dependency, and `app/routers/data_lake.py`'s `POST /api/data-lake/ensure-data` already calls it exactly this way — in-process, no Backend round trip — for the observatory/backfill surface. #1833/#1834 apply the same call at the run-materialization seam instead of a request handler; the pattern is not new, only its second call site is pending.

This is a deliberate simplification, not an oversight: the spec's HTTP hop existed to let .NET own orchestration state while Python did the work, but the consumers of lake data (the Python engine, the LEAN sidecar's launch preparation) already run in or adjacent to the Python data service. Routing through .NET and back adds a same-host network boundary with no isolation benefit once the writer and its primary callers are colocated. The Backend's own run lifecycle (GraphQL mutation, its EF-owned audit tables) is unchanged by this decision; what changes is who drives `ensure_data` for an engine run.

### 3a. Partial-coverage acceptance is owned by the run-materialization seam, not by `ensure_data` itself

The rejected flow named an owner for this: spec §2.3's "Backend evaluates partial-coverage policy on the result" step, sitting between `ensure-data` returning and `prepare-run` being called. Collapsing the HTTP hop must not collapse that ownership into silence. `ensure_data()` returns `DataAvailabilityResult.overall_status: Literal["complete", "partial", "failed"]` — it reports coverage, it does not raise on partial — and the code still names the old owner: the market-hours bootstrap-failure path in `ensure_data.py` comments that a failure is "surface[d] as `ArtifactFailure` so Backend's partial-coverage policy can gate," a reference to an owner that no longer orchestrates the flow at all.

This ADR reassigns that ownership: **the run-materialization seam that calls `ensure_data()` — the Python engine's lake bridge and the LEAN sidecar's equivalent, both #1833/#1834 — decides whether to launch, refuse, or narrow a run on a `partial` result.** `ensure_data` itself must not be the accept/refuse authority, because it cannot know whether a given caller's policy is "refuse on any missing day" (a strategy backtest whose parity claim depends on full coverage) or "proceed on best-effort" (an exploratory or operator-triggered run) — that is a caller-scoped policy question, not a lake-scoped one. This decision fixes *who* answers it; the specific policy value each seam applies is left to #1833/#1834's implementation and is not prescribed here.

### 3b. Durable run-receipt binding is deferred, not silently dropped

The rejected flow's `data_lake_runs` row was also where a run's identity met the bytes it consumed. Neither side of the in-process replacement writes that binding today: `Backend/Models/MarketData/DataLakeRun.cs` and its `AppDbContext` registration exist, but no Backend code path ever inserts a row, and no Python `catalog_client` function does either — grep across both stacks confirms the table is schema-only. What the engine currently carries is `DataAvailabilityResult.data_availability_hash`, present on the `ensure_data()` response (and whatever the caller chooses to log), with nothing durable joining that hash back to a specific run's record after the process exits.

This is recorded as a named follow-up of the integration slice, not papered over: a durable run → manifest-fingerprint binding (satisfying the PRD's user story 4 — "every run records a manifest fingerprint of the exact bytes it consumed") still needs an owner and a write path, whether that is finally populating `data_lake_runs` from Python or a different durable receipt a later slice chooses. Until one lands, "which exact bytes did run X consume" is answerable only from that run's own logs, not from a queryable store.

### 4. Provider-licensing caveat — recorded, not resolved

Polygon.io's provider terms may bound how long fetched historical data may be retained. **This ADR does not resolve that question and neither does the lake's implementation.** No code under `app/data_lake/` enforces a retention TTL or expiry, and the catalog schema has no expiry column — the lake, as built, retains everything it ever fetches, indefinitely, by omission rather than by a reviewed decision that indefinite retention is licensed. This is recorded here as an open, unresolved compliance gate specifically so it is never mistaken for a cleared one. Clearing it is a human/legal task outside engineering scope (the PRD's "Out of Scope" says the same); until it is cleared, the honest state is "unknown," not "fine."

### 5. Spec recovery

The pruned design spec is recovered with:

```shell
git show 8441f4f6^:docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md
```

Sections cited above: §2.1 (service roles), §2.2 (volume layout / mount table), §2.3 (control flow, the flow Decision 3 departs from), §3 (catalog schema, mirrored by `catalog_schema.py`), §4.4 (concurrency primitives, used by `sweep.py`).

### 6. TimescaleDB deferral — two triggers, two different consequences, neither decided here

No SQL/columnar analytics projection (TimescaleDB or otherwise) is introduced by this decision. Two triggers are named, and they do not lead to the same place:

- **Live WebSocket ingestion** — recording a live broker/vendor feed (IBKR bars, a Massive WebSocket) into queryable time-series storage. This trigger, if it fires, is **not** a projection of the lake: live ticks never enter `lake/` (live feed evidence ledgers are a separate authority from market-data history, per the PRD's "Out of Scope"), so there would be no canonical lake artifact underneath a Timescale table to project *from*. Firing this trigger is a **separate ingestion-store decision** — its own future ADR, deciding what is canonical for live-recorded data in the first place. This ADR does not pre-decide that question, and TimescaleDB is not implied to be its answer.
- **A large symbol universe** — where per-artifact catalog and coverage queries against `data_lake_artifacts` stop being sufficient for the operator's or a strategy's hot-path questions. This trigger, unlike the first, *is* answerable within this ADR's scope: the canonical bytes already exist in `lake/`, so an analytics table serving it would be a **reader-side projection downstream of the lake's canonical hashed files**, rebuildable from them, never a second bar-byte authority — the same shape Decision 2 already establishes for Postgres's own catalog.

Until either fires, `data_lake_artifacts` already serves catalog and coverage queries at the platform's current scale, and introducing a second store to answer questions the first already answers is exactly the premature-migration Postgres-as-substrate ADR 0001 rejected.

## Considered and rejected

**Storing bar data in Postgres (or Timescale) directly, bypassing the file substrate.** Rejected for the same reason ADR 0001 rejected a Postgres-authoritative control plane: it discards content-hash and atomic-rename audit properties the file substrate provides for free, and it is the option this PRD's leak-patch slice (Task 1, throwaway insurance) exists specifically to avoid needing — a database row has no independent hash to verify against a redownload.

**Keeping the spec's Backend-orchestrated HTTP flow.** Rejected per Decision 3: the isolation benefit it was designed for does not apply once the writer and its primary callers share a process, and the flow was never actually built to abandon in place.

**Treating the provider-licensing question as implicitly cleared because nothing has gone wrong yet.** Rejected. Silent-pass is exactly the failure mode ADR discipline exists to prevent (the PRD's own compliance-owner user story asks for the caveat recorded, not resolved by assumption).

**Making `ensure_data` itself refuse on any partial coverage.** Rejected in Decision 3a: it would hard-code one policy for every caller, when a strategy backtest's parity claim and an operator's exploratory backfill plausibly want different answers to the same `partial` result.

**Normalizing the LEAN on-disk format to `int64 ms UTC`.** Rejected in Decision 1a: the format is vendored specifically so the Python engine and the LEAN sidecar's C# reader stay byte-for-byte compatible; changing the serialization to satisfy ADR 0022 at rest would break the two-engine parity claim the lake exists to prove, in exchange for a rule ADR 0022 does not actually require of a vendored on-disk format.

**Fixing the expired-lease race in this ADR.** Rejected as out of scope for a decision-standing record: the race (Decision 1b) is a protocol bug in `atomic.py`/`catalog_client.py`, not an architecture decision. A fix — most plausibly a fencing generation checked atomically at `os.replace` time, the same shape ADR 0048 required for the SQLite Alpaca authority — is implementation work for a future slice.

## Consequences

**Positive:**

- One authority answers "what market data do we have," replacing three unauthoritative fetch paths with one, once the remaining PRD slices land.
- The lake's file-canonical, content-hashed design is proven ADR-0001-compatible rather than merely unopposed by it — a future reviewer does not have to re-derive whether the two decisions conflict.
- The provider-licensing gap is on the record as *unresolved*, which is strictly better than the status quo (nowhere on the record at all) even before it is cleared.
- The Backend-orchestration deviation is documented for the spec's successors, satisfying the PRD's user story 24 directly.
- The partial-coverage policy owner, the temporal scoping against ADR 0022, and the v1 consumer boundary are now decisions on record rather than gaps a future reviewer would have had to rediscover independently.

**Negative / accepted:**

- Postgres remains a hard operational dependency for coordination (claims, leases, coverage queries) even though it stores no bar bytes. No catalog-rebuild-from-files tool exists in the tree today; losing the catalog does not lose data, but recovering coordination state would require building that rebuild path first, not just re-hashing files by hand.
- The provider-licensing gate stays open. Nothing in this PRD or ADR blocks on it, which means the lake will accumulate data under an unresolved retention question until a human clears it — accepted deliberately rather than blocking the whole PRD on a legal answer no engineering task here can produce.
- **The expired-lease overwrite race (Decision 1b) ships unfixed.** A zombie writer can silently invalidate a just-published, catalog-marked-complete file's hash. Named, not resolved, by this ADR; the fix is future implementation work, tracked here so it cannot be waved off as "the files are immutable, so this can't happen."
- **The durable run → manifest-fingerprint binding does not exist yet (Decision 3b).** `data_lake_runs` is schema-only on both stacks; until a slice populates it (or a substitute durable receipt), "what bytes did run X consume" is answerable only from logs.
- **Six named consumers stay on direct Polygon calls (Decision 1c)** — `jobs.py`'s research jobs, `dataset_service.py`, `batch_runner.py`, the options/IV and volatility paths, and the divergence ingest job. None gets the lake's delta-fetch or hash-receipt benefit in this PRD; each is a separate future adoption decision.
- This ADR records a decision, not a conformance claim (ADR 0039). At the time of writing, `DATA_LAKE_ENABLED` is still `False` and the engines still read the policy-store cache; the wiring that makes this authority real in production is the rest of this PRD's slices (#1833, #1834, and others), several in flight concurrently with this record.
- **Status stays `Proposed`.** Per the issue's HITL marking, only the user may move this to `Accepted`; the `Vocabulary:` obligation above (a `CONTEXT.md` entry for the market-data lake) is owed at that time, per ADR 0040 Decision 4, not before.
