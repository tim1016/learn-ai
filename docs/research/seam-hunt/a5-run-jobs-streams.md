# A5 — Run-session and jobs streams: can a run or job show the wrong terminal state?

Research ticket: tim1016/learn-ai#2293 (map #2276, seam A5). Static read at
`a14f1df1` (master, 2026-09-23). Every `file:line` below is at that SHA.
Nothing was run and no live service or container was touched.

## Answer

**Research-only. This seam is off the money path.** The jobs stream carries
research and data-lab work only: dataset ZIPs, backtests, LEAN runs, grid
search, walk-forward, recency, cross-sectional, feature and signal research,
and lake backfill (`Backend/Jobs/JobsApi.cs:37-60`). No module under
`app/broker/`, and no `bot_*` service, imports `app.jobs`. The only importers
are research, data-lake and engine-backtest code, plus `main.py` (checked with
`grep -rl "from app.jobs"`). No bot, fleet, broker or clerk component in
`Frontend/` injects `JobsService`. On the map's severity scale no gap here can
exceed **P2**. No `bug` issue was filed.

**Yes, a run or job can show the wrong terminal state**, in two proven ways
and three suspected ones:

- **Proven (P2):** if the relay answers a reconnect with anything other than
  a 200 event stream, the job sticks at "running" in this tab until reload.
  The data-lab run card stays on "fetching"/"bundling", and Cancel does not
  help (D-1).
- **Suspected (P2/P3):** the stream closes without a terminal frame, a hung
  worker stays "running" in the stream while the research record's REST
  answer says `interrupted`, and a resumed tab loses the run card.

Payload fidelity is sound. Every field the frontend reads is emitted with a
compatible type. The drift is limited to one field that is never sent
(`friendly`), unused extras, and ISO date strings on the wire (P3).

**Corrections to the ticket premise:**

1. The job stream's TS types are **not** in
   `Frontend/src/app/api/broker-models.ts`. That file mirrors broker SSE
   payloads only (`broker-models.ts:1-13`). Job frames are typed loosely in
   `jobs.service.ts:22-35` (`[key: string]: unknown`). Consumers narrow them
   with `as` casts at the use site. On the Python side the events are plain
   dicts, not Pydantic models (`app/jobs/progress.py:260-261`). Nothing ties
   the two sides together except convention.
2. The public stream is served by the **.NET facade**
   (`Backend/Jobs/JobsApi.cs:174-250`), not by FastAPI. The dev proxy sends
   `/api/jobs` to the backend (`Frontend/proxy.conf.js:162-166`). The map
   condemns .NET, but it is the only relay for this stream, so it is traced
   here as transport.

**Recurring A-seam patterns:**

| Pattern | Present here? |
|---|---|
| `end` frame freezes the consumer (A1 G / [#2322](https://github.com/tim1016/learn-ai/issues/2322)) | No `end` frame. A terminal `job.*` event is the intended close. The inverse problem exists instead: a close *without* the terminal frame (G1). |
| Epoch race (A1/A2) | Mechanism exists in `RunSessionService._downloadResult` (G4). The UI blocks it in the normal flow. |
| Keepalive-masked stall (A2 [#2326](https://github.com/tim1016/learn-ai/issues/2326)) | Nothing masks a stall, because the relay sends **no** keepalive or comment at all. But nothing detects one either (G3). |
| 1 MB relay cap ([#2328](https://github.com/tim1016/learn-ai/issues/2328)) | Absent. The .NET relay writes each Redis entry whole (`JobsApi.cs:252-264`), and the stream does not pass through the coordinator. |
| Auth expiry mid-stream | Not applicable. `/api/jobs` has no auth and uses a native `EventSource` (`jobs.service.ts:278`). |

## (a) Trace

### Producer (Python worker thread)

1. The .NET facade `POST /api/jobs/{type}` mints the id and writes
   `job:{id}:state` with `status=queued`. It adds the id to `jobs:active`
   (`JobsApi.cs:107-128`), then forwards to
   `/api/jobs-internal/<type>` (`JobsApi.cs:133-151`). If Python refuses, the
   hash is marked failed **without writing any stream event**
   (`JobsApi.cs:426-440`). The POST also returns an error, so `startJob`
   throws and no stream is opened (`jobs.service.ts:159-172`).
2. `run_in_thread` acquires the lease on the request path
   (`app/jobs/runner.py:68`). The daemon thread then emits `job.started`,
   runs the work, and calls `completed(result)` on a non-`None` return. It
   maps `JobCancelled` to `cancelled`, and any other exception to `failed`
   (`runner.py:70-88`).
3. Dataset ZIP (the `RunSessionService` job):
   `routers/jobs.py:382-483`. It emits `phase("loading_bars")` (`:429`), the
   chunk events from `dataset_service.py:302,322-329,345-352`, `chunk_paced`
   from `polygon_client.py:88`, then `fetch_complete` (`jobs.py:441-448`),
   `phase("bundling")` (`:450`), the bundle events
   (`routers/dataset.py:170,180-184,415,419,423`) and
   `options_companion_service.py:533-541`. It finishes with
   `phase("packaging")` and `completed_blob` (`jobs.py:463-468`).
4. Every emit is an `XADD job:{id}:events` with a single field
   `event=<json>`, capped at approximately 50k entries. It also slides the
   events TTL and the lease forward (`progress.py:260-277`).
5. **Terminal ordering.** Every terminal verb patches the state hash to the
   terminal status **before** it XADDs the terminal event:
   - `completed`: `progress.py:322-330`
   - `completed_blob`: `progress.py:360-372`
   - `failed`: `progress.py:376-382`
   - `cancelled`: `progress.py:386-390`
   - the cache path: `app/jobs/cache.py:117-146`

   This is load-bearing for G1.
6. On startup, `fail_jobs_without_a_worker` emits `job.failed`
   (`DATA_SERVICE_RESTARTED`) for every queued or running id in the active
   set. It runs only on roles that run the data-plane core
   (`progress.py:398-431`, `main.py:384-393`).

### Relay (.NET `GET /api/jobs/{id}/events`)

7. `Last-Event-ID` is read directly as a Redis stream id. It defaults to
   `0-0`, which means full replay (`JobsApi.cs:188-189`).
8. Replay uses `XRANGE (lastId +` (`:201-207`). The tail loop is a
   non-blocking `XREAD` of 64 entries. On an empty read it does
   `HGET status`. If the status is terminal, it runs one more `XRANGE` drain
   and then **closes the response** (`:211-237`). Otherwise it sleeps 500 ms
   (`:241`).
9. Each frame is written as `id: <stream id>` and `data: <json>`, with no
   `event:` name, so it arrives as `onmessage`. There are no comment,
   keepalive or retry fields (`:252-264`). A missing or expired state hash
   (`status.HasValue == false`) is never treated as terminal, so the loop
   polls until the client leaves.

### Consumer (Angular)

10. `JobsService.openStream` creates one native `EventSource` per job and
    refuses a second one while any handle is stored (`jobs.service.ts:276-290`).
    `onerror` closes the source only if the job is already terminal
    (`:282-289`). Otherwise it relies on the browser's automatic reconnect.
    That reconnect sends `Last-Event-ID`, and the relay resumes from it.
11. `applyEvent` parses the frame and silently drops invalid JSON
    (`:305-311`). It folds known `job.*` verbs through the pure reducer
    `applyJobEvent` (`:364-444`). It then fans every raw frame out to
    `onEvent` listeners (`:321-323`), and on a terminal verb it schedules
    `closeStream` (`:325-329`). `closeStream` also deletes the listeners
    (`:292-303`).
12. The REST side is `GET /api/jobs?active=true`, which runs `SMEMBERS` and
    then `HGETALL` for each id (`JobsApi.cs:336-363`). It is read at boot and
    on demand (`jobs.service.ts:148-150,238-274`). It adds only jobs **this
    tab does not already know** (`:258`). A known job's status comes from its
    stream alone and is never re-read from REST.
13. `RunSessionService.start()` calls `startJob('dataset-zip')`, then
    subscribes with `onEvent` (`run-session.service.ts:292-325,370-380`). The
    stream is already open by then, but no frame can be lost: the POST
    resolves inside one task, and `onEvent` registers in that task's
    microtask continuation. An `EventSource` message is its own task, so it
    cannot run in between. On a terminal verb the service unsubscribes and
    resolves. `_handleEvent` maps:
    - `job.completed` → `done`
    - `job.failed` → `error`
    - `job.cancelled` → `error` with kind `cancelled`

    (`:387-427`). It then auto-downloads the ZIP (`:559-586`).
14. Rendered terminal state: `dockState`, `headline` and `headlineLevel`
    (`run-session.service.ts:229-268`) drive the shared run dock.
    `export.component.ts:120-125` blocks Generate while the state is
    `fetching`/`bundling`, or while `start()` is still awaiting.

## (b) Payload fidelity (Python emit → TS read)

| Frame / field | Python emits | TS reads as | Verdict |
|---|---|---|---|
| `job.started` | `{}`, or `{cached: true}` on the cache path (`progress.py:306`, `cache.py:138`) | `cached as boolean ?? prev` (`jobs.service.ts:387`) | OK |
| `job.phase.phase` | `str` (`progress.py:310`) | `string` (`:390`) | OK |
| `job.phase.friendly` | **never emitted**. The friendly label goes out as a separate `job.log` (`routers/jobs.py:922-940`) | `phaseLabel = friendly ?? humanise(phase)` (`:396`, `:475`) | P3: dead field; the label is always humanised |
| `job.progress.current/total` | `int`, always present (`progress.py:312-316`) | `number` (`:402-403`); `toLocaleString()` (`:481`) | OK |
| `job.progress.unit/message` | `str`; `message` is absent when `None` (`:313-315`) | `?? prev` (`:404-405`) | OK: absent, never null |
| `job.log.level/message` | `str` (`progress.py:319`) | `string` (`:409-410`, `run-session.service.ts:433-434`) | OK |
| `job.completed` (JSON) | `result_url` (`progress.py:330`), plus `cached`, `cached_at:int` ms (`cache.py:139-146`) | `resultUrl`, `cached`, `cachedAt:number` (`:422-424`) | OK. `cached_at` is int64 ms |
| `job.completed` (blob) | `download_url`, `filename`, `size_bytes:int`; **no `result_url`** (`progress.py:365-372`) | RunSession reads all three (`run-session.service.ts:398-402`); JobsService sets `resultUrl = undefined` (`:422`) | OK. Blob jobs have no `resultUrl` by design |
| `job.failed.code/message` | `str` (`progress.py:382`) | `string` (`:431-432`) | OK |
| `job.cancelled.reason` | `str`: `"job <id> cancelled"` or the chunker's text (`runner.py:80`, `jobs.py:436-439`) | `message` (`:439`) | OK |
| `chunk_plan.total` | `int ≥ 1` (`dataset_service.py:299-302`) | `number` (`run-session.service.ts:447`) | OK |
| `chunk_start.index/total/from/to` | `int`, `int`, `"YYYY-MM-DD"` strings (`dataset_service.py:316-329`) | `number`, `number \| undefined`, `string` (`:460-463`) | Types match. P3: date strings on the wire (temporal-rigor); display-only |
| `chunk_done.bars_returned` | `int` (`:345-352`) | `number` (`:472`) | OK |
| `chunk_paced.wait_seconds` | `float`, plus a `label` (`polygon_client.py:88`) | `Math.round(number)`; `label` ignored (`:480-487`) | Type OK. P3: the wait is attached to the *next queued* chunk, but it pauses the one currently fetching |
| `fetch_complete.*` | `int` × 3 (`jobs.py:441-448`) | `number ?? 0` (`:491-493`) | OK |
| `bundle_start.components` | `list[str]`, including placeholders `"calls/"` and `"puts/"` (`dataset.py:409-415`) | `string[]` (`:504`) | P3: see the next row |
| `bundle_component_done.name` | For options, the **per-slot path**, e.g. `calls/atm-03.csv` (`dataset.py:443-444`) | Matched by name against the list (`:529-531`) | P3: `calls/` and `puts/` never reach `done`, so bundling progress stays below 100% until `job.completed` |
| `bundle_progress` | `component`, `step:int`, `label`, plus `day` and `expiry` (`options_companion_service.py:533-541`) | The first three; extras ignored (`:520-523`) | OK |
| `processing_indicators`, `dividend_adjusted` | `int`s (`dataset.py:170,180-184`) | `number` (`:539-548`) | OK |
| SSE `id:` | Redis id `<ms>-<seq>` (`JobsApi.cs:259`) | `streamTimestamp()` → int64 ms (`jobs.service.ts:495-500`) | OK: server time, int64 ms |
| REST `started_at` | Stringified ms from Redis (`progress.py:305`, relayed as strings `JobsApi.cs:359`) | `string` → `Number()` (`jobs.service.ts:92,266`) | Value OK. P3: a `*_at` field typed `string` in a TS interface is on the temporal-rigor ban list |
| REST `status` | Only `queued`, `running`, `completed`, `failed`, `cancelled` are ever written | Cast `as JobStatus` (`:259`) | OK today. Nothing enforces it |
| `RunLogEntry.timestamp` | none; set on the client | `Date.now()` (`run-session.service.ts:174`) | P3: shows client receipt time, not server event time (the SSE id carries the latter) |

No `int64 ms` field is typed as a `number` that could arrive as a string on
the stream. Nothing that could be `null` is read without a `??` fallback,
except `chunk_*` and `bundle_*` numerics. Python always emits those.

## (c) Terminal-state races

**Stream closes before the terminal frame (G1).** Step 5 flips the hash
before the XADD. The relay's close check (step 8) can see this order:

1. `XREAD` is empty.
2. `HGET` returns a terminal status.
3. The drain `XRANGE` is still empty.
4. The relay closes.

The response then ends without the terminal frame. The browser reconnects
with `Last-Event-ID` after its retry delay (about 3 s by default). The relay
replays from there, so the frame arrives on the second connection. **Heals
in the normal case.** It does not heal if the terminal XADD itself fails
after the hash flip. `runner.py:81-86` then calls `failed()`, which patches
and XADDs again. If Redis is still down, that raises inside the `except`
block and the thread dies. The hash stays terminal with no terminal event,
and the client reconnects forever. Each reconnect replays nothing and closes
immediately, so the job reads "running" in the tab.

**Stream vs REST disagreement.** For a job this tab knows, REST is never
consulted (step 12), so the two sides can only disagree through a second
reader:

- **Research-record REST** (grid search, WFO, recency) presents
  `interrupted` once the worker lease has expired
  (`app/research/persistence/lifecycle.py:35-57,65-73`). A hung worker keeps
  `status=running` and emits nothing. The stream (and `JobsService`) then
  says **running** while the record REST says **interrupted** (G3). The
  lease semantics themselves belong to seam F, which is out of scope.
- **Boot snapshot race.** `ListJobsAsync` runs `SMEMBERS` and then `HGETALL`
  per id. The terminal verbs remove the id from the active set only **after**
  the emit (`progress.py:333,373,383,391`). So the snapshot can list a job
  whose status is already `completed`. `readUnknownActive` then upserts it
  as terminal and opens no stream (`jobs.service.ts:259-272`). The state is
  correct. The only thing missing is `finishedAt`/`resultUrl`. Strategy
  Lab's own-job path handles this by calling `handleEngineJobCompleted(id)`
  (`strategy-lab-runner.service.ts:513-517`). No wrong state results.

**Reconnect after completion.**

- If the tab already received the terminal frame, `setTimeout(closeStream)`
  wins (`jobs.service.ts:325-329`). Any later `onerror` also closes, because
  the job is terminal (`:285-288`).
- If the tab did not receive it, the reconnect replays the tail and receives
  it.
- A `Last-Event-ID` older than the approximate 50k trim loses the trimmed
  frames silently (`progress.py:56,262-267`; P3).
- An events key that expired after 24 h gives a relay that polls forever
  with no frames (`JobsApi.cs:226-227`). That needs 24 h of silence, which
  in practice means a hung worker.

**Reconnect refused (D-1, proven).** Per the WHATWG `EventSource`
processing model, a reconnect answered with a non-200 status or a non-
`text/event-stream` content type *fails* the connection: `readyState`
becomes `CLOSED` and there are no further retries. This happens when the dev
proxy returns 502/504 while .NET restarts, or when .NET returns 500 because
Redis is down during the replay `XRANGE` (`JobsApi.cs:202`). `onerror` sees
a non-terminal job and does nothing (`jobs.service.ts:282-289`). The dead
handle stays in `sources`, so `openStream` refuses to replace it (`:277`).
The REST refresh skips known ids (`:258`). The job stays `running` in this
tab until reload, whatever happens server-side. For the data-lab run:

- `RunSessionService.start()` never resolves (`run-session.service.ts:320`).
- `generateStarting` stays `true`, so Generate stays disabled
  (`export.component.ts:120-125,258-271`).
- The dock shows `fetching`/`bundling` with Cancel enabled. Cancel's DELETE
  succeeds (`JobsApi.cs:316-330`), but the `job.cancelled` it produces
  cannot arrive.

## (d) Named suspected gaps

| ID | Hypothesis (falsifiable) | Sev | Prototype sketch |
|---|---|---|---|
| **G1** | A terminal verb's XADD fails after its state-hash flip, and `failed()` then fails too. The relay then closes every connection without a terminal frame, and the tab shows the job "running" until reload. (In the benign form, a relay poll between the flip and the XADD closes early, and the frame arrives about 3 s late on reconnect.) | P2 (P3 benign) | Fake Redis or fakeredis with an `xadd` that raises once after `hset`. Run `run_in_thread` with a trivial work function. Assert the stream holds no terminal entry while `status` is terminal. Drive the relay's close predicate in a port of `StreamJobEventsAsync`'s loop: assert it closes with zero terminal frames. |
| **G2** | A reconnect answered with non-200 leaves the job non-terminal in `JobsService` for good, and `RunSessionService` in `fetching`/`bundling` for good. The server-side terminal is never observed. (The **mechanism is proven by reading**, as D-1. The prototype pins it as a regression test.) | P2 | Vitest with a fake `EventSource` class: emit two frames, then set `readyState=CLOSED` and fire `error`. Then have the "server" complete the job. Assert `jobs.job(id).status === 'running'`, `runSession.state() === 'fetching'`, and that `refreshActive()` does not recover it. |
| **G3** | A worker thread that hangs (no emit, no cancel check for more than 300 s) leaves the job stream and drawer at "running" indefinitely. Meanwhile the research-record REST reports `interrupted` and permits a resume claim, so the two views disagree and Cancel is a no-op. | P2 | Python: start a job whose work blocks on an Event. Advance past `JOB_LEASE_TTL_SECONDS` (monkeypatch it to 1 s). Assert `job_is_live(...) is False` and `HGET status == 'running'`, with no terminal frame. Do not test the lease-claim half (seam F, out of scope). |
| **G4** | A dataset-zip job whose ZIP download is still in flight can write `error` onto a newer run's state. `_downloadResult` sets `_state`/`_error` without checking that `_sessionId` still matches the run it downloads for (`run-session.service.ts:560-585`). Reachable only if a second `start()` can run during the download, for example after the export component is re-created mid-download, which resets its local `generateStarting`. | P3 | Vitest: stub `fetch` with a deferred 500. Call `start()` for job A and let it reach `done`. Before resolving the fetch, call `start()` for job B. Resolve the fetch and assert B's state is not `error`. |
| **G5** | After a page reload mid-run, `JobsService` resumes the `dataset-zip` job but `RunSessionService` stays `idle`. The dock says "idle — no run in flight" while the drawer shows it running, Generate is re-enabled (a second concurrent job), and the finished ZIP is never auto-downloaded. | P3 | Vitest: seed `refreshActive` with a running `dataset-zip` id. Construct `RunSessionService`. Assert `state() === 'idle'` while `jobs.activeJobs()` contains the job. |

## (e) Defects proven by reading

- **D-1 (P2): a refused reconnect freezes the job at "running"** in the tab.
  Evidence: `jobs.service.ts:277,282-289,258`, the WHATWG fail-the-connection
  rule, and `run-session.service.ts:320,370-380`. Research-only, so no `bug`
  issue per the map (P2 is listed in the resolution comment only). G2 is its
  regression prototype.
- **D-2 (P3): `calls/`/`puts/` bundle placeholders never complete.** The
  per-slot names at `routers/dataset.py:443-444` never match the placeholders
  at `:409-413`, so the run card's bundling fraction stalls below 100%
  (`run-session.service.ts:197-201,527-531`).
- **D-3 (P3): `job.phase.friendly` is read but never emitted.**
  `jobs.service.ts:396,475` vs `progress.py:308-310` and
  `routers/jobs.py:922-940`.
- **D-4 (P3): `chunk_paced` is attributed to the wrong chunk.** The pause
  happens inside the fetching chunk's `fetch_aggregates`
  (`dataset_service.py:330-340` → `polygon_client.py:88`), but the UI marks
  the next queued chunk (`run-session.service.ts:481-485`).
- **D-5 (P3): stale doc claim.** `BackfillJobRunner.observe` says that with
  no `Last-Event-ID` "the stream replays the job's whole history first"
  (`shared/data-lake/backfill-job-runner.ts:110-114`). But `observe` rides
  the `EventSource` that `JobsService` already opened
  (`jobs.service.ts:196-207`), and that source may have delivered the
  history before the listener registered. Listeners get no replay. This is
  seam F (lake backfill), out of scope; noted only.
- **D-6 (P3, temporal-rigor):** `chunk_start.from/to` are ISO date strings on
  the wire, and `ServerJobState.started_at`/`completed_at` are typed `string`
  (`jobs.service.ts:92-93`). Both are display or arithmetic-only today.

No P0/P1 was proven or suspected, so **no `bug` issue was filed**.

## Invariants each side assumes of the other

| Assumed by | Invariant | Guaranteed? |
|---|---|---|
| Relay | "Status terminal" means the terminal event is already in the stream | **No.** Python flips the hash first (step 5). Healed by client reconnect (G1). |
| JobsService | The browser will reconnect after any drop | **No.** Not after a non-200 response (D-1). |
| JobsService | A known job's stream is the only truth needed; REST need not be re-read | Holds only while the stream stays alive (D-1, G3). |
| RunSessionService | `onEvent` sees every frame of the run | Yes for a started run (step 13). No for a resumed run: it never subscribes (G5). |
| Python worker | Something will close a record whose worker died | Only on a data-plane restart (`progress.py:398-431`). A hung, still-alive worker is never closed (G3). |
