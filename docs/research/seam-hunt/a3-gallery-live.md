# A3: Gallery live store. Does the fleet gallery ever show false live state?

Research ticket: #2291 (map #2276). Static trace at `a14f1df1` (master, 2026-09-23).
All citations are `file:line` at that SHA. Paths are shortened:
`PDS/` = `PythonDataService/`, `FE/` = `Frontend/src/app/`.

## Answer

**Yes.** The payload types are sound: the snapshot is generated from OpenAPI, and
the hand-declared update is pinned by a type-level test. The false state comes
from **stream behaviour**, in two proven ways.

1. **Proven P1.** On an ordinary trading day, once the account's charted symbols
   hold about 6,900 five-second bars in total, the gallery's SSE `snapshot`
   frame is larger than the fleet coordinator's 1 MB per-event cap. Two
   symbols reach this by about 14:20 ET, and three by about 12:40 ET. The
   coordinator kills the stream. After that the wall stays frozen at the REST
   bootstrap it took on page load. The footer keeps flipping between `Live`
   and `Delayed`, and the 5 s fallback poll never runs.
2. **Proven P1.** The wall's `●Live` reports that the SSE transport is open. It
   says nothing about market data. The hub never reads the aggregator's feed
   status. The stream sends an `update` frame every second whether or not new
   bars arrived. `last_bar_at_ms` and `as_of_ms` go over the wire, but nothing
   renders them. While the IBKR feed is dead, the wall shows `●Live` over
   frozen candles.

Per-lane partial failure (ADR 0062 §9) holds at page level. A down lane shows
the explicit `Gallery feed unavailable` or `Live feed delayed` banner, never
omission. At bot level there is one silent omission (fill markers, P3).

## (a) Trace

### Server: lane process

| Step | What happens | Cite |
|---|---|---|
| Hub cache | One `GalleryHub` per `(broker, account_id)`, built on the event loop (atomic get-or-create), `resolution="5s"`, IO cache TTL 800 ms. It is never evicted. | `PDS/app/routers/broker_v2_gallery.py:133-166`, `:67-75` |
| REST bootstrap | `GET …/gallery/snapshot` → `hub.build_snapshot()`. No error translation: `PanelUnavailableError` falls through to the catch-all 500. | `broker_v2_gallery.py:169-175`, `PDS/app/utils/error_handlers.py:38-45` |
| SSE open | `StreamingResponse(_gallery_event_source(...))`. The generator's **first** statement is `await hub.build_snapshot()`. | `broker_v2_gallery.py:259-271`, `:197-198` |
| Headers before body | Starlette 0.27 sends `http.response.start` before it iterates the body (pinned `requirements-lock.txt:33`). uvicorn 0.24 writes the headers to the socket straight away. So a 200 reaches the client before any snapshot exists. | `starlette/responses.py:254-262`, `uvicorn/protocols/http/httptools_impl.py:535` (host venv = pinned versions) |
| Reset | A cursor epoch that differs from the hub epoch → `event: reset` then `snapshot`. The epoch carries a per-process nonce. | `broker_v2_gallery.py:199-205`, `PDS/app/services/broker_v2_panel/gallery_hub.py:62-70`, `:242` |
| Poll loop | Every 1 s: `build_update(since_bar_ms, known_sids, since_marker_keys)`. All three cursors are local to the stream. | `broker_v2_gallery.py:207-239` |
| Emission | `changed = has_new_bars or bool(bots_delta) or bool(removed_sids)`. `bots_delta` is always the full roster, so an update goes out **every second while any bot is shown**. The keepalive fires only when the roster is empty. | `broker_v2_gallery.py:240-256`, `gallery_hub.py:677-685` |
| Publication order | Collection and `_version += 1` both run under `_publish_lock`, so versions are unique and ordered by coherent build, shared across every client of the account. No `await` inside has a timeout. | `gallery_hub.py:248`, `:587-599`, `:656-671` |
| Catalog | `get_catalog` raises `PanelUnavailableError` when the SQLite roster is missing or unprojectable. The hub does not catch it. | `PDS/app/services/broker_v2_panel/panel_data_source.py:281-297`, `gallery_hub.py:391-410` |
| Bars | `ensure_subscribed_5s` (+1m for session %) per shown symbol, then an append-only ring-buffer read `start_ms > since_ms`. The feed `status` / `last_error` on `_SymbolState` is **never read**. | `gallery_hub.py:283-296`, `:428-440`; `PDS/app/services/live_bar_aggregator.py:156-178`, `:189-201`, `:342-409` |
| 5 s buffer | 4,000 bars per symbol (≈5 h 33 m), seeded from today's persisted JSONL. | `live_bar_aggregator.py:58-62`, `:280-309` |
| Markers | Per-bot fan-out. One bot's failure → logged, and the bot is **omitted** from `markers`. | `broker_v2_gallery.py:78-102`, `gallery_hub.py:465-530` |
| Resume action | Per-bot fan-out. Failure → fails closed with an explicit `disabled_reason`. | `broker_v2_gallery.py:108-127`, `gallery_hub.py:298-347`, `:532-576` |

### Fleet delivery hop (coordinator)

| Step | What happens | Cite |
|---|---|---|
| URL | `operationUrl('gallery_snapshot' / 'gallery_stream')` → `/api/brokers/{b}/clerks/{clerk}/accounts/{acct}/gallery/…`. The browser's `bindingGeneration` / `routingEpoch` are **not** sent. | `FE/fleet/operation-url.ts:107-117`, `FE/fleet/clerk-scoped-url.ts:17-22`; catalog `PDS/app/broker/alpaca/clerk/fleet_adapter.py:519-534` |
| Stream open | `lane.stream_read` resolves the session, then `delivery.stream`. Open failures become `ClerkIdentityMismatch` / `ClerkUnreachable` (typed refusal → non-200). | `PDS/app/broker/fleet/routing.py:285-351`, `PDS/app/routers/broker_clerks.py:395-420` |
| HTTP posture | `HttpLaneDelivery.stream` (read timeout lifted) verifies the echo headers, then parses with `iter_sse_from_response` using the **default** `max_event_bytes`. | `PDS/app/broker/fleet/delivery.py:313-355`, `:357-370` |
| Combined posture | `_LocalAsgiHandler` parses with `iter_sse_events(chunks())`. Same default cap. | `routing.py:779-789`, `:846`, `:971-975` |
| The cap | `DEFAULT_MAX_EVENT_BYTES = 1_000_000`. An event larger than this raises `FleetStreamError`. | `PDS/app/broker/fleet/internal_http.py:52`, `:271-275`, `:309-313` |
| Per-event provenance | The lane stamps `x-fleet-*` fields into every frame. The coordinator validates each event and closes on a mismatch, so a rebind mid-stream closes the stream. | `PDS/app/broker/fleet/agent_identity.py:109-121`, `:212-250`, `:360-395`; `delivery.py:218-259` |
| Re-emit | `_sse_frames` re-serialises events. On 15 s idle it emits **its own** `: keepalive`, whatever the lane is doing. An iterator exception propagates, and the response is aborted after the 200 was already sent. | `broker_clerks.py:324-352`, `:411-420` |

### Client

| Step | What happens | Cite |
|---|---|---|
| Host | `BotGalleryPageComponent` provides one `GalleryLiveStore`. An effect restarts it whenever the directory lane changes. | `FE/components/broker/v2-panel/gallery/bot-gallery-page/bot-gallery-page.component.ts:59`, `:118-131` |
| Start | Awaits the REST bootstrap (failure swallowed on purpose), then opens the SSE. | `gallery-live-store.service.ts:149-187`, `:245-264` |
| Transport | Native `EventSource`. `open` → reset backoff to 500 ms, emit `'open'`. `error` → emit `'error'`, reconnect with backoff (max 5 s). No inactivity watchdog. | `FE/services/authenticated-sse-connection.ts:33-72` |
| Status | `'open'` → **`live`** + `stopFallback()`. `'error'` → `stale` (or `error` if no snapshot yet) + `startFallback()` (5 s REST poll). | `gallery-live-store.service.ts:319-352` |
| Ingest | Snapshot: full replace unless it has the same epoch and is not newer. Update: dropped unless `surface_version > current`. **The update's epoch is never checked** (the `id:` line is ignored). | `gallery-live-store.service.ts:196-243` |
| Render | `viewState`: `(connecting, 0 bots)` → loading. `error` → error. `0 bots` → **empty** ("No bots yet / This account has no bots set up"). Otherwise the dock, with `stale` → banner. Footer: `Live` / `Delayed` / `Feed error` / `Connecting…`. | `bot-gallery-page.component.ts:107-116`, `.html:10-41`; `bot-gallery-dock.component.ts:56-61` |
| Unread fields | `last_bar_at_ms`, `as_of_ms`, `phase`, `desired_state` are read nowhere in the gallery components (grep over `FE/components/broker/v2-panel/gallery/`, non-spec). | n/a |
| Actions | A quick action re-reads the authoritative panel before it runs, so stale tile state cannot by itself fire a wrong command. | `bot-gallery-page.component.ts:134-158` |

### Invariants each side assumes

| Assumer | Assumes | Guaranteed? |
|---|---|---|
| Store | Transport open ⇒ data is live | **No.** Open happens before the first frame is built, and a stream can be open and silent (G2, G3, G4). |
| Store | Any event the lane emits reaches the browser | **No.** The coordinator drops the whole stream for any event over 1 MB (G1). |
| Store | Versions within one epoch are totally ordered across REST and SSE | Yes. `_publish_lock` + post-collection version (`gallery_hub.py:587-599`). |
| Store | An update belongs to the adopted epoch | Unchecked. Holds today because of single-worker uvicorn (`compose.yaml:266`) and one hub per process (G5). |
| Router | `build_update` returns promptly | Unbounded. No timeouts under `_publish_lock` (G4). |
| Hub | Aggregator buffers reflect a live feed | **No.** An errored or stalled feed just stops appending, and the hub ignores `status` / `last_error` (G2). |
| Coordinator | Lane events are small (< 1 MB) | **No** for the gallery (G1). |

## (b) Payload fidelity

`GalleryLiveSnapshot`, `GalleryBotView`, `GallerySymbolBars`, `GalleryPrimaryAction`,
`ChartBar` and `ChartFillMarker` are **generated** OpenAPI aliases
(`FE/components/broker/v2-panel/gallery/lib/gallery.types.ts:21-38`,
`FE/components/broker/v2-panel/lib/broker-v2-panel.types.ts:170-178`), so CI
guards them. `GalleryLiveUpdate` is hand-declared
(`gallery.types.ts:49-56`) but pinned field-by-field *and* type-by-type by
`PDS/tests/schemas/test_broker_v2_gallery.py:144-176`. `GalleryResetEvent`
(`gallery.types.ts:67-70`) is pinned by a router test. The store never reads it.

| Field | Python (`PDS/app/schemas/broker_v2_gallery.py`, `broker_v2_panel.py`) | TS | Fidelity |
|---|---|---|---|
| `stream_epoch` | `str` (:90) | `string` | OK. Guard requires non-empty (`gallery-live-store.service.ts:38-39`). |
| `surface_version` | `int` (:91, :104) | `number` + `Number.isInteger` guard | OK |
| `as_of_ms` | `int` ms UTC (:92, :105) | `number` | OK type. **Never rendered** (G2). |
| `resolution` | `Literal["5s","1m"]` (:20) | generated union + runtime guard | A new Literal member makes the guard drop the whole snapshot silently (G10, P3). |
| `bots[]` / `bots_delta[]` | `GalleryBotView` / subclass `GalleryBotDelta` with no extra fields (:68-73) | `GalleryBotView` | OK |
| `sid, symbol, label` | `str` | `string` | OK. `label` defaults to `""` via `getattr` (`gallery_hub.py:377`). |
| `running`, `needs_attention` | `bool`, `getattr(..., False)` (`gallery_hub.py:378`, `:381`) | `boolean` | OK type. A missing attribute defaults silently to "not running". |
| `phase`, `desired_state` | open `str`, default `""` | `string` | No enum, so no enum drift. Unread by the client. |
| `realized_pnl_today`, `open_pnl`, `day_pnl`, `session_change_pct` | `float \| None` (required) | `number \| null` | OK. NaN/inf would serialize as `null` (Pydantic v2 default). |
| `fills_today` | `int \| None` | `number \| null` | OK |
| `last_bar_at_ms` | `int \| None = None` (:64) | `last_bar_at_ms?: number \| null` | OK type. **Never rendered** (G2). It is the latest bar **end** (`gallery_hub.py:129-135`). |
| `primary_action.disabled_reason` | `str \| None = None` | optional nullable | OK. Store treats absent = null. |
| `symbols[].bars` | `list[ChartBar]` default `[]` | optional in generated | OK. Store uses `?? []` (`:204`, `:222`). |
| `ChartBar.start_ms/end_ms` | `int` ms UTC | `number` | OK |
| `ChartBar.open..close` | `str` (Decimal text) | `string` | OK, exact decimal preserved |
| `ChartBar.volume` | `int` | `number` | OK |
| `ChartBar.source` | `Literal["ibkr","polygon","mixed"]` | generated union | OK. No runtime guard, so an unknown member would pass through. |
| `ChartFillMarker.*` | `filled_at_ms:int`, `side:Literal[buy,sell]`, `quantity/price: float`, `order_ref`, `event_key: str` | generated | OK. Merge is keyed on `event_key` on both sides. |
| `markers` (snapshot) | `dict[...]` default `{}` | optional | OK. Guard admits absent. |
| `markers_delta`, `removed_sids` (update) | default factories, always serialized | required | OK. `model_dump_json` emits defaults. |

**Result: no fidelity defect.** Every `*_ms` value is `int` → `number`. No
timestamp is typed `string`. Null and absent are handled at every read site.

## (c) Stream behaviour

- **Reconnect / replay.** There is no `Last-Event-ID` replay. The client
  reconnects with `?cursor=epoch:version` (`gallery-live-store.service.ts:364-369`).
  The server compares only the epoch. On a mismatch it sends `reset`, and in
  both cases it sends a **fresh full snapshot** (`broker_v2_gallery.py:197-205`).
  That is snapshot-then-delta on every connection. It is sound, but each
  reconnect rebuilds and ships the full ~0.6 MB-per-symbol snapshot (feeds G1
  and G3).
- **Ordering against concurrent REST.** Sound. Versions are assigned after
  collection under one lock. A REST snapshot with a higher version was built
  later, so it subsumes every update it causes the store to drop. One narrow
  exception is the ghost bot (G9).
- **Error frames.** There are none. Every server-side failure after headers is
  a mid-stream abort, which the client sees as a generic `error`.
- **Partial lane failure (ADR 0062 §9).** The page is keyed by
  `(broker, clerkId, accountId)`. A lane refusal before headers → `error`
  banner (no snapshot) or `stale` banner (snapshot held). That is explicit,
  not omission. Inside the lane: a failed per-bot resume read fails closed
  explicitly, but a failed per-bot fill read is **omitted** silently (G7).
- **Auth expiry.** Not applicable in the usual sense. The data-plane secret is
  static and the dev proxy injects it (`Frontend/proxy.conf.js:1-11`). The
  EventSource carries only `control_intent`
  (`FE/services/broker-sse.ts:148-154`). The secret is checked only when the
  connection opens. A rotated secret surfaces on the next reconnect as an
  error without `open` → `stale` + banner, which is explicit.

## (d) Named suspected gaps

Severity uses the map's scale. "Proven" means the whole chain is shown by reading.

### G1: A large gallery snapshot exceeds the coordinator's 1 MB event cap and freezes the wall behind a flickering `Live`. **P1, PROVEN**

*Hypothesis.* Once the shown symbols' 5 s buffers hold more than about 6,900
bars in total, every `snapshot` SSE event is over 1,000,000 bytes.
`iter_sse_events` raises `FleetStreamError` (`internal_http.py:271-275`) in
both postures (`delivery.py:357-370`, `routing.py:971-975`). `_sse_frames`
aborts after the 200 was sent (`broker_clerks.py:330-352`, `:411`). The
browser sees `open` → `live`, which also cancels the fallback poll
(`gallery-live-store.service.ts:332-335`) and resets the backoff to 500 ms
(`authenticated-sse-connection.ts:39-42`). Then `error` → `stale`, then a
reconnect about 0.5 s later, and so on forever. The fallback's 5 s interval
is cancelled by every `open` before it can fire. So the wall keeps the tiles
from the REST bootstrap it took on page load (REST is not capped,
`delivery.py:277-311`). Running flags, P&L and fills stay frozen while the
footer flips between `Live` and `Delayed`. Every cycle also makes the lane
build and ship a >1 MB snapshot.

*Measured* with the real `ChartBar` / `GallerySymbolBars` models: one full 5 s
buffer (4,000 bars) = **580,025 bytes** (145 B/bar). Two full symbols =
1.16 MB. In RTH, two symbols reach the cap at about 3,450 bars each (≈14:18 ET).
Three symbols reach it at ≈2,300 each (≈12:42 ET). Buffers seeded from the
day's persisted JSONL can pass it at page load.

*Prototype sketch.* In-process: build the combined-posture `LaneRouter` with
`build_local_delivery(app)`, override `get_gallery_hub` with a hub whose fake
aggregator returns 2×4,000 bars, and open `…/clerks/{c}/accounts/{a}/gallery/stream`
with `httpx.AsyncClient(ASGITransport)`. Assert that the response is 200
and that the body ends without any `event: snapshot`. On the frontend, a
store spec with a fake `EventSource` driving `open`/`error` at 500 ms asserts
that the fallback `bootstrap` is never called over 30 s.

### G2: `●Live` means "SSE open", never "market data flowing". **P1, PROVEN**

*Hypothesis.* During an IBKR Gateway logout, blackout or errored
`reqRealTimeBars` line in RTH, the aggregator stops appending and records
`status="errored"` / `last_error` (`live_bar_aggregator.py:396-409`). The hub
never reads those fields (`gallery_hub.py:283-296`, `:428-440`). The router
still emits an `update` every ~1 s because `bots_delta` is non-empty
(`broker_v2_gallery.py:248`). The coordinator path stays healthy, so the
store stays `live` (`gallery-live-store.service.ts:332-335`). The per-bot
`last_bar_at_ms` and frame `as_of_ms` that would expose the staleness are sent
but never rendered (grep: no reader in `FE/components/broker/v2-panel/gallery/`).
The operator sees `●Live`, no banner, running tiles and a flat, frozen chart.
The constant update cadence also means no client-side watchdog could detect
it: the stream is genuinely alive.

*Prototype sketch.* Hub unit: a fake aggregator with `snapshot_5s` frozen and
`status="errored"`. Assert that `build_update` output carries nothing that
distinguishes it from a healthy quiet market (same shape, `last_bar_at_ms`
unchanged). Page spec: feed snapshots whose `last_bar_at_ms` is 10 min old and
assert that the page shows no stale indication while `status()==='live'`.

### G3: Transport `open` is treated as live before the first frame. A failing snapshot build turns it into a Live/Delayed flicker, a 500 ms hot loop and a "No bots yet" flash. **P2, PROVEN**

*Hypothesis.* When `get_catalog` raises (`panel_data_source.py:288-297`),
`_gallery_event_source` fails at its first `await`, after headers have gone
out (`broker_v2_gallery.py:198`, Starlette `responses.py:254-262`). Each cycle
is `open`→`live` (fallback cancelled, backoff reset), then abort →
`stale`/`error`, then reconnect after 500 ms. With a held snapshot, the
footer and banner flicker at ~1–2 Hz. On first load with a failed REST
bootstrap, the `live` + 0-bot window renders **"No bots yet / This account has
no bots set up"** (`bot-gallery-page.component.ts:110-116`, `.html:16-21`).
That is false for an account with running bots, but it lasts only
milliseconds. It becomes G4 if the build is slow rather than failing. The hot
loop hits the SQLite roster about twice a second per open tab.

*Prototype sketch.* Router test with a catalog fake that raises
`PanelUnavailableError`. Assert that the response status is 200 and that the
stream ends with zero frames. Store spec: fake transport `open`→`error` pairs
at 500 ms. Record the `viewState()` sequence and assert that `empty` appears.

### G4: A hung `build_update` freezes every client of the account with the badge showing `Live`. **P1, SUSPECTED**

*Hypothesis.* Nothing awaited under `_publish_lock` has a timeout: the catalog
SQLite read, per-stopped-bot `get_panel` (resume admission), per-bot fills and
aggregator subscribe (`gallery_hub.py:587-599`, `:656-671`). One hang blocks
REST and every SSE client of that account. The lane then sends nothing. The
coordinator's own 15 s `: keepalive` (`broker_clerks.py:338-345`) keeps the
browser connection healthy. The browser has no inactivity timeout
(`authenticated-sse-connection.ts`), so the status stays `live` indefinitely
with frozen tiles. On first load (REST also hung) the state is `live` + 0 bots,
which renders "No bots yet".
The open question for the prototype is whether any real read in that set can
block for tens of seconds (SQLite busy-wait, a slow resume preview).

*Prototype sketch.* Inject a catalog source that awaits an `asyncio.Event`
that is never set. Open the stream through `build_local_delivery` and read
for 40 s: expect only `: keepalive`. Store spec: `open` then silence, and
assert that the status is still `live` at t=60 s.

### G5: Update frames carry no epoch, so the store would merge a foreign epoch's updates. **P3, SUSPECTED**

`ingestUpdate` checks only `surface_version` (`gallery-live-store.service.ts:215`).
The epoch lives only in the SSE `id:`, which the store never reads. A REST
snapshot from another process landing between a stream's snapshot and its
updates would re-key the store to a foreign epoch, and later updates would be
merged or dropped against the wrong counter. This is unreachable today
(single uvicorn worker, `compose.yaml:266`). It becomes live the moment the
gallery is served by more than one worker or replica. *Prototype:* store spec
that ingests snapshot E1 v300, then E2 updates v5..v400, and asserts
convergence.

### G6: The gallery drives a 1 Hz resubscribe loop on a dead IBKR line. **P2, SUSPECTED**

`ensure_subscribed_5s` / `ensure_subscribed` start a new task whenever the
previous one is `done()` (`live_bar_aggregator.py:145-154`, `:171-177`).
`_pump` ends immediately on `NotConnectedError` (`:396-399`). Every gallery
poll (1 s, per account hub) therefore re-subscribes every shown symbol for 5 s
and 1 m during a Gateway outage. It shares the `reqRealTimeBars` line with
the bots' own feed (`:159-163`). The hypothesis is that this churn interferes
with D5 recovery or with IBKR pacing for the bot feed. *Prototype:* fake IBKR
client raising `NotConnectedError`, one gallery stream open for 60 s. Count
subscribe attempts, then repeat with a bot feed on the same symbol.

### G7: One bot's fill-evidence failure silently omits its markers. **P3, PROVEN**

`_PanelChartFillSource` returns `("", ())` on `PanelUnavailableError` /
`UnknownBotError` (`broker_v2_gallery.py:93-102`). The hub omits the bot from
`markers` (`gallery_hub.py:524-527`). The tile still shows the catalog's
`fills_today` count with no markers. This is omission rather than explicit
partial failure (ADR 0062 §9 in spirit, at bot level). The resume path does
the right thing and fails closed with a reason.

### G8: REST snapshot maps a roster outage to a generic 500. **P3, PROVEN**

`get_gallery_snapshot` does not translate `PanelUnavailableError`
(`broker_v2_gallery.py:174-175`), so it hits `polygon_exception_handler` → 500
(`error_handlers.py:38-45`, registered `PDS/app/main.py:1460`). The panel
router uses a typed 503.

### G9: Ghost bot when a REST snapshot introduces a sid the stream never knew. **P3, SUSPECTED**

`removed_sids` is diffed against the stream's own `known_sids`
(`broker_v2_gallery.py:222`, `:239`; `gallery_hub.py:658-659`). Suppose a
fallback or `reset` REST snapshot, built after the stream's last poll, adds
bot X, and X is retired before the stream's next poll. The next stream
update's `removed_sids` omits X, so X stays on the wall with its last state
until reload. The window is ≤1 s. *Prototype:* store spec, or hub test with
interleaved `build_snapshot` / `build_update`.

### G10: An unknown `resolution` silently drops every snapshot. **P3, PROVEN (latent)**

`isGalleryResolution` accepts only `'5s' | '1m'` (`gallery-live-store.service.ts:31-33`, `:42`).
A new backend Literal member regenerates the TS union, but a deployed old
bundle drops every frame with no status change (`parseAndIngest`, `:303-317`).

### G11: Client bar maps grow without bound, and directory reloads restart the stream. **P3, PROVEN**

`mergeBarsBySymbol` never trims (`gallery-live-store.service.ts:63-68`), while
the server buffer caps at 4,000. The page effect restarts the store on every
directory response change, and a first load with the directory still cold
does a wipe plus a second bootstrap (`bot-gallery-page.component.ts:118-131`,
`gallery-live-store.service.ts:156-182`).

## (e) Defects proven by reading

| Id | Severity | Filed |
|---|---|---|
| G1: SSE snapshot > 1 MB coordinator cap freezes the wall behind a flickering `Live` | P1 | #2328 |
| G2: `●Live` reflects transport only; a dead IBKR feed shows as live | P1 | #2330 |
| G3: open-before-first-frame → flicker, hot loop, "No bots yet" flash | P2 | listed only |
| G7, G8, G10, G11 | P3 | listed only |

Suspected, for Prototype tickets: **G4 (P1)**, **G6 (P2)**, G5 (P3), G9 (P3).

Cross-seam note for the other A tickets: the per-bot panel stream
(`bot_panel_live_stream`, `fleet_adapter.py:486`) passes through the same
1 MB cap and carries one symbol's `live_chart` bars. At about 0.58 MB for a full
5 s buffer plus the panel, it sits under the cap today but shares G1's
failure mode.
