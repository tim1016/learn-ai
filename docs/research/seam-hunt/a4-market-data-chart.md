# A4: Market-data feed stream to charts. Can a live chart show a bar the bot never saw, or miss one it did?

Research ticket #2292, part of map #2276. Static trace. Every citation is at `a14f1df1` unless noted. Paths are relative to `PythonDataService/` for Python and to `Frontend/src/app/` for TypeScript.

## Answer

**Yes, in both directions, and nothing in the system notices.** The chart and the bot are not two views of one bar stream. They are **two separate IBKR subscriptions**, each with its own minute assembler and its own rules for continuity.

- **The bot** subscribes with `use_rth=False` (`app/services/bot_trade_strategy.py:271`). Its minutes go through `ContinuityLoop`, which keeps one assembler across reconnects (`app/marketdata/ibkr_feed.py:265-287`). Every bar it receives is written to the durable source-bar ledger before it reaches the strategy (`bot_trade_strategy.py:272-273`).
- **The chart** subscribes with `use_rth=True` (`app/services/live_bar_aggregator.py:325`, default at `app/broker/ibkr/bars.py:879`). It builds a **new** `MinuteAssembler()` on every stream start, and a start happens after any error, and after every 1101 resubscribe (`live_bar_aggregator.py:233-266`). The registry key includes `use_rth` (`bars.py:245-247`), so the two never share a line.
- **Nothing reconciles the two.** No chart path reads the bot's ledger bars. The panel reads only its continuity *events* (`app/services/broker_v2_panel/panel_data_source.py:234-242`).

So the chart can show a minute with an OHLCV the bot never decided on. This happens after any chart-side restart, and it builds on D1 P-2, the dead partial-first-bar guard. The chart can also silently lose minutes the bot decided on: it has no backfill and no notice, and the chart library closes the gap so the missing minute is invisible. In extended hours the chart shows none of the bot's PRE/POST activity: no bars, no fill markers, and no "trades today" entries.

The chart's stream state never reaches the operator. `ChartWindowResult.is_streaming` is computed and then thrown away. The Live tab's lit signal dot and its "Refreshes every 5s" line are static template text. The panel's market-pulse headline describes the **bot's** line, not the chart's.

**No P0/P1 is proven by reading, so no bug issue is filed.** Two P1s are provisional and hinge on whether they can be reached: G-1, a chart-only freeze shown as "Live", and G-7, a panel frozen by the `end` frame. Both go to prototypes.

**Correction to the ticket's premise.** `services/market-data-feed.service.ts` is a REST-only client, and it carries no stream (`market-data-feed.service.ts:14-24`). The live chart streams are:

- the bot-panel SSE (`/bots/{sid}/live-stream`);
- the gallery SSE (`/gallery/stream`);
- the triage-detail REST poll of `/chart/live`.

The market-data-feed service is covered below only for payload fidelity.

## (a) Trace: the bot's path vs the chart's path

### Bot path (what the strategy decides on)

1. IBKR `reqRealTimeBars`, 5 s TRADES, with `useRTH=False`. The line is leased from `_REALTIME_BAR_SUBSCRIPTIONS` (`bars.py:222-260`).
2. `stream_minute_bars(..., assembler=loop.assembler)` (`app/marketdata/ibkr_feed.py:274-282`). One assembler outlives every resubscribe (D1 I2).
3. `ContinuityLoop.resolve_emitted` decides whether each minute is delivered, flagged `realtime_across_reconnect`, omitted as a gap, or refused as fatal (`ibkr_feed.py:283-296`, `_deliver` at `:298-315`).
4. `_translate` produces `MarketDataBar` (`ibkr_feed.py:451-472`).
5. `_RetainedSourceBarFeed.stream_bars` runs `admit_on_delivery`, then `ledger.append`, then `session.includes`, and only then yields to the strategy (`bot_trade_strategy.py:271-282`).

### Chart path (what the operator sees)

1. IBKR `reqRealTimeBars`, 5 s TRADES, with `useRTH=True`. This is a different line from the bot's.
2. `LiveBarAggregator._run_stream` calls `stream_minute_bars(client, sym, assembler=MinuteAssembler())`. The assembler is new on every start (`live_bar_aggregator.py:320-329`). The 5 s pane uses `stream_raw_5s_bars` verbatim (`:331-340`).
3. `_pump`. The partial-first-bar guard is dead (D1 P-2, `:366-387`). The bar is persisted (`:389`, `_persist_bar` `:410-435`) and appended to a ring buffer of 500 one-minute bars or 4 000 five-second bars (`:391`). Any exception sets `errored` and ends the task (`:396-408`). The next `ensure_subscribed` restarts it (`:145-153`).
4. `resolve_chart_window` merges persisted parquet, the replayed JSONL, and the ring buffer by `start_ms`, last one wins (`app/services/live_chart_window.py:187-215`). It computes `is_streaming` (`:175-182`, `:448-469`).
5. `_build_live_chart_from_fills` bounds the read to `live_window(now)` and sets `polygon_overlay_enabled=False` (`app/services/broker_v2_panel/panel_chart_data_source.py:61-85`).
6. `build_live_chart` maps bars to `ChartBar` and filters fill markers to the same window (`app/services/broker_v2_panel/chart_projection_service.py:219-250`). It never reads `chart_window.is_streaming`.
7. The chart reaches the browser by one of three transports:
   - **Bot panel.** `SurfaceHub` assembles `BotPanelLiveSnapshot{panel, live_chart}` every 5 s (`app/services/broker_v2_panel/live_projection.py:39-57`). The SSE route is `app/routers/broker_v2_panel.py:478-549`. The client is `BotPanelLiveStore` (`components/broker/v2-panel/lib/bot-panel-live-store.service.ts`), and the chart component is `DualPaneChartComponent` (`toCandle`, `components/broker/v2-panel/lib/chart-bar-mapping.ts:12-26`).
   - **Gallery.** `GalleryHub` reads the **raw** ring buffer at 5 s, with no window (`app/services/broker_v2_panel/gallery_hub.py:273-301`, router `app/routers/broker_v2_gallery.py:152-164`). It is a poll-driven SSE stream that sends a snapshot and then deltas (`broker_v2_gallery.py:198-253`). The client is `GalleryLiveStore` (`components/broker/v2-panel/gallery/lib/gallery-live-store.service.ts`).
   - **Triage tape.** A REST poll of `/chart/live` (`components/broker/v2-panel/bot-triage-detail/bot-triage-detail.component.ts:215-219`).

### Invariants each side assumes

| # | Assumed by | Assumption | Guaranteed? |
|---|---|---|---|
| I1 | Operator | The Live chart shows the bars the bot decided on. | **No.** It is a different line and a different assembler, and nothing reconciles them (Answer, G-2, G-9). |
| I2 | Live pane | A minute that is missing from the chart is missing from the feed. | **No.** Chart outages are not backfilled (overlay disabled, `panel_chart_data_source.py:84`), no notice is emitted, and lightweight-charts closes the gap (G-3). |
| I3 | Operator | "Live", with a lit dot and "Refreshes every 5s", means the chart is receiving bars. | **No.** That copy is static (`dual-pane-chart.component.html:53-57`, `:118-121`). `is_streaming` never leaves Python (P-1). |
| I4 | Operator | Market-pulse "Market data live" describes the chart. | **No.** `build_market_pulse` reads the bot's `MarketDataFeed` (`app/services/broker_v2_panel/market_pulse.py:35-60`, `panel_data_source.py:435`), not `LIVE_BAR_AGGREGATOR`. |
| I5 | Live pane, trades-today, economics | "Today" means every session the bot trades in. | **No.** It is the RTH session window only (`chart_projection_service.py:196-216`, `app/lean_sidecar/trading_calendar.py:297-312`, `app/broker/alpaca/clerk/sqlite/economic_projection.py:821-847`) (P-2). |
| I6 | Gallery client | Bars for a symbol have unique `start_ms` values, all from today's session. | **No.** The 5 s buffer holds raw redeliveries (P-3), and the buffer is never cut back at the day boundary (G-6). |
| I7 | Panel client | An `end` frame means "stop for good". | **It holds, but the effect is unsafe.** The store closes and never reconnects, and no UI element shows the closed state (G-7). |
| I8 | Both SSE clients | Ordering within one epoch follows `surface_version`. | **Holds.** The panel adopts only a higher version of the same epoch (`services/versioned-snapshot-stream.ts:24-30`). The gallery gives out versions after all inputs are collected, under one lock (`gallery_hub.py:448-460`, `:517-532`). |

## (b) Payload fidelity

### Chart and snapshot payloads (generated types)

`ChartBar`, `ChartFillMarker`, `ChartOverlayNoticeView`, `ChartLiveResponse`, `BotPanelLiveSnapshot`, `GalleryLiveSnapshot`, `GalleryBotView` and `GallerySymbolBars` are **generated aliases** (`components/broker/v2-panel/lib/broker-v2-panel.types.ts:163-182`, `components/broker/v2-panel/gallery/lib/gallery.types.ts:21-38`). The SSE `snapshot` payload is the same Pydantic model that the REST bootstrap returns, so the OpenAPI gate covers it.

| Field | Python | TS | Finding |
|---|---|---|---|
| `ChartBar.start_ms` / `end_ms` | `int` (`app/schemas/broker_v2_panel.py:909-910`) | `number` | OK. int64 ms. `toCandle` floors to seconds (`chart-bar-mapping.ts:21`), which is exact for 5 s and 1 m. The candle is plotted at **start** time. |
| `ChartBar.open/high/low/close` | `str`, from `str(Decimal)` (`chart_projection_service.py:147-150`) | `string`, then `Number()` | OK. `Number()` also accepts `1E+2`. |
| `ChartBar.volume` | `int`, from `int(bar.volume)` (`:151`) | `number` | OK. Truncation happens upstream (D1 G-9). |
| `ChartBar.source` | `Literal["ibkr","polygon","mixed"]` (`:895`) | generated union | OK. The live pane can never emit `polygon`, because its overlay is disabled. |
| `ChartFillMarker.quantity` / `price` | `float` (`:930-931`) | `number` | OK for display. The Decimal-to-float conversion is lossy only past 15 significant digits. |
| `ChartLiveResponse` | no streaming or status field (`:954-971`) | — | **Gap.** The chart's feed state has no place in the payload (P-1). |
| Fields with Pydantic defaults (`last_bar_at_ms`, `disabled_reason`, `markers`) | always serialized (`model_dump_json`) | optional (`?:`) in the generated type | OK. The client is looser than the wire. The stores read `?? []` / `?? null` (`gallery-live-store.service.ts:199-205`). |

### Hand-declared stream payloads

| Payload | Authority | TS | Finding |
|---|---|---|---|
| Gallery `update` | `GalleryLiveUpdate` (`app/schemas/broker_v2_gallery.py:99-109`) | hand interface (`gallery.types.ts:49-56`) | Pinned by `tests/schemas/test_broker_v2_gallery.py:144-176`. `bots_delta` is `list[GalleryBotDelta]` in Python and `GalleryBotView[]` in TS. The subclass adds no fields (`broker_v2_gallery.py:68-73`), so they match. |
| Gallery `reset` | inline `json.dumps` (`broker_v2_gallery.py:203-205`) | `GalleryResetEvent` (`gallery.types.ts:67-70`) | Pinned by a router test. The client does not parse it; it just re-bootstraps. |
| Panel `reset` | inline `{reason, cursor}` (`broker_v2_panel.py:515-517`) | untyped; it only triggers `refresh()` | OK. |
| Panel / gallery `error` | inline `{"error": str}` (`broker_v2_panel.py:527-530`) | read in `authenticated-sse-connection.ts:51-71` | OK. The named `error` event goes to the same listener as a transport error: the message is surfaced, the store reconnects and falls back to polling. |
| Panel `end` | `data: {}` (`broker_v2_panel.py:524-526`) | closes the connection (`versioned-snapshot-stream.ts:60-62`) | See G-7. |

### `market-data-feed.service.ts` REST types (hand-mirrored in `api/broker-models.ts`)

| Type | Python | Finding |
|---|---|---|
| `IbkrApiEvidenceEvent` / `Request` / `Response` evidence | OpenAPI `IbkrApiEvidenceEvent` | Matches. The `call` and `callback` enums match their OpenAPI enums exactly. The nullable fields are `T \| null` on both sides. |
| `SessionDataCapability` / `SessionCapability` | `app/schemas/broker_capability.py:15-48` | Matches, including all enums. Python's `sessions: dict[str, ...]` requires the four keys but **allows extra keys**, which TS `Record<SessionKind, ...>` does not model. Only a new session kind could trip this (P3). |
| `BrokerCapabilityResponse` | `BrokerCapabilityReadResponse` / `BrokerCapabilityProbeResponse` (`:51-60`) | Same shape under a different name. `capability()` has no caller. |
| `IbkrStrikeList` | `app/broker/ibkr/models.py:351-368` | Matches (`strikes: list[float]`). |
| `ExpirationsResponse`, `OptionContractsResponse` | naked `dict` returns (`app/routers/broker.py:278-284`, `:322-339`) | **Not mechanically tied.** The OpenAPI schema is bare `object`, so neither side's CI can catch drift (P3, a B1-shaped item). |
| `IbkrChainSnapshot` / `IbkrOptionQuote` / surface (SSE) | `app/broker/ibkr/models.py:316-426` | Matches. NaN and `UNSET_DOUBLE` become `None` in Python (`models.py:34-48`), so `number \| null` holds. |

**No int64 ms field is typed as `string` anywhere on these surfaces.** No enum member exists on one side and not the other, except the open `sessions` key set.

## (c) Stream behaviour

**Bot-panel SSE.** Each event is a full, latest-wins document (ADR 0028).

- The server sends `id: epoch:version`, but the client passes its own `cursor` query (`bot-panel-live-store.service.ts:163-176`). It reconnects by hand (backoff 0.5 s to 5 s, `authenticated-sse-connection.ts:67-70`), so `Last-Event-ID` is unused.
- On reconnect in the same epoch, `hub.subscribe()` queues `_latest` straight away (`app/services/surface_hub.py:227-229`). The client adopts it only if its version is higher.
- An epoch change sends `reset`, and the client re-reads REST.
- REST `live-snapshot` returns `hub._latest` without assembling a new one (`surface_hub.py:193-200`), so it can never be fresher than the stream. The ordering is sound.
- An assembly failure sends an `error` frame and closes (`broker_v2_panel.py:527-530`). The client shows the message, reconnects, and polls every 5 s. Reconnects keep returning 503 (`surface_hub.py:198-199`) until an assembly succeeds.
- **Dedup:** the panel has no bar-level deltas. Each document carries the whole day's chart, deduplicated by `start_ms` on the server (`live_chart_window.py:196-215`).

**Gallery SSE.** Each connection gets a snapshot, then deltas.

- Every connection, including every reconnect, starts with a fresh `build_snapshot()` (`broker_v2_gallery.py:198-206`).
- Its delta cursors are private to that connection: `start_ms` per symbol, and `event_key` sets per bot (`:208-218`).
- Versions come from one hub-wide counter, and are given out only after collection, under a lock (`gallery_hub.py:448-460`).
- The REST fallback runs only while the stream is in error (`gallery-live-store.service.ts:337-347`). Every REST snapshot is a full replace, so a dropped delta whose version was already overtaken is always covered by that later full snapshot. The ordering is sound.
- **Dedup:** deltas are merged on the client by `start_ms` (`gallery-live-store.service.ts:62-67`), but a full snapshot is copied verbatim (`:199-201`) (P-3, G-5).
- **Auth expiry:** the data-plane control intent is a URL parameter (`services/broker-sse.ts:148-154`). A refused reconnect looks like a transport error. The panel shows the fallback poll's error. The gallery shows `stale`, and swallows the bootstrap error by design (`gallery-live-store.service.ts:255-268`). Neither shows false-live.

**Triage tape.** A plain REST poll. Every poll calls `ensure_subscribed` (`panel_chart_data_source.py:72-75`). It has no streaming state either.

## (d) Named suspected gaps

Severities are provisional, on the map's P0–P3 scale. "Needs paper" means the gap depends on IBKR behaviour that can only be settled against a real paper feed.

**G-1. A chart-only freeze is shown as "Live" beside a healthy headline. P1 provisional.**
- *Hypothesis:* the chart's `use_rth=True` line ends up `errored` or stalled on every restart while the bot's `use_rth=False` line stays healthy. The operator then sees a chart frozen at its last candle. The tab is still labelled Live, with a lit dot and "Refreshes every 5s", and the market-pulse headline still says "Market data live". Nothing in `ChartLiveResponse` can say otherwise (P-1).
- *Prototype:* run in-process with a fake `IbkrClient` whose `useRTH=True` list raises `IBKRBarSubscriptionStalled`, or stops appending, while the `useRTH=False` list keeps printing. Drive `get_live_snapshot_parts` for 10 hub cycles. Assert that the chart bars stop advancing, that `market_pulse.feed_state == "LIVE"`, and that no payload field changes. The test falsifies G-1 if restarts always recover within one cycle.

**G-2. After a chart-side restart, the chart's minute differs from the bot's minute. P2.**
- *Hypothesis:* after `resubscribe_all` (1101), or a chart pump error mid-minute, the chart's new assembler emits minute M from only the bars after the restart. It persists that short minute and shows it. The bot's `ContinuityLoop` completes the same M across the reconnect, flagged `realtime_across_reconnect`, or refuses it. The candle on screen therefore differs from what the strategy decided on. This builds on D1 P-2 and I2; the new part is the consequence across the two consumers.
- *Prototype:* use one fake client serving both lines. Interrupt at `:20` inside minute M and restore at `:25`. Collect the bot's delivered M and the chart's snapshot M. Assert that they differ in open, high/low or volume, and that the chart's M carries no mark.

**G-3. The chart silently loses minutes the bot saw. P2.**
- *Hypothesis:* while the chart's task is `errored`, or before anyone views the chart after a clerk restart (subscription is lazy, `live_bar_aggregator.py:126-154`), minutes arrive only on the bot's line. The live pane has no backfill (`panel_chart_data_source.py:84`) and emits no gap notice. lightweight-charts closes up the missing candles. The operator sees a continuous tape that omits minutes the bot decided on.
- *Prototype:* start the bot's feed at 10:00. First call `get_live_chart` at 10:30 on a new aggregator with a persistence root that has no 10:00-10:30 data. Assert that the response has no bars in [10:00, 10:30), no notice, and that the bot's ledger holds 30 minutes.

**G-4. The extended-hours trading day is invisible on the live pane. P2 (the mechanism is proven, P-2).**
- *Hypothesis:* for a `use_rth=False` lane that fills in PRE or POST, the live pane shows no bars for that session and no markers for those fills. The trades-today list, which is fed by the live markers (`trader-lens.component.html:28-34`), omits them too. `fills_today` and `realized_pnl_today` use the same RTH window, so the day's P&L omits a round trip closed in POST. The position and `open_pnl` still reflect the trade. The owner should confirm the severity; if the operator acts on day P&L, it reads closer to P1.
- *Prototype:* seed the SQLite fixture with a fill at 17:05 ET. Call `get_live_snapshot_parts` at 17:10 ET and assert that the fill is absent from `fill_markers`, `fills_today` and `realized_pnl_today`.

**G-5. Gallery tiles and the detail pane disagree on 5-second bars after an IBKR redelivery. P3.**
- *Hypothesis:* `stream_raw_5s_bars` yields redeliveries and same-timestamp corrections verbatim (`bars.py:817-872`) into the 5 s buffer. The detail pane deduplicates, last one wins. The gallery's full snapshot passes both copies through as two candles. A correction that arrives after the connection's `start_ms` cursor has passed that `start_ms` is never sent in a delta (`live_bar_aggregator.py:201` uses a strict `>`), so the tile keeps the uncorrected bar.
- *Prototype:* append `[t, t']` to the fake 5 s list with the same `t` and a different close. Assert that `build_snapshot` returns two bars at `t`, and that `build_update` with `since = t` returns neither.

**G-6. Gallery tiles draw the previous session's bars flush against today's. P3.**
- *Hypothesis:* the ring buffer is never cut back at the day boundary. The gallery reads it raw (`gallery_hub.py:296`). The client merges deltas without limit (`gallery-live-store.service.ts:62-67`). The tile renderer draws by index. At 09:31 the tile therefore shows yesterday's afternoon joined straight onto today's first minute, with no visible gap. Its `session_change_pct` is correctly limited to today (`gallery_hub.py:84-110`), so the header and the drawing disagree.
- *Prototype:* build a hub whose aggregator holds bars from D-1 15:00-16:00 and D 09:30. Assert that `build_snapshot().symbols[0].bars` spans both days.

**G-7. An `end` frame freezes the whole bot panel with no indicator. P1 provisional.**
- *Hypothesis:* if `SurfaceHub.stop()` runs while a client is subscribed, the `end` frame reaches the browser. That happens in `stop_live_projection_hubs` during a graceful clerk shutdown (`live_projection.py:118-131`, `surface_hub.py:310-315`). The browser then closes the stream for good: no reconnect, and the fallback is stopped (`bot-panel-live-store.service.ts:189-193`). `liveStreamStatus` feeds only the loading flag (`bot-panel-shell.component.html:73`). The panel keeps showing the last health, position and chart until the operator navigates away. Clerks are restarted routinely after merges.
- *Open question:* whether uvicorn still flushes the frame at that stage of shutdown, or whether the connection has already dropped (which leads to an ordinary reconnect).
- *Prototype:* run an ASGI app with the real router and a stub hub. Call `stop_live_projection_hubs()` while an `httpx` stream reader is attached, and assert that it receives `event: end`. Then run the store spec, and assert that it makes no further connection attempt and that no UI state changes.

**G-8. An old-epoch frame can overwrite newer-epoch state on the panel. P3.**
- *Hypothesis:* `adoptVersionedSnapshot` replaces the current snapshot whenever the epoch *differs* (`versioned-snapshot-stream.ts:28`), not only when it is newer. A delayed frame from a hub that has been retired and replaced overwrites the new hub's state until that hub's next change. Reaching it needs a retire-and-recreate while a response is in flight.
- *Prototype:* a unit test on `adoptVersionedSnapshot(new, old)`.

**G-9. In steady RTH, the two IBKR lines may differ. P3, needs paper.**
- *Hypothesis:* `useRTH=True` and `useRTH=False` 5 s TRADES bars differ in some RTH windows even with no reconnect. The likeliest places are the 09:30:00 opening-print bar and trade-condition filtering. The two consumers would then disagree without any event.
- *Prototype (paper):* subscribe to both lines for one session and diff the 5 s bars from 09:30 to 16:00.

**G-10. The weekend window uses UTC midnight, though the docstring says NY day. P3.**
- *Hypothesis:* `live_window` falls back to the UTC day (`chart_projection_service.py:212-216`), although its docstring promises the "NY calendar day" (`:201-202`). On Saturday from 00:00 to 04:00 UTC, the window is Friday evening ET.
- *Prototype:* call `live_window` at Saturday 01:00 UTC.

## (e) Defects proven by reading

**P-1. The chart's streaming state never reaches the operator. P2, no bug issue.**
- `is_streaming` is computed (`live_chart_window.py:175-182`). `build_live_chart` never reads it (`chart_projection_service.py:237-250`), and no other module does either. A grep for `is_streaming` finds only its definition and construction.
- `ChartLiveResponse` has no status field (`app/schemas/broker_v2_panel.py:954-971`). The aggregator's `status` and `last_error` go only to the legacy `/api/broker/bars/snapshot` route (`app/routers/broker.py:570-578`).
- The Live tab's signal dot and "Refreshes every 5s" are unconditional (`components/broker/v2-panel/dual-pane-chart/dual-pane-chart.component.html:53-57`, `:118-121`).
- Whether this is P1 depends on G-1.

**P-2. The live pane, trades-today and today's economics are RTH-only. P2, no bug issue; the owner should confirm the severity.**
- `live_window` means `current_trading_session_window`, which is the RTH open to close (`chart_projection_service.py:209-211`, `trading_calendar.py:297-312`).
- Markers are filtered to that window (`chart_projection_service.py:238`). The chart line is `use_rth=True`.
- `session_fills`, `fills_today` and `realized_pnl_today` use the same window (`economic_projection.py:821-847`, window at `app/services/broker_v2_panel/sqlite_panel_source.py:360`).
- Extended-hours lanes trade with `use_rth=False` (`bot_trade_strategy.py:271`). No gate reads these fields: a grep for `realized_pnl_today` / `fills_today` outside projections and schemas finds only `fifo_pnl.py:421`. So this is display-only.

**P-3. The 5-second chart buffer admits duplicate `start_ms` values. P3.**
- The raw 5 s path does no duplicate bookkeeping (`bars.py:817-872`, per its docstring).
- `_pump` appends every bar (`live_bar_aggregator.py:391`), while `BarPersistence` deduplicates and applies corrections (`app/services/bar_persistence.py:209-226`). The in-memory buffer and the on-disk log therefore disagree after a redelivery.
- The gallery full snapshot forwards duplicates (G-5).

**P-4. The `live_window` docstring and code disagree on the weekend day. P3.** See G-10.

## Not repeated here (already charted)

These come from D1 (#2278), and A4 builds on them:
- the dead partial-first-bar guard (D1 P-2);
- the chart showing yesterday's 15:59 minute the next morning (D1 G-5);
- PRE/POST minutes stamped `CLOSED` (D1 P-3).

G-2 and G-3 describe their consequence across the two consumers, not the mechanism itself.

## Cleared (static) at this seam

- Payload shapes for every chart and snapshot type (generated and CI-gated), the gallery `update` and `reset` (pinned by tests), and the options SSE quotes.
- SSE ordering within one epoch, for both the panel and the gallery.
- Gallery replay by snapshot on reconnect, with delta cursors private to each connection.
- Marker identity by `event_key` on both server and client.
- Timestamps are int64 ms end to end. No temporal field is typed as a string.

## Bugs filed

None. No P0/P1 is proven by reading. G-1 and G-7 are provisional P1s for prototypes.
