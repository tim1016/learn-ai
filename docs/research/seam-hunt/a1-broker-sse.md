# A1 — Broker SSE → broker/clerk panel stores

Research ticket #2289, part of map #2276. Baseline `a14f1df1` (master, 2026-09-23).
Method: static reading, plus one in-process probe of `_sse_frames` on the host venv
(no containers, no live services). Every claim cites `file:line` at `a14f1df1`.

## Answer

**Could the operator see false live state from this seam? Not by any path I could prove
at `a14f1df1`.** No P0 or P1 defect was proven, so no `bug` issue was filed.

- **Payload fidelity is clean.** Every hand-mirrored type in
  `Frontend/src/app/api/broker-models.ts` matches its Pydantic model field by field:
  nullability, enum members, numeric types, and `int64 ms` timestamps typed as
  `number`. See the table in §2.
- **One defect is proven, and it is P2.** The coordinator's SSE hop **ends every routed
  lane stream after the first 15 s with no lane event**. Its keepalive timeout cancels
  the lane iterator (§4, D-1). The browser then reconnects, so nothing freezes. The cost
  is reconnect churn about every 15 s on quiet bot panels.
- **D-1 is also the only reason the next gap stays closed.** The coordinator's keepalive
  comes from the coordinator itself, and it drops the lane's own keepalives. If D-1 is
  fixed naively, a half-open or wedged lane looks to the browser like a healthy, quiet
  stream (G-1, P1). Any fix for D-1 must add a lane-liveness check.
- **Two P1 gaps are suspected and need a prototype:**
  - **G-2:** on a clerk restart, the bot panel may freeze on its last snapshot. It gets
    `event: end`, then shows a transport status the UI never renders.
  - **G-3:** during a drain/retire handover, an open stream may stay pinned to the old
    lane.
- **The IBKR option streams are off the money path but mislabelled (D-2, P2).** The
  "live · last tick" caption shows the time the snapshot was *emitted*, not the time of
  the last tick.

## 1. Scope and trace

Several SSE streams exist on the broker surface. A1 owns the **shared transport**: the
coordinator hop and the two frontend SSE primitives, plus the IBKR streams in
`app/routers/broker.py`. The payload-level stores built on this transport belong to
sibling tickets: the bot panel to A2 (#2290), the gallery to A3 (#2291), and the
qualification hold stream to A6 (#2294). Where an A1 transport behaviour reaches their
stores, I name the effect and leave the store internals to them.

### 1a. Fleet-routed clerk streams (coordinator hop)

The catalog declares two SSE operations: `bot_live_stream`
(`app/broker/alpaca/clerk/fleet_adapter.py:480-487`) and `gallery_stream`
(`fleet_adapter.py:528-534`).

1. **Lane producer.** `stream_live_snapshot_scoped` (`app/routers/broker_v2_panel.py:478-547`)
   subscribes to a `SurfaceHub` (`app/services/surface_hub.py:216-231`). Subscribing
   primes the queue with the latest snapshot (`surface_hub.py:227-228`), so a stream
   always starts with a snapshot and then carries deltas.
   - A snapshot frame carries `id: <epoch>:<version>` (`broker_v2_panel.py:531-532`).
   - A cursor from another epoch gets `event: reset` first (`:516-517`).
   - The producer emits `: keepalive` after 15 s of silence (`:519-522`).
   - It emits `event: end` when the hub closes the watcher (`:523-525`), and
     `event: error` followed by a return on a refresh failure (`:526-529`).
   - The hub publishes **only on a semantic change** (`surface_hub.py:283-294`). The
     fingerprint excludes transport-only fields and age fields (`surface_hub.py:22-36`).
     So a quiet, healthy panel emits no frames at all.
2. **Lane identity injection.** `FleetIdentityMiddleware` wraps any `text/event-stream`
   response in a `_FrameInjector` (`app/broker/fleet/agent_identity.py:361-393`). The
   injector re-frames the body and adds `x-fleet-broker`, `x-fleet-clerk-id`, and the
   optional `x-fleet-routing-epoch` / `x-fleet-binding-generation` fields to every frame.
   It reads them from the runtime's *current* identity provider on each chunk
   (`agent_identity.py:212-258`).
3. **Coordinator delivery.**
   - `LaneRouter.stream_read` (`app/broker/fleet/routing.py:285-366`) resolves the lane
     once, when the stream opens. It passes `expected_binding_generation=None`
     (`routing.py:306`) and pins the session's routing epoch and the assignment's
     confirmed binding generation (`routing.py:679-681`).
   - It writes no routing receipt (docstring, `routing.py:294-299`).
   - `HttpLaneDelivery.stream` (`app/broker/fleet/delivery.py:313-353`) opens the lane
     with **no read timeout** (`delivery.py:315`). It parses frames with
     `iter_sse_events` (`app/broker/fleet/internal_http.py:241-321`), which drops
     comment lines (`internal_http.py:294-295`) and caps an event at 1 MB
     (`internal_http.py:52`).
   - It validates each event's `x-fleet-*` fields against the pinned attempt
     (`delivery.py:218-259`).
4. **Public re-emission.** `_sse_frames` (`app/routers/broker_clerks.py:330-352`)
   re-frames each verified event: `event:`, the identity fields, `id:`, and one `data:`
   line per data line. After 15 s with no event it yields `: keepalive`
   (`broker_clerks.py:326-345`). A lane refusal (4xx) is returned as a JSON body with the
   lane's status (`broker_clerks.py:401-410`).
5. **Browser transport.** `openAuthenticatedSseConnection`
   (`Frontend/src/app/services/authenticated-sse-connection.ts:21-84`) creates a new
   native `EventSource` on each (re)connect.
   - Any `error` event closes it and schedules a reconnect: 500 ms, doubling to at most
     5 s, and reset on `open` (`:39-42, :51-71`).
   - The URL may be a function, so the cursor is recomputed on each reconnect (`:36`).
6. **Snapshot adoption.** `openVersionedSnapshotStream`
   (`Frontend/src/app/services/versioned-snapshot-stream.ts:32-66`) parses `snapshot`
   frames.
   - It maps `reset` to `onReset` (`:59`).
   - It maps `end` to a **permanent `close()`** (`:60-62`).
   - `adoptVersionedSnapshot` (`:24-30`) lets any *different* epoch replace the current
     one, and within an epoch keeps the higher version.
7. **Store (A2's side, cited only for effect).** `BotPanelLiveStore`
   (`Frontend/src/app/components/broker/v2-panel/lib/bot-panel-live-store.service.ts`)
   bootstraps over REST (`:78-86`) and then opens the stream (`:163-196`).
   - On `error` it starts a 5 s REST fallback poll; on `open` or `closed` it stops it
     (`:189-194`).
   - The shell renders the transport status **only** as a loading hint, when the chart
     is still null and the status is `connecting` (`panel-shell/bot-panel-shell.component.html:73`).

### 1b. IBKR data-plane streams (no fleet hop)

- **`/api/broker/option-chain/{symbol}`** (`app/routers/broker.py:362-427`) is fed by
  `stream_option_chain` (`app/broker/ibkr/market_data.py:271-307`).
  - Every debounce window it reads the cached `ib_async` tickers and yields an
    `IbkrChainSnapshot` stamped `as_of_ms=now_ms_utc()` (`market_data.py:307`).
  - It does not check whether any tick arrived, or whether the client is still
    connected.
  - Errors reach the browser as one curated `event: error` frame, after which the
    generator ends (`broker.py:405-421, :542-551`).
- **`/api/broker/option-surface/{symbol}`** (`broker.py:433-536`) works the same way,
  with `as_of_ms=now_ms_utc()` (`app/broker/ibkr/surface.py:251`).
- **`/api/broker/ibkr/evidence/stream`** (`broker.py:108-135`) backfills and then
  follows. **No frontend consumer exists**: it appears only in the generated
  `broker.types.ts:724`.
- **Consumer.** `brokerSse` (`Frontend/src/app/services/broker-sse.ts:52-146`) uses the
  browser's native reconnect.
  - The `open` event sets `status='open'` and clears `lastError` (`:69-72`).
  - A named server `error` frame, or a transport error, sets `status='error'` and keeps
    the buffer (`:101-117`).
- **Pages.** The options-chain page (`broker-options-chain.component.ts:274`, `.html:163-180`)
  and the options-surface page (`broker-options-surface.component.ts:366`, `.html:132-137`)
  render `as_of_ms` as "Updated" / "live · last tick" / "Last tick".
  - Both hide the stream UI when `BrokerHealthService` (a 5 s REST poll,
    `services/broker-health.service.ts:5,82`) reports the broker as disconnected
    (`broker-options-chain.component.html:27-31`).

### 1c. Invariants each side assumes

| # | Assumed by | Invariant | Guaranteed? |
|---|---|---|---|
| I-1 | Coordinator `_sse_frames` | The events iterator survives a timed-out `__anext__`, so the stream continues after a keepalive | **No.** `asyncio.wait_for` cancels the in-flight `__anext__`, which finalizes the async generator. Proven by probe (D-1). |
| I-2 | Browser / stores | An `open` transport with no frames means "healthy and unchanged" | Only while the coordinator's liveness equals the lane's. Lane keepalives are dropped (`internal_http.py:294`), and the coordinator writes its own (`broker_clerks.py:345`). Masked today only by D-1 (G-1). |
| I-3 | Lane `SurfaceHub` | Every subscriber starts with the latest snapshot | Yes (`surface_hub.py:227-228`) |
| I-4 | Coordinator | Every event proves its lane identity | Yes, per event (`delivery.py:218-246`). Fails closed on the next event after a lane re-registers. |
| I-5 | Coordinator | The account's *routing* is still the one pinned at open | **Not re-checked** for open streams. Only the lane's self-stamped identity is compared (G-3). |
| I-6 | `versioned-snapshot-stream.ts` | A different epoch is always newer | **No.** Epochs are `process_epoch:uuid4` strings (`surface_hub.py:265-266`) with no order (G-4). |
| I-7 | `versioned-snapshot-stream.ts` | `end` means "this stream is finished for good" | The lane sends `end` when a hub stops, including during app shutdown (`app/main.py:994-996` → `live_projection.py:118-131` → `surface_hub.py:310-315`). "Finished for good" is not true across a restart (G-2). |
| I-8 | IBKR chain/surface UI | `as_of_ms` is the time of the last tick | **No.** It is the time the snapshot was emitted (D-2). |
| I-9 | Coordinator parser | No lane event exceeds 1 MB | Not guaranteed for a late-session 5 s panel snapshot (G-5). |

## 2. Payload fidelity (Python → TS)

The Pydantic models live in `app/broker/ibkr/models.py` and `app/broker/ibkr/api_evidence.py`.
The TS types live in `Frontend/src/app/api/broker-models.ts`.

Serialization is `model_dump_json()` with no `exclude_none`, so an optional field is
always present and is either `null` or a value, never absent. A float NaN serializes as
`null` (Pydantic v2's default `ser_json_inf_nan='null'`). The producer also coerces NaN
and IBKR's `-1` sentinel to `None` (`market_data.py:102-110`).

| Model.field | Python | TS | Match |
|---|---|---|---|
| `IbkrOptionQuote.symbol` | `str` | `string` | yes |
| `.expiry_ms` | `int` | `number` | yes (int64 ms) |
| `.strike` | `float` | `number` | yes |
| `.right` | `Literal["C","P"]` (`models.py:28`) | `'C' \| 'P'` | yes |
| `.bid/.ask/.last` | `float \| None` | `number \| null` | yes |
| `.bid_size/.ask_size` | `int \| None` (via `_coerce_size`) | `number \| null` | yes |
| `.iv/.delta/.gamma/.theta/.vega/.underlying_price` | `float \| None` | `number \| null` | yes |
| `.greeks_source` | `Literal[model,bid,ask,last,none]` (`models.py:345`) | same five | yes |
| `.ts_ms` | `int` (tick time, or wall clock as fallback, `market_data.py:139-150`) | `number` | yes (unused by the UI) |
| `IbkrChainSnapshot.{symbol,expiry_ms,underlying_price,quotes,as_of_ms}` | `str,int,float\|None,list,int` (`models.py:371-385`) | `string,number,number\|null,[],number` | yes |
| `IbkrSurfaceExpiry.{expiry_ms,quotes}` | `int,list` | `number,[]` | yes |
| `IbkrSurfaceSnapshot.{symbol,underlying_price,expiries,line_count,as_of_ms}` | `str,float\|None,list,int,int` (`models.py:403-426`) | `string,number\|null,[],number,number` | yes |
| `IbkrApiEvidenceEvent.seq/ts_ms` | `int` (seq ≥ 1) | `number` | yes |
| `.account_id/.symbol/.strategy_instance_id/.error` | `str \| None` | `string \| null` | yes |
| `.request.call` | `IbkrApiRequestName`, 9 members (`models.py:119-129`) | same 9 (`broker-models.ts:29-38`) | yes |
| `.response` | `IbkrApiResponseEvidence \| None` | `... \| null` | yes |
| `.response.callback` | 8 members (`models.py:130-139`) | same 8 (`broker-models.ts:39-47`) | yes |
| `.response.fields` / `.request.params` | `dict[str, JsonValue]` | `Record<string, IbkrEvidenceValue>` | yes |
| `.response.serializer_warnings` | `list[IbkrSerializerWarning]` | `IbkrSerializerWarning[]` | yes |
| Fleet-hop control frames | `reset {reason,cursor}`, `end {}`, `error {error}`, `snapshot` | `versioned-snapshot-stream.ts` reads only `reset` (ignores its data), `end`, `error.error`, `snapshot` | yes |

**Conclusion: no fidelity gap.** The fleet hop is byte-faithful for these frames:
- A multiline `data` is split into one `data:` line per line (`broker_clerks.py:351`).
- `id` persists across events, as WHATWG specifies (`internal_http.py:303-308`).
- `x-fleet-*` identity lines are unknown SSE fields, which the browser ignores.

There are two latent traps, neither live:
- `retry:` fields would be dropped by the coordinator parser. No producer emits one.
- A data-less frame is not dispatched. No producer emits one; `end` carries `{}`.

## 3. Stream behaviour

- **Reconnect and replay (fleet streams).** The browser does not use `Last-Event-ID`.
  `openAuthenticatedSseConnection` opens a new `EventSource` with a `cursor` query
  parameter taken from the adopted snapshot. The lane compares only the cursor's epoch:
  if it differs, it sends `reset`, then the primed latest snapshot. This is
  snapshot-then-delta with latest-wins, not replay. That is sound for a full-document
  surface.
- **Reconnect (IBKR streams).** The browser reconnects natively after the server closes.
  After a server `event: error`, the generator returns, the browser reconnects, `open`
  clears `lastError`, and a persistent error loops about every 3 s (G-6).
- **Ordering against REST.**
  - Inside one epoch, `adoptVersionedSnapshot` is monotonic, so a late REST
    `live-snapshot` cannot regress an SSE snapshot.
  - Across epochs there is no order (G-4).
  - Both REST and SSE resolve the same `clerk_id` through the same router, so they
    cannot reach different lanes except across a handover (G-3).
- **Error frames.**
  - Lane `event: error` passes through intact. The browser's `error` listener sees
    `data`, reports it, and reconnects.
  - A *coordinator-side* mid-stream failure (identity mismatch, `FleetStreamError`, a
    lane socket error) propagates out of `_sse_frames` after the response has started.
    The ASGI server aborts the connection, and the browser sees a data-less transport
    error. It fails closed, but the client gets no reason (P3, G-8).
- **Auth expiry.** GET streams are guarded by `require_data_plane_control_secret_always`
  (`broker_clerks.py:520-524`). The check runs once, when the stream opens. The secret is
  static and has no expiry, so nothing can expire mid-stream. A refused open (401/403) is
  a non-200 response: `EventSource` reports a data-less error, and the helper retries
  every 5 s indefinitely, without telling the operator "auth refused" (P3, G-8).

## 4. Defects proven by reading (plus one probe)

### D-1 — Coordinator keepalive ends every quiet routed stream after 15 s (P2, proven)

- **Code.** `_sse_frames` calls `asyncio.wait_for(iterator.__anext__(), timeout=15)`
  (`broker_clerks.py:338-345`). When the timeout fires, `wait_for` **cancels** the
  in-flight `__anext__`.
- **Mechanism.** The cancellation raises `CancelledError` inside the async-generator
  chain (`_identity_validated_events` → `_closing_event_iterator` → `iter_sse_events`).
  The generator finalizes, and its `finally` closes the lane response and client
  (`delivery.py:357-370`). The next `__anext__` raises `StopAsyncIteration`, so
  `_sse_frames` returns.
- **Probe.** The probe script (`$TMPDIR/a1-keepalive-probe.py`, thrown away) ran on the
  host venv at `a14f1df1` with the interval patched to 0.3 s. The lane sent snapshot 1,
  stayed silent for 1 s, then sent snapshot 2. Output: snapshot 1, then
  `[lane iterator finally ran -> response/client closed]`, then `: keepalive`, then
  **stream ended; snapshot 2 never delivered**.
- **Reach.** Every `bot_live_stream` whose hub has no semantic change for 15 s: a stopped
  bot, or no market pulse. Also `gallery_stream` on an account with zero shown bots.
- **Effect.** The public stream ends cleanly. The browser gets a transport error and
  reconnects after 500 ms. The bot-panel store flips to `error`, starts the REST
  fallback, then returns to `open`. The panel is not frozen: it *refreshes* on each
  reconnect. The costs:
  - reconnect churn about every 15.5 s for each quiet panel, and one `stream_read` plus a
    new lane HTTP client each time;
  - status flapping;
  - the keepalive comment itself is useless.
- **Severity.** P2: wrong, but it corrects itself.
- **Fix constraint for the fix issue.** Do not simply shield the `__anext__`. The
  current bug is what closes G-1; a shielded `__anext__` turns G-1 into a live P1. The
  fix needs lane liveness in the same change:
  - the lane emits a data-bearing `heartbeat` event, or the parser surfaces comments as
    liveness; and
  - the coordinator ends the stream after N intervals without either.
- **No test pins `_sse_frames`.** `grep` finds no match for `_sse_frames` or
  `_KEEPALIVE_INTERVAL_S` in `tests/`.

### D-2 — IBKR chain/surface "last tick" is the emission time, not the tick time (P2, proven)

- **Code.** `stream_option_chain` stamps `as_of_ms=now_ms_utc()` on every debounce
  window, whether or not any ticker changed (`market_data.py:271-307`). The surface
  stream does the same (`surface.py:207-251`). Neither loop checks
  `client.ib.isConnected()` or tick recency.
- **UI.** The chain page renders `'live · last tick ' + as_of_ms`
  (`broker-options-chain.component.html:176-180`). The surface page renders
  `Last tick {{as_of_ms}}` (`broker-options-surface.component.html:132-136`).
- **Effect.** If the API connection stays up but market data stops, the caption says
  "live, last tick just now" over frozen quotes indefinitely. Market data can stop while
  the connection stays up in several ways: a market-data farm disconnect, a competing
  live session, or a line re-routed after "restored, data lost" (compare D5 #2318).
  A true API disconnect is hidden within about 5 s by the health poll
  (`broker-options-chain.component.html:27-31`).
- **Severity.** P2, not P1: these pages are options research and place no orders; Alpaca
  equities are the money path. The per-quote `ts_ms` (the real tick time,
  `market_data.py:139-142`) is already on the wire and unused.

## 5. Named suspected gaps

Each gap is a falsifiable hypothesis with a prototype sketch. The sandbox is in-process
on the host venv plus Vitest, as the map requires.

| ID | Hypothesis | Sev | Prototype sketch |
|---|---|---|---|
| **G-1** | **Lane liveness is invisible to the browser.** If D-1 is fixed by shielding `__anext__`, a lane whose TCP connection goes half-open (sleeping or partitioned host, or a remote clerk after #2151) or whose event loop wedges keeps the public stream `open` forever. The coordinator keeps writing its own `: keepalive`, and `read_timeout_s=None` means httpx never gives up (`delivery.py:315`). The panel shows its last snapshot as current; the store has no staleness check (`bot-panel-live-store.service.ts:189-194`). | P1 (latent while D-1 stands) | Feed `_sse_frames` an iterator that yields one event and then awaits an `asyncio.Event` that never fires, with D-1 patched to `asyncio.shield`. Assert whether any frame other than a keepalive reaches the client within 3× the interval. |
| **G-2** | **A clerk restart freezes the open bot panel.** On graceful shutdown, `stop_live_projection_hubs` closes every watcher with `None` (`main.py:996` → `surface_hub.py:310-315`), and the lane emits `event: end`. If that frame reaches the browser, `openVersionedSnapshotStream` closes *permanently* (`versioned-snapshot-stream.ts:60-62`). The store goes to `closed` and stops its fallback poll (`bot-panel-live-store.service.ts:191`). Nothing reconnects, and the shell never renders `closed`. The panel keeps showing pre-restart health, position and readiness until the operator navigates away. | P1 | Two halves. **(a) Python:** run the lane ASGI app in-process behind `LocalLaneDelivery` with an open stream, trigger the lifespan shutdown, and record whether `event: end` reaches `_sse_frames`' output before the connection drops. Also run it under uvicorn with the compose stop timeout, to settle whether uvicorn's graceful shutdown waits on the stream first. **(b) Vitest:** a fake `EventSource` delivers `snapshot` then `end`. Assert that the store never reconnects and that the rendered shell shows no stale marker. |
| **G-3** | **Streams outlive a routing handover.** `stream_read` pins the lane once, when it opens (`routing.py:300-318`). Per-event checks compare only the lane's *self-stamped* identity (`delivery.py:218-246`). A drain/retire that moves the account (C5, ADR 0063) without changing the old lane's broker, clerk, epoch or generation leaves an open panel on the old lane's projection. This is the map's A × C case. | P1 | In-process fleet service with two lanes. Open a routed `bot_live_stream` to lane A, then run the C5 handover to lane B (retire A while it is still up). Assert whether the stream closes, or keeps delivering lane A snapshots that disagree with a fresh REST read. |
| **G-4** | **Cross-epoch regression.** `adoptVersionedSnapshot` lets *any* different epoch win (`versioned-snapshot-stream.ts:28`), and epochs have no order. A slow REST snapshot from epoch E1 that lands after an SSE snapshot from E2 regresses the panel to E1. The hub restarts on a new epoch after an idle retirement (`live_projection.py:103-110`) or a producer death (`surface_hub.py:174-177`). Because the hub publishes only on a semantic change, E1 can stay displayed until the next change. | P2 | Vitest: adopt SSE `{E2, v3}`, then resolve a delayed `refresh()` with `{E1, v9}`. Assert what the store displays. |
| **G-5** | **Oversized panel snapshot.** The coordinator parser caps an event at 1 MB (`internal_http.py:52, 269-273`). A 5 s live chart spans the whole RTH session (`chart_projection_service.py:196-216`), up to 4,680 bars, plus the panel. Late in the session one snapshot may exceed 1 MB, so each stream open fails with `FleetStreamError` and the panel runs on the 5 s REST fallback, flapping between `error` and `open`. | P2 | Build a `BotPanelLiveSnapshot` with 4,680 5 s bars and 20 markers, measure `len(model_dump_json())`, then push it through `iter_sse_events`. |
| **G-6** | **IBKR stream error loop clears its own error.** After an `event: error` frame the generator ends. The browser reconnects natively, and `open` clears `lastError` (`broker-sse.ts:69-72`), so a persistent qualification or broker error flashes and clears about every 3 s. Each retry repeats contract qualification against IBKR. | P3 | Vitest: a fake `EventSource` emits `error{data}`, then `open`, then `error{data}`. Assert that `lastError` alternates. Count qualification calls per minute with a fake client. |
| **G-7** | **Identity-provider loss mid-stream crashes the injector.** When the provider returns a non-mapping, `_FrameInjector.push` substitutes `{}` (`agent_identity.py:251-253`). `_identity_field_lines({})` then indexes `identity['broker']` (`:112`), and the `KeyError` aborts the lane response. The failure is closed, but untyped. | P3 | Unit test: `_FrameInjector(lambda: None).push(b"event: x\ndata: 1\n\n")`. |
| **G-8** | **Silent refusal loops.** A stream refusal (4xx/5xx at open) or a coordinator-side mid-stream abort reaches the browser as a data-less error. `openAuthenticatedSseConnection` retries every 5 s indefinitely and reports `onError(null)`, so the operator never learns the reason (auth refused, clerk unreachable, identity mismatch). | P3 | Vitest: a fake `EventSource` fails with no data 10 times. Assert that no operator-visible reason is produced. |
| **G-9** | **Dead replay client.** `durableEventFeed` (`services/durable-event-feed.ts`) and its `gap`/`reset` protocol have no production consumer and no Python producer: `last_safe_cursor` appears nowhere under `app/`. Anyone who wires it later will trust an untested contract. | P3 | None needed. Delete it, or ticket its first producer with a contract test. |

## 6. Bugs filed

None. No P0 or P1 defect was proven by reading. D-1 and D-2 are proven at P2; per the
map they are listed here and in the resolution comment only.

## 7. Verdict for the map

A1 is a **charted hazard**:
- the fidelity half is *cleared (static)*;
- the transport half has one proven P2 (D-1), whose fix is coupled to G-1;
- G-2 and G-3 are P1 suspects that need prototypes.
