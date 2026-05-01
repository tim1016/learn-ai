# SSE job streams — feature runner, signal engine, cross-sectional

A unified live-progress mechanism for long-running research jobs.
Replaces ad-hoc polling and the synchronous GraphQL "spinner until done"
pattern with a typed event stream. Cross-sectional has used this since
PR #57; feature runner and signal engine moved to the same plumbing on
2026-05-01.

## Goals

1. **One transport for all three runners.** Cross-sectional, feature
   research, and signal engine all stream through `/api/jobs/{id}/events`.
2. **User-friendly messages, not stage IDs.** The backend declares
   phases as `(id, label, weight)` tuples in `app/jobs/phases.py`; the
   UI renders the friendly label.
3. **Bounded log buffer.** The Angular `RunLogBuffer` caps at 500
   entries and silently discards the oldest. Heavy phases (walk-forward,
   backtest grid) won't blow out the panel.
4. **Idempotent re-runs via a result cache.** A successful run with
   identical params is served from Redis instead of recomputing. The UI
   skips the live-progress panel on cache hits and renders the final
   report directly.

## Architecture

```
┌──────────┐    POST /api/jobs/{type}      ┌──────────┐    POST /api/jobs-internal/{type}    ┌──────────┐
│ Frontend │ ─────────────────────────────▶│  .NET    │ ─────────────────────────────────────▶│  Python  │
│ (signal/ │   (mints job_id)              │ JobsApi  │   (forwards body + job_id)            │  jobs    │
│ feature/ │                               │          │                                       │  router  │
│ batch)   │ ◀────────── 202 Accepted ─────│          │ ◀──── 202 {job_id, status:queued|cached}│         │
└──────────┘                               └──────────┘                                       └──────────┘
     │                                                                                              │
     │   GET /api/jobs/{id}/events  (SSE)                                                           │
     │  ◀────────── job.started, job.phase, job.progress, job.log, job.completed ─────────────────  │
     │                                                                                              │
     │                                  Redis Streams: job:{id}:events ←─ ProgressEmitter ──────────│
     │                                  Redis Hash:    job:{id}:state                               │
     │                                  Redis String:  job:{id}:result                              │
     │                                  Redis String:  result:{type}:{params_hash}  (24h TTL)       │
     │                                                                                              │
     │   GET /api/jobs/{id}/result                                                                  │
     │  ◀────────── final JSON payload ────────────────────────────────────────────────────────────│
```

## Job types

Registered in two places:

1. **`Backend/Jobs/JobsApi.cs:JobTypeRoutes`** — public slug → Python path.
2. **`PythonDataService/app/routers/jobs.py`** — one `@router.post(...)` per type.

| Public slug | Python path | Cache key params |
|---|---|---|
| `backtest` | `/api/jobs-internal/backtest` | not cached |
| `dataset-zip` | `/api/jobs-internal/dataset-zip` | not cached |
| `engine_backtest` | `/api/jobs-internal/engine-backtest` | not cached |
| `cross_sectional` | `/api/jobs-internal/cross-sectional` | feature, tickers, dates, target |
| `feature_research` | `/api/jobs-internal/feature-research` | ticker, feature, dates, bar resolution |
| `signal_engine` | `/api/jobs-internal/signal-engine` | ticker, feature, dates, flip_sign, regime_gate |

## Phase vocabulary

`app/jobs/phases.py` is the single source of truth. Each runner declares
its phases as `(id, label, weight)`. The `id` is a stable token used in
tests and CSS hooks. The `label` is what the UI shows.

Adding a new phase:

1. Append to the runner's tuple in `phases.py`.
2. Emit it from the runner via `on_phase("new_id")`.
3. The frontend will display the new label automatically — no client
   change required for the label, only for any new visualization.

The `humanisePhase` helper on the frontend handles dynamic per-iteration
phases (e.g. cross-sectional's `ticker_3_AAPL` → "Ticker 3 AAPL").

## Cancellation

The framework cancel pathway is cooperative. Frontend calls
`DELETE /api/jobs/{id}` which sets `cancel_requested=1` on the state hash.
The runner polls via `cancel_check()` at every phase boundary. If the
cancel flag is set, `cancel_check()` raises `JobCancelled`, which
`run_in_thread` catches and turns into a terminal `job.cancelled` event.

**Default cancel semantics (revisit if a user objects):** discard the
partial report; do **not** cache cancelled runs. The user sees the final
log line `Run cancelled` and can start a fresh run.

## Result cache

`app/jobs/cache.py` is a thin wrapper over a Redis `result:{type}:{hash}`
key with a 24h TTL.

- **Hit**: dispatch route returns `{job_id, status: "cached"}` AND emits
  `job.started` + `job.completed` (with `cached: true`) to the new
  job's event stream. The frontend's `JobsService` reducer surfaces
  `cached` on `JobState`. The UI skips the live-progress panel and
  renders the report immediately.
- **Miss**: dispatch route spawns the worker thread as today. On
  `job.completed`, the worker writes the result back to the cache
  before returning.
- **Force re-run**: `force=true` in the request body bypasses the
  lookup. UI exposes this as a "Force re-run" toggle on each runner
  form.
- **What's NOT cached**: failures, cancellations, partial results. Only
  successful `job.completed` writes a cache entry.

## Frontend integration

| Concern | Where it lives |
|---|---|
| EventSource lifecycle | `Frontend/src/app/services/jobs.service.ts` |
| Rolling FIFO log (cap 500) | `Frontend/src/app/utils/run-log-buffer.ts` |
| Shared status/progress/log panel | `Frontend/src/app/components/research-lab/shared/run-progress-panel/` |
| Feature runner UI | `Frontend/src/app/components/research-lab/feature-runner/` |
| Signal runner UI | `Frontend/src/app/components/research-lab/signal-runner/` |
| Cross-sectional UI | `Frontend/src/app/components/research-lab/batch-runner/` |

Each runner UI:

1. Calls `JobsService.startJob(type, params)`. JobsService POSTs to
   `/api/jobs/{type}` and opens an EventSource on `/api/jobs/{id}/events`.
2. Watches `JobsService.jobs()` via an `effect()` and forwards new log
   entries into a per-component `RunLogBuffer`.
3. Renders `<app-run-progress-panel>` while the job is running (skips
   the panel on cache hits).
4. On `status === 'completed'`, fetches the result via
   `JobsService.fetchResult<T>(id)` and renders the existing report
   component (`feature-report`, `signal-report`, or the inline batch
   verdict block).

## Tests

- `PythonDataService/tests/jobs/test_phases.py` — vocabulary lookups,
  fallback humanisation.
- `PythonDataService/tests/jobs/test_cache.py` — params-hash
  determinism, store/lookup round-trip, `serve_cached_result` writes
  the right Redis keys + emits the right events.
- `PythonDataService/tests/jobs/test_runner_callbacks.py` — runners emit
  the documented phase sequence, friendly labels appear in the log,
  `JobCancelled` propagates through to the wrapper.
- `Frontend/src/app/utils/run-log-buffer.spec.ts` — cap behavior,
  unique stable ids, level mapping.

## Critical decisions taken (2026-05-01)

- **Cancel semantics: discard partial result, no cache.** Chosen
  default. Revisit if users want to keep partial output.
- **24h TTL on cached results.** Matches the existing
  `JOB_TTL_SECONDS`. Revisit when multi-user or longer research
  campaigns land.
- **Cache key normalisation upper-cases tickers and sorts string
  lists.** `["spy", "QQQ"]` and `["SPY", "qqq"]` collide. Date
  strings are passed through verbatim.
- **`InterruptedError` was rejected for cancel propagation.** Runners
  let `JobCancelled` propagate. To avoid a `research → jobs` import
  inversion, the `except Exception` handler in each runner sniffs by
  exception class name (`type(e).__name__ == "JobCancelled"`) and
  re-raises.
- **No new .NET code beyond two `JobTypeRoutes` entries.** The
  `JobsApi` is already type-agnostic; cache hits are signalled to the
  frontend via the `cached: true` flag on the `job.completed` event,
  not via a new HTTP response shape.
- **Cross-sectional log message style left mostly intact.** The
  per-ticker `[1/3] AAPL: building IV history...` format was kept —
  it's already concise and readable. Only the kickoff message was
  rewritten for friendliness.
