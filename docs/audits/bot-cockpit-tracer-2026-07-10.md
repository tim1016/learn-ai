# Bot Cockpit Tracer Audit (2026-07-10)

## Scope

This audit stress-tested the Bot Cockpit data plane around eight tracer bullets:

1. State SSE contract
2. Durable event gap/reset recovery
3. WAL rotation race
4. Account-owner fencing takeover
5. Data-plane boot resilience
6. Staleness honesty
7. Frontend backfill/reconnect loop
8. Cockpit pending-mutation dead stream

## Executive Summary

The bug hunt found one severe production bug in the broker session mirror history path. The data plane could be OOM-killed at boot by reading a 1.3GB `_broker/session_roster_history.jsonl` file into memory. The growth source was a reads-write violation: high-frequency per-bot status/diagnostic assembly called `BrokerSessionMirrorService.snapshot()` and every call appended durable mirror history.

The fix makes history writes opt-in for the mirror page/stream, makes history reads byte-bounded to a 64MB tail window, and compacts the JSONL history log on append once it exceeds the byte budget.

Live verification on 2026-07-10/2026-07-11:

- Restarted `polygon-data-service`; `/health` returned healthy.
- Called `/api/broker/session-mirror` with the control-secret header; the mirror responded with `observer_status=online`.
- Existing history log compacted from 1.3GB to 43MB.
- Container remained `healthy`, with memory under the 2.1GB cgroup cap.

## Severe Finding

### T5 — Broker Session History OOM at Data-Plane Boot

Severity: Severe production availability bug.

Root cause chain:

- `BrokerSessionMirrorService.snapshot()` always appended roster history.
- Per-bot diagnostics composed that snapshot at high frequency, so ordinary reads wrote durable mirror history.
- `_broker/session_roster_history.jsonl` grew to 1.3GB.
- `BrokerSessionHistoryService` loaded the whole file with `read_text().splitlines()`.
- Concurrent boot/status reads could push the container over its memory cap and leave the uvicorn reload worker dead.

Fix:

- `BrokerSessionMirrorService.snapshot(record_history=False)` defaults to no durable write.
- The mirror page endpoint and mirror stream call `snapshot(record_history=True)`.
- History reads tail only the last 64MB and then retain the configured snapshot window.
- Appends compact the file back under budget by writing the retained tail atomically.

Regression coverage:

- Oversized history log compacts to the retention window.
- Oversized history reads are byte-bounded and drop partial first lines.
- Default mirror snapshot reads do not write history.
- Mirror endpoint fakes assert the page path opts into `record_history=True`.

## Tracer Results

### T1 — State SSE

Status: Pass.

The existing direct stream coverage was already strong. Added an ASGI HTTP-layer tracer for `/operator-surface/stream` that verifies route/middleware behavior, the required `X-Data-Plane-Control-Secret` header, `text/event-stream`, and the first snapshot event without hanging on an infinite stream.

### T2 — Gap/Reset via HTTP

Status: Pass.

Added HTTP tracers for:

- Overflow gap → `event: gap` with `last_safe_cursor`.
- REST backfill from the safe cursor recovering every missed row.
- Resubscribe at the recovered high-water mark and receive live rows again.
- Mid-stream WAL replacement → `event: reset` and stream close.

### T3 — WAL Rotation Race

Status: Pass.

The suspected bypass did not exist. Pinned the race with a durable-channel test: `publish()` notices external WAL replacement before accepting a row onto the old stream identity.

### T4 — Fencing Takeover

Status: Pass.

Added the Stage 6 exit-criterion test: owner A pauses, owner B advances durable generation, owner A resumes stale. The stale owner is refused at both boundaries:

- Intent acceptance: `OWNER_GENERATION_MISMATCH`.
- Broker-write fence: `OWNER_GENERATION_STALE_AT_BROKER_WRITE`.

### T5 — Boot Resilience

Status: Pass after fix.

Added boot/degradation tracers for daemon-down startup and corrupt mutation-attempt artifacts. Existing corrupt-path resilience was preserved. The severe history OOM bug was fixed and live-verified.

### T6 — Staleness Honesty

Status: Pass.

Provider-level stale-observation behavior was already covered. Added roster-level coverage so stale retained daemon payloads do not present old process truth as live; rows render `unreachable` while preserving the old source timestamp.

### T7 — Frontend Backfill Loop

Status: Pass, with one minor finding.

The feared infinite 409 loop cannot happen: recovery is bounded and transitions to `error`. Added frontend coverage for the bounded recovery path.

Minor finding: when recovery backfill fails after a stream drop, the feed honestly marks the status as `error` and preserves rows, but it does not schedule a retry. This is not an infinite-loop bug; it is an availability/UX follow-up.

### T8 — Cockpit Dead Stream

Status: Pass.

Added frontend coverage that a pending mutation survives a stream outage and clears only when a reconnect snapshot confirms the mutation receipt.

## Verification

Python:

- `pytest tests/services/test_broker_session_history.py tests/services/test_broker_session_mirror.py tests/routers/test_broker_activity.py tests/routers/test_live_instances.py tests/engine/live/test_account_owner.py tests/services/test_durable_event_channel.py -q`
  - `245 passed`
- `pytest tests/routers -q`
  - `466 passed`
- `ruff check` on touched Python files
  - clean

Frontend:

- `npx eslint src/app/components/broker/bot-control/bot-surface-store.service.spec.ts src/app/services/durable-event-feed.spec.ts`
  - clean
- `npx ng test --watch=false --include src/app/components/broker/bot-control/bot-surface-store.service.spec.ts --include src/app/services/durable-event-feed.spec.ts`
  - `17 passed`
- `npx ng test --watch=false`
  - `165 test files passed`
  - `1524 tests passed`

Live:

- `curl http://localhost:8000/health`
  - `{"status":"healthy","service":"polygon-data-service","git_sha":null}`
- `curl -H 'X-Data-Plane-Control-Secret: local-dev-control-secret' http://localhost:8000/api/broker/session-mirror`
  - `observer_status=online`
- `PythonDataService/artifacts/live_runs/_broker/session_roster_history.jsonl`
  - compacted from 1.3GB to 43MB
- `podman ps`
  - `polygon-data-service` healthy

Known baseline note:

- Full `ruff check .` still fails on unrelated pre-existing issues in `run_spy_partial_parity.py` and `scripts/build_iv30_golden.py`; those files were not changed in this audit.

## Follow-Ups

1. Add a retry policy for frontend recovery-backfill failure after a stream drop.
2. Consider excluding `tests/` from the live uvicorn reload watcher; test-file edits triggered live reloads during this audit.
3. Consider lowering broker-session history further after compaction if 64MB is more than the mirror UI needs.
