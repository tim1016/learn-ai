# PRD: Failure-isolated bot market tape — responsive controls and coordinator-owned Polygon history

- **Date:** 2026-09-18
- **Status:** Approved for issue creation (revised after seam review; owner decisions recorded 2026-09-18)
- **Product surface:** Alpaca Broker V2 bot detail → Trader lens → market tape; Operator lens resource-safety guard
- **Delivery posture:** Three tracer bullets: frontend failure isolation, graceful history baseline, then one-hop coordinator-owned historical retrieval
- **Authority:** IBKR remains the live market-data provider for every Alpaca bot. The Clerk owns account custody and fill evidence. The fleet-coordinator role owns Polygon credentials and historical market-data retrieval. Angular owns interaction and presentation only.
- **Source:** Browser-driven diagnosis on the live Alpaca bot detail page, confirmed against browser errors, Clerk logs, runtime configuration, component tests, the existing fleet transport, and ADR 0062.

---

## 0. Owner decisions (2026-09-18)

Four open questions were settled before issue creation. They are recorded here
because each one removed scope that a reader of an earlier draft may expect.

| Question | Decision | Effect |
|---|---|---|
| Keep last-good bars on screen when history fails? | **No.** Show the unavailable state. | Removed the keyed cache, the stale label, and the late-result/cross-timeframe bug class. |
| A cold `1D` walk may exceed the 10-second fleet default. | **Give it more time.** This route gets its own larger bound. | Both hops widen (FR-010); no partial batch, no coordinator cache. |
| Must credential-free qualification proof exist before shipping? | **No — ship the fix first.** | §11.7 becomes a tracked follow-up; success proven by a hand-run smoke test. Residual risk accepted and stated. |
| Fix the same bug on the Operator lens here? | **Yes.** | `journalPage` guard and its focused spec stay in slice A. |

## 1. Executive summary

The Alpaca Broker V2 bot market tape becomes unreliable whenever its optional
Polygon history request fails. Fleet Clerk agents deliberately receive a
**present-but-empty** `POLYGON_API_KEY`, but the Clerk-served history route calls
Polygon directly. Polygon therefore returns `401`, the Clerk emits an unhandled
server error, and fleet routing reports the lane as unavailable.

The frontend then calls `histChart.value()` while the Angular resource is in an
error state. That read throws `ResourceValueError` during rendering. The chart's
own controls still mutate their signals, but Angular abandons the render pass
before the DOM reflects them. Expand/Collapse, Local/ET, source and interval
selectors, indicator filters, category disclosures, and the trader action menu
therefore appear dead or one click behind. The delayed pane remains on an
indefinite loading state rather than showing the settled failure.

The same unsafe template pattern exists in the Operator lens:
`journalPage.value()` is read without first proving `journalPage.hasValue()`.
That has not caused the reported Trader failure, but it is the same render-freeze
class and is in scope for the shared resource-safety rule.

This PRD restores three existing boundaries:

1. **Failed resources are safe to render.** Optional history or journal failure
   cannot abort the live panel or unrelated controls.
2. **History failure degrades honestly.** The existing Polygon notice taxonomy
   is reused so missing credentials and known provider refusals become a settled,
   visible unavailable state rather than an unhandled `500`/routed `503`.
3. **Polygon access stays coordinator-owned.** A Clerk makes one bounded,
   authenticated request to the fleet-coordinator role. The coordinator owns the
   full backward history walk and returns one complete batch. The Polygon
   credential never enters the Clerk process.

The public bot-history URL and successful candle/fill semantics remain stable.
The history response gains an additive notice field. No live market-data
provider changes, no Alpaca market-data subscription, and no new frontend
numerical authority are introduced.

## 2. Problem statement

### 2.1 One optional dependency can freeze the Trader lens

`BotPanelShellComponent` creates a `resource()` for Polygon history and passes
its value into the Trader lens with an unconditional `histChart.value()`
template read. Angular resource values throw while the resource is errored.
Because the read occurs above the market tape, the exception aborts rendering
for healthy live-chart state and unrelated user interactions.

Observed behavior under the current failure:

- Expand/Collapse does not update its pressed state or layout on the initiating
  click.
- Local/ET does not update the selected timezone on the initiating click.
- Live resolution, Live/Delayed source, and Polygon timeframe selections can
  render only after a later interaction.
- Indicator categories and pane filters can appear one interaction behind.
- The trader action menu can appear inert until a later render succeeds.
- The delayed pane says `Loading Polygon candles…` indefinitely, with no error
  explanation or retry affordance.
- The browser console receives repeated `ResourceValueError` exceptions.

The top-level Trader/Operator switch remains functional, and the live IBKR chart
continues receiving bars. The fault is therefore not a global pointer-event or
chart-library failure; it is a render failure caused by the errored history
resource.

### 2.2 The history route violates the deployed credential boundary

Fleet Clerk agents intentionally receive `POLYGON_API_KEY: ""`. The field must
be present because `Settings.POLYGON_API_KEY` has no default; omitting it would
prevent Settings construction and brick the Clerk at boot. The fleet-coordinator
role receives the usable credential as part of the data-plane market-data
boundary.

The current Clerk history implementation nevertheless calls
`fetch_aggregate_bars(..., settings.POLYGON_API_KEY)` directly. In the deployed
topology this produces:

1. Clerk calls Polygon with an empty key.
2. Polygon returns `401 API Key was not provided`.
3. `PolygonAuthError` escapes the history route as an unhandled `500`.
4. Fleet routing maps the Clerk's `5xx` to a typed `clerk_unreachable` `503`.
5. Angular reads the errored resource value and aborts rendering.

Injecting the Polygon key into each Clerk would make the request succeed, but it
would violate the existing fleet boundary and multiply a data-plane credential
across execution lanes.

### 2.3 The naive seam would create repeated re-entrant hops

The existing `_fetch_history_bars` implementation is a backward-walking loop.
It calls its `HistoryBarSource` repeatedly, widening the date range until it has
the display bars plus indicator warmup bars or reaches the two-year floor.

The browser's public request already travels coordinator → Clerk. If the
existing per-window `HistoryBarSource` were merely replaced with a remote call,
one browser request would become:

```text
browser → coordinator → Clerk → coordinator → Polygon
                              ↳ coordinator → Polygon
                              ↳ coordinator → Polygon …
```

That design would hold the routed request while making N calls back into the
same coordinator. It is especially unsafe in qualification, where request pools
are deliberately starved.

The required shape is instead one Clerk → coordinator call. The coordinator
runs every Polygon window iteration locally and returns the completed batch:

```text
browser → coordinator → Clerk → coordinator ──N local vendor fetches──→ Polygon
                              ←──────── one completed bar batch ────────
```

### 2.4 Existing tests omit the failure path

The main shell spec currently contains 28 `it(...)` cases and there is one
separate correlation spec. They mock `getHistoryChart` successfully, so the
suite passes while the production-shaped failure remains reproducible. The
dual-pane chart tests prove isolated signal handlers, but no full-shell test
starts with a rejected history resource and then exercises user controls.

The main shell spec is already about 61 KB and the frontend test gate enforces a
hard 120-second budget per shard. The regression must live in a separate,
focused spec rather than further widening that file.

## 3. Goals

1. Keep the live IBKR market tape and all local chart controls immediately
   responsive when Polygon history is loading, unavailable, or failed.
2. Present a truthful, actionable delayed-history state instead of an
   indefinite loading state.
3. Keep `POLYGON_API_KEY` exclusively usable in the fleet-coordinator/data-plane
   role while retaining a present-but-empty value in every Clerk environment.
4. Preserve the Clerk as authority for account-scoped custody and fill evidence.
5. Preserve the public bot-history route and candle/fill semantics.
6. Reuse the existing `ChartOverlayNotice` Polygon failure vocabulary rather
   than introduce a competing taxonomy or third error envelope.
7. Ensure one browser history request causes at most one Clerk → coordinator
   HTTP request, regardless of the number of Polygon ranges required.
8. Bound the internal hop so a hung coordinator becomes a settled
   `coordinator_unavailable` state rather than an indefinite spinner.
9. Prevent automatic retry storms while retaining an explicit retry path.
10. Add deterministic component, contract, topology, qualification, and
    browser-level regression evidence.

## 4. Non-goals

- Changing IBKR as the live market-data provider for Alpaca Paper, Live Shadow,
  or Live bots.
- Adding or enabling an Alpaca market-data subscription.
- Giving a usable Polygon credential to Clerk agents.
- Moving custody, fills, account authority, or execution decisions into the
  fleet coordinator.
- Changing candle mathematics, timestamp semantics, fill-to-candle mapping,
  indicators, tolerances, or chart-library behavior.
- Replacing `lightweight-charts` or redesigning the market tape.
- Restoring or building on deprecated IBKR bot-control or navigation surfaces.
- Making Polygon availability a prerequisite for live bot operation.
- Reusing unauthenticated `POST /api/aggregates/fetch`. That route returns a
  sanitized vendor-oriented shape and collapses exceptions to a string `500`;
  it does not satisfy the fleet authentication or failure contract.
- Serving this history from the local lake. The lake artifact is LEAN
  deci-cent CSV-in-zip, the relevant source is minute-granularity, it does not
  provide this contract's native 15m/30m/1h batches, and production mounts it on
  the coordinator rather than the Clerks. Building a new lake reader/resampler
  is a separate product and mathematical-authority decision.

## 5. Users and jobs

### Trader

- Expand the live chart without depending on delayed-history availability.
- Change Local/ET and immediately see labels and pressed state update.
- Change the live interval and source with immediate visual feedback.
- Understand when delayed history is unavailable and retry intentionally.
- Continue inspecting live IBKR bars, fills, indicators, and account summary
  while Polygon or the coordinator history seam is down.

### Operator

- Open and close audit evidence without an errored journal resource freezing the
  Operator lens.
- Distinguish a historical-data dependency failure from a live-feed or custody
  failure.
- See a bounded reason and next step without reading browser or container logs.
- Know that retrying history cannot mutate orders, custody, or bot state.

### Maintainer

- Reproduce the original failure with one deterministic browser check.
- Prove no Broker V2 template reads an errored resource value.
- Run the fleet with an empty Clerk Polygon key and obtain history through the
  coordinator-owned seam.
- Prove one public history request produces one internal history request.
- Classify vendor failures without exposing credentials or raw vendor payloads.

## 6. Product principles

1. **Live operation is independent of optional history.** A delayed archive
   outage never impairs the live IBKR chart or local UI interactions.
2. **Resource values are guarded at presentation boundaries.** A template may
   read a resource value only after proving `hasValue()`.
3. **Credentials stay with their authority.** Coordinator-owned data-plane
   credentials do not spread into provider execution lanes.
4. **Custody stays with the Clerk.** The coordinator returns historical bars;
   it never owns or combines fill, order, position, P&L, or custody facts.
5. **The backward walk stays beside the vendor.** One internal request may make
   several Polygon calls inside the coordinator, never several fleet hops.
6. **User intent renders immediately.** Pressed, selected, expanded, and menu
   states update in the same interaction that changes them.
7. **Retry is explicit and bounded.** No render loop, poll, directory refresh,
   or focus change repeatedly reissues a failed request.
8. **No silent substitution.** A Polygon failure never swaps in IBKR bars and
   never labels another source as Polygon.

## 7. Required experience

### 7.1 Healthy live pane with failed history

When the initial history request fails or returns an unavailable notice:

- The Trader lens renders normally.
- Live remains the selected source.
- IBKR candle and fill data remain visible.
- Expand/Collapse, Local/ET, 5s/1m, indicator controls, and trader menus update
  immediately.
- No `ResourceValueError` reaches the browser console.
- A concise non-blocking history status is available near the source selector,
  but it does not dominate the live chart.

### 7.2 Delayed pane failure state

When the user selects `15m Delayed` while history is unavailable:

- The source tab changes immediately.
- The selected Polygon timeframe remains visible.
- The chart stage shows `Polygon history unavailable`, not a loading spinner.
- The state uses the backend-authored safe notice message or an existing closed
  frontend copy map; raw exception text is never shown.
- A `Retry history` button issues one `histChart.reload()`.
- Switching back to Live is immediate and does not wait for retry completion.
- Changing the Polygon timeframe starts one request for that timeframe and
  does not retry any previous timeframe.

### 7.3 Recovery

After a successful explicit retry or timeframe request:

- The unavailable state clears.
- Candles and exact containing-candle fill markers render through the existing
  mapping.
- The selected source, timeframe, timezone, and expanded state are preserved.
- The chart fits content after the existing render path completes.

A failed attempt shows the unavailable state; it never leaves earlier bars on
screen. Retaining last-good history was considered and rejected (owner decision,
2026-09-18): it conflicts with principle 8, and a keyed cache introduces
late-result and cross-timeframe display bugs that a settled unavailable state
cannot have.

## 8. Functional requirements

### FR-001 — Enforce guarded resource reads across Broker V2

Every resource `.value()` read in a Broker V2 template must be guarded by that
resource's `.hasValue()` in the same Angular expression, or be replaced by a
component `computed()` that performs the guard.

At minimum, fix both known violations:

- `bot-panel-shell.component.html` → `histChart.value()`
- `operator-lens.component.html` → `journalPage.value()`

Add a static contract test over
`Frontend/src/app/components/broker/v2-panel/**/*.html` that fails on an
unguarded resource-value binding. The already-correct
`hasValue() ? value() : null` pattern in bot triage is the reference behavior.

Scope the scan to a declared list of resource identifiers, not to every
`.value()` occurrence. Signals, form controls, `Map` entries, and other
non-resource members legitimately expose `.value()`; a blanket match would
false-positive on unrelated work and get disabled the first time it blocks an
unrelated pull request. Adding a resource to a Broker V2 component means adding
its name to that list.

### FR-002 — Model history presentation state explicitly

The Trader lens receives or derives a typed history presentation state with:

- `data: ChartHistoryResponse | null`
- `loading: boolean`
- `unavailable: ChartOverlayNoticeView | null`
- a retry event

Separate inputs are acceptable, but raw Angular `ResourceRef.error()` values
and raw `HttpErrorResponse` objects must not reach leaf components.

### FR-003 — Isolate the live and Operator panes

No history loading/error branch may prevent live chart inputs or local control
signals from rendering. No journal loading/error branch may prevent Operator
health, Clerk, run-history, or action controls from rendering.

### FR-004 — Immediate chart-control feedback

The following controls must update their accessible and visual state on the
initiating interaction:

- Expand/Collapse
- Local/ET
- Live/15m Delayed
- live 5s/1m resolution
- Polygon 1m/15m/30m/1h/1D timeframe
- indicator pane and category filters
- indicator category disclosures
- trader action menu disclosure

### FR-005 — Explicit failed-history presentation

The delayed pane must distinguish `loading`, `unavailable`, and successful empty
history. It may not describe a settled error as loading or as
`No candles in this window`.

### FR-006 — Stable history request identity and explicit retry

The history resource parameters are exactly the read identity:

```text
broker + clerk_id + account_id + strategy_instance_id + timeframe
```

They must not include `ResourceTarget.bindingGeneration`,
`ResourceTarget.routingEpoch`, the whole `target()` object, live-store versions,
quote state, or current-run state. Binding generation and routing epoch fence
commands; they are not history read identity.

Consequences:

- a directory refresh or same-lane rebind does not reload history;
- one Retry click causes at most one request;
- live/current-run polling, quote updates, change detection, focus changes, and
  unrelated route-directory observations do not reload history; and
- a route identity or timeframe change causes exactly one new request.

### FR-007 — One authenticated coordinator history-batch operation

Add a sixth operation to the existing `/internal/fleet/*` agent-to-coordinator
family. Do **not** reuse `/api/aggregates/fetch`.

The request is a typed POST body containing only:

- `clerk_id`
- `symbol`
- `timeframe` from the existing closed history enum
- `required_bar_count`, bounded by the closed display + warmup policy
- `as_of_ms` as `int64 ms UTC`

It is authenticated with the existing agent transport identity:
`X-Fleet-Clerk-Id` plus `X-Fleet-Agent-Token`, checked against
`FLEET_AGENT_SERVICE_TOKENS_JSON`. The header Clerk identity must equal the body
`clerk_id`. The client must reuse `enforce_private_http_target()` and
`build_internal_client()` so redirects and environment proxies remain refused.
No browser secret and no `X-Data-Plane-Control-Secret` are introduced.

The response is a typed completed history batch containing sorted Polygon bars,
source/provenance, the effective `as_of_ms`, and zero or more canonical overlay
notices. It contains no account, fill, order, position, P&L, or custody facts.

### FR-008 — Coordinator owns the complete backward walk

Move the `_fetch_history_bars` widening loop behind the coordinator operation.
The Clerk's history provider makes exactly one internal request per public
history attempt. The coordinator may make N vendor calls within that one
request until `required_bar_count` is satisfied or the existing two-year floor
is reached.

Refactor the `HistoryBarSource` injection seam from the current per-date-window
call into a complete-batch provider. The production coordinator implementation
uses Polygon; unit/qualification implementations inject deterministic recorded
batches.

No code path may implement the loop by repeatedly calling the coordinator from
the Clerk.

### FR-009 — Pin temporal conversion at the coordinator

No `date` or ISO timestamp crosses the new HTTP boundary. `as_of_ms` is the
only temporal request anchor and every returned bar timestamp is `int64 ms UTC`.

The coordinator maps `as_of_ms` and timeframe to Polygon's date-based API using
`America/New_York` and the existing NYSE trading-calendar helpers. It preserves
the current rules for completed bars, half-days, daily session close, display
count, warmup count, and the two-year floor. This is a relocation of the
existing walk, not new candle or calendar math.

### FR-010 — Bound the internal hop

FR-008 concentrates every Polygon fetch and every pagination page of one
backward walk into a single hop. A cold `1D` request can therefore exceed the
existing `DEFAULT_INTERNAL_TIMEOUT_S = 10.0` fleet default. This operation
carries its own explicit, larger bound (owner decision, 2026-09-18: allow the
slow load rather than return a partial batch or add a coordinator cache).

`build_internal_client()` already accepts `timeout_s` and `read_timeout_s`, so
this is an argument at the call site, not a new client. Connection, write, pool,
and read phases all remain bounded; an unbounded read timeout is not permitted.

**Both hops must be widened, and only for this route.** The outer
coordinator → Clerk delivery hop uses `build_internal_client()` with no
arguments today (`app/broker/fleet/delivery.py`), so it also expires at 10
seconds. If only the inner Clerk → coordinator call is widened, the outer
request expires first, the user sees `clerk_unreachable`, and the coordinator's
completed work is discarded. The outer bound must be strictly larger than the
inner bound, and a test must pin that ordering.

Timeout, connection failure, malformed success payload, or unexpected
coordinator response becomes the stable `coordinator_unavailable` notice with
safe retry guidance. It must not become an indefinite in-flight resource or
leak a hostname, port, token, or response body.

Repeat vendor cost is unchanged from today: each timeframe selection re-walks
Polygon. Coordinator-side caching was considered and deferred (owner decision,
2026-09-18); it is not in scope here.

### FR-011 — Reuse the canonical Polygon notice taxonomy

`app.services.live_chart_window` is the existing authority for these provider
classifications and codes:

- `polygon_api_key_missing`
- `polygon_auth_error`
- `polygon_entitlement_error`
- `polygon_rate_limited`
- `polygon_unknown_symbol`
- `polygon_fetch_error`
- `polygon_overlay_empty` where applicable

Extract its existing exception-to-code mapping into a reusable helper and have
`live_chart_window` call that helper, so exactly one definition survives.
History must call the same helper; it may not recreate the switch elsewhere.
Extraction is chosen over "history imports the private mapping as-is" so the
two callers cannot drift — but it edits a live-path module, so the live-chart
notice tests must pass unchanged as part of the same change.

This costs less than it reads. `ChartOverlayNoticeView` already exists in
`app/schemas/broker_v2_panel.py`, `overlay_notices` is already a field on
`ChartLiveResponse`, `chart_projection_service` already projects notices into
it, and the frontend already has the generated `ChartOverlayNoticeView` type.
Adding the identically-named field to `ChartHistoryResponse` is a sibling-model
copy, not a new contract concept, and needs no new frontend type.

Known provider failures return a completed batch with no fabricated bars and a
canonical notice. The Clerk carries those notices into an additive
`overlay_notices` field on `ChartHistoryResponse`. This makes missing credentials
and known vendor failures honest settled states, matching live-chart degradation
rather than throwing an unhandled `500`.

`coordinator_unavailable` is the one history-transport extension to that notice
vocabulary. It is not a Polygon error.

### FR-012 — Reuse existing refusal envelopes; do not add a third

The new internal operation's authentication, validation, and control-plane
refusals use the existing flat fleet shape:

```json
{ "reason": "...", "message": "...", "next_step": "..." }
```

Do not add a `{code, message, retryable}` error envelope. Retry semantics remain
derived from the existing status/reason policy and the closed notice map.
Existing public panel custody errors keep their established panel envelope;
this work does not invent a third shape or rewrite unrelated routes.

Any code-like reason displayed in the frontend goes through `receiptLabel`.
Backend-authored operator prose is rendered as prose, not piped.

### FR-013 — Graceful baseline before the remote seam

Before or with the frontend isolation slice, make the current direct history
path classify an empty key and known Polygon exceptions through the FR-011
helper and return an unavailable notice instead of throwing. This is the
rollback-safe baseline: it removes the `401 → 500 → 503` cascade even if the
coordinator seam is delayed.

The baseline does **not** count as completion because it cannot return working
history from a Clerk with the intentionally empty key. It is removed or reduced
to a defensive fallback when the coordinator provider becomes authoritative.

### FR-014 — Clerk assembles the public bot-history response

The Clerk remains responsible for:

- validating broker/account/bot scope;
- reading account-scoped SQLite chart and fill evidence;
- calculating the existing display and indicator bar budgets;
- making one complete-batch history request;
- combining returned bars with Clerk-owned fill markers; and
- returning the existing public `ChartHistoryResponse` plus additive
  `overlay_notices`.

The coordinator must not store or derive custody, fills, orders, positions,
exposure, P&L, or risk.

### FR-015 — Credential containment

`POLYGON_API_KEY` remains **present and empty** in every `FLEET_ROLE=clerk_agent`
environment because Settings construction requires the field. A usable key is
provided only to `FLEET_ROLE=fleet_coordinator`/data-plane core environments.

The key must not appear in URLs, logs, routing receipts, refusal excerpts,
payloads, browser content, or qualification artifacts.

### FR-016 — Preserve live provider boundaries

`IBKR_BROKER_ENABLED=true`, `IBKR_READONLY=true`, and distinct Gateway client
IDs remain required on Clerk agents. The work must not introduce Alpaca market
data or Polygon bars into the live-decision path.

## 9. Interaction and accessibility requirements

1. Source, interval, timeframe, and timezone controls reflect correct
   `aria-selected` or `aria-pressed` values in the initiating click.
2. Expand changes its accessible name between `Expand market chart` and
   `Exit expanded market chart` immediately.
3. The history unavailable state uses `role="status"`; it does not steal focus.
4. Retry has an accessible name and visible keyboard focus.
5. Keyboard source-tab navigation and Escape-to-collapse continue to work.
6. Error text does not rely on color alone and meets WCAG AA contrast.
7. The last good live chart remains visible while history loads or fails.

## 10. Implementation decisions

### 10.1 Frontend failure boundary

- Project guarded `histChart` and `journalPage` values in their component
  classes or use an inline `hasValue()` guard.
- Add the static Broker V2 template contract from FR-001.
- Add a stable history presentation mapper at the shell/service boundary.
- Pass history unavailable state and retry through `TraderLensComponent` to
  `DualPaneChartComponent`.
- Keep `fullscreen`, `timeZone`, and `activePane` as component signals.
- Do not add manual `detectChanges()` calls. Removing the thrown render is the
  zoneless fix.
- Add no last-good cache and no `linkedSignal` holder. A failed attempt shows
  the unavailable state; Angular's own resource semantics are left alone.

### 10.2 Read identity versus command fencing

- Construct history resource parameters directly from
  broker/clerk/account/sid/timeframe primitives.
- Continue using `ResourceTarget`, `bindingGeneration`, and `routingEpoch` for
  command preparation and command dispatch.
- Do not let fleet-directory observation changes invalidate read data that is
  independent of those command fences.

### 10.3 Coordinator transport and provider

- Extend the existing authenticated `RemotePresence`/`/internal/fleet/*`
  transport family rather than add a standalone HTTP client pattern.
- A dedicated history client may share the internal transport helpers without
  expanding the `FleetPresence` lifecycle protocol if that keeps presence
  semantics narrow.
- Mount the new route only when the coordinator surface is installed.
- Use `build_internal_client()` with a bounded timeout and
  `enforce_private_http_target()` with the deployment-owned coordinator URL.
- Exclude `/api/aggregates/fetch` explicitly.
- Keep all direct Polygon SDK/HTTP usage and all backward-walk iterations in
  the coordinator role.

### 10.4 Provider result and public projection

- The complete-batch provider returns bars plus canonical notices.
- `build_history_chart` consumes an already-complete batch; it no longer owns
  remote range iteration.
- The Clerk still computes fill windows and markers using existing code.
- `ChartHistoryResponse.overlay_notices` is additive and defaults to an empty
  list for healthy results.
- Frontend presentation treats a no-bars response with an unavailable notice
  as unavailable, not as successful empty history.

### 10.5 Retry behavior

- History loads on Trader activation, selected-timeframe change, or route
  identity change.
- After a settled unavailable state it remains settled until explicit retry or
  a new FR-006 key.
- A directory generation/epoch refresh is inert.
- Live/current-run polling never reloads it.

## 11. Validation plan

### 11.1 Frontend resource-safety contract

Add a focused static test that scans Broker V2 templates and rejects an
unguarded resource `.value()` binding. It must fail against both current unsafe
sites and pass for the guarded bot-triage reference.

Add focused Operator-lens coverage proving a rejected `journalPage` still
renders health, run history, and actions and shows its existing audit error.

### 11.2 Frontend history-failure regression

Create a separate spec, for example
`bot-panel-shell.history-failure.spec.ts`; do not add the interaction sequence
to the 61 KB main shell spec.

With `getHistoryChart` rejected or returning an unavailable notice, assert:

- the Trader lens still renders the live market tape;
- Expand updates name, pressed state, and indicator-rail visibility;
- ET and Local update both buttons and persistence immediately;
- 5s/1m update the live-resolution pressed state;
- Delayed selects immediately and shows `Polygon history unavailable`;
- Retry issues exactly one history call;
- a directory generation/epoch refresh issues no new history call;
- a failed attempt leaves no earlier bars on screen; and
- no uncaught `ResourceValueError` is produced.

Use `userEvent`; assert rendered output and accessibility state, not private
signals. Do not require an out-of-band `fixture.detectChanges()` after each
click. Keep this spec shard-aware and prove the affected shard plus the full
`npm test` budget gate remain below 120 seconds.

### 11.3 Canonical notice tests

Parameterize the existing Polygon exception classes against the one canonical
helper and assert the stable codes already emitted by `live_chart_window`.
Run the same helper through live and history callers to prove they cannot drift.

Add the graceful-baseline regression: an empty direct key or known Polygon auth
failure returns a history response with no fabricated bars and the expected
notice instead of an unhandled `500`.

### 11.4 Coordinator operation and one-hop tests

Cover:

- correct token + matching Clerk header/body is accepted;
- missing/wrong token, missing identity, and mismatched header/body are refused
  with the existing flat fleet envelope;
- public/browser control secrets cannot authenticate the internal route;
- route is absent outside the coordinator surface;
- request validation accepts only the closed timeframe vocabulary, bounded
  required count, and integer millisecond anchor;
- a full history build makes exactly one Clerk → coordinator request while a
  fake Polygon source requires multiple widening iterations;
- widening stops at the requested count or two-year floor;
- ms→ET date conversion, completed bars, half-days, and daily close retain the
  current behavior;
- timeout and transport failure become `coordinator_unavailable`;
- malformed responses cannot leak raw body or topology;
- no key/token appears in logs or payloads; and
- returned bars remain sorted and use `int64 ms UTC`.

### 11.5 Clerk assembly tests

Cover:

- Clerk runtime has a present-but-empty Polygon key;
- production Clerk history assembly uses the complete-batch coordinator
  provider and never calls Polygon directly;
- exact symbol, timeframe, required count, and `as_of_ms` reach the provider;
- SQLite fill evidence remains account-scoped and is combined only in Clerk;
- canonical notices flow into `ChartHistoryResponse.overlay_notices`; and
- a healthy result preserves the existing bars, indicator budget, marker, and
  timestamp semantics.

### 11.6 Fleet topology tests

Inspect services by `FLEET_ROLE`, not by service name. This is required because
production names the role `fleet-coordinator` while dev uses `python-service`
with `FLEET_ROLE=fleet_coordinator`.

Assert across production, dev, and qualification Compose renders:

- every `FLEET_ROLE=clerk_agent` has `POLYGON_API_KEY` present and equal to
  `""`;
- every production/dev `FLEET_ROLE=fleet_coordinator` receives the data-plane
  key configuration;
- qualification may use its recorded provider despite its non-working
  placeholder key;
- Clerks retain IBKR read-only market-data settings; and
- the internal operation is reachable only on the private deployment network
  with the existing agent service-token mapping.

### 11.7 Qualification recorded provider — tracked follow-up, not a gate

The Compose qualification environment cannot use
`qualification-polygon-placeholder` to prove a vendor success, so a
qualification-only recorded history provider — injected at the coordinator's
complete-batch seam under the existing random qualification namespace gate —
is the only way to prove the success path deterministically.

**Owner decision, 2026-09-18: this does not block delivery.** It is filed as its
own follow-up issue and is not an acceptance criterion for slices A–D. The
success path is proven before merge by a hand-run smoke test against a real
coordinator Polygon key, recorded in the pull-request description with the
timeframe exercised and the resulting bar count.

**Accepted residual risk, stated plainly:** slice D changes where bars come from
with no deterministic, credential-free proof that history works. A regression
after that merge would be caught by a person, not by CI. The follow-up closes
this gap and should be scheduled rather than left open indefinitely.

When that follow-up lands, its harness must prove:

1. healthy recorded-provider history returns candles through the full
   browser → coordinator → Clerk → coordinator path;
2. injected unavailable/timeout mode produces the expected settled notice;
3. recovery followed by explicit Retry renders candles; and
4. the Paper lane's one-slot request pool completes without starvation —
   which matters more now that one hop may hold a slot for the widened FR-010
   timeout.

The fixture must contain deterministic native bars sufficient to exercise the
display + warmup path; it must not perform frontend or ad-hoc resampling.

### 11.8 Contract and security gates

Contract regeneration is mandatory in the implementation change:

- regenerate and commit
  `contracts/openapi/python-data-service.openapi.json`;
- regenerate and commit the derived frontend API types;
- run `export_openapi_contract.py --check`; and
- run the Broker V2 vocabulary/operation catalog checks to prove the internal
  route did not accidentally become a public provider operation.

The new `/internal/fleet/*` route is deliberately excluded from public OpenAPI
and from `contracts/data-plane-control-surfaces.json`: that manifest's schema is
for browser `/api/*` prefixes and would attach the wrong
`X-Data-Plane-Control-Secret` policy. Instead, add a direct security contract
beside `test_data_plane_control_security.py` proving the route is
coordinator-role-only and guarded by `X-Fleet-Clerk-Id` +
`X-Fleet-Agent-Token`. Also assert the internal prefix remains absent from the
browser manifest.

### 11.9 Browser regression loop

Against a running fleet with coordinator history deliberately unavailable:

1. Open a bot detail in the Trader lens.
2. Click Expand and assert expanded state immediately.
3. Click ET and Local and assert each pressed state immediately.
4. Toggle 5s/1m and Live/Delayed.
5. Select every Polygon timeframe.
6. Exercise indicator pane/category filters and disclosures.
7. Open and close the trader action menu without executing an action.
8. Assert the delayed-pane unavailable state and explicit Retry.
9. Assert no `ResourceValueError` or uncaught exception in the browser console.
10. Return to Live and verify the IBKR chart remains populated.
11. Switch to Operator, open audit trail under a forced journal failure, and
    prove the rest of the lens remains responsive.

Repeat the history portion against a coordinator holding a real Polygon key.
Verify that one internal request returns Polygon-labeled candles, that an
explicit retry recovers from the injected failure, and that a cold `1D` request
completes inside the FR-010 bound. Record the observed wall time — it is the
evidence that the chosen bound is large enough.

## 12. Acceptance criteria

1. With history unavailable, Expand and Local/ET update in the initiating click
   and remain correct without another interaction.
2. All controls in FR-004 meet the same immediate-feedback requirement.
3. The live IBKR chart remains populated and interactive throughout history
   failure.
4. The delayed pane shows a specific unavailable state and Retry; it never
   spins after a settled error.
5. Retry issues exactly one request and can recover without a page reload.
6. Browser console contains no `ResourceValueError` from the bot panel.
7. An errored Operator journal cannot freeze the Operator lens.
8. A fleet directory generation/epoch refresh issues no new history request.
9. A failed history attempt leaves no earlier bars on screen.
10. One public history attempt causes exactly one Clerk → coordinator history
    call, even when the coordinator performs multiple Polygon fetches.
11. Known Polygon failures use the canonical notice codes and never become an
    unhandled `500`.
12. Coordinator timeout becomes `coordinator_unavailable` within the bounded
    timeout and leaves no indefinite spinner, with the outer
    coordinator → Clerk bound strictly larger than the inner one.
13. Clerk logs contain no direct Polygon request from production history
    handling.
14. Every Clerk role has `POLYGON_API_KEY` present and empty; coordinator roles
    own the usable configuration.
15. Healthy history preserves the existing public candle, indicator-budget,
    fill-marker, timeframe, and millisecond timestamp semantics, with only the
    additive notice list.
16. A hand-run smoke test against a real coordinator Polygon key returns
    candles, and its timeframe, bar count, and wall time are recorded in the
    pull-request description. Deterministic recorded-provider qualification is
    a tracked follow-up (§11.7), not a gate.
17. OpenAPI, generated frontend types, security contracts, targeted suites, and
    the two-minute frontend/Python gates pass.
18. Existing IBKR live-feed continuity, Alpaca order/execution behavior, and
    custody tests remain unchanged and green.

## 13. Delivery slices

### Slice A — Frontend and Operator failure isolation

- Guard both known resource-value reads.
- Add the Broker V2 static resource-safety contract, scoped to a declared
  resource-identifier list.
- Introduce explicit history unavailable presentation and Retry.
- Remove generation/epoch from history resource parameters.
- Add focused history-failure and Operator journal-failure specs outside the
  large main shell spec.

This slice removes the user-facing freeze independently of backend delivery.

### Slice B — Graceful backend baseline

- Reuse the canonical live-chart Polygon notice mapping in current history.
- Turn an empty key and known provider exceptions into an unavailable history
  response rather than an unhandled `500`.
- Regenerate the public contract for additive history notices.

This is a rollback-safe failure fix, not working historical data in the fleet.

### Slice C — One-hop coordinator history batch

- Add the authenticated internal fleet operation and typed models.
- Move the full backward walk to the coordinator.
- Refactor the `HistoryBarSource` seam to a complete-batch provider.
- Add the bounded Clerk client using existing hardened internal transport, and
  widen the outer coordinator → Clerk delivery bound for this route.
- Add auth, timeout, temporal, one-hop, failure, and redaction tests.

### Slice D — Clerk assembly migration

- Replace the direct Polygon closure in `panel_chart_data_source.py` with the
  complete-batch coordinator provider.
- Preserve SQLite fill evidence and the public projection.
- Delete the direct Polygon fetch outright. The FR-011 classification helper
  stays — FR-010 needs it for `coordinator_unavailable` — but retaining a
  second production path to the same vendor would be an abstraction-boundary
  leak, not a safety net.

### Slice E — Cross-stack verification

- Run the browser control loop and console assertion in both failed and healthy
  history states.
- Run the hand-run success smoke test against a real coordinator key and record
  timeframe, bar count, and wall time.
- Regenerate OpenAPI/frontend types and run security/topology gates.
- Prove affected test shards stay within 120 seconds.

### Follow-up (not in this PRD's acceptance) — qualification recorded provider

Per §11.7, file separately: the recorded complete-batch provider and the
four-part qualification harness that proves success, failure, recovery, and
pool health without a real credential.

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Frontend isolation masks the ownership violation | Slice A is independently releasable, but coordinator migration remains an acceptance criterion. |
| Per-range remote calls create re-entrant pool starvation | Coordinator owns the complete backward walk; exactly one internal request is asserted. |
| Coordinator begins owning custody-derived computation | Internal response contains bars/notices only; Clerk reads and combines fills. |
| A cold `1D` walk exceeds the 10-second fleet default | This route carries its own larger explicit bound on both hops; the smoke test records observed wall time as evidence the bound fits. |
| Only the inner hop is widened, so the outer one expires first | Outer coordinator → Clerk bound must be strictly larger than the inner bound, pinned by a test. |
| Timeout turns into a permanent spinner | The bounded timeout maps to a settled `coordinator_unavailable` notice. |
| Polygon key or fleet token leaks through logging | Hardened client plus explicit URL/log/payload/redaction tests. |
| Directory refresh reloads errored history | History resource key excludes binding generation, routing epoch, and the target object. |
| A second error taxonomy drifts from live behavior | `live_chart_window` remains canonical; both callers are parameterized against one helper. |
| Extracting the notice mapping regresses the live chart | The existing live-chart notice tests must pass unchanged in the same change. |
| Slice D ships with no credential-free proof that history works | Accepted (§11.7). Hand-run smoke test before merge; recorded-provider follow-up filed and scheduled. |
| Repeat timeframe selection re-walks Polygon | Unchanged from today's behavior; coordinator caching deferred by owner decision. |
| Internal route ships with browser or no authentication | Reuse agent-token auth and add a direct role/auth security contract; keep it outside public manifests. |
| Frontend regression breaches the shard budget | Use separate focused specs, targeted shard measurement, and the hard 120-second gate. |
| Provider migration affects live decisions | IBKR boundary and topology/behavior regressions remain mandatory. |

## 15. Documentation and authority impact

No new ADR is required. The justification is not that ADR 0062 describes the
coordinator as an execution authority—it explicitly does not. The justification
is the deployed data-plane topology:

- in `compose.fleet.yaml`, Backend's `PolygonService__BaseUrl` already points to
  `http://fleet-coordinator:8000`; and
- the old combined `python-service` is under the `legacy-combined` profile and
  is not started in a fleet deployment.

Therefore the fleet-coordinator role already owns the data-plane/Polygon
surface. This work adds a narrow authenticated agent consumer of that existing
market-data role; it does not give the coordinator broker execution or custody
authority.

Other documentation impacts:

- No glossary change is required; no new domain term is introduced.
- Update `docs/known-gaps.md` when implementation begins so the open defect has
  one canonical tracking entry, then remove it when acceptance is proven.
- Update API contract/generated types for the additive history notice field.
- If implementation cannot keep the complete walk in one coordinator call, or
  would require moving custody/math authority, stop and request an explicit
  architecture decision. Do not put the Polygon key in a Clerk.

## 16. Evidence and relevant seams

- Unsafe history read:
  `Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.html`
- History resource currently keyed by fenced target:
  `Frontend/src/app/components/broker/v2-panel/panel-shell/bot-panel-shell.component.ts`
- Unsafe Operator journal read:
  `Frontend/src/app/components/broker/v2-panel/operator-lens/operator-lens.component.html`
- Correct guarded resource reference:
  `Frontend/src/app/components/broker/v2-panel/bot-triage-detail/bot-triage-detail.component.html`
- Current direct Polygon closure:
  `PythonDataService/app/services/broker_v2_panel/panel_chart_data_source.py`
- Existing history planner/backward walk and injection seam:
  `PythonDataService/app/services/broker_v2_panel/chart_projection_service.py`
- Canonical Polygon notice taxonomy:
  `PythonDataService/app/services/live_chart_window.py`
- Existing agent → coordinator transport:
  `PythonDataService/app/broker/fleet/presence.py`
- Hardened internal HTTP posture:
  `PythonDataService/app/broker/fleet/internal_http.py`
- Existing authenticated internal route family:
  `PythonDataService/app/routers/internal_fleet.py`
- Role-scoped router mounts:
  `PythonDataService/app/main.py`
- Present-but-empty Clerk credential and production role topology:
  `compose.fleet.yaml`
- Dev coordinator role on `python-service`:
  `compose.fleet.dev.yaml`
- Qualification placeholder and constrained request pool:
  `compose.fleet.qualification.yaml`
- Public contract and security surface manifests:
  `contracts/openapi/python-data-service.openapi.json`,
  `contracts/data-plane-control-surfaces.json`
- Governing fleet and provider decision:
  `docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md`
