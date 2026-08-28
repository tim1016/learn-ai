# ADR 0049 — The data lake is the market-data authority: files canonical, Postgres catalog and coordination only

**Status:** Proposed 2026-08-27
**Provenance:** Decision ticket [#1831](https://github.com/tim1016/learn-ai/issues/1831), child of the Data Lake Enablement PRD [#1825](https://github.com/tim1016/learn-ai/issues/1825). HITL: standing requires human acceptance; this ADR must not be marked `Accepted` without the user.
**Decision drivers:** The platform fetches historical market data three separate ways with no shared authority — the backtest engines' policy-keyed zip cache re-downloads a full requested range whenever a single day is missing (one symbol's recorded provenance shows 43 overlapping fetches, per the PRD), the chart/indicator routers hit the Polygon API fresh behind only a 15-minute cache, and a fully designed, built, and tested Postgres-catalogued LEAN-format data lake sits dark behind `DATA_LAKE_ENABLED=False` (`PythonDataService/app/config.py:100`) with its final wiring never completed. This PRD finishes and enables the lake as the single authority; this ADR gives that standing a decision record instead of an implied one.
**Related:** ADR 0001 (control-plane substrate: files + hash sidecars canonical, Postgres a future projection, never a source of truth — this ADR's scoping baseline), ADR 0022 (temporal authority: every lake timestamp is `int64 ms UTC`), ADR 0039 (an ADR's Status states decision standing, not code conformance — load-bearing here, since parallel PRD slices are still wiring engines onto the lake as this is written).
**Vocabulary:** Owed on acceptance: `CONTEXT.md` has no entry today for *data lake*, *artifact*, or *catalog* in the market-data sense — its "Custody log and fold" and "Execution ledger" entries are the broker-custody domain, a different authority than the one this ADR names. Per ADR 0040 Decision 4.

## Context

### What the lake already is in the tree

`PythonDataService/app/data_lake/` holds a complete, working implementation: `ensure_data.py` (the delta-fetch entry point, `async def ensure_data(spec: DataRunSpec) -> DataAvailabilityResult`), `catalog_client.py` (an `asyncpg` pool against Postgres — claim/lease/coverage queries, no bar bytes), `catalog_schema.py` (the Python mirror of the EF Core migration, drift-tested against `pg_catalog`), `path_policy.py`, `lean_writer.py`, `factor_files.py`, `map_files.py`, `sweep.py` (lease-expiry reclaim), and the Polygon-side fetchers. `catalog_schema.DATA_LAKE_ARTIFACTS` has no bytes/blob column — only `FilePath`, `FileSha256`, `FileSizeBytes`, row counts, timestamps, and lease/claim state. Postgres was built to hold metadata about the files, never the files' contents. `Backend/Migrations/20260521033222_AddDataLakeArtifactsAndRuns.cs` is the EF Core migration that owns the schema, per the design's service-role split (Backend owns Postgres migrations; Python owns the only writer).

The lake is currently reachable in-process (`app/routers/data_lake.py` exposes `ensure_data` over a thin `POST /api/data-lake/ensure-data` used by the observatory/backfill surface this PRD's later slices add) but is not yet the engines' or charts' read path — `DATA_LAKE_ENABLED` defaults `False`, the Python engine and LEAN sidecar still resolve data through the older policy-store cache (`app/engine/data/policy_store.py`), and no Backend code path calls `ensure-data` or `prepare-run` today (grep confirms only the EF migration references those table names). This ADR names the target authority; wiring the remaining consumers onto it is the rest of the PRD's slices, several of which are landing in parallel with this record. Per ADR 0039 Decision 1, the `Proposed` status below states standing, not that every consumer has been re-pointed yet.

### The original design authority

The lake's design is `docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md`, pruned from the tree but recovered from git history with:

```
git show 8441f4f6^:docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md
```

The lake's live source cites that spec by section number throughout (e.g. `catalog_schema.py`'s docstring points at "§ 3", `sweep.py`'s at "§ 4.4"); this ADR does the same, and any successor reading a `§ N` citation in the code should recover the spec with the command above before assuming the section renumbered.

The spec's **§2.1 service roles** table draws the authority split this ADR ratifies: Python `app/data_lake/` is "the only writer to the lake" and hosts the reader; Postgres is "Catalog + audit. Knows what artifacts exist and whether they are valid. **Never stores bar bytes.**" Its **§2.2 volume layout / mount table** enforces that split at the mount boundary — a writer-only `LEAN_DATA_WRITE_ROOT` mount (`/lean-data-writer`, rw) that only `app/data_lake/` ever references, versus a `LEAN_DATA_ROOT` mount (`/lean-data`, ro) for every reader (`LeanMinuteDataReader`, the LEAN sidecar's own `/lean-run/data` mount), with `lake/` and `staging/` sharing one filesystem specifically so the atomic `rename(2)` publish is real, not copy-then-unlink. Its **§2.3 control flow** is the flow Decision 3 below deliberately departs from.

## Decision

### 1. The lake is the single authority for historical bar data; files are canonical, Postgres is catalog and coordination only

Immutable, content-hashed LEAN-format artifact files under `lake/` are the canonical bytes for every historical bar the platform consumes — both engines and, per this PRD, the chart/indicator read path for completed sessions. Postgres's `data_lake_artifacts` / `data_lake_runs` tables hold metadata about those files (path, hash, size, status, lease/claim state, provenance) and coordinate concurrent writers; they never hold a bar. Deleting every row and rebuilding the catalog by re-hashing `lake/` would lose zero bar data — that is the operational test for "coordination, not authority," and the schema (no bytes column, confirmed above) satisfies it structurally, not by convention.

### 2. Scoping against ADR 0001

ADR 0001 decided "files + Parquet + hash sidecars canonical; no Postgres in the live-runtime control plane," scoped explicitly to that plane — the run ledger, decision/execution/trade Parquet, halt flags. The market-data lake is a different authority domain: it governs historical bar acquisition and storage, not live-run state, so it does not fall inside ADR 0001's original scope statement by name.

It honors ADR 0001's doctrine anyway, by choice rather than by accident. The lake's Postgres usage is structurally the same shape ADR 0001's own amendments later sanctioned for `clerk_transactions` and the IBKR lifecycle projector: a rebuildable read-model derived from a canonical substrate, never a second custody of the substrate's content. The one real difference — the catalog row is written concurrently with the file it describes (claim → fetch → atomic rename → mark complete), not tailed after the fact from a journal — changes *when* the row is written, not *what it is for*: arbitrating concurrent writers and answering "what do we have," never answering "what are the bytes." A lake whose catalog stored bar bytes, or whose files were mutable once complete, would conflict with ADR 0001; this one does neither.

### 3. Deliberate deviation from the 2026-05-20 spec's §2.3 Backend-orchestrated flow

The spec's §2.3 control flow routes every run through the .NET Backend: insert a `data_lake_runs` audit row, `POST ensure-data`, evaluate partial-coverage policy, `POST prepare-run`, then launch the engine and update the run row. That flow was never built — the only piece of §2.3 the Backend ships today is the EF Core migration for the two Postgres tables; no Backend code calls `ensure-data` or `prepare-run`.

The implemented decision instead is **in-process Python orchestration at run materialization**: the Python engine and the LEAN sidecar call `ensure_data()` directly, as a function call in the same process, at the point where the retiring policy-store export currently sits — not through a Backend-mediated HTTP round trip. `app/routers/data_lake.py`'s `POST /api/data-lake/ensure-data` remains, but as a narrow surface for the observatory and backfill UI (Tasks in this same PRD), not as the primary path run materialization takes.

This is a deliberate simplification, not an oversight: the spec's HTTP hop existed to let .NET own orchestration state while Python did the work, but the consumers of lake data (the Python engine, the LEAN sidecar's launch preparation) already run in or adjacent to the Python data service. Routing through .NET and back adds a same-host network boundary with no isolation benefit once the writer and its primary callers are colocated. The Backend's own run lifecycle (GraphQL mutation, its EF-owned audit tables) is unchanged by this decision; what changes is who drives `ensure_data` for an engine run.

### 4. Provider-licensing caveat — recorded, not resolved

Polygon.io's provider terms may bound how long fetched historical data may be retained. **This ADR does not resolve that question and neither does the lake's implementation.** No code under `app/data_lake/` enforces a retention TTL or expiry, and the catalog schema has no expiry column — the lake, as built, retains everything it ever fetches, indefinitely, by omission rather than by a reviewed decision that indefinite retention is licensed. This is recorded here as an open, unresolved compliance gate specifically so it is never mistaken for a cleared one. Clearing it is a human/legal task outside engineering scope (the PRD's "Out of Scope" says the same); until it is cleared, the honest state is "unknown," not "fine."

### 5. Spec recovery

The pruned design spec is recovered with:

```
git show 8441f4f6^:docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md
```

Sections cited above: §2.1 (service roles), §2.2 (volume layout / mount table), §2.3 (control flow, the flow Decision 3 departs from), §3 (catalog schema, mirrored by `catalog_schema.py`), §4.4 (concurrency primitives, used by `sweep.py`).

### 6. TimescaleDB deferral — a future reader-side projection, not a store decided here

No SQL/columnar analytics projection (TimescaleDB or otherwise) is introduced by this decision. It stays deferred until one of two named triggers fires:

- **Live WebSocket ingestion** — recording a live broker/vendor feed (IBKR bars, a Massive WebSocket) into queryable time-series storage. Out of scope today: live feed evidence ledgers are a separate authority from market-data history (per the PRD's "Out of Scope"), and nothing currently writes live ticks into the lake.
- **A large symbol universe** — where per-artifact catalog and coverage queries against `data_lake_artifacts` stop being sufficient for the operator's or a strategy's hot-path questions.

Until one fires, `data_lake_artifacts` already serves catalog and coverage queries at the platform's current scale, and introducing a second store to answer questions the first already answers is exactly the premature-migration Postgres-as-substrate ADR 0001 rejected. If a trigger does fire, the correct shape is settled in advance by Decision 2: TimescaleDB would be a **reader-side projection downstream of the lake's canonical hashed files**, rebuildable from them, never a second bar-byte authority competing with `lake/`.

## Considered and rejected

**Storing bar data in Postgres (or Timescale) directly, bypassing the file substrate.** Rejected for the same reason ADR 0001 rejected a Postgres-authoritative control plane: it discards content-hash and atomic-rename audit properties the file substrate provides for free, and it is the option this PRD's leak-patch slice (Task 1, throwaway insurance) exists specifically to avoid needing — a database row has no independent hash to verify against a redownload.

**Keeping the spec's Backend-orchestrated HTTP flow.** Rejected per Decision 3: the isolation benefit it was designed for does not apply once the writer and its primary callers share a process, and the flow was never actually built to abandon in place.

**Treating the provider-licensing question as implicitly cleared because nothing has gone wrong yet.** Rejected. Silent-pass is exactly the failure mode ADR discipline exists to prevent (the PRD's own compliance-owner user story asks for the caveat recorded, not resolved by assumption).

## Consequences

**Positive:**

- One authority answers "what market data do we have," replacing three unauthoritative fetch paths with one, once the remaining PRD slices land.
- The lake's file-canonical, content-hashed design is proven ADR-0001-compatible rather than merely unopposed by it — a future reviewer does not have to re-derive whether the two decisions conflict.
- The provider-licensing gap is on the record as *unresolved*, which is strictly better than the status quo (nowhere on the record at all) even before it is cleared.
- The Backend-orchestration deviation is documented for the spec's successors, satisfying the PRD's user story 24 directly.

**Negative / accepted:**

- Postgres remains a hard operational dependency for coordination (claims, leases, coverage queries) even though it stores no bar bytes. No catalog-rebuild-from-files tool exists in the tree today; losing the catalog does not lose data, but recovering coordination state would require building that rebuild path first, not just re-hashing files by hand.
- The provider-licensing gate stays open. Nothing in this PRD or ADR blocks on it, which means the lake will accumulate data under an unresolved retention question until a human clears it — accepted deliberately rather than blocking the whole PRD on a legal answer no engineering task here can produce.
- This ADR records a decision, not a conformance claim (ADR 0039). At the time of writing, `DATA_LAKE_ENABLED` is still `False` and the engines still read the policy-store cache; the wiring that makes this authority real in production is the rest of this PRD's slices, several in flight concurrently with this record.
- **Status stays `Proposed`.** Per the issue's HITL marking, only the user may move this to `Accepted`; the `Vocabulary:` obligation above (a `CONTEXT.md` entry for the market-data lake) is owed at that time, per ADR 0040 Decision 4, not before.
