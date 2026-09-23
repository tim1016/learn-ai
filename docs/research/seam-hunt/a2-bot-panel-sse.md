# A2 — Bot panel SSE → v2 panel: does the panel ever show false live state?

Research ticket: tim1016/learn-ai#2290 (map #2276, seam A2). Static read at
`a14f1df1` (master, 2026-09-23). Every `file:line` below is at that SHA.
No code was run against live services. A local Pydantic field walk of
`BotPanelLiveSnapshot` was the only thing executed.

## Answer

**Yes, conditionally (P1, suspected, needs a prototype).** The payload is sound.
The stream's liveness is not. The panel's live view is a latest-wins document.
The producer publishes it **only when its semantic fingerprint changes**. Both
hops of the transport send `: keepalive` comments during any silence, and
`EventSource` never exposes comments to JavaScript. The Angular store treats
transport `open` as healthy and has no frame-age watchdog. So nothing on the
client can tell "nothing changed" from "the producer stopped". If the lane's
producer stops publishing while the socket stays open, the panel keeps showing
the last snapshot. That includes `market_pulse.feed_state = LIVE`, the
"Running" health card and the presented actions. It shows no error and no
stale marker, for as long as the stall lasts. Causes include a blocked event
loop, a starved `to_thread` pool, a hung assembly, or a silent coordinator→lane
TCP connection.

**Correction to the ticket premise.** The v2 panel stream is **not**
hand-mirrored in `Frontend/src/app/api/broker-models.ts`. Its TS type is
`components['schemas']['BotPanelLiveSnapshot']`
(`Frontend/src/app/components/broker/v2-panel/lib/broker-v2-panel.types.ts:184`).
That type is generated from the OpenAPI schema of the REST twin
`GET …/live-snapshot`, which has `response_model=BotPanelLiveSnapshot`
(`PythonDataService/app/routers/broker_v2_panel.py:458-475`). The SSE route
serialises the same model instance
(`broker_v2_panel.py:531-532`). Payload fidelity is therefore held by the
generated-types gate (B2), not by hand. The only link is a convention: the SSE
route has no response model. See G7.

No P0/P1 defect is **proven** by reading alone, so no `bug` issue was filed.
Two P2s and one P3 are proven. They are listed in section (e).

## (a) Trace

### Producer (lane / data plane)

1. **Hub per (broker, account, sid, resolution).**
   `get_or_start_live_projection_hub` creates one `SurfaceHub` per key. Its
   assembler builds `BotPanelLiveSnapshot(stream_epoch="", surface_version=0,
   panel, live_chart)` from `panel_data_source.get_live_snapshot_parts`. It
   refreshes every **5 s**
   (`PythonDataService/app/services/broker_v2_panel/live_projection.py:29-66`,
   cadence at `:56`). Panel and chart come from one fill cut, observed at one
   `now_ms_utc()`
   (`app/services/broker_v2_panel/panel_chart_data_source.py:159-186`).
2. **Versioning.** `SurfaceHub._assemble_and_store` fingerprints the snapshot.
   It bumps `surface_version` only when the fingerprint changes, stamps
   `stream_epoch`/`surface_version`, stores `_latest`, and **publishes to
   watchers only on a semantic change**
   (`app/services/surface_hub.py:268-295`). The fingerprint excludes
   transport-only paths (`surface_hub.py:23-36`) and every key named `age_ms`,
   `age_ms_at_generation` or `now_ms` (`:37`, `:85-96`). The one exception is
   `panel.market_pulse.age_ms`, quantised to 5 s and kept (`:76-77`, `:93`).
   So a snapshot is re-published about every 5 s only while a latest bar
   exists and is ageing. When there are no bars (market closed, bot stopped,
   feed missing), frames stop entirely.
3. **Epoch.** `stream_epoch = "<process uuid>:<uuid>"` (`surface_hub.py:22`,
   `:265-266`). It is regenerated when a stopped hub restarts (`:176-180`), and
   a process restart always changes it.
4. **Failure.** When an assembly raises, the hub sets `_last_refresh_failed`
   and pushes a `SurfaceHubRefreshFailure` to watchers
   (`surface_hub.py:273-277`, `:303-308`). `snapshot()` then raises
   `SnapshotUnavailableError` until a later success (`:193-200`).
5. **Watchers.** `subscribe()` uses a bounded queue of size 1 and primes it
   with `_latest`. On a stopped hub it primes `None` instead
   (`surface_hub.py:216-230`). A slow consumer loses intermediate snapshots,
   which is correct for complete documents (`:297-301`).
6. **Idle retirement.** A hub with 0 watchers is removed 30 s after the last
   release (`live_projection.py:16`, `:83-115`). Every `get_or_start` resets
   the timer (`:59-65`).

### Router (`PythonDataService/app/routers/broker_v2_panel.py`)

- `GET …/live-snapshot` returns `hub.snapshot()`, the **stored** `_latest`,
  with no refresh. It returns 503 while the last refresh failed or before the
  first success (`:430-475`).
- `GET …/live-stream?resolution=&cursor=` works like this (`:478-547`):
  - Validates scope and takes `current` (`:495`). A failed read is an HTTP
    error before the stream opens.
  - If a `cursor` is given and its epoch ≠ the current epoch → `event: reset`
    (`:503-517`).
  - Loops on the queue with a 15 s timeout → `: keepalive` (`:518-523`).
  - `None` → `event: end` and return (`:524-526`).
  - Failure → `event: error {"error": …}` and return (`:527-530`).
  - Snapshot → `id: <epoch>:<version>` / `event: snapshot` /
    `data: model_dump_json()` (`:531-532`).
- `POST …/actions`: after `ds.run_action` commits, the router calls
  `schedule_live_projection_refresh`, a fire-and-forget task
  (`:553-563`; `live_projection.py:134-197`).

### Fleet hop (coordinator → lane), when the browser addresses a clerk

- The operation is `bot_live_stream`, `stream=OperationStream.SSE`, capability
  `BOT_PANEL_READ` (`app/broker/alpaca/clerk/fleet_adapter.py:480-487`). Reads
  carry no binding generation in the URL
  (`Frontend/src/app/fleet/operation-url.ts:107-117`).
- `HttpLaneDelivery.stream` opens the lane stream with **`read_timeout_s=None`**
  (`app/broker/fleet/delivery.py:313-356`, `:315`). It verifies the identity
  echo and validates `x-fleet-*` provenance on **every event**
  (`delivery.py:219-259`).
- `iter_sse_events` parses WHATWG framing. It **drops `:` comments**
  (`app/broker/fleet/internal_http.py:294`), keeps the `id` buffer, and caps
  one event at `DEFAULT_MAX_EVENT_BYTES = 1_000_000` (`:52`, `:241-312`). It
  has no idle or liveness timer. The docstring at `internal_http.py:205`
  claims "the framing layer owns stream liveness". Nothing in it does.
- `_sse_frames` re-emits each verified event with its event name, identity
  fields and `id`. **It emits its own `: keepalive` after every 15 s of
  upstream silence** (`app/routers/broker_clerks.py:327-352`, used at `:412`).

### Consumer (Angular)

- `openAuthenticatedSseConnection` wraps a native `EventSource`
  (`Frontend/src/app/services/authenticated-sse-connection.ts:21-84`):
  - Any `error` event, whether the server's named `event: error` or a
    transport error, closes the source and reconnects with backoff from 500 ms
    to 5 s (`:51-71`).
  - `open` resets the backoff and reports `open` (`:39-42`).
- `openVersionedSnapshotStream` listens for `snapshot` plus the control events
  `reset` and `end` (`Frontend/src/app/services/versioned-snapshot-stream.ts:32-66`).
  - **`end` closes the connection for good** (`:60-62`). That reports status
    `closed` with no reconnect.
  - `adoptVersionedSnapshot`: a different epoch always replaces; the same
    epoch replaces only on a higher version (`:24-30`).
- `BotPanelLiveStore`
  (`Frontend/src/app/components/broker/v2-panel/lib/bot-panel-live-store.service.ts`):
  - REST bootstrap, then the stream, with the cursor
    `"<epoch>:<version>"` taken from the held snapshot (`:59-88`, `:163-176`).
  - `isLiveSnapshot` is a **shallow** guard: epoch, version, and
    `panel`/`live_chart` being objects (`:27-39`).
  - `reset` → REST `refresh()` (`:186-188`).
  - `error` status → a 5 s REST fallback poll. `open` or `closed` → the
    fallback stops (`:189-194`, `:217-226`).
  - `adopt` clears `error` (`:199-207`).
- `BotPanelShellComponent`:
  - `panel` is `liveStore.snapshot().panel` (`panel-shell/bot-panel-shell.component.ts:311`).
  - `liveStreamStatus` is used **only** for the chart-loading spinner
    (`:316`; `bot-panel-shell.component.html:73`).
  - `loadError` shows `liveStore.error()` as an alert *above* the still-rendered
    last panel (`component.ts:398-404`; `html:1-3`, `:11`).
  - After every action it `await`s `liveStore.refresh()` (`component.ts:541`,
    `:564`).
- Rendered live state includes the market-feed headline and attention tone
  (`trader-lens/trader-metrics.component.html:56-61`,
  `trader-metrics.component.ts:28-32`), health and run timing, the clerk
  card's `outstanding_intents`, working orders, and presented actions.

### Invariants each side assumes

| # | Consumer assumes | Producer / transport actually guarantees | Holds? |
|---|---|---|---|
| I1 | An open stream means a live producer | Keepalives come from the router loop (`broker_v2_panel.py:519-523`) and the coordinator (`broker_clerks.py:338-344`), **independent of the producer task** | **No** → G1 |
| I2 | A frame arrives whenever state changes | Only on semantic change. Liveness fields (`feed_state`, `decision_stale`) flip only when the producer runs (`market_pulse.py:65-67`, `:153-197`) | Only while the producer runs |
| I3 | Same-epoch versions are monotonic across REST and SSE | Both read the same hub: REST `_latest` and SSE watcher (`surface_hub.py:193-230`) | Yes |
| I4 | A new epoch is newer | Epoch is an unordered uuid (`surface_hub.py:265-266`). Nothing orders two epochs | **No** → G4 |
| I5 | `refresh()` after a committed action returns post-action state | REST returns the stored `_latest`. The post-action prompt is fire-and-forget and coalesces onto an in-flight assembly (`surface_hub.py:205-214`) | **No** → G2 (proven P2) |
| I6 | The server's `error` frame is followed by recovery | The server closes. The client reconnects; the reconnect 503s until a success; the fallback poll shows the error | Yes |
| I7 | `end` means the page is going away | `end` is sent only when the hub is stopped or subscribed after stop (`surface_hub.py:224-226`, `:310-315`) | Unclear → G3 |
| I8 | Payload shape equals the generated TS type | Same Pydantic model for REST and SSE. No aliases or serializers in the tree | Yes, except version skew → G6 |

## (b) Payload fidelity (Python `BotPanelLiveSnapshot` → TS `components['schemas']['BotPanelLiveSnapshot']`)

Method: walked every field of the model tree with Pydantic (374 fields),
checked the tree's schema modules for `alias`, `field_serializer` and
`computed_field` (none in `app/schemas/broker_v2_panel.py` or the modules it
imports), and compared against `Frontend/src/app/api/broker.types.ts:9244-9251`.

| Concern | Python | TS (generated) | Verdict |
|---|---|---|---|
| Top level | `stream_epoch: str`, `surface_version: int`, `panel`, `live_chart` | same; `surface_version: number` | match |
| null vs absent | Optionals are `X \| None` **required** (always emitted as `null`), e.g. `MarketPulseView.age_ms`, `latest_bar_at_ms`, `ClerkCard.hold_since_ms` | `X \| null`, required | match. `model_dump_json()` defaults `exclude_none=False`, same as the FastAPI REST twin |
| Defaulted fields | 54 fields with defaults (e.g. `RecentFillView.slippage_*`, `authority_kind`, seal contract literals) | output-mode schema marks them required | match on current code; see G6 for older clerks |
| Enums | `feed_state` `LIVE/IDLE/STALE/MISSING`; `session`, `market_state`; `FeedContinuityView.state`; `ClerkCard.hold_reason`; `StationView.state`; `ChannelHealthView.state` | generated unions. Also hand-kept `ChannelState`/`StationState` (`broker-v2-panel.types.ts:13-19`) | match today. The hand-kept unions feed `Record<…, string>` icon maps (`clerk-card.component.ts:49`, `transaction-rail.component.ts:52`), so a new member renders no icon (P3, G8) |
| Timestamps | every `*_ms` is `int`, for example `updated_at_ms`, `observed_at_ms`, `as_of_ms`, `trading_date_open_ms` | `number` | match (int64 ms UTC) |
| int vs float | `quantity`, `price`, `exposure: dict[str, float]`, `realized_pnl_today`, `open_pnl` are float; `ChartBar` prices are **strings** | `number` / `string` | match. A NaN float serialises as `null` (Pydantic `ser_json_inf_nan` default) into a TS `number` (P3, theoretical) |
| Required-but-new | `BotPanelView.feed_continuity` is required in Python | required in TS, but the FE falls back when it is absent (`broker-v2-panel.types.ts:89-111`) | handled; the pattern is not general (G6) |
| Route contract | SSE route has no `response_model`; the OpenAPI entry for `…/live-stream` has no body schema | type comes from the REST twin | convention only (G7) |

## (c) Stream behaviour

- **Snapshot-then-delta:** none. Every frame is a complete document. The first
  frame comes from the queue primed with `_latest` (`surface_hub.py:227-228`),
  after a REST bootstrap on the client. Cleared.
- **Reconnect / replay:** no `Last-Event-ID` replay. The browser's native
  reconnect is disabled because the wrapper closes and reopens. A `cursor`
  query carries `<epoch>:<version>`. A different epoch → `reset` → REST
  refresh. The same epoch gets the primed latest. Latest-wins makes replay
  unnecessary. Cleared, except G3 (`end`) and G4 (epoch ordering).
- **Ordering against a concurrent REST read:** within an epoch, version guards
  hold (I3). Across epochs, any different epoch wins (G4). After a committed
  action, REST can return pre-action state (G2).
- **Error frames:** `event: error` → the client shows the message, reconnects,
  and gets 503 while the hub is failed. The fallback poll keeps the error
  visible and the last panel stays under the alert. Visible, so not false
  state. Cleared.
- **Auth expiry mid-stream:** not applicable. The browser sends a static
  `control_intent` marker (`services/broker-sse.ts:148-154`,
  `security/data-plane-control-intent.interceptor.ts:4-6`). The dev proxy
  injects the process-static data-plane secret. Nothing expires mid-stream.
  If the secret rotates, reconnects fail loudly through the fallback-poll
  error. Cleared.
- **Fleet provenance:** a lane re-registering mid-stream is refused per event
  (`delivery.py:219-259`). The public stream aborts and the client reconnects
  to the new pin. Cleared.
- **Liveness:** absent at every hop. See G1.

## (d) Named suspected gaps

**G1 — Keepalive-masked producer stall shows frozen "live" state. P1.**
*Hypothesis:* the producer stops publishing while the socket stays open. Then
the panel keeps rendering the last snapshot indefinitely, with `feed_state
LIVE`, a non-attention market-feed row, Running health and the presented
actions, while `status()` stays `open`, `error()` stays null and the fallback
poll stays off. Possible causes: a hung assembly with no overall timeout, a
starved default `to_thread` pool (for example D3's SDK threads outliving
their timeout), a blocked lane event loop behind the coordinator, or a silent
coordinator→lane TCP connection with `read_timeout_s=None`.
*Why believed:* keepalives are produced independently of the producer
(`broker_v2_panel.py:519-523`; `broker_clerks.py:338-344`). Comments are
invisible to `EventSource` and dropped by the coordinator parser
(`internal_http.py:294`). The store changes status only on
`open`/`error`/`closed` (`bot-panel-live-store.service.ts:189-194`).
Publication is semantic-change-only (`surface_hub.py:293-294`). The assembly
has no timeout (`live_projection.py:39-51`; `surface_hub.py:268-270`).
*Prototype:* build an in-process `SurfaceHub` with an assembler that succeeds
once, then awaits a never-set `Event`. Drive the real router generator through
`httpx.AsyncClient(ASGITransport)`. Assert that only `: keepalive` arrives for
more than 60 s. Repeat through `_sse_frames` over a fake upstream that goes
silent. On the FE, a Vitest spec with a fake `EventSource` that opens, sends
one snapshot, then nothing: assert that the store exposes no stale signal after
N×5 s. For contrast, the bots-list page has one: `FLEET_STALE_AFTER_MS`,
`bots-list-page.component.ts:208-217`.

**G2 — Post-action refresh returns pre-action state. P2, proven (see e1).**
*Hypothesis:* a Pause that commits while a producer assembly is in flight
leaves the panel showing Running with Pause presented for up to about 5 s plus
one assembly, after a success toast.
*Prototype:* a `SurfaceHub` whose assembler blocks on a gate. Start `refresh()`,
commit a fake action, call `schedule_live_projection_refresh`, then release the
gate. Assert that the prompt returns the pre-commit snapshot and that REST
`snapshot()` returns it too.

**G3 — `event: end` freezes the panel with no reconnect and no fallback. P2 (latent).**
*Hypothesis:* any path that stops a watched hub, or a subscribe racing a
`stop()`, sends `end`. The client then goes `closed` for good
(`versioned-snapshot-stream.ts:60-62`, fallback stopped at
`bot-panel-live-store.service.ts:192`). The panel stays on its last snapshot
with no error. Today, reaching it needs `stop()` on a hub with watchers. That
happens only at shutdown, where uvicorn 0.24 cancels request tasks before
lifespan shutdown (`--timeout-graceful-shutdown 10`,
`PythonDataService/Dockerfile:72`). Through the coordinator, a lane's `end` is
forwarded verbatim.
*Prototype:* subscribe to a hub, call `hub.stop()`, and assert the router
yields `end`. In Vitest, assert the store never re-polls after `end`.

**G4 — Cross-epoch regression: an older-epoch REST response overwrites a newer-epoch stream frame. P2.**
*Hypothesis:* during a lane restart, a `refresh()` GET served by the old
process lands after the new process's primed SSE frame. `adoptVersionedSnapshot`
accepts it because the epoch differs (`versioned-snapshot-stream.ts:28`). The
panel then shows the old process's state until the new hub's next semantic
change. Off hours, with no bars, that may be hours.
*Prototype:* in Vitest, feed the store an SSE frame at epoch B, then resolve a
pending `getLiveSnapshot` with epoch A. Assert that A is displayed.

**G5 — The coordinator's 1 MB event cap can reject a large live snapshot. P2.**
*Hypothesis:* late in an RTH session, a 5 s snapshot (about 4,680 bars × about
130 B plus the panel) nears or exceeds `DEFAULT_MAX_EVENT_BYTES`
(`internal_http.py:52`). The coordinator raises `FleetStreamError`, the public
stream aborts, and the client loops `connecting → open → error`. Each reconnect
is primed with the same oversize frame. The panel updates only through
intermittent fallback polls, with no error shown.
*Prototype:* measure `len(snapshot.model_dump_json())` for a synthetic full
session plus a realistic panel. Push one through `iter_sse_events` and check it
against the cap.

**G6 — Version skew: an un-restarted clerk omits fields the FE type marks required. P2.**
*Hypothesis:* the frontend is watch-mode and deploys on `git pull`, while lane
clerks run the code they were started with. A newly required field is then
absent at runtime. `isLiveSnapshot` checks only the top level
(`bot-panel-live-store.service.ts:27-39`). A boolean or state read through the
missing field renders as falsy or blank, not as an error. `feed_continuity` is
the one field with an explicit fallback (`broker-v2-panel.types.ts:89-111`).
*Prototype:* a Vitest render of the shell with a snapshot missing a recently
added required field (e.g. `clerk.freeze_active`). Assert what the operator
sees.

**G7 — The SSE payload is tied to the generated type only by convention. P3.**
The `live-stream` route declares no body schema (`broker_v2_panel.py:478-487`).
If the handler ever emits a different model than the REST twin, no gate fires.
*Prototype:* a contract test asserting that the stream's `data` validates as
the REST `response_model`.

**G8 — Hand-kept `ChannelState`/`StationState` unions. P3.** A new Python
member yields an `undefined` icon (`broker-v2-panel.types.ts:13-19`). Fix
direction: derive them from `components['schemas']`.

**G9 — First-assembly hang blocks the bootstrap forever. P3 (visible).**
`hub.start()` waits for `initial_cycle_done` (`surface_hub.py:190-191`), and
the REST call has no client timeout (`broker-v2-panel.service.ts:446-458`). The
page stays on "Loading bot control…". It is visible, so it is not false state.

## (e) Defects proven by reading

- **e1 (G2), P2.** A post-action refresh can return pre-action state. Steps:
  1. `_run_action` schedules a fire-and-forget refresh
     (`broker_v2_panel.py:562`; `live_projection.py:145-150`).
  2. `refresh()` coalesces onto any assembly task already in flight
     (`surface_hub.py:205-214`), which may predate the commit.
  3. The shell's `await liveStore.refresh()` (`bot-panel-shell.component.ts:541`)
     reads the stored `_latest` without refreshing (`broker_v2_panel.py:444`).

  It self-corrects on the next 5 s producer cycle, so it is P2 and gets no bug
  issue. It also weakens the stale-token retry in `refreshFlattenQuote`
  (`bot-panel-shell.component.ts:673-678`): the "refreshed" token can still be
  the stale one.
- **e2, P3.** On a non-session day, the closed-market fallback of `live_window`
  uses **UTC** midnight (`chart_projection_service.py:212-216`), while its
  docstring promises "the NY calendar day" (`:200-202`).
- **e3, P3.** `internal_http.py:205` says the framing layer owns stream
  liveness. It has no timer (`:241-312`). The comment is wrong, and it is part
  of why G1 is unguarded.

**How D-seam bugs surface here.** The panel projects the Clerk's durable belief
faithfully. It has no independent broker read. So #2306 (a websocket-cancelled
ENTER stuck `in_progress`) shows as a persistent `outstanding_intents ≥ 1`
(`panel_projection_service.py:248`; `trader-metrics.component.html:54`) and
drives presented-action gating (`presented_actions.py:81`). The same holds for
#2305's short position. That is false state, but it originates in D4 (charted),
not in this seam.

## Seam status

**Charted hazard.** Payload fidelity is cleared (static). The stream's liveness
is not guarded (G1, P1 suspected), and ordering has P2 gaps (G2 proven, G3–G6).
