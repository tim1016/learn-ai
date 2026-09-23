# C2: coordinator-to-lane delivery (local vs HTTP, SSE passthrough)

Research ticket [#2285](https://github.com/tim1016/learn-ai/issues/2285), part of map [#2276](https://github.com/tim1016/learn-ai/issues/2276).
Baseline `a14f1df1` (master, 2026-09-23). Every `file:line` is at that SHA. All paths are relative to the repo root; `PDS/` stands for `PythonDataService/`.

Method: a static trace of the code, plus two throwaway in-process probes on the host venv (listed under "Evidence" below). No live service, container, registry, Gateway, Alpaca or Polygon was touched.

## Answer

**Mostly yes on identity. No on stream liveness and command idempotency.**

- The two transports use the same response-identity check (`verify_identity_echo`) and the same per-event provenance check (`validate_event_identity`).
- The coordinator never retries on its own.
- Streams cannot starve commands on a lane, because the stream, request and command pools are separate.

The seam breaks in three proven places, all on the coordinator side:

1. **The coordinator's keepalive timer ends every routed SSE stream after 15 s of lane silence.** Instead of emitting a keepalive and carrying on, it closes the stream cleanly. The browser then flaps to `error`, polls, and reconnects. This is proven with a probe (P2).
2. **The routing ledger's "never redispatch" gate only fires once an attempt has *settled*.** A second request with the same idempotency key, sent while the first is still in flight or after the coordinator lost its outcome, is dispatched to the lane again. This is proven with a probe (**P1, filed**).
3. **A lane refusal that happens before the handler runs still burns the idempotency key.** This covers pre-handler refusals and the frontend's own 409 retry, and it is reported as "may have been applied" (P2).

The HTTP and local transports also differ in their error mapping. In production this does not matter: the fleet compose file runs HTTP delivery only (`compose.fleet.yaml:74`, `:136`, `:160`, and `PDS/app/main.py:437-441`, where `local_app` is set only for `FLEET_ROLE=combined`).

## (a) Trace

### Resolve, pin, dispatch

1. **Public route.** `register_catalog_operations` mounts one handler per catalog operation (`PDS/app/routers/broker_clerks.py:505-530`). The handler strips `command_context` out of the body before forwarding it (`broker_clerks.py:387-394`).
2. **Router selection.** Streams go to `LaneRouter.stream_read` (`broker_clerks.py:395-402`). Reads go to `deliver_read`. Effectful operations go to `deliver_command` (`broker_clerks.py:421-440`).
3. **`_resolve`** checks the registry route and the provider's served-context gate (`PDS/app/broker/fleet/routing.py:581-658`). **`_request`** pins broker, clerk, `session.routing_epoch` and `assignment.confirmed_binding_generation` into a `DeliveryRequest` (`routing.py:660-683`).
4. **Headers on the wire.** Only broker, clerk, epoch and generation travel as `X-Fleet-*` headers (`PDS/app/broker/fleet/delivery.py:117-127`). **The envelope's capability and idempotency key are never forwarded.** The lane sees an idempotency key only if the provider payload repeats it. `PanelActionRequest` does (`PDS/app/schemas/broker_v2_panel.py:645`).
5. **Commands only (`deliver_command`, `routing.py:377-577`):**
   - validate the envelope (`routing.py:685-755`);
   - `open_routing_attempt`, which returns the *existing* row for a known key (`PDS/app/broker/fleet/service.py`, `service.py:2250-2268`);
   - gate on `receipt.state != NOT_DISPATCHED` (`routing.py:453-468`);
   - `mark_routing_dispatched` (`routing.py:472`);
   - deliver;
   - settle.
6. **Settlement map for commands (`routing.py:475-577`):**
   - identity mismatch → `OUTCOME_UNKNOWN` (503);
   - typed refusal after dispatch → `OUTCOME_UNKNOWN`;
   - any other `Exception` → `OUTCOME_UNKNOWN`;
   - status ≥ 500 → `OUTCOME_UNKNOWN`;
   - status 4xx → `PROVIDER_REFUSED`;
   - status 2xx → `DELIVERED` with the receipt ref taken from the body (`routing.py:758-776`).
7. **Settlement map for reads and streams:**
   - transport exception or ≥ 500 → `ClerkUnreachable` (503);
   - identity mismatch → `ClerkIdentityMismatch` (409) (`routing.py:221-283`, `routing.py:319-373`).

### HTTP transport (production)

- **`deliver`** builds a fresh pinned client for each call. The read timeout is `operation.read_timeout_s` (10 s default, `PDS/app/broker/fleet/internal_http.py:32`). It verifies the echo and closes the client (`delivery.py:277-311`).
  - The client never follows redirects and ignores proxy environment variables (`internal_http.py:193-218`).
  - httpx transports do not retry by default, so the coordinator never re-sends.
- **`stream`** builds a client with `read_timeout_s=None` (`delivery.py:315`) and sends with `stream=True` (`delivery.py:316-327`). It then:
  - verifies the echo (`delivery.py:328-337`);
  - on a status ≥ 400, reads the body into `error_body` (`delivery.py:338-347`);
  - otherwise wraps the byte stream as `_identity_validated_events(_closing_event_iterator(...))` (`delivery.py:348-370`).
- **Framing.** `iter_sse_events` (`internal_http.py:241-321`):
  - WHATWG framing;
  - a size cap that also covers an unterminated tail;
  - `:` comments dropped (`:294`);
  - an event with no `data` is not dispatched (`:282`);
  - an unterminated trailing event is dropped at EOF (`:320-321`).

### Local transport (combined posture only)

- `_LocalAsgiHandler` (`routing.py:836-975`) dispatches raw ASGI into the same app. It carries the pinned headers and the process's own coordinator token (`routing.py:854-870`).
- **Non-streaming:** it collects every message and returns a `DeliveryResult`. There is no timeout (`routing.py:900-917`, `delivery.py:389-405`).
- **Streaming:** it bridges ASGI body messages through an `asyncio.Queue`. The end sentinel is enqueued only when a body message has `more_body=False` (`routing.py:924-932`). A refused open drains the body into `error_body` (`routing.py:951-970`).

### Lane side

- **Middleware order.** `add_middleware` wraps outermost-last. The resulting order is CORS → `FleetIdentityMiddleware` → `FleetLaneRuntimeMiddleware` → TrustedHost → app (`PDS/app/main.py:1025-1095`).
- **`FleetIdentityMiddleware`** (`PDS/app/broker/fleet/agent_identity.py:270-396`):
  - refuses unpinned or unproven mutations (`:304-321`);
  - refuses a mutation pinned to a stale epoch or generation *before the handler runs*, echoing the served identity (`:349-356`);
  - echoes identity headers on every response (`:358-373`);
  - stamps `x-fleet-*` field lines into every SSE frame, reading the identity provider again for each frame (`:212-267`, `:374-394`).
- **`FleetLaneRuntimeMiddleware`** (`PDS/app/broker/fleet/lane_runtime.py:492-744`):
  - Each call draws from the command pool (non-GET/HEAD, `lane_runtime.py:166-193`) or the request pool.
  - At `http.response.start` with `text/event-stream`, the call gives back its request slot and takes a stream slot (`lane_runtime.py:702-722`).
  - Exhaustion returns a 503 `fleet_lane_capacity_exhausted` before the handler (`lane_runtime.py:659-696`).
  - Sizing in `compose.fleet.yaml:40-49`: 16 requests, 4 streams, 16 commands, queue 64, 5 s.
- **Public re-emit.** The coordinator re-emits verified events through `_sse_frames` (`broker_clerks.py:330-351`), which has a 15 s keepalive (`broker_clerks.py:324-327`).
- **Browser.** The browser consumes the stream with a native `EventSource`. On `error` it reconnects after 500 ms, backing off to 5 s (`Frontend/src/app/services/authenticated-sse-connection.ts:51-70`). An `end` control event closes it for good (`Frontend/src/app/services/versioned-snapshot-stream.ts:59-62`).

## (b) Invariants each side assumes

| # | Assumed by | Invariant | Guaranteed? |
|---|---|---|---|
| I1 | Coordinator | Every 2xx response carries the served broker, clerk, epoch and generation echo. | **Yes** for fleet-addressed traffic. The identity middleware adds it on every pinned request (`agent_identity.py:358-373`). The one exception is a lane with no served identity yet, which forwards with no echo and logs it (`agent_identity.py:334-347`). The coordinator then refuses a 2xx (`delivery.py:183-190`). |
| I2 | Coordinator | Every streamed event carries provenance fields, so identity is verified per event. | **Yes.** The injector stamps every frame (`agent_identity.py:250-256`), and `validate_event_identity` refuses a missing field (`delivery.py:235-246`). A re-registration mid-stream is re-stamped and closes the stream. |
| I3 | Lane | A mutation reaching the handler was pinned to the current epoch and generation. | **Yes** when both sides have a value. The check is skipped when either side is `None` (`agent_identity.py:149-171`). |
| I4 | Coordinator (D11, ADR 0062 addendum item 7) | A key whose attempt was already dispatched never dispatches again. | **No.** "Dispatched but unsettled" is stored as `state='not_dispatched'` with `dispatched_at_ms` set (`PDS/app/broker/fleet/store.py:982-986`, `:1174-1183`). The gate reads only `state` (`routing.py:453`). See **C2-G2**. |
| I5 | Coordinator | The provider clerk deduplicates by idempotency key. | **Only where the payload carries the key.** The envelope is stripped (`broker_clerks.py:389-393`) and no header carries it (`delivery.py:117-127`). |
| I6 | Browser | Silence on a routed stream is carried by keepalives and never ends the stream. | **No.** The keepalive path ends the stream. See **C2-G1**. |
| I7 | Coordinator | An SSE stream's liveness is owned by the framing layer (`internal_http.py:203-206`). | **No such mechanism exists.** `iter_sse_events` has no timer. The only liveness is `_sse_frames`'s 15 s `wait_for`, and it is broken (G1). A stream open has no bound on waiting for response headers (G9). |
| I8 | Lane | A 409 on a bot action is a pre-execution rejection, so a same-key re-POST is the retry (`PDS/app/routers/broker_v2_panel.py:172-184`; `Frontend/.../broker-v2-panel.service.ts:280-355`). | **No through the coordinator.** A 4xx settles `PROVIDER_REFUSED`, and the same key is then refused as `clerk_routing_attempt_conflict`. See **C2-G3**. |
| I9 | Operator | "Outcome unknown" means the command may have run. | **Over-reported.** Lane capacity 503s and pre-handler identity refusals provably did not run, yet they settle `OUTCOME_UNKNOWN`. See **C2-G4**. |
| I10 | #2204 F1 | A slow read or a stream never holds the slot a command needs. | **Yes.** The pools are disjoint (`lane_runtime.py:685-722`). Only other commands or POST-reads can hold command slots. See **C2-G10** for how those slots can accumulate. |

## (c) Named suspected gaps

Severity uses the map's scale. "Paper" says whether the prototype needs the Alpaca paper account.

### C2-G1: a quiet routed stream is cut after 15 s (PROVEN, P2, no paper)

**What happens.**

- `_sse_frames` wraps `iterator.__anext__()` in `asyncio.wait_for(..., 15)` (`broker_clerks.py:339-341`).
- On timeout, `wait_for` cancels the pending `__anext__`. That throws `CancelledError` into the async-generator chain, which finishes the generator. On HTTP, `_closing_event_iterator`'s `finally` also closes the lane connection (`delivery.py:368-370`).
- The loop emits one `: keepalive`. The next `__anext__` then raises `StopAsyncIteration`, so the function returns (`broker_clerks.py:345-346`) and the public stream ends cleanly.
- The lane's own 15 s keepalives (`broker_v2_panel.py:522`) are comments. The framing layer drops them, even after injection (`internal_http.py:282`, `:294`), so they never reset the coordinator's timer.

**Consequence.** Every `bot_live_stream` and `gallery_stream` is torn down within about 15 s of lane quiet (outside market hours, stopped bots, a quiet gallery). The browser then:

- sees an EventSource error;
- flips its status to `error`, starts the fallback poll and reconnects after 500 ms (`authenticated-sse-connection.ts:64-69`; `bot-panel-live-store.service.ts:193`).

No data is lost, because `SurfaceHub.subscribe` re-primes the latest snapshot on reconnect (`PDS/app/services/surface_hub.py:227-228`). The lane's stream pool and hub retain/release churn roughly every 15 s for each open panel.

**Evidence.** Probe 1 below: the second and third events never reach the public stream.

**Prototype.** Run a routed stream through the real `HttpLaneDelivery` against a local uvicorn lane that emits one event, stays quiet for longer than the interval, then emits again. Assert that the public stream stays open and delivers both events. The fake stands in for a lane whose provider publishes less often than every 15 s.

### C2-G2: a key already dispatched can be dispatched again (PROVEN, **P1, filed [#2319](https://github.com/tim1016/learn-ai/issues/2319)**; P0 if any effectful operation lacks lane-side key dedup; paper for the P0 leg)

**The gap.** Hypothesis: a second request with the same idempotency key, arriving (a) while the first is awaiting the lane, or (b) after the coordinator lost the first's outcome, is forwarded to the lane a second time. Case (b) happens when the process dies or the task is cancelled between `mark_routing_dispatched` and settlement. `CancelledError` is not an `Exception`, so none of the `except` clauses at `routing.py:475-529` settle the attempt.

**Why the gate misses it.**

- `open_routing_attempt` returns the existing row. Its `state` is still `not_dispatched`.
- `routing.py:453` lets it through.
- `mark_routing_dispatched` is idempotent (`store.py:1178-1183`, `COALESCE`).
- The ADR says `outcome_unknown` means "never auto-resubmit" and "dispatch is one-way" (ADR 0062 addendum item 7). The in-flight state is the one the gate does not cover.

**Where duplicate effect is stopped, if at all.** Whether a second *effect* happens depends only on the lane deduplicating by a key the payload happens to carry (I5).

- Bot panel actions carry the key.
- Other `DURABLE_KEY` operations (`PDS/app/broker/alpaca/clerk/fleet_adapter.py`, `_DURABLE` rows) must each be checked.
- `ONE_SHOT` operations get a fresh `oneshot-…` key for each request (`routing.py:443-446`), so they are outside this gap.

**Evidence.** Probe 2 below: after `mark_routing_dispatched`, reopening the key returns `state=not_dispatched` with `dispatched_at_ms` set.

**Prototype.** Two concurrent same-key POSTs through `LaneRouter.deliver_command`, using a fake lane whose handler blocks on an event. Count the dispatches the lane receives. Second variant: cancel the first task after dispatch, then retry the key. For the P0 leg, repeat against a paper clerk with each `DURABLE_KEY` operation whose body lacks `idempotency_key`.

### C2-G3: the frontend's 409 retry is always refused by the coordinator (PROVEN by reading, P2, no paper)

- `runBotAction` retries a 409 with the *same* target, and therefore the same key (`Frontend/src/app/components/broker/v2-panel/lib/broker-v2-panel.service.ts:329-355`).
- The first 409 settles `PROVIDER_REFUSED` (`routing.py:554-565`).
- The retry hits `ClerkRoutingAttemptConflict` ("already settled as provider_refused … reconcile") (`routing.py:453-468`).
- The lane's documented same-key re-POST recovery for `StaleRevisionError`, `ExecutionAuthorityRevivedError` and `DryRunAuthorityLeaseLostError` (`broker_v2_panel.py:172-184`) cannot happen through the fleet.

**Prototype.** A lane fake returns 409, then 200. Drive `deliver_command` twice with the same key and assert that the second call is refused.

### C2-G4: provably-unapplied refusals are reported as "may have been applied" (PROVEN by reading, P2, no paper)

- A lane capacity 503 is sent before the handler (`lane_runtime.py:690-696`). It settles `OUTCOME_UNKNOWN` (`routing.py:530-553`).
- A pre-handler stale-pin refusal (`agent_identity.py:349-356`) carries the *served* identity echo. That becomes `DeliveryIdentityMismatch` and then `OUTCOME_UNKNOWN` (`routing.py:475-493`).

In both cases the key is burned and the operator is told to reconcile a command the clerk never saw. This is conservative, never unsafe.

**Prototype.** A lane fake returns the capacity 503 body. Assert the settled state and check whether the retry path is usable.

### C2-G5: the HTTP stream open leaks its client on connect failure (PROVEN by reading, P3, no paper)

`client.send(...)` at `delivery.py:316-327` sits outside any `try`. A `ConnectError`, a connect timeout or a pool timeout leaves the `AsyncClient` unclosed.

### C2-G6: a local-transport stream hangs on a mid-stream app exception (P3, combined posture only, no paper)

**Hypothesis.** If the lane app raises after `http.response.start`, no body message with `more_body=False` is ever sent, so `chunks()` waits on `queue.get()` forever (`routing.py:938-949`). G1 then turns that into a *clean* end after 15 s.

HTTP diverges here: uvicorn aborts the chunked body, httpx raises, and the public response aborts. The browser sees an error on HTTP and a normal end on local.

**Prototype.** Use `build_local_delivery` with an app whose SSE generator raises after one event.

### C2-G7: the local command path has no timeout (P3, combined posture only, no paper)

`LocalLaneDelivery.deliver` has no bound (`delivery.py:389-398`, `routing.py:906`). A hung handler leaves the attempt unsettled, and the drain gate is blocked (`service.py:799-808`), until the handler returns. HTTP settles `OUTCOME_UNKNOWN` after 10 s.

### C2-G8: a single-bot Stop or Flatten can outlive its 10 s outer bound (P1 suspected, paper for the Flatten leg)

**Hypothesis.** `bot_panel_action` keeps the 10 s default (`fleet_adapter.py:46`; the operation declares no `read_timeout_s`). But `internal_http.py:33-41` documents that one Stop can take "well over 5 s": a Clerk STOP, up to 5 s waiting for cancellation, then a fresh custody proof at the broker.

Under load, a Stop or `flatten_stop` taking more than 10 s would return `ClerkRoutingOutcomeUnknown` (503) while the lane completes it. The operator re-issues it through a new interaction, which mints a new key. The question is whether the lane's action fence (revision and concurrency token) refuses a second `flatten_stop` on a fresh key, or sells again.

**Prototype.** Use a lane fake whose Stop sleeps 11 s and check the coordinator's settlement. Then, on paper during RTH, run `flatten_stop` twice with distinct keys against a bot holding a position, and assert one sell.

### C2-G9: a stream open has no bound on waiting for response headers (P2, no paper)

**Hypothesis.** `read_timeout_s=None` (`delivery.py:315`) also lifts the bound on waiting for the *response headers*. The lane handler awaits `_live_snapshot` and hub start before it returns its `StreamingResponse` (`broker_v2_panel.py:488-500`), and it keeps its request slot until response start (`lane_runtime.py:714-716`).

So a lane that hangs before the first byte:

- holds the coordinator request open indefinitely;
- holds one lane request slot per open.

Sixteen such opens starve lane reads (not commands).

**Prototype.** A local uvicorn lane whose stream handler blocks before returning. Open 17 routed streams and assert that an ordinary read is refused with `fleet_lane_capacity_exhausted`.

### C2-G10: coordinator timeouts do not free lane command slots (P2, no paper)

**Hypothesis.** When the coordinator's 10 s read timeout fires, it closes the socket and settles `OUTCOME_UNKNOWN`. The lane's non-streaming handler keeps running and keeps its command lease until it returns (`lane_runtime.py:740-744`).

Operator retries under fresh keys accumulate slots. Past 16 in flight plus a queue of 64 waiting 5 s, the next Stop gets a 503, which is reported as `OUTCOME_UNKNOWN` (G4).

This answers #2204 F1 as follows: **streams cannot starve commands; only a pile-up of slow commands or POST-reads can.**

**Prototype.** A lane fake whose command handler sleeps. Fire 17 or more commands with a short coordinator bound, then assert on the fate of a Stop.

### C2-G11: a clean lane EOF or `end` event can freeze an open panel with no error (P1 suspected, no paper; cross-seam A×C)

**Hypothesis.**

- `SurfaceHub.stop` closes its watchers with `None` (`surface_hub.py:310-315`, `:386-389`).
- `stop_all` runs on lane shutdown (`PDS/app/services/broker_v2_panel/live_projection.py:119-131`).
- The lane then emits `event: end` (`broker_v2_panel.py:524-526`), and the coordinator forwards it as-is.
- The browser treats `end` as terminal. It closes the connection with status `closed`, which also stops the fallback poll (`versioned-snapshot-stream.ts:60-62`; `bot-panel-live-store.service.ts:192`).

If a lane restart (every Apply restarts the agent) reaches this path while panels are open, those panels would show their last snapshot as current, with no error and no reconnect.

It is uncertain whether uvicorn's graceful shutdown reaches the lifespan `stop_all` while SSE connections are still open. Separately, a clean mid-event EOF drops the partial event silently (`internal_http.py:320-321`) (P3).

**Prototype.** Route a stream through `HttpLaneDelivery` from a lane process that is shut down gracefully. Record whether the public stream carries `end`, a clean EOF, or an abort, and what the frontend store's status becomes.

### Documentation drift (P3)

- ADR 0062 §5 says the agent validates "capability" (and, by implication, the durable idempotency identity) before any mutation. Neither reaches the lane (`delivery.py:117-127`, `broker_clerks.py:389-393`).
- `internal_http.py:203-206` says the framing layer owns stream liveness. It does not (I7).

## (d) Defects proven by reading

| ID | Severity | Filed |
|---|---|---|
| C2-G2: a same-key in-flight or lost-outcome attempt redispatches | P1 (gate fails open) | [#2319](https://github.com/tim1016/learn-ai/issues/2319) |
| C2-G1: the keepalive timeout ends routed streams | P2 | listed only |
| C2-G3: the frontend 409 same-key retry is refused | P2 | listed only |
| C2-G4: a pre-handler refusal settles `outcome_unknown` | P2 | listed only |
| C2-G5: the stream-open client leaks | P3 | listed only |

## Evidence (throwaway probes, not committed)

- **Probe 1 (G1).** Ran `_sse_frames(iter_sse_events(chunks))` with `_KEEPALIVE_INTERVAL_S=0.2`. The lane yields event `a`, sleeps 0.6 s, then yields `b` and `c`. Output: `[b'event: a…data: first\n\n', b': keepalive\n\n']`, after which the stream ended. `b` and `c` were never delivered.
- **Probe 2 (G2).** A pytest using the `fleet_service`, `provision_lane` and `bind_lane` fixtures ran `_attempt(key)`, then `mark_routing_dispatched`, then `_attempt(key)` again. The second call returned `state=not_dispatched` with `dispatched_at_ms=1789000000000`, which passes `routing.py:453`.
