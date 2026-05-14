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
* **`run_status.json` sidecar** written by `cmd_start` on transition (start, exit). Provides authoritative `started_at_ms`, `ended_at_ms`, `exit_code`, `exit_reason` so the router doesn't have to grep `live.log` for the clean-shutdown signal. (Reviewer Must-Fix #1.)
* Server-side run-state inference (`idle | waiting_for_bars | warming_up | running | stale | halted | poisoned | complete | stopped | unknown`) — sidecar-primary, file-mtime fallback for legacy runs without it.
* Server-side `[BAR]` heartbeat parser with graceful degradation.
* In-process LRU caching keyed on file mtimes; Parquet row counts via PyArrow file metadata (no full DataFrame read).
* New env var to disable the python-service container's IBKR connection while a host-venv runner owns the IBKR session (preserves spec § 5 single-client invariant).
* `LIVE_RUNS_ROOT` env var for the artifact root path (host vs container differ); plumbed through `IbkrSettings` and `compose.yaml`.
* Info-level banner (not warning/error — broker disabled is intentional state) on the existing `/broker/*` pages indicating the host runner owns IBKR.

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

### 4.1 New env vars

Two new env vars in this spec, both wired through `IbkrSettings` AND added to `compose.yaml` (Reviewer Should-Adjust — without explicit compose entries, flipping `.env` may not propagate to the container):

* **`IBKR_BROKER_ENABLED`** (bool, default `True`) — see below for behavior when `False`.
* **`LIVE_RUNS_ROOT`** (path, default `PythonDataService/artifacts/live_runs` host-side / `/app/artifacts/live_runs` container-side) — the artifact root the live-runs router walks. Host vs container paths differ because of the compose mount; surfacing as a config knob avoids hard-coding either side. Tests parameterize this for fixture isolation.

`compose.yaml` adds:
```yaml
- IBKR_BROKER_ENABLED=${IBKR_BROKER_ENABLED:-true}
- LIVE_RUNS_ROOT=/app/artifacts/live_runs
```

#### `IBKR_BROKER_ENABLED=False` behavior

When `IBKR_BROKER_ENABLED=False`:

* The FastAPI lifespan **does not** instantiate or connect the process-wide `IbkrClient`.
* `/api/broker/health` returns **HTTP 200** with `connected=false, disabled=true, reason, since_ms` (Reviewer Must-Fix #4 — Angular `HttpClient` routes 503 to the error path; 200 keeps the existing `BrokerHealthService.bannerState` codepath intact). The current response model gains two optional fields (`disabled: bool = False`, `reason: str | None = None`); existing callers ignore them by default.
* `/api/broker/diagnose` returns a **discriminated union** — either the existing `DiagnosticReportActive` (renamed; `disabled: Literal[False] = False`) or a new `DiagnosticReportDisabled(disabled: Literal[True], reason: str, since_ms: int)`. UI checks `report.disabled` first; the existing `pass | warn | fail` literal stays untouched on the active branch (Reviewer Must-Fix #3 — extending the literal would cascade through the generated TypeScript and frontend type guards).
* The other broker endpoints (`/account`, `/positions`, `/orders/*`, `/pnl/*`, `/expirations/*`, `/strikes/*`, `/option-chain/*`) return **HTTP 503** with the disabled body. These are not in the routine UI poll path; the 503 is correct semantically (resource unavailable) and the UI's existing error handlers degrade gracefully.

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
    session_start_ms: int          # ledger.start_date_ms (the trading session's 09:30 ET in ms)
    created_at_ms: int             # ledger.created_at_ms (when init-ledger ran)
    run_started_at_ms: int | None  # sidecar.started_at_ms (when cmd_start fired)
    ended_at_ms: int | None        # sidecar.ended_at_ms (when cmd_start exited)
    last_activity_ms: int          # max(mtime) across run-dir files
    state: RunState                # see § 4.4
    decision_count: int
    execution_count: int
    halt_flag_set: bool
    poisoned_flag_set: bool
```

(Reviewer Should-Adjust: `started_ms` was ambiguous between session-start and process-start. Three distinct fields now; UI picks the one its panel needs.)

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

### 4.3 `run_status.json` sidecar (Reviewer Must-Fix #1)

**The load-bearing structural addition over the original spec.** Without this, the only way to detect "clean shutdown" is to grep `live.log` for `[START] run completed cleanly`, which `cmd_start` emits via `print()` not `logger.info()` so it never lands in `live.log` at all (Reviewer caught the bug).

`cmd_start` writes (and atomically updates via tempfile + rename) `<run_dir>/run_status.json`:

```python
class RunStatusSidecar(BaseModel):
    schema_version: int = 1
    run_id: str
    started_at_ms: int                 # set on cmd_start entry
    last_update_ms: int                # bumped on each transition or per N seconds
    ended_at_ms: int | None = None     # set on exit (any path)
    exit_code: int | None = None       # set on exit
    exit_reason: ExitReason | None = None  # see literal below
    host_pid: int                      # for "stopped" detection if process is gone but no exit recorded
```

`ExitReason` literal:
```
"normal" | "force_flat_complete" | "keyboard_interrupt" | "signal" |
"max_orders_exceeded" | "fatal_halt" | "recovery_flatten" | "exception"
```

Write points (4 total, all in `cmd_start` / `_drive_engine`):

1. **Entry** — after ledger load + before `_drive_engine` runs. Writes `{started_at_ms: now, last_update_ms: now, host_pid: os.getpid(), ended_at_ms: null}`. `state` is inferred by the router from this absent-`ended_at_ms` + sidecar age, plus file mtimes.
2. **On engine.run normal completion** — `{ended_at_ms: now, exit_code: 0, exit_reason: "normal" or "force_flat_complete"}`.
3. **On graceful shutdown / KeyboardInterrupt** — `{ended_at_ms: now, exit_code: 0, exit_reason: "keyboard_interrupt" or "signal"}`.
4. **On halt / exception path** — `{ended_at_ms: now, exit_code: 1 or 3, exit_reason: "max_orders_exceeded" / "fatal_halt" / "exception"}`.

Atomic write: `_atomic_write_json(path, payload)` writes to `path.tmp`, fsyncs, renames to `path`. Avoids the router seeing a half-written file mid-update.

Forward-compat: `schema_version: 1` so a future schema change (e.g., adding `decision_count_at_exit`) is detectable without breaking the router.

### 4.4 Caching

Per Tim's Q3 call + Should-Adjust. Three-layer LRU.

**Layer 1: directory listing** for `/api/live-runs`.
* Key: `run_root_path`
* TTL: 15 s
* Invalidation: TTL-only (no inotify; we accept up to 15 s lag for new runs to appear in the picker)

**Layer 2: per-run status** for `/api/live-runs/{run_id}/status`.
* Key: `(run_id, mtime_signature)` where `mtime_signature` is a tuple of mtimes for `(run_ledger.json, run_status.json, live.log, halt.flag, poisoned.flag, decisions.parquet, executions.parquet, trades.parquet, reconcile_dir)`
* Stat the eight files first (cheap), build the signature, look up in cache, return cached body if hit. Cache misses do the full read.
* Eviction: bounded LRU at 256 entries (covers ~36 days of runs)
* **Row counts via Parquet metadata only** (Reviewer Caching note): `pyarrow.parquet.ParquetFile(path).metadata.num_rows` reads the footer, not the data — O(1) per file. Latest-N rows for the panels (e.g., last 5 fills) need a real read; cache those separately keyed on `(path, mtime, n)`.

**Layer 3: log tail** for `/api/live-runs/{run_id}/log-tail`.
* Per-process state: `{path: (inode, last_size, last_offset)}`
* On request: if size grew, seek to `last_offset`, read tail, parse new lines, append to in-memory deque (capped at 1000 lines per run); update `last_size`. If file was rotated (inode changed), re-open from start.
* Cache the **parsed** lines so repeated tail requests for the same N are served from RAM.

This bounds the request cost to ~8 `os.stat` calls + Parquet-footer reads + 1 small log-file read per `/status` call, regardless of run-dir age or parquet size.

### 4.5 Run-state inference

Server-side function `infer_state(run_dir: Path, now_ms: int) -> RunState`. Pure function over file metadata + sidecar; no IBKR calls. Tested with a fixture-tree per state.

**Inference is sidecar-primary.** The router reads `run_status.json` first (when present) for the authoritative `started_at_ms` / `ended_at_ms` / `exit_reason`. File-mtime inference is the **fallback** for legacy runs (pre-sidecar) or when the sidecar is missing/corrupted.

States and rules (first match wins, top to bottom):

| State | Rule |
|---|---|
| `poisoned` | `poisoned.flag` exists |
| `halted` | `halt.flag` exists |
| `complete` | sidecar exists AND `ended_at_ms` set AND `exit_reason in {"normal", "force_flat_complete"}` |
| `stopped` | sidecar exists AND `ended_at_ms` set AND `exit_reason in {"keyboard_interrupt", "signal"}` (Reviewer state-set: distinguishes operator-interrupted from clean force-flat) |
| `waiting_for_bars` | sidecar exists AND `ended_at_ms` is null AND no `[BAR]` line yet AND `started_at_ms` within last 60 s (Reviewer state-set: distinguishes "started, no bars yet" from `idle`/`stale`) |
| `warming_up` | sidecar exists AND `ended_at_ms` is null AND last `[BAR]` mtime ≤ 90 s AND `decisions.parquet` doesn't exist or has 0 rows |
| `running` | sidecar exists AND `ended_at_ms` is null AND last `[BAR]` mtime ≤ 90 s AND `decisions.parquet` has ≥ 1 row |
| `stale` | sidecar exists AND `ended_at_ms` is null AND last `[BAR]` mtime > 90 s but ≤ 5 min |
| `idle` | No `run_status.json` for any run within the last 24 h, OR all recent runs have `ended_at_ms` set |
| `unknown` | None of the above (defensive — UI shows it as a fallback badge) |

When `run_status.json` is missing (legacy run from before this spec lands), fallback rules apply: `complete` becomes "live.log contains `[START] run completed cleanly` substring" (will not match for runs that were interrupted), and `stopped` is unreachable (legacy can't distinguish). Operators see legacy runs correctly even though new runs are sidecar-driven.

Thresholds (`60` for `waiting_for_bars` window, `90` for stale, `300` for stale-max, `86_400` for idle) live in module constants, tunable for tests.

### 4.6 `[BAR]` heartbeat parser

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

**Top-strip design principle** (Reviewer UX-nit): four questions answered at a glance, in this order:

1. *Is the runner alive?* → state badge color + last-`[BAR]` age
2. *Is it safe?* → halt/poisoned flag indicators (red dot if either present)
3. *Has the strategy emitted decisions yet?* → decision count + warmup progress
4. *Did anything require operator action?* → halt flag, exit_reason on `stopped`/`halted`/`poisoned`

Layout (top to bottom):

1. **Top strip** (sticky)
   * Mode pill: `PAPER` / `READONLY` / `LIVE-LOCKED`
   * Connected account: `DUM284968`
   * Run state badge: color-coded per state — green = `running`, blue = `warming_up`/`waiting_for_bars`, yellow = `stale`, red = `halted`/`poisoned`, grey = `idle`/`complete`/`stopped`
   * Last `[BAR]` age: `42 s ago` (red if > 90 s)
   * Decisions emitted: `4 / N` (warmup progress when N < 15)
   * Action-required indicator: red dot when `halted` / `poisoned` / `stopped` with non-`keyboard_interrupt` reason
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

4. **Position & exposure panel** (Reviewer Must-Fix #5 — corrected from the original "decisions.parquet reference price" approach)
   * Current exposure derived from `executions.parquet` net signed `fill_quantity` per symbol PLUS open trades from `trades.parquet`. `decisions.parquet` does NOT carry quantity — it has signal/indicators/intended_price only — so it's not a position source.
   * Recent fills from `executions.parquet` (last 5)
   * Trade log from `trades.parquet` (last 5)
   * **`--readonly` runs label this explicitly: "Intended/simulated exposure (readonly run — no broker fills)."** Only the IBKR side has truth in non-readonly runs; this panel reflects what the engine THINKS it holds based on its own writers (`/api/broker/positions` is disabled while the host runner owns IBKR).

5. **Safety flags panel**
   * `halt.flag` payload if present (collapsible JSON)
   * `poisoned.flag` payload if present
   * Reconcile receipt link to most recent `day-N.md`

6. **Artifacts panel**
   * File list with row counts (Parquet footer metadata, no full read) + mtimes for the four parquets, the two flag files, the reconcile dir, the `run_status.json` sidecar
   * Download links for `live.log` and (if present) `host_run.log` — the latter is operator-shell convention and only exists when the launcher script tee's stdout (Reviewer Should-Adjust)

### 5.3 Existing `/broker/*` pages — disabled-mode banner

When `IBKR_BROKER_ENABLED=false`:

* `BrokerHealthService.bannerState` adds a new value `disabled-host-runner-active`
* `app-shell` banner shows: "Broker connection disabled — paper-run is active. Visit `/broker/paper-run` for live status." **Info-level styling (blue/info), not warning/error** (Reviewer UX-nit — disabled is intentional state, not a failure).
* Each existing `/broker/*` page (`/broker`, `/broker/options-chain`, `/broker/account-monitor`, `/broker/orders`, `/broker/reconciliation`) shows a top-of-page info banner explaining the host runner owns IBKR for this paper window.
* No data is fabricated; pages render empty states gracefully when the underlying endpoint returns 503 (or when `/health` returns 200 with `disabled=true`).

### 5.4 Run picker

Default view: today's active run (if any), then most recent up to 14 days back.

Filter chips: `Today · Last 14 days · Halted · Completed · All`. Selecting a chip updates the dropdown's filtered list. No pagination in the UI for Phase 1; the API exposes `limit`/`cursor`/`status`/`from_ms`/`to_ms` for forward-compat.

## 6. Run-state state machine (Frontend mirror)

Frontend state lives in a `signal<RunState>` driven entirely by polled status. No client-side inference — the Angular component trusts the server. Transitions trigger Phase-2 notifications later; in Phase 1 the badge color and panel content update is the only visual response.

```
idle ──► waiting_for_bars ──► warming_up ──► running ──► complete
                  │                  │            │
                  ▼                  ▼            ▼
                stale              stale       stopped (Ctrl+C / SIGINT)
                  │                  │            │
                  ▼                  ▼            ▼
                halted ──► (manual reconcile, then idle)
                  │
                  ▼
              poisoned ──► (manual reset, then idle)
```

* `waiting_for_bars` is reached immediately after `cmd_start` writes the sidecar but before the first `[BAR]` line lands. It distinguishes "runner started, waiting for first 5-second bar" from `idle` (no run started) and from `stale` (run started, then bar source went silent for > 90 s).
* `stopped` is the operator-interrupted exit path (Ctrl+C / SIGINT) — distinct from `complete` which is the clean force-flat / end-of-bars exit. Phase-2 notifications can route differently for each (a `stopped` run after EMA crossover has fired is more interesting than a `stopped` warmup run).
* `unknown` is a fallback; in normal operation we never reach it.

## 7. Tests

Per `.claude/rules/numerical-rigor.md` and `testing.md`:

### Backend (pytest)

* `app/services/live_log_parser.py` — table-driven test cases for happy `[BAR]` lines, malformed lines, mid-line truncation. Asserts shape and `degraded` flag behavior.
* `app/engine/live/run_status.py` (new sidecar writer) — atomic-write tests, schema-version round-trip, every `ExitReason` literal exercised.
* `cmd_start` / `cmd_emergency_flatten` integration — verify sidecar is written on entry AND on each exit path (clean, KeyboardInterrupt, FatalHaltError, MaxOrdersPerDayExceeded, generic Exception). Reuse the existing `_LifecycleClient` pattern from PR #233's tests.
* `app/routers/live_runs.py` — 9 integration tests using a fixture run-dir per state (`idle`, `waiting_for_bars`, `warming_up`, `running`, `stale`, `halted`, `poisoned`, `complete`, `stopped`). Use `httpx.AsyncClient` with `ASGITransport` per repo testing rules. Plus a 10th legacy-fallback test (run-dir without `run_status.json`).
* Cache layers — separate unit tests with monkeypatched `os.stat` returning controlled mtimes. Verify cache hit/miss behavior, eviction, tail-state across rotation, Parquet `num_rows` from footer (no full read).
* `IBKR_BROKER_ENABLED=false` lifespan path — verify `IbkrClient` is NOT instantiated; verify `/api/broker/health` returns HTTP 200 with the `disabled` body; verify `/api/broker/diagnose` returns the `DiagnosticReportDisabled` discriminated-union variant; verify the other broker endpoints return HTTP 503.

### Frontend (Vitest + Angular Testing Library)

* `broker-paper-run.component.spec.ts` — render with each state's mock status (all 9), assert badge color + panel content + empty states + the four-questions top-strip layout.
* `broker-paper-run.component.spec.ts` — run picker filter chips work; selecting a chip filters the list correctly.
* `broker-paper-run.component.spec.ts` — position/exposure panel correctly derives net signed quantity from a multi-fill `executions.parquet` mock; in `--readonly` mode shows the "intended/simulated" label.
* `broker-health.service.spec.ts` — `bannerState` returns `disabled-host-runner-active` when `/health` returns the 200 + `disabled=true` body.
* TypeScript discriminated-union test for `DiagnosticReport` — `report.disabled === true` narrows to `DiagnosticReportDisabled`; `report.disabled === false` narrows to `DiagnosticReportActive` with the `pass | warn | fail` literal preserved.

## 8. Open issues / forward-compat hooks

Not blockers for Phase 1; documented so Phase 2/3 don't surprise:

1. **SSE channel for events** (`/api/live-runs/{run_id}/events`) — sketched in § 4.2 but not implemented in Phase 1. Phase 2 implementation will derive events server-side from the same polled state inference, push as SSE.
2. **`live_events.jsonl` sidecar** — § 4.5 mentions this as the planned replacement for log parsing. When it lands, the parser module's interface stays; only its source flips. Phase 1 keeps the parser.
3. **Multi-account** — Phase 1 assumes one paper account. Schema fields (`account_id`) are present on every response so Phase N can group by account without a migration.
4. **Run-control surface** (Phase 3) — the new page is named `paper-run` rather than `paper-run-status` so Phase 3 can add a `/control` sub-route under the same parent without renaming. Run picker / state badge shape stays usable.
5. **Reconcile receipt inline render** — Phase 1 links to the Markdown file; Phase 2 may render via `marked` or a server-side rendered HTML.

## 9. Acceptance criteria for Phase 1

A reviewer can validate Phase 1 is complete when:

1. With the python-service container UP and a host-venv `cmd_start` running, `/api/live-runs/{run_id}/status` returns the correct `RunState` for each of the **nine** states (validated against fixture run-dirs that include `run_status.json` sidecar variants).
2. Setting `IBKR_BROKER_ENABLED=false` in `.env` and restarting the container: the existing `/broker` page shows the info-level disabled banner; the new `/broker/paper-run` page works normally; `/api/broker/health` returns 200 with `disabled=true`.
3. **After-hours fixture-run validation** (Reviewer Must-Fix #2 — was originally written as a real after-hours run, which is impossible since no bars = no `[BAR]` = can't reach `warming_up`): a fixture-tree with a synthetic sidecar (`started_at_ms = now`, `ended_at_ms = null`, no `[BAR]` lines yet) drives the UI through `idle → waiting_for_bars`. After 60 s without `[BAR]`, the inferred state advances to `stale` (consistent with the threshold). After a synthetic `[BAR]` line is appended, state advances to `warming_up`. Real after-hours `start --readonly` runs only validate `idle → waiting_for_bars → stopped` (the `[BAR]` path requires a market-hours bar source).
4. A real RTH session (separately) drives the UI through `warming_up → running` once the first decision row lands at ~3h45m. The position panel correctly derives net signed quantity from `executions.parquet` (NOT from `decisions.parquet` which doesn't carry quantity).
5. A `halt.flag` written by `reconcile.write_day_report` flips the badge to `halted` within one polling interval (≤ 5 s).
6. Ctrl+C during an active run: `cmd_start` writes the sidecar with `exit_reason="keyboard_interrupt"`; UI flips to `stopped` (not `complete`) within one polling interval.
7. Project-scope `pytest tests/` and `npx ng test --watch=false` both pass; `ruff check`, `dotnet format --verify-no-changes`, and Angular lint all clean.

## 10. References

* `app/engine/live/run.py` — `cmd_start` (the runtime this UI observes)
* `app/engine/live/live_engine.py:432` — the `[BAR]` heartbeat emission point
* `app/engine/live/reconcile.py` — `halt.flag` emission point
* `docs/ibkr-integration-authority.md` § 6 (live runtime), § 11 Phase 10 prereqs (read what the UI exposes)
* `docs/runbooks/ibkr-paper-dry-run.md` — operator workflow this UI complements
* PR #225 (Phase 8), PR #229 (heartbeat), PR #231 (wedged-source SIGINT), PR #233 (operator-safety bundle) — code paths referenced
