# ADR 0004 — The operator console is an instance-addressed control room; the process registry owns the live binding; desired-state is the single intent knob

**Status:** Accepted 2026-05-30. **Amended 2026-06-21 (PRD #619-B) — adds daemon `boot_id` + lease + DRAINING semantics, the child watchdog's ordered shutdown contract, and orphan classification on daemon boot. See "Amendment 2026-06-21" block after the Consequences section.**
**Decision drivers:** PRD-A through PRD-D shipped a per-`run_id` "Paper Run" status page, but the backend identity model is strategy-instance-centric (clientId, order namespace, durable desired-state, and the managed-process registry are all keyed by `strategy_instance_id`). A grilling session found the UI behaves like a *run artifact viewer with controls attached* when it needs to be an *instance control room with the current run/artifacts attached as evidence*. The run-centric framing is the root of half the operator-grade findings.
**Related:** ADR 0002 (shadow = separate OS process per instance), ADR 0005 (engine-authored readiness & broker ownership), `CONTEXT.md`, `docs/ibkr-paper-deployment-plan.md` § 16.

## Context

The operator's mental unit is the **strategy instance** — that is what owns the `ib_client_id`, the `bot_order_namespace`, the durable desired-state sidecar, and (after PR-A) the managed-process registry slot. A `run_id` is just one process lifetime of that instance. One instance has many runs over time.

The shipped console inverts this. `broker-paper-run.component.ts:115` makes `selectedRunId` the spine: it lists runs, picks an active-or-first one, and *derives* `strategyInstanceId` **from the selected run's status** (the reverse of the direction the operator needs). Consequences fall out everywhere:

- It structurally cannot answer "is my SPY-EMA strategy healthy?" across a restart — a new `run_id` is a new page.
- It cannot honestly represent a fleet (executing EMA + shadow VWAP are separate OS processes per ADR 0002), because everything hangs off one selected run.
- Two "pause" surfaces (durable desired-state vs the command channel) had **different live effects**: `POST /desired-state` (`live_runs.py:822`) only writes the sidecar; the running engine actuates intent **only** via the command-channel poll and reads desired-state only at start (`live_engine.py:336`). So pausing via the durable control on a *live* instance showed `DESIRED=PAUSED` while the bot kept trading — a safety-relevant lie.

Three structural questions had to be answered together because they are coupled:

1. **What is the console's primary entity** — `run` or `strategy_instance`?
2. **What authoritatively answers "what run is this instance writing to *right now*"** — and how is that distinguished from "the latest run, possibly dead"?
3. **How do durable intent and live actuation stop disagreeing?**

For (2), three authorities were considered: derive by scanning runs and ledgers; make the process registry authoritative; or write a durable `current_run_id` pointer beside `desired_state.json`. Scanning reinvents the registry and is racy. A durable pointer can record what was *last started/intended* but cannot *prove a process is alive and currently writing* — that is a process fact, not an artifact fact. The registry already has to answer the liveness question, and `process_registry.ManagedProcess` already launches the run (it just discards the `run_id` into the subprocess argv).

## Decision

**The operator console is instance-addressed. The process registry is the sole authority for the live binding. Durable desired-state is the single, liveness-independent operator intent knob.**

### Console spine

The console's subject is the `strategy_instance_id`. The current run and its artifacts are attached as **evidence**, never as the object being operated. An account-level fleet overview lists instances; selecting one opens its control room. The operator top-strip ladder reads **`INSTANCE / PROCESS / CURRENT RUN / DESIRED / BROKER`**.

### Binding authority — four sources, never conflated

- **Live binding** (`strategy_instance_id → live bound run_id | null`): owned by the **process registry**, which is extended to carry `run_id, run_dir, process state, pid, start time, exit state`. "Live" is a process fact only the registry can prove.
- **Evidence binding** (`strategy_instance_id → latest evidence run_id | null`): **derived** from the run scan / ledger index. Rendered as labeled stale/completed evidence. **Never** a command-routing authority.
- **Durable operator intent**: the desired-state sidecar (below).
- **Run artifacts**: evidence only.

Liveness is resolved **server-side** and returned with names that make misuse hard. Commands route **only** to a live binding; with none, command controls disable while evidence panels still render, labeled "latest completed/stale run."

```
instance status:
  process:          { state, pid, bound_run_id, started_at_ms }
  live_binding:     { run_id, run_dir, source: "registry" } | null
  evidence_binding: { run_id, state: "latest_run_by_ledger", is_live: false } | null
  desired_state:    { … }
  readiness:        { … }   # see ADR 0005
```

### Intent — single knob, single primary writer

Durable desired-state is the single intent knob with one liveness-independent semantic: **PAUSED** (no new decisions/orders), **RUNNING** (may act when readiness gates pass), **STOPPED** (must not restart without explicit operator change).

`POST /api/live-instances/{id}/desired-state`: (1) writes durable intent first; (2) if a live binding exists, enqueues the matching actuation command to that run; (3) returns durable-write status **and** live-actuation ack pointer; (4) with no live binding, returns "durable only; will gate next start."

- **Primary writer:** the desired-state endpoint.
- **Reconciling writers:** the engine command dispatcher and CLI emergency controls. They persist intent as *reconciliation, not ownership*; same-value/idempotent writes are accepted (version churn, not semantic drift).
- **Invariant:** any live actuation of PAUSE/RESUME/STOP must leave `desired_state.json` at the same semantic state as the action it executed. "Paused-but-still-trading" becomes structurally hard: durable state changes first, actuation is queued, the UI shows pending/acked actuation against the same intent.

`PAUSE`/`RESUME`/`STOP` are **removed as first-class UI controls** (retained as backend-compatible verbs for CLI/panic/older run-addressed paths). The one-shot command channel is reserved for `FLATTEN_NOW`, `RECONCILE_NOW`, `MARK_POISONED`.

### API reshape

Operator endpoints become instance-addressed:

```
GET  /api/live-instances
GET  /api/live-instances/{instance_id}/status
POST /api/live-instances/{instance_id}/desired-state
POST /api/live-instances/{instance_id}/commands
POST /api/live-instances/{instance_id}/start
POST /api/live-instances/{instance_id}/stop
```

Run-addressed endpoints are **demoted to artifact/evidence reads**:

```
GET /api/live-runs/{run_id}/status
GET /api/live-runs/{run_id}/log-tail
GET /api/live-runs/{run_id}/commands
GET /api/live-runs/{run_id}/reconcile/...
```

The command-channel response is canonicalized to a unified, backend-joined timeline: `{ entries: [{ seq, verb, status: queued|acknowledged|failed, issued_by, reason, queued_at_ms, acked_at_ms, outcome, outcome_detail }], poll_interval_ms }`. `poll_interval_ms` is server-provided (the dispatcher owns its poll cadence); the client's staleness threshold derives from it rather than a hardcoded constant.

## Consequences

**Positive:**
- The UI spine matches the backend identity model. Multi-process fleet view and "this strategy" framing are natural, not bolted on. Restart continuity is free (the instance persists; the run rotates underneath).
- The "paused-but-still-trading" failure mode is structurally eliminated, not merely relabeled.
- The client never scans runs to infer liveness — that stale-run trap cannot be reproduced in TypeScript.

**Negative:**
- `process_registry.ManagedProcess` must carry the `run_id`/`run_dir` it already launches, and the registry must be wired into FastAPI (PR-A in the § 16 queue — still pending; `host_daemon.py` retains `_current`).
- A net-new instance-addressed router and status assembler are required; the run-addressed router is refactored to evidence-only.
- The Angular console is restructured from `selectedRunId`-spine to `selectedInstanceId`-spine; command/desired-state components re-address to the instance.
- The command-channel contract change requires a runtime contract test against the real serialized shape (the prior component test missed the drift because the fake returned the Angular-only shape).

**Non-consequences:**
- Concurrent *executing* (non-shadow) instances on one account: not unblocked here; that is the deferred broker-executor / virtual-book separation. This ADR only makes the console honest about however many processes the registry manages.
- The on-disk substrate is unchanged: durable desired-state stays at `artifacts/live_state/<instance_id>/desired_state.json`, per-run commands at `artifacts/live_runs/<run_id>/commands/` (ADR 0001).

## Amendment 2026-06-21 (PRD #619-B) — daemon boot_id + lease + child watchdog + orphan classification

**Driver.** The 2026-04 / -05 audits that triggered PRD #619 found four interrelated control-plane gaps the original ADR did not address:

1. **Process-registry persistence.** `RunnerProcessManager._managed` is an in-memory dict; a daemon restart resets the registry while the child trading processes keep running with `start_new_session=True`. The cockpit reads `IDLE` from the new daemon while the old child is still placing orders.
2. **Daemon liveness signal.** Children have no way to detect that the daemon they were spawned by has died or restarted — every operator-action path assumes the daemon is up, with no fallback.
3. **Orphan classification.** A new daemon coming up has no structured way to enumerate runs left behind by a previous boot.
4. **Child shutdown contract.** When the daemon disappears, the child's mid-flight obligations (block submissions, persist intent, flush evidence, disconnect broker) have no ordered enforcement — the order matters because evidence flushed after broker disconnect captures a torn state.

**Decision.** Add the following to the control plane:

### A. Daemon boot identity + heartbeat lease

- Every daemon process generates a `boot_id` (UUID hex) at startup. The id is immutable for the process lifetime.
- The daemon renews `artifacts/control_plane/daemon_lease.json` at **1 Hz** with `{schema_version, boot_id, written_at_ms, status, lease_cadence_ms, lease_threshold_ms}`.
- Freshness threshold is **5 s**. A reader treats the lease as expired iff `now - written_at_ms > lease_threshold_ms`.
- `status ∈ {CONNECTED, DRAINING}`. `DRAINING` signals graceful daemon shutdown — children pause + flush + disconnect + exit without flattening positions.
- The daemon's `/health` endpoint exposes `daemon_boot_id` and the lease state for the data plane to observe directly (no file-read for the cockpit hot path).
- When the daemon spawns a child, it sets `LIVE_RUNNER_DAEMON_BOOT_ID=<boot_id>` in the child's env. The child captures this as `expected_daemon_boot_id` for the lifetime of its run.

### B. Child watchdog (PRD §B5)

A child-side async task polls the lease at the same 1 Hz cadence. **Two failure modes** trigger the lease-loss handler:

1. **Lease expired** — `now - written_at_ms > lease_threshold_ms` OR the file is missing.
2. **Boot id changed** — `lease.boot_id != expected_daemon_boot_id`.

On either, the watchdog runs the **5-step ordered contract**. Order is the contract; tests assert it:

1. **Block submissions immediately.** No new order can leave the child while we're in the handling path.
2. **Persist durable `desired_state=PAUSED` and write the `control_plane_lease_lost.json` incident** to the run dir. A next-start reconciliation MUST observe PAUSED.
3. **Evidence-flush grace** (5–10 s default). Bar loop, command poll, and engine_runtime publisher get bounded time to flush their state.
4. **Disconnect the broker.** Stops streaming + drops the IBKR socket.
5. **Request bounded engine exit** (15 s deadline). The watchdog sets the engine's shutdown event; the bar loop observes it on the next iteration.

**No auto-flatten.** Positions are never closed by the watchdog. The child stops creating new exposure; existing positions stay until the operator decides.

### C. Orphan classification on daemon boot (PRD §B6)

On startup the daemon calls `classify_runtime_candidates_on_boot(live_runs_root, this_boot_id, now_ms)` which walks `artifacts/live_runs/` and labels each `engine_runtime.json` it finds:

- **`FRESH_OWNED_BY_THIS_BOOT`** — sidecar age within threshold AND `expected_daemon_boot_id == this_boot_id`.
- **`ORPHANED_CONTROL_PLANE`** — sidecar age within threshold AND owned by a different `boot_id`. The child may still be trading.
- **`EXITED_UNMANAGED`** — sidecar older than threshold; producer task has stopped writing.
- **`NO_SIDECAR`** — run dir without a readable `engine_runtime.json`.

**A sidecar alone never proves a process is alive.** Process identity verification is a separate follow-up step before the daemon decides to block new starts for the instance, signal recovery, or kill the candidate. Verified adoption is explicitly **out of scope** here and belongs in a future ADR.

### D. Backend freshness composition (PRD §B7)

The data plane composes a `RuntimeFreshness` from the per-run `engine_runtime.json` plus the daemon lease + this daemon's `boot_id`. Posture demotes to last-known when:

- `command_loop` heartbeat stale > 3 s, OR
- `broker` probe stale > 25 s (or missing), OR
- `control_plane` lease stale OR `observed_daemon_boot_id != expected_daemon_boot_id`.

`bar_loop` staleness alone does not demote posture (a closed market is not a posture event) — it is rendered as informational. Thresholds are server config with validated defaults, not Angular constants.

Action policy is asymmetric by safety effect:

- Resume and Flatten-and-pause disable while posture is demoted because they require current runtime evidence.
- Durable Pause and Stop remain available because removing the operator's fail-safe intent controls during a control-plane/runtime incident would be less safe.
- Mark-poisoned remains available as an incident-recovery action.

The mutation endpoints evaluate the same backend-authored freshness gate as the status projection. Angular only renders the resulting capabilities and reason codes.

### Wire artifacts pinned by this amendment

| Artifact | Path | Owner |
|---|---|---|
| `daemon_lease.json` | `artifacts/control_plane/daemon_lease.json` | daemon |
| `engine_runtime.json` | `artifacts/live_runs/<run_id>/engine_runtime.json` | child engine_runtime publisher |
| `control_plane_lease_lost.json` | `artifacts/live_runs/<run_id>/control_plane_lease_lost.json` | child watchdog (on lease loss only) |

All timestamps are `int64 ms UTC` at the artifact boundary per `.claude/rules/numerical-rigor.md`. Atomic writes via `tmp + fsync + replace`.

### Non-decisions (still out of scope)

- **Adoption / reclamation.** The classifier surfaces orphan candidates; the daemon does not re-take ownership without a separate ADR covering process identity, run identity, account identity, namespace ownership, broker reconciliation, and replay-safe handshake semantics.
- **Auto-flatten on daemon loss.** Explicitly rejected — the child stops creating new exposure but the operator decides on existing positions.
- **Daemon-enforced mutation idempotency.** Future PRD covers daemon-side `operation_id` deduplication; 619-B carries `mutation_attempt_id` audit-only.

### References (619-B implementation)

- `PythonDataService/app/engine/live/control_plane.py` — `DaemonLease` schema + writer + `DaemonLeaseWriter` task.
- `PythonDataService/app/engine/live/engine_runtime.py` — `EngineRuntimeSnapshot` schema + atomic writer.
- `PythonDataService/app/engine/live/engine_runtime_publisher.py` — `EngineRuntimeAggregator` + `EngineRuntimePublisher`.
- `PythonDataService/app/engine/live/orphan_classifier.py` — `classify_runtime_candidates_on_boot`.
- `PythonDataService/app/engine/live/child_watchdog.py` — `ChildWatchdog` + 5-step contract.
- `PythonDataService/app/engine/live/runtime_producer.py` — engine-side block composition.
- `PythonDataService/app/services/runtime_freshness.py` — backend freshness evaluator.
- `PythonDataService/app/schemas/artifact_io.py` — canonical fail-closed Pydantic-artifact reader.

## References

- `PythonDataService/app/engine/live/process_registry.py` — gains `run_id`/`run_dir`; becomes the FastAPI-wired live authority.
- `PythonDataService/app/engine/live/host_daemon.py` — `_current` retired in favor of the registry (PR-A).
- `PythonDataService/app/routers/live_runs.py` — split into instance-addressed operator routes + demoted run-addressed evidence routes.
- `PythonDataService/app/engine/live/live_engine.py:1029-1089` — command dispatch + `_persist_desired_state` (the reconciling writer).
- `Frontend/src/app/components/broker/broker-paper-run/` — re-spined from run to instance.
- `CONTEXT.md` — operator-console glossary.
