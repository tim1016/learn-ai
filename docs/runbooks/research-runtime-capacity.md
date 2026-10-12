# Research runtime: executor selection, drain, and rollback

**Scope:** the isolated research runtime (ADR 0077) and its shared worker
pool — Golden Search stages, Grid Search cells, Walk-Forward sweeps, and
Recency runs. The API keeps its own serial execution in `local` mode;
nothing here touches the market-data provider boundary or clerk stores.

## Preflight inventory (before any executor change)

1. Confirm the current profile: `RESEARCH_EXECUTION_MODE` on the API
   (`local` = the API executes serially; `research` = the API forwards) and
   `RESEARCH_WORKER_COUNT` on the `research` service.
2. Count research-owned Postgres connections against the cap: admission
   (`RESEARCH_ADMISSION_CONNECTIONS`, default 8) plus coordination
   (`RESEARCH_COORDINATION_CONNECTIONS`, default 4) must stay ≤ 12, with
   ≥ 20 server connections unallocated for operational headroom. The
   runtime refuses startup on a configuration that cannot fit — never raise
   PostgreSQL's limit to make one fit.
3. Check the cgroup fits the width: the `research` service profile is
   `cpus: 8.0 / mem_limit: 16g / pids_limit: 512`. The pool refuses a lane
   count whose worst-case task estimate cannot fit 80 % of the PID cap;
   do not raise the cap to force a wider pool.
4. `podman logs research` — the startup line names the profile and the
   pool facts; `/healthz` answers 503 while authority is not held.

## Selecting the executor width

- `RESEARCH_WORKER_COUNT=1` is the serial parity runtime: one spawned
  worker per dispatch, exactly the #3044 shape. Correct, no overlap.
- `2/4/8` join the process's one shared pool: round-robin fairness across
  studies, the global submission bound, warm-lane affinity, and
  per-second-coalesced progress with capacity/adaptive waiting states.
- The qualified default is recorded in PRD #3034's qualification evidence
  (issue #3053): choose it, not a larger number, unless a separately
  measured configuration says otherwise. Twelve workers is not a
  qualified profile; it needs its own measurement inside its own envelope.
- Width changes are **capacity changes**: drain first (below), then
  restart at the new profile. A second width against the live pool is
  refused, not merged.

## Drain (planned capacity/version changes)

```
curl -X POST -H "X-Data-Plane-Control-Secret: $DATA_PLANE_CONTROL_SECRET" \
  http://research:8000/api/jobs-internal/pool/drain
```

The drain stops new assignments, waits (default 60 s, bounded) for owned
work to complete, interrupts what cannot, reaps the children, and leaves
the pool closed — the process refuses to quietly recreate it. In-flight
studies read as `interrupted`; their durable cells stand, and an explicit
Finish (resume) reclaims them under a new attempt. Then restart the
service at the new profile.

## Rollback (direct new research back to the serial path)

1. Set `RESEARCH_EXECUTION_MODE=local` on the API and redeploy it. In this
   mode the API never forwards: dispatches execute on the API's own worker
   threads, serially, unstamped by the runtime.
2. Active jobs keep their existing owner until drained or interrupted —
   the rollback does not kill them; their Finish flows work unchanged.
3. Durable results and identities are executor-independent: a study run
   pooled resumes serial and vice versa, cell for cell, under the same
   receipts.

## Explicit Finish after a runtime restart

A research-runtime restart fences its own generations and closes its
jobs' sweep protection; it never auto-resumes them. Each interrupted study
is resumed through its own Finish (resume) command — new job id, same
durable identity, recorded cells skipped.
