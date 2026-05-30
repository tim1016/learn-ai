# ADR 0004 — The operator console is an instance-addressed control room; the process registry owns the live binding; desired-state is the single intent knob

**Status:** Accepted 2026-05-30
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

## References

- `PythonDataService/app/engine/live/process_registry.py` — gains `run_id`/`run_dir`; becomes the FastAPI-wired live authority.
- `PythonDataService/app/engine/live/host_daemon.py` — `_current` retired in favor of the registry (PR-A).
- `PythonDataService/app/routers/live_runs.py` — split into instance-addressed operator routes + demoted run-addressed evidence routes.
- `PythonDataService/app/engine/live/live_engine.py:1029-1089` — command dispatch + `_persist_desired_state` (the reconciling writer).
- `Frontend/src/app/components/broker/broker-paper-run/` — re-spined from run to instance.
- `CONTEXT.md` — operator-console glossary.
