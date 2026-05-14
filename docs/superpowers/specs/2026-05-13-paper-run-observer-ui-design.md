# Paper-run observer UI — design

**Status:** Draft (this spec). Awaiting Tim's review before implementation plan.
**Author:** Claude Opus 4.7 (1M context)
**Date:** 2026-05-13
**Companion docs:** [`ibkr-integration-authority.md`](../../ibkr-integration-authority.md), [`runbooks/ibkr-paper-dry-run.md`](../../runbooks/ibkr-paper-dry-run.md), spec [`2026-05-08-ibkr-paper-shadow-deployment-design.md`](2026-05-08-ibkr-paper-shadow-deployment-design.md)

## 1. Context

The IBKR paper-shadow runtime (`app/engine/live/`) is operator-driven via a host-venv CLI today. Operators have **no in-app visibility** into a running dry-run / paper-week session; they `tail -f live.log` and `ls live_runs/<run_id>/` from a shell. Today's misdiagnosis chain (issues #227 → #228 → PR #229 heartbeat) showed the operator-blindness window costs real time when something looks wrong but isn't, or vice-versa.

This spec adds a **read-only observer UI** for the live runtime. It is the first of three planned phases:

1. **Phase 1 (this spec)** — single page that reads artifact files + tails `live.log`; no process control.
2. **Phase 2 (separate spec, deferred)** — notifications layer over the same observer state.
3. **Phase 3 (separate spec, deferred)** — UI-driven run-control wizard.

The phases are sequenced so the state model is proven against real artifacts before the UI takes responsibility for launching or stopping a trading process.

## 2. Scope

In-scope:

* New FastAPI router `app/routers/live_runs.py` exposing read-only endpoints over `PythonDataService/artifacts/live_runs/`.
* New Angular page (`/broker/paper-run`) — operator console with five panels.
* Server-side run-state inference (`idle | warming_up | running | stale | halted | poisoned | complete`).
* Server-side `[BAR]` heartbeat parser with graceful degradation.
* In-process LRU caching keyed on file mtimes.
* New env var to disable the python-service container's IBKR connection while a host-venv runner owns the IBKR session (preserves spec § 5 single-client invariant).
* Banner on the existing `/broker/*` pages indicating broker connection is intentionally disabled when the host runner is active.

Out-of-scope (this spec):

* Any UI control of the run lifecycle (start, stop, emergency-flatten, init-ledger, pre-flight). All deferred to Phase 3.
* Toast / persistent event log notifications. Deferred to Phase 2 — but the SSE endpoint they will consume is sketched here as a forward-compat hook.
* Reconcile receipt rendering inside the page. Phase 1 links to the Markdown receipt; Phase 2 may inline-render it.
* Multi-account support. Phase 1 assumes one paper account at a time, matching the rest of the system.

## 3. Architecture decision: A-prime

**Direction:** the UI is a read-only observer over artifact files. FastAPI gains a new router that walks `PythonDataService/artifacts/live_runs/` and tails `live.log` — never spawns a subprocess, never owns IBKR state.

**Why not (B) FastAPI-as-supervisor:** makes the FastAPI app responsible for a long-running trading process. Crash-recovery, graceful shutdown coupling, log-routing all become FastAPI concerns. Wrong owner.

**Why not (C) host daemon:** correct end-state but premature. Phase 1's job is to prove the state model against real runs.

**Why "A-prime" not the literal (A):** the literal (A) leaves the python-service container's lifespan `IbkrClient` running with `client_id=42` while the host runner ALSO wants `client_id=42` for the dry-run. Two options to resolve: (a) have the container use a different client_id (would require rewriting spec § 5 to mean "single trading client" instead of literally "one IBKR connection"), or (b) keep the spec § 5 invariant exactly as written and **disable the container's IBKR connection** during a paper run. Tim's call: option (b). The container stays up to serve the new live-runs router (which is artifact-only), but its `/api/broker/*` endpoints surface as "broker connection disabled — host runner owns IBKR" while the runner is active.

## 4. Backend changes

### 4.1 `IBKR_BROKER_ENABLED` env var

New `IbkrSettings` field, default `True`. When set to `False`:

* The FastAPI lifespan **does not** instantiate or connect the process-wide `IbkrClient`.
* All existing `/api/broker/*` endpoints that depend on the live client (`/health`, `/diagnose`, `/account`, `/positions`, `/orders/*`, `/pnl/*`, `/expirations/*`, `/strikes/*`, `/option-chain/*`) return HTTP 503 with a structured body:
  ```json
  {
    "disabled": true,
    "reason": "IBKR_BROKER_ENABLED=false; the host runner owns IBKR for this paper window",
    "since_ms": 1778670000000
  }
  ```
* `/api/broker/diagnose` returns its existing `DiagnosticReport` shape with every check `status=skip` and a top-level `overall_status=disabled`. (UI gates on this rather than HTTP 503 for the diagnose page specifically, so the existing component degrades cleanly.)

The new `/api/live-runs/*` router is **unaffected** — it never touches the `IbkrClient`.

Rationale per Tim's call on Q1: preserves spec § 5 literal invariant ("exactly one IBKR client connected during the paper window"). The container stays up purely to serve artifact reads; the host venv owns the IBKR connection at `client_id=42`. Operators flip `IBKR_BROKER_ENABLED=false` in `.env` (or via the dry-run launcher script) before the host runner starts, flip it back to `true` afterwards.

### 4.2 New router `app/routers/live_runs.py`

Mounted at `/api/live-runs`. Three endpoints in Phase 1; the SSE endpoint is sketched as a forward-compat hook for Phase 2.

| Endpoint | Method | Body | Purpose |
|---|---|---|---|
| `/api/live-runs` | GET | query: `limit`, `cursor`, `status`, `from_ms`, `to_ms` | List of `LiveRunSummary` |
| `/api/live-runs/{run_id}/status` | GET | — | `LiveRunStatus` (top-strip + panels' state) |
| `/api/live-runs/{run_id}/log-tail` | GET | query: `lines` (default 200, max 1000) | Last N parsed log lines |
| `/api/live-runs/{run_id}/events` | GET | — | **DEFERRED to Phase 2** — SSE channel of inferred state-transition events |

Pydantic v2 response models live in `app/schemas/live_runs.py` (new file). Snake_case field names (consumer convention).

`LiveRunSummary`:
```python
class LiveRunSummary(BaseModel):
    run_id: str
    account_id: str
    started_ms: int          # int64 ms UTC
    last_activity_ms: int    # max(mtime) across run-dir files
    state: RunState           # see § 4.4
    decision_count: int
    execution_count: int
    halt_flag_set: bool
    poisoned_flag_set: bool
```

`LiveRunStatus` (the load-bearing one):
```python
class LiveRunStatus(BaseModel):
    run_id: str
    account_id: str
    state: RunState
    last_bar_time_ms: int | None
    last_bar_age_s: float | None
    heartbeat_parse_status: Literal["ok", "degraded", "no_bars_yet"]
    decisions: DecisionsSummary       # row count, latest_decision (if any)
    executions: ExecutionsSummary     # row count, last_3_fills
    trades: TradesSummary              # row count, open_position
    flags: FlagsSummary                # halt_flag, poisoned_flag bodies
    artifacts: ArtifactsSummary       # file list with sizes + mtimes
    reconcile: ReconcileSummary       # latest day-N receipt link
    fetched_at_ms: int                # response timestamp for staleness debugging
```

Each sub-model is small (4–8 fields) and pinned in the schemas file.

### 4.3 Caching

Per Tim's Q3 call. Two-layer LRU.

**Layer 1: directory listing** for `/api/live-runs`.
* Key: `run_root_path`
* TTL: 15 s
* Invalidation: TTL-only (no inotify; we accept up to 15 s lag for new runs to appear in the picker)

**Layer 2: per-run status** for `/api/live-runs/{run_id}/status`.
* Key: `(run_id, mtime_signature)` where `mtime_signature` is a tuple of mtimes for `(run_ledger.json, live.log, halt.flag, poisoned.flag, decisions.parquet, executions.parquet, trades.parquet, reconcile_dir)`
* Stat the seven files first (cheap), build the signature, look up in cache, return cached body if hit. Cache misses do the full read.
* Eviction: bounded LRU at 256 entries (covers ~36 days of runs)

**Layer 3: log tail** for `/api/live-runs/{run_id}/log-tail`.
* Per-process state: `{path: (inode, last_size, last_offset)}`
* On request: if size grew, seek to `last_offset`, read tail, parse new lines, append to in-memory deque (capped at 1000 lines per run); update `last_size`. If file was rotated (inode changed), re-open from start.
* Cache the **parsed** lines so repeated tail requests for the same N are served from RAM.

This bounds the request cost to ~7 `os.stat` calls + 1 small file read per `/status` call, regardless of run-dir age.

### 4.4 Run-state inference

Server-side function `infer_state(run_dir: Path, now_ms: int) -> RunState`. Pure function over file metadata; no IBKR calls. Tested with a fixture-tree per state.

| State | Rule (first match wins, top to bottom) |
|---|---|
| `poisoned` | `poisoned.flag` exists |
| `halted` | `halt.flag` exists |
| `complete` | `live.log` contains `[START] run completed cleanly` (grep last 50 lines) |
| `idle` | `live.log` doesn't exist OR last `[BAR]` mtime > 24 h ago |
| `stale` | last `[BAR]` mtime > 90 s but ≤ 5 min |
| `warming_up` | last `[BAR]` mtime ≤ 90 s AND `decisions.parquet` doesn't exist or has 0 rows |
| `running` | last `[BAR]` mtime ≤ 90 s AND `decisions.parquet` has ≥ 1 row |
| `unknown` | None of the above (defensive — UI shows it as a fallback badge) |

Thresholds (`90`, `300`, `86_400`) live in module constants, tunable for tests.

### 4.5 `[BAR]` heartbeat parser

`app/services/live_log_parser.py` (new). One regex per known event type:

```
[BAR] <iso-time> consolidator_emitted=<int> snapshot=<set|None>
```

Returns `BarEvent(ts_ms, consolidator_emitted, snapshot_set)`. On parse failure: returns `RawLine(ts_ms, raw_text)` AND the `/status` endpoint flips `heartbeat_parse_status: "degraded"`. The status endpoint never raises on a parse failure — it returns the structured fields it could derive plus a `degraded` marker.

Forward-compat: `live_events.jsonl` sidecar (one JSON object per event, types `bar | decision | order_fill | halt | reconcile_ready`) is the planned replacement. The parser module's interface is shaped so swapping the source is local.

## 5. Frontend changes

### 5.1 New route + component

`Frontend/src/app/components/broker/broker-paper-run/broker-paper-run.component.ts` plus `.html` and `.scss`.

Route: `path: "broker/paper-run"`, lazy-loaded.

Standalone component, OnPush, signal-driven. Uses `resource()` for the polled status (5 s default, 10 s when state is `complete` or `idle`), `rxResource()` for the log-tail (10 s).

### 5.2 UI shape

Single page, no sub-routes in Phase 1. Layout (top to bottom):

1. **Top strip** (sticky)
   * Mode pill: `PAPER` / `READONLY` / `LIVE-LOCKED`
   * Connected account: `DUM284968`
   * Run state badge: color-coded per state
   * Last `[BAR]` age: `42 s ago` (red if > 90 s)
   * Run ID: truncated 8-char prefix, click-to-copy
   * Run picker: dropdown of recent runs (filter chips below: Today / Last 14 days / Halted / Completed / All)

2. **Heartbeat panel**
   * Last 10 `[BAR]` lines, each with relative timestamp
   * Aggregate: bars seen this session, consolidator emissions, snapshot set count
   * Degraded marker if parser flagged

3. **Strategy state panel**
   * Latest decision row: signal (`ENTER` / `EXIT` / `HOLD`), EMA5, EMA10, RSI14
   * Warmup progress: `N / 15 consolidated bars` (uses RSI(14)'s `samples >= period + 1` rule per the corrected runbook)
   * Empty state: "Strategy in warmup; no decisions yet."

4. **Position & orders panel**
   * Current position from latest `decisions.parquet` row's reference price (not from `/api/broker/positions`, which is disabled)
   * Recent fills from `executions.parquet` (last 5)
   * Trade log from `trades.parquet` (last 5)

5. **Safety flags panel**
   * `halt.flag` payload if present (collapsible JSON)
   * `poisoned.flag` payload if present
   * Reconcile receipt link to most recent `day-N.md`

6. **Artifacts panel**
   * File list with row counts + mtimes for the four parquets, the two flag files, the reconcile dir
   * Download links for `live.log`, `host_run.log`

### 5.3 Existing `/broker/*` pages — degraded-mode banner

When `IBKR_BROKER_ENABLED=false`:

* `BrokerHealthService.bannerState` adds a new value `disabled-host-runner-active`
* `app-shell` banner shows: "Broker connection disabled — paper-run is active. Visit `/broker/paper-run` for live status."
* Each existing `/broker/*` page (`/broker`, `/broker/options-chain`, `/broker/account-monitor`, `/broker/orders`, `/broker/reconciliation`) shows a top-of-page warning explaining why the broker data is stale (the last-known timestamp is shown).
* No data is fabricated; pages render empty states gracefully when the underlying endpoint returns 503.

The disabled state is intentional, not an error. The banner reads as informational, not alarming.

### 5.4 Run picker

Default view: today's active run (if any), then most recent up to 14 days back.

Filter chips: `Today · Last 14 days · Halted · Completed · All`. Selecting a chip updates the dropdown's filtered list. No pagination in the UI for Phase 1; the API exposes `limit`/`cursor`/`status`/`from_ms`/`to_ms` for forward-compat.

## 6. Run-state state machine (Frontend mirror)

Frontend state lives in a `signal<RunState>` driven entirely by polled status. No client-side inference — the Angular component trusts the server. Transitions trigger Phase-2 notifications later; in Phase 1 the badge color and panel content update is the only visual response.

```
idle ──► warming_up ──► running ──► complete
              │            │
              ▼            ▼
            stale       halted ──► (manual reconcile, then idle)
                          │
                          ▼
                       poisoned ──► (manual reset, then idle)
```

`unknown` is a fallback; in normal operation we never reach it.

## 7. Tests

Per `.claude/rules/numerical-rigor.md` and `testing.md`:

### Backend (pytest)

* `app/services/live_log_parser.py` — table-driven test cases for happy `[BAR]` lines, malformed lines, mid-line truncation. Asserts shape and `degraded` flag behavior.
* `app/routers/live_runs.py` — 7 integration tests using a fixture run-dir per state (`idle`, `warming_up`, `running`, `stale`, `halted`, `poisoned`, `complete`). Use `httpx.AsyncClient` with `ASGITransport` per repo testing rules.
* Cache layers — separate unit tests with monkeypatched `os.stat` returning controlled mtimes. Verify cache hit/miss behavior, eviction, and tail-state across rotation.
* `IBKR_BROKER_ENABLED=false` lifespan path — verify `IbkrClient` is NOT instantiated; verify `/api/broker/health` returns the `disabled` 503 body.

### Frontend (Vitest + Angular Testing Library)

* `broker-paper-run.component.spec.ts` — render with each state's mock status, assert badge color + panel content + empty states.
* `broker-paper-run.component.spec.ts` — run picker filter chips work; selecting a chip filters the list correctly.
* `broker-health.service.spec.ts` — `bannerState` returns `disabled-host-runner-active` when health endpoint returns the disabled-503 body.

## 8. Open issues / forward-compat hooks

Not blockers for Phase 1; documented so Phase 2/3 don't surprise:

1. **SSE channel for events** (`/api/live-runs/{run_id}/events`) — sketched in § 4.2 but not implemented in Phase 1. Phase 2 implementation will derive events server-side from the same polled state inference, push as SSE.
2. **`live_events.jsonl` sidecar** — § 4.5 mentions this as the planned replacement for log parsing. When it lands, the parser module's interface stays; only its source flips. Phase 1 keeps the parser.
3. **Multi-account** — Phase 1 assumes one paper account. Schema fields (`account_id`) are present on every response so Phase N can group by account without a migration.
4. **Run-control surface** (Phase 3) — the new page is named `paper-run` rather than `paper-run-status` so Phase 3 can add a `/control` sub-route under the same parent without renaming. Run picker / state badge shape stays usable.
5. **Reconcile receipt inline render** — Phase 1 links to the Markdown file; Phase 2 may render via `marked` or a server-side rendered HTML.

## 9. Acceptance criteria for Phase 1

A reviewer can validate Phase 1 is complete when:

1. With the python-service container UP and a host-venv `cmd_start` running, `/api/live-runs/{run_id}/status` returns the correct `RunState` for each of the seven states (validated against fixture run-dirs).
2. Setting `IBKR_BROKER_ENABLED=false` in `.env` and restarting the container: the existing `/broker` page shows the disabled banner; the new `/broker/paper-run` page works normally.
3. A 90-second host-venv `start --readonly` (after-hours, no bars) drives the UI through `idle → warming_up` → (KeyboardInterrupt → `complete` once `[START] run completed cleanly` is logged). The page shows a populated heartbeat panel, even though no decisions are written.
4. A real RTH session (separately) drives the UI through `warming_up → running` once the first decision row lands at ~3h45m. The position panel reflects the `decisions.parquet` reference price.
5. A `halt.flag` written by `reconcile.write_day_report` flips the badge to `halted` within one polling interval (≤ 5 s).
6. Project-scope `pytest tests/` and `npx ng test --watch=false` both pass; `ruff check`, `dotnet format --verify-no-changes`, and Angular lint all clean.

## 10. References

* `app/engine/live/run.py` — `cmd_start` (the runtime this UI observes)
* `app/engine/live/live_engine.py:432` — the `[BAR]` heartbeat emission point
* `app/engine/live/reconcile.py` — `halt.flag` emission point
* `docs/ibkr-integration-authority.md` § 6 (live runtime), § 11 Phase 10 prereqs (read what the UI exposes)
* `docs/runbooks/ibkr-paper-dry-run.md` — operator workflow this UI complements
* PR #225 (Phase 8), PR #229 (heartbeat), PR #231 (wedged-source SIGINT), PR #233 (operator-safety bundle) — code paths referenced
