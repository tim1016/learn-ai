# Clean-up — Frontend broker workspace (#3158)

Read at `8784061c` (origin/master, 2026-10-14). Module: `Frontend/src/app/components/broker/`, 370 files, 2.4 MB, split into nine slices by sub-package (never by lens). `components/brokers/` (plural), `Frontend/src/app/api/` and the shell are not in this ticket; hits there are pointers.

## Result in one screen

- **Module:** `Frontend/src/app/components/broker/`, 370 files, 87 components/directives (3 routed, 84 rendered by a parent template; the "rendered nowhere" list is empty). Nine readers, one per sub-package, each read its files fully (370 of 370).
- **Features traced:** 174 rows across the nine slices. By classification: implemented 135; partial 18; missing 3; unverified 17; dead mode 1.
- **P0:** none.
- **P1:** three new, each reproduced or traced to the line, filed at once — [#3178](https://github.com/tim1016/learn-ai/issues/3178) (the safe-flatten plan card says "No order has been submitted" after an extended-hours send whose outcome is unknown; reproduced), [#3179](https://github.com/tim1016/learn-ai/issues/3179) (Deploy's status read frees the submission key on any 404, including the fleet's `clerk_not_found`; reproduced), [#3180](https://github.com/tim1016/learn-ai/issues/3180) (a failed refresh of Order records leaves "History is current"; traced, store and template read). Two earlier P1s sit in this module and were confirmed from the Frontend side: [#3164](https://github.com/tim1016/learn-ai/issues/3164) (header "IBKR market data live") and [#3165](https://github.com/tim1016/learn-ai/issues/3165) (the "Last check against Alpaca" row does not age — the Frontend renders it generically; the fix is backend).
- **Cut:** [#3181](https://github.com/tim1016/learn-ai/pull/3181) (branch `chore/3158-frontend-broker-workspace-dead-machinery`) — 24 files, 37 insertions (re-indentation), 715 deletions, no behaviour change; ESLint and `tsc` clean; 385 tests in the 12 affected spec files pass; one independent review.
- **Not cut, on purpose:** everything in "Left for handoff" below. The module was already lean at the grep level: zero `eslint-disable`, `@ts-ignore`, `any`, `console.*` or `TODO` in non-spec code; the only `new Date(` sites are one local-calendar-day filter (not a trading date) and a test fixture.

### The findings an owner should read first

| # | finding | where | why it matters | disposition |
|---|---|---|---|---|
| 1 | Both options pages (Options Chain, Options Surface) can never leave "Broker is disconnected": nothing calls `BrokerHealthService.start()` since #2167 removed the shell's poll; the pages gate their whole body on it. They are in the app menu. The 2026-09-16 decision retained IBKR market data, and the route is back. No spec renders either page, which is why it went unseen. Latent behind it: an expirations retry storm (H-F2) and nine more P2s | H-F1, H-F2..F10 | a retained, read-only IBKR surface that has been unusable for a month | owner-ruling: restore (one-line poll start + a render spec, then fix the P2s) or retire; **do not cut without the owner** |
| 2 | Historical exact-execution recovery's confirm re-sends the prepare step's idempotency key to a different operation; the fleet ledger refuses it (`ClerkRoutingAttemptConflict`), so recovery cannot complete from this UI. Fails closed | C-F1 | a custody recovery path that cannot finish; owner may rate P1 | fix: `commandTarget(draft.target)` at confirm; the spec at :1058-1059 pins the reuse and must change |
| 3 | The bot page does not mount (Stop, Flatten and Settle included) until `GET /api/brokers/{broker}/panel-profile` answers, though the profile feeds only a rail that accepts null; no retry | C-F2 = D-F2 | an unrelated read can hide the money controls | fix with the `profile` cut chain (CC5/CC11) |
| 4 | Option endings (assigned / exercised / expired) are served on every Activity row and no Frontend code reads them | G-F04 | ADR 0076 #22; the row shows "No request details recorded" | owner-ruling: build or record |
| 5 | A Deploy answered for account A while the owner has switched to B writes into B's form (reproduced: B's submission key rotated, `receipt` holds A's); if B held an unsettled key it is lost | F1-2 | idempotency across accounts | fix (not delete-only) |
| 6 | `run_fills=()` and `orders_sent=0` shown as facts beside "this bot's latest run record could not be read" | A-F5 | unavailable shown as zero | fix backend (`null`) then a branch here |
| 7 | The custody timeline calls A1 "Awaiting" though no producer writes it; A0 "Clerk durably accepted the instruction" is shown for externally observed orders | G-F02, G-F05 | custody belief shown beyond the evidence | fix |
| 8 | Wall (Home `?view=wall`): `status`, bootstrap errors and invalid frames are never surfaced; a dead feed draws last candles unmarked | D-F3 | the machinery #2708's addendum kept has no reader | owner-ruling |

## What the cut removed

All lens 2; every piece re-searched by the driver across `Frontend/src`, `tests`, `scripts`, `eslint-rules` and templates before deletion.

Taken from the slice tables below: A (`activated`; the health-groups duplicate), C (C1, C3–C6, C9), D (CC1–CC4, CC7), E (`.quote-line > strong`), F2 (six fixtures, `accountId`), G (C1, C3), H (surface spec describe, `setupLoading`, `.error`, `.disabled-banner a`). Everything else marked certain, probable or conditional in those tables was not cut; "Left for handoff" says why for the ones that matter. One cascade the review named: `BOT_COCKPIT_RECONCILE_ANCHOR` and `movesForBlocker` in `Frontend/src/app/api/operator-blocker.types.ts` are now consumed only by `operator-blocker.contract.spec.ts` (`api/` is outside this ticket).

| piece | evidence | kill-list |
|---|---|---|
| `PanelActionButton`'s retired blocker-cure and prose-suppression modes: `suppressedBlockerId`, `suppressedBlockerReasonCode`, `moveIsSupported`, `moveRequested`, `requestMove`, `isSelfDispatchable`, the `Router` injection, the `'primary'` tone and their template/SCSS; the toolbar's `noMoves = () => false`; the seven specs that proved only that behaviour | the only host is `bot-toolbar.component.html`, which binds `noMoves` and neither suppressed input; `moveRequested` has no listener; the backend tone is `danger \| warning \| neutral`; the hosts were removed in #2794 | family of #2708 "two modes that no host turns on" |
| `BotOrderRecordsComponent.activated` + its `@if` and gate + host `[activated]="true"` | bound only to a constant since #2794 | — |
| `bot-panel-shell`: the never-read `queryParams` signal, the `route` it alone used, two imports | `rg` → declaration only | — |
| `DeployPaperAccessComponent.accountId` + host binding + ten spec bindings | never read in its ts or html (two readers found it independently) | — |
| six unused exports in `option-deploy.fixtures.ts` (275 lines) | one hit each, the declaration | — |
| four unused type aliases `ChannelState`, `ChannelHealthView`, `ClerkCard`, `ReadinessCheckView` | declaration only | #2708 "14 unused type aliases" (four missed by #2754) |
| `PREVIEW_ASK_AGAIN_FLOOR_MS` | declaration only | — |
| `broker-options-chain`: write-only `setupLoading`; SCSS `.error`, `.disabled-banner a` | no reader / no markup | — |
| SCSS that matches no markup: `.raw-receipt`/`.event-receipt` (drawer), `.run-history__heading`/`__tabs` (run history), `.quote-line > strong` (tape), `.panel-action__blocker/__moves/__move` | no matching markup in the owning template | — |
| tests: broker-options-surface "projected line-count cap budget" (mock theater: the lambda is declared in the test); bot-health-groups "splits health into this run…" (the shell spec's same-named test asserts the three headings and the feed line) | read both sides | #2732 child/parent duplicates |

## Left for handoff

**Cut candidates not taken** (each needs a ruling, a re-pointed test, or another module's cut):
- Gallery `resolution()`, `refusalReason()` and the last-price-tag renderer path (CC8–CC10): #2708 rows kept by #2754 "for surviving tests"; the addendum names only `bots()`/`status()`. Renderer cut needs a fill-count re-base (needs a run).
- `profile` chain — `TransactionRail.profile` input, shell `profile` resource, `getPanelProfile`, `PanelProfile`, six shell specs, three e2e stubs, backend `fleet_compatibility_reads.py:21` (CC5/CC11): removing the mount gate changes behaviour; owner ruling.
- `BotEndCard.showChange` + its button + `.bot-end__change`: production Change end is the toolbar; ten-plus money-path specs click the card's own button, so a cut re-points them.
- `showScopeControl` mode of the transaction history (C2): a mode no host turns on, strands `getPortfolioHistory` and the `portfolio_history_read` operation; the kill-list precedent says cut, a re-pointed spec says a separate PR.
- Custody drawer epoch rows, the four never-written clocks and the A1 stage (C5): no producer in `PythonDataService/app`; waits on the Python schema.
- Legacy toolbar for a panel with no `bot_page`, `feedContinuityFor`/`FEED_CONTINUITY_NOT_RECORDED`, `presentFeed`'s five never-emitted cases: conditional on every lane running a current build (the family of #2708 rows 21-22).
- Eight child/host duplicate Deploy specs added after the kill lists, the FLATTEN-constant pin, the source-tab-label pin: money-adjacent, "when unsure, keep".
- `diffBps` and its pre-fix assertion (#2708 row 11): kept by the addendum unless the assertion is dropped.
- Instrument-quote unused view fields and `sourceLabel` input; the rail's unreachable empty branch; board hook classes; inert cohort-outcome class names.

**Fix slices (one PR each, none delete-only):** (1) options pages restore (H-F1 + F2..F10 + a render spec); (2) historical recovery confirm key (C-F1); (3) Deploy error-shape drift — flat fleet refusals read as nested (F1-3, R-2) with the status-read 404 (needs a backend typed code, #3179); (4) cross-account answer write (F1-2, F1-6); (5) failed launch receipt tone (R-1); (6) Order records failure rendering (#3180, A-F7/D-F8, A-F8 option-buying-power retry); (7) flatten ticket (E-F2, E-F3, E-F4, E-F8, #3178); (8) custody drawer honesty (G-F02, G-F05, G-F06, G-F03 figures past `figures_good_until_ms`); (9) live-store lifecycle (D-F1 rail overwrite, D-F4 SSE `end`, D-F5 restart identity, D-F6 timeouts); (10) strategy view after the close (B-F1, B-F2).

**Waits-on rows:** the `profile` chain waits on slices A/C of this module and a backend alias cut (#2706 family); `portfolio_history_read` and the fleet catalog snapshots wait on C2; the `TransactionFeedState` union (C6) and the custody timeline fields (C5) wait on the Python schema; `components/brokers/` (plural) holds the hosts of `account-desk-transaction-history`, `account-picture`, `alpaca-deploy-workflow`, `fee-attribution`, `cohort-flatten-drawer` and the home sparkline's `showLastPriceTag: false` (later Frontend wave).

**Owner rulings (all slices):** options pages restore or retire; C-F1 rating; option endings in Activity; the `profile` gate; which accessibility specs go under the 2026-10-12 ruling (about 20 `axe.run` and focus tests across the module; `.claude/rules/angular.md` still says "All UI must pass AXE"); a restored Live selection on the Deploy draft; failed launch receipt now or later; Pause semantics on the options pages; whether `known-gaps.md` F12 (closed by #2355/#2401) leaves the open list and F5's half-sentence is corrected.

## Method and limits

- Pinned `8784061c`; no open PR touched the module; the kill lists #2708/#2732/#2733/#2734 and the #2703/#2704/#2724/#2725/#2727 comments (with the ◇ addenda) were handed to every reader; each row was re-checked against master.
- Readers could not write report files; their reports came back inline and are condensed below. Every cut was re-verified by the driver; the three P1 filings were re-derived by the driver (two reproduced with throwaway specs that were not committed, one traced through the store and template).
- Nothing was deployed or run against a live machine. Findings marked "needs-a-run" or "traced by reading" say so. The #2708 rows 21-22 restart confirmation cannot be re-verified from the tree.
- Accessibility findings are out of scope (owner, 2026-10-12); the rows that mention `axe.run` are owner rulings about whether the tests stay.

---

# Slice reports

## Slice A — `v2-panel/bot-page/` (54 files, all read)

19 features: 14 implemented, 2 partial, 3 unverified. 4 cut candidates. 16 finding rows (2 P1 already filed: #3164, #3165). No new P0/P1.

### Feature table

| # | feature (requirement) | entry point → reachable | normal / empty / failure | classification: evidence |
|---|---|---|---|---|
| 1 | Banner: bot-owned status pill + run summary, no LIVE chip (#2794 R1/R3/R9) | `app-bot-page-header`, shell html:39 → yes | no `bot_page` → no pill | implemented: `bot-panel-shell.component.spec.ts:2392` |
| 2 | Banner line "IBKR market data live · updated HH:MM:SS" (#2794) | header html:29-33 → yes | says "live" whatever the feed does | **partial (P1 #3164)**: no spec asserts the line |
| 3 | Banner figures: Open P&L, Held, Dry Run chip (#2794 R1) | html:42-63 → yes | None / "No price" | unverified: no unit spec |
| 4 | Action toolbar: Bot+Fix on bar, Inspect under More, blocked disabled with reason, primary filled (#2794 R2/R6) | `app-bot-toolbar` → yes | unknown id inert | implemented: `bot-toolbar.component.spec.ts`; e2e `account-owner-walk-through.spec.ts:552` |
| 5 | More menu: All actions + Labels (localStorage, blocked-safe) | More button → yes | storage blocked → page-only | implemented: toolbar spec |
| 6 | Toolbar for a panel with no `bot_page` (commit 1628459c) | `legacyToolbar` → only vs a pre-#2794 data plane | all `enabled=false` → blocked | unverified (conditional) |
| 7 | Page notices: attention reason, non-trading, cleared + History link (#2607, #2574) | shell html:55 → yes | each only when it applies | implemented (e2e only): `account-owner-walk-through.spec.ts:164,416` |
| 8 | End card + Change end editor via toolbar (#2607, #2663, #2858) | shell html:195 → yes | refusal in fields or on card | implemented: `bot-end-card.component.spec.ts` (20) |
| 9 | Health: two groups + market pulse + startup join + duty outcome (#2794 R9/R5) | shell html:229 → only if `bot_page` | line rendering not unit-asserted | implemented: `bot-health-groups.component.spec.ts` (5) |
| 10 | Alpaca option buying power card (ADR 0076 d26, #3098) | shell html:152 → traded option | error → alert; no re-read after a failed read (F8) | implemented: `bot-option-buying-power.component.spec.ts` (13) |
| 11 | Order records: working orders, order path, audit trail (#2794 R4, #2563) | shell html:166 → yes | alert + "No journal entries" together (F7) | partial, no unit spec: `bot-panel-shell.correlation.spec.ts:260-285` counts reads |
| 12 | Setup: strategy, settings, exit terms, build, proof hashes (#2794 R5) | shell html:179 → yes | "Not set", "—" | unverified: no spec |
| 13 | Setup: untested traded-asset warning + acceptance (#2912) | same → yes | none for an ordinary bot | implemented: `bot-setup.component.spec.ts` (3) |
| 14 | Stranded warning + one confirmed Flatten… sequence (#2560 D2, #2839) | `runFlattenSequence` shell:1158 → stopped and holding | plan mismatch sends nothing | implemented (money path): `flatten-sequence.spec.ts` (11), shell spec:1000-1004,1155,1303,2543 |
| 15 | Extended-hours quote refresh read (#2007) | shell:1288 → yes | ended with the Clerk's reason | implemented: `flatten-sequence.spec.ts` |
| 16 | Settle uncertain closing order ticket (#2854) | shell html:71 → yes | refusal as written | implemented (money path): `settle-order-ticket.component.spec.ts` (16) |
| 17 | Fills this run (dated, newest 4, drawer) + fill table (#2794 R8) | shell html:218 → unless `log_only` | empty "No fills this run." (F5, F9) | implemented: `run-fills-list.component.spec.ts`, `fill-table.component.spec.ts` |
| 18 | Recent decisions: chips, expand, row↔candle link (#2639, #2912, #2915) | shell html:207 → yes | "values not recorded" | implemented: `recent-decisions-list.component.spec.ts` (10) |
| 19 | Tape wrapper (#2794 R7) | shell html:120 → yes | all inputs/outputs bound | implemented (thin) |

### Cut candidates

| path::symbol | evidence | orphans | confidence |
|---|---|---|---|
| `bot-order-records.component.ts::activated` + `@if (activated())` (.html:43) + `params` gate (.ts:64-75) + host `[activated]="true"` (shell html:173) | bound only to constant `true` since #2794 (20431723) | stale comment `bot-panel-shell.correlation.spec.ts:267` | certain |
| `bot-end-card.component.ts::showChange` + button (.html:4-6) + `.bot-end__change` (.scss:24) + host `[showChange]="false"` (shell html:200) | production "Change end" is the toolbar → `endCard()?.openEditor()`; no spec sets it | `bot-end-card.component.spec.ts` helper `openEditor` (l.69-73) clicks the dead button (19 tests), l.106/l.205 assert it | probable (needs a run) |
| `bot-health-groups.component.spec.ts` "splits health into this run and the account right now" (l.52-57) | duplicate of `bot-panel-shell.component.spec.ts:2400-2408` (same name, strictly more) | none | certain |
| conditional: `bot-toolbar.component.ts::legacyToolbar`, `LEGACY_GROUPS`, `isToolbarActionId`, spec l.285-293; shell `run_fills ?? []`, header html:16 | `panel_data_source.py:613` always sets `bot_page` | three helpers, one spec | conditional: confirm every data plane serves `bot_page` |

### Kept on purpose

`BotToolbarActionTemplateDirective` (scan false positive: used at `bot-toolbar.component.html:2`, exists for `ngTemplateContextGuard`); `FlattenSequenceDeps` (parameter type); settle-ticket exports (all imported by the shell); public methods the shell calls (`focusAction`, `openEditor`, `openConfirm`, `focus`); `showAsset`/`showRefusalSentences` (opt-in per #2912/#2915); documented catch blocks `bot-toolbar.component.ts:150-157,334-341`; all money-path specs.

### Finding rows

| id / path::symbol | lens | severity | evidence | disposition |
|---|---|---|---|---|
| A-F1 `bot-page-header.component.html:29-33` | 3 | **P1 filed #3164** | prints "IBKR market data live · updated" from `panel.updated_at_ms`, never reads `market_pulse.feed_state`; the only reader bypassing `feedContinuityFor()` | pointer |
| A-F2 `bot-page-header.component.scss:82-95` | 3 | P2 | running-pill green dot comment claims "The feed is live while the bot runs" | fix with A-F1 |
| A-F3 `bot-health-groups.component.html:6-18`; backend `bot_page_projection.py:640-649` | 3 | **P1 filed #3165** | "Last check against Alpaca" renders `state=ok`/"Clean" with an absolute time and no age test; channel lines in the same card do age (`panel_projection_service.py:176`) | pointer; fix is backend |
| A-F4 `bot-health-groups.component.html:6-18` ← `feed_continuity_projection.py:134-152` | 3 | P2 | run line "IBKR market data feed – Continuous" is a green dot from recorded interruptions only | leave-with-reason |
| A-F5 `run-fills-list.component.html:23`; `panel_data_source.py:485,670` | 3 | P2 | unreadable run → `run_fills=()`, `orders_sent=0`: the page prints "No fills this run." and "Orders sent - 0 orders" (ok) beside the notice that the run record could not be read | fix backend (`run_fills: null`) then a branch here |
| A-F6 `bot-toolbar.component.ts:120-148` | 3 | P2 (pre-`bot_page` data plane only) | legacy mapping reads `enabled=false` as `blocked`, ignoring `needed` | leave or cut with candidate 4 |
| A-F7 `bot-order-records.component.html:42-49` + `operator-lens/journal-tail.component.html:4,36-37` | 3 | P2 | failed audit-trail read renders the alert and "0 of 0 events" / "No journal entries." | fix |
| A-F8 `bot-option-buying-power.component.ts:106-111` | 5 | P2/robustness | effect reloads only `if hasValue()`; after a failed read it never re-reads and has no Retry | fix |
| A-F9 `fill-table.component.html:12` | 5 | robustness | `track fill.order_ref`; partial fills of one order share it (types doc says key on `event_key`) | fix |
| A-F10 `tests/e2e/account-owner-walk-through.spec.ts:293` | 3/6 | P2 (cannot fire) | asserts `Change end` button count 0; the card never draws it | fix or drop |
| A-F11 `bot-health-groups.component.spec.ts` | 6 | test gap | no test renders a health line; all 6 pass with the `@for` block deleted — why no spec saw A-F3/A-F4 | owner-ruling |
| A-F12 `flatten-sequence.spec.ts:161` | 6 | hygiene | 0 ms real `setTimeout` between two `expectOne` calls | leave |
| A-F13 `bot-day-chart.component.ts:26`, `bot-page-header.component.ts:27`, `stranded-position-warning.component.ts:27`, `flatten-sequence.ts:163` | 7 | hygiene | cite "hurdle H15/H23/H30-H32", a list never filed as an issue or doc | fix (cite #2560 or drop) |
| A-F14 `recent-decisions-list.component.spec.ts:83-87` | 6 | hygiene | tombstone `queryByText('Simulation')` inside a mixed test | leave (whole tests only) |
| A-F15 `bot-day-chart.component.html:1` | 7 | hygiene (a11y; out of scope) | — | none |
| A-F16 `flatten-sequence.ts:221` | 7 | hygiene | `export` on a type used only in-file | leave |

### Owner rulings

Candidate 4 (legacy toolbar and `bot_page` null guards): is every data plane on a build that serves `bot_page`? #3165: what age makes "Last check against Alpaca" stale? #3164: drop "live" or show `market_pulse.latest_bar_at_ms`? Two axe tests (`bot-end-card.component.spec.ts:408`, `settle-order-ticket.component.spec.ts:400`) and three settle-ticket focus tests under the 2026-10-12 ruling.


## Slice B — `v2-panel/strategy-view/` (46 files, all read)

16 features, all implemented. 0 cut candidates. P0 0, P1 0, P2 1, robustness 4, hygiene 9. Lens 4 clean: zero ban-list hits in the slice; every chart time is `int64 ms UTC` from the backend, converted once to chart seconds for lightweight-charts and never persisted.

### Feature table

| feature | requirement | entry point | classification | evidence |
|---|---|---|---|---|
| Strategy tab: decision candles shaded by the active gate | #2794 R7, US 31/36; #2639 | `bot-panel-shell.component.html:108` → `bot-chart-panel` → strategy-view → strategy-chart | implemented | `strategy-chart.component.spec.ts` "fills each candle from the active gate's recorded result…", "re-shades the same candles…"; `bot-chart-panel.component.spec.ts` empty / failure+Retry |
| Strategy / Tape tabs, tape created lazily | #2794 R7, US 32 | `bot-chart-panel.component.html:4-29,65-95` | implemented | `bot-chart-panel.component.spec.ts` "opens on the strategy's decision candles and creates the tape only when its tab opens" |
| Gate chip + picker, remembered per strategy, fallback to default | US 37-38; #2639 D8-D10 | `strategy-gate-picker.component.html` | implemented | `bot-chart-panel.component.spec.ts` (switches shading and remembers; storage blocked; deleted gate → default) |
| Custom gate editor: new/edit/delete (two-step), preview, save | US 39 | `strategy-gate-picker.component.html:10-21,72`, `strategy-gates-state.ts` | implemented | `bot-chart-panel.custom-gates.spec.ts` (preview, stale-draft guard, save, refusal in the data plane's words, edit/delete, list failure) |
| Indicators chip: strategy values always on + searchable catalogue | US 40; #2639 D12; #2800 | `strategy-indicators.component.html`, `strategy-catalogue-indicators.ts` | implemented | custom-gates spec (draws on lead-in, survives re-read, never on another strategy's candles) |
| Legend | US 36 | `strategy-view.component.html:27-49` | implemented | `bot-chart-panel.component.spec.ts` (swatch colours not asserted) |
| Before-start / after-end shade, "Bot started" / "Ended" / "Ends" lines | US 41 | `strategy-chart.component.ts:140-168`, `strategy-chart-overlay.ts` | implemented | `strategy-chart.component.spec.ts`, `strategy-chart-overlay.spec.ts` |
| Running bot: "Now" line, forming outline, next decision | US 47 | `chart-lanes.ts:69-78` | implemented (+ P2 F1) | `strategy-chart.component.spec.ts`, `chart-lanes.spec.ts` "formingBar (#2794)" |
| Lanes under the chart (Decisions, Orders, Market data, traded asset, Bot) | R4, US 34-35; #2915 | `chart-lanes.ts`, `chart-lanes.component.*` | implemented | `chart-lanes.spec.ts`, `strategy-chart.component.spec.ts` |
| Same lanes under the tape on its Local/ET clock | #2808 | `dual-pane-chart.component.ts:59-61,475` (slice E) | implemented | `chart-lanes.spec.ts` "chartLanes on the tape's ET setting" |
| Candle click popover | US 45 | `strategy-view.component.html:60-67` | implemented | `bot-chart-panel.component.spec.ts` |
| Linked selection chart ↔ decisions list | US 46 | `selectedBarCloseMs` model | implemented | `bot-panel-shell.strategy-view.spec.ts`, `recent-decisions-list.component.spec.ts` |
| Opens on the run, keeps zoom on re-read | R7 | `strategy-view-model.ts:249-269` | implemented | `strategy-chart.component.spec.ts` |
| Value panes with dashed band | US 42 | `strategy-view-model.ts:130-158` | implemented | an unready value is skipped, never drawn as zero |
| Keyboard stepping through candles | #2639 follow-up | `strategy-chart.component.ts:394-420` | implemented (accessibility — owner ruling 2) | `strategy-chart.component.spec.ts` |
| Panel reused in Strategy Lab with no run context | #2639 D13 | `strategy-lab-strategy-view.component.ts:29-37` | implemented (host spec unread) | custom-gates spec renders with no `runContext` |

### Finding rows

| path::symbol | lens | severity | evidence | disposition |
|---|---|---|---|---|
| B-F1 `chart-lanes.ts::formingBar` (:69-78), `botLane` (:175-181) | 3/4 | P2 (reachability unverified) | "Forming · decides HH:MM", "Now" and "Next decision" are `lastClose + timeframe` with no calendar: after the day's last bar (16:00, 13:00 early close) a still-running no-end bot shows a 15-minute window claiming a decision at 16:15 that cannot happen (#2607: "the bot never decides after the close"). No spec puts `nowMs` after the last bar. | owner-ruling: backend-written next decision, or the client reads the canonical calendar |
| B-F2 `strategy-gates-state.ts:109-113,162-165`, `strategy-gates-model.ts:118-122` | 3 | robustness | A draft-preview POST re-judges the saved gates, so an unjudgeable saved gate's notice is in both responses; the sentence renders twice and `@for … track notice` gets duplicate keys. No spec. | fix (dedupe) |
| B-F3 `strategy-view-model.ts::decisionMarkers`, `chart-lanes.ts::decisionLane` | 3 | robustness | Marker and lane glyph key on `signal`, not `outcome`: a blocked ENTER still draws "▲ Enter"; the outcome is only in the popover title. Documented semantics. | leave-with-reason |
| B-F4 `strategy-chart-overlay.ts::StrategyChartOverlay.current` (:159-161) | 2 | hygiene | Called only by specs (`strategy-chart.component.spec.ts`, `bot-panel-shell.strategy-view.spec.ts:234`). | test-only, kept by the #2708 ruling |
| B-F5 `popover-placement.ts::popoverPosition`, `chart-lanes.ts` type exports | 2/7 | hygiene | `export` keyword is for specs only; the function and types are live in production signatures. | leave (test seam) |
| B-F6 `chart-lanes.ts:64` vs `strategy-chart.component.ts:73` `minuteOf`; `Math.floor(ms/1000) as UTCTimestamp` ×5 | 7 | hygiene | Duplicate minute-of-day helper and chart-time conversion. | owner call |
| B-F7 `strategy-chart.component.ts:33` | 7 | hygiene | Imports `formatChartCrosshairTime` from a component file (layering only). | leave |
| B-F8 `strategy-catalogue-indicators.ts:81-85` | 3/5 | robustness | `catchError(() => of({… error: 'Indicators could not be calculated…'}))` drops the HTTP reason; the operator sees that it failed, not why. Not silent. | fix, optional |
| B-F9 `strategy-view-model.ts:43-44` | 7 | hygiene | Comment cites "the owner-approved mockup" (a throwaway artifact). | fix (reword) |
| B-F10 `chart-lanes.ts:28, 117-118, 197-198` | 7 | hygiene | `scheduledEndAtMs` doc says "ahead" but the backend sends it for `ending` too; a comment claims every non-time word comes from the backend but `Bought`/`Sold`/`Working`/`Next decision`/`Ends` are literals; `${order.quantity ?? ''}` renders "Working sell  SPY" when null. | fix comments |
| B-F11 `strategy-gate-preference.ts:10-26` | 7/3 | hygiene | Two `catch` bodies that are only comments (per-viewer-convenience fallback; proven by `bot-chart-panel.component.spec.ts:145-161`). | leave-with-reason; owner ruling 3 |
| B-F12 `bot-chart-panel.component.spec.ts:139-143` | 6 | hygiene | Title promises two clauses, asserts one (the other is its own test at :163-167). | fix (rename) |
| B-F13 `strategy-gates-model.ts:27` `CANDLE_VARIABLES`, `strategy-gate-editor.component.ts:96` | 4/5 | hygiene | TS copy of the data plane's `CANDLE_FIELDS` (`strategy_gates.py:72`) with no parity test; the editor identifies the searchable group by its display title. | leave, or add a flag |
| B-F14 `strategy-chart.component.ts:157`, `bot-panel-shell.component.ts:632` | 3 | robustness | "Now" is `panel.updated_at_ms`; under a producer stall it stays at the last snapshot while labelled "Now". Mitigated by the `live-stall` alert and stale dimming. Same family as #3164. | leave-with-reason |

### Kept on purpose

`STRATEGY_CHART_FACTORY` (DI token with a production default, test seam); `popoverPosition` (called by `placePopover`); `LaneKey`/`LaneMark`/`ChartLane`/`LaneClock` (parts of exported production signatures); `keepInsideViewport`/`closePopover`/`anchorBelow` (used by `bot-toolbar`, `bot-page-header`, `options-backtest/cards/saved-setups-menu`); `StrategyChecksComponent`/`orderedChecks` (used by `recent-decisions-list`); the `src/app/testing/strategy-*` helpers (every export referenced).

### Owner rulings

1. Next decision after the final bar (B-F1): show "Forming / Next decision" only when the backend names one, or accept the 15-minute after-close window?
2. Do the AXE and keyboard/screen-reader-mark specs (`bot-chart-panel.component.spec.ts:229-240`, `custom-gates.spec.ts:435-443`, `strategy-chart.component.spec.ts:217-257,335-341`) retire under the 2026-10-12 accessibility ruling? Left in place; they pass.
3. Is the localStorage-blocked silent `catch` an accepted exception to "no silent exception handlers"?

## Slice C — `v2-panel/panel-shell/` + `panel-action-button/` + `startup-join/` + `instrument-quote/` + `resource-value-guard.contract.spec.ts` (26 files, all read)

27 features: 23 implemented, 2 partial, 2 unverified. 12 cut candidates (+1 rider). 12 finding rows: P2 4, robustness 5, hygiene 3. No P0/P1 firm.

**Wiring map.** The shell is the routed host (`app.routes.ts:432-439`). Every bound child input is live and every child output is handled; no input is bound twice. Every `protected` member is read by the template or another member except `queryParams` (C1). Every timer/observer has a stop (poll interval cleared in `destroyRef.onDestroy` ts:722-727; quote interval `onCleanup` ts:443-449; Resize/Mutation observers and rAF cleaned ts:814-819; startup-join interval `onCleanup`). No `catch {}`; the one `.catch` (ts:1547) tells the operator. Constant-bound inputs: `bot-order-records [activated]="true"` (C9), `bot-end-card [showChange]="false"` (kept: bot end specs click the card's own button).

**`resource-value-guard.contract.spec.ts` still fires.** It collects (`ci-spec-durations.json:77`, 0.743 s); strict discovery finds all four shell resources; its one `ALLOWED` entry (`bot-panel-shell.component.html::profile`) is still needed and true; synthetic fixtures pin both directions. Limits: scope is `v2-panel/` only (about 20 other files declare resources — F11), and `ALLOWED` is keyed by file+identifier, so a second unguarded `profile.value()` in the shell template would pass.

### Feature table

| feature (requirement) | entry → reachable | normal / empty / failure | classification: evidence |
|---|---|---|---|
| Bot page host: route inputs → live store → page (#2794) | `app.routes.ts:432-439` → yes | loading / error banner / 404 panel | implemented: `bot-panel-shell.component.spec.ts:914,1956` |
| Not-found (404 before first snapshot) | store `notFound` → yes | way back shown; a loaded page keeps controls | implemented: spec:2420 |
| Producer-stall notice, frozen dimmed page (#2353) | stream `stale` / 503 → yes | clears on next snapshot | implemented: spec:795-873 |
| Chart-line-only outage notice (#2355) | `live_chart.feed` → yes | chart notice, no stall | implemented: spec:875 |
| Command dispatch under the lane fence; stale-generation refresh (#2068, #2874) | toolbar → `onActionRequested` | refusal receipt, single send, re-read | implemented: spec:2154-2267 |
| Outcome ownership across rebinding (#2471) | `beginActionOwnership` | late outcome off the new bot | implemented: spec:1180-1264 |
| One-confirmation Flatten sequence (H30, #2595) | `onFlattenConfirmed` | mismatched plan refused; Dry Run simulated | implemented (money path): spec:950-1006, 2486-2594 |
| Extended-hours limit ticket (#2007) | `needs_limit` → yes | refused pricing, plan gone, token rotation | implemented; rotation claim unproven (F4): spec:1266-1670 |
| Settle an uncertain closing order (#2854) | toolbar → ticket | check, settle, no answer | implemented: spec:2619-3003 |
| Historical exact-execution recovery: prepare → typed RECOVER → confirm (#1665) | toolbar action | stale-plan refusal rendered | **partial: confirm reuses prepare's idempotency key (F1)**: spec:1008, 1100; no fleet-level test |
| Open custody timeline with one exact identity (#2560) | toolbar → `router.navigate` | first recognised ref wins | implemented: spec:1071 |
| Change the bot's end (#2607) | toolbar → `endCard().openEditor()` | stale generation refreshes lane | implemented: spec:2269-2355 |
| Option buying-power review (#3098) | option bots | rebound refused; refusal shown | implemented: `bot-panel-shell.option-buying-power.spec.ts` (5) |
| Strategy view read / re-read / failure kept to chart (#2639) | `strategyView` resource | Retry in chart panel | implemented: `bot-panel-shell.strategy-view.spec.ts` (5) |
| Finished run's tape reads that run's window (#2794 R7) | `histChart` | newest bars while running | implemented: spec:2410 |
| Polygon history failure and zero-bar isolation (#2202, #2211) | `histChart` | one request per Retry; no stale bars | implemented: `bot-panel-shell.history-failure.spec.ts` (5); sole proof (no `bot-day-chart` spec) |
| Current-run poll while running (#2592) | `runPollTimer` | Retry card on failure | implemented: spec:1865, 1904 |
| Workspace header slot hand-off (#2794) | `WorkspaceHeaderSlot` | cleared on destroy | implemented: spec:1699, 1726 |
| Panel-profile load gates the page | `profile` resource | fails closed, no retry | **partial (F2)**: no spec for a failed profile |
| `PanelActionButton`: confirm, blocker tooltip, ticket hand-off (#1689, #2854) | `bot-toolbar.component.html:5-17` → yes | `aria-disabled` + guard | implemented: `panel-action-button.component.spec.ts:28-226` |
| `PanelActionButton`: blocker cures and prose suppression (#1778) | none | n/a | unverified: test-only (C3, C4): spec:134, 262-370 |
| `PanelActionReceipt` (#2007) | shell html:58; `alpaca-sqlite-custody.component.html:127` | code shown once | implemented: `panel-action-receipt.component.spec.ts:30-54` |
| Tape header quote "IBKR · last bar" (H15) | `dual-pane-chart.component.html:10` | change shows "—" | implemented: spec:1749; `instrument-quote.component.spec.ts:7` |
| Startup-join status label and facts (#2410) | `bot-health-groups.component.html:24` | backend copy | implemented: `bot-health-groups.component.spec.ts:86-90` |
| Startup-join countdown (#2410) | same → when `filling` | clamps at 0:00 | **unverified: no spec (F5)** |
| Exposure notices (#2504) | health groups, stranded warning | ≤ 2 kinds per run | implemented: `bot-health-groups.component.spec.ts:125-131` |
| Resource-value guard (#2202) | contract spec | collects; can fail on real code | implemented |

### Cut candidates

| id path::symbol | evidence | orphans | confidence |
|---|---|---|---|
| C1 `bot-panel-shell.component.ts::queryParams` (L304-306), `route` (L297), imports `toSignal`, `ActivatedRoute` | `rg "this\.queryParams\|this\.route\b\|queryParams\(\)" panel-shell/` → L304-305 only; no spec injects ActivatedRoute. Added 2026-07-31 (`641169ec`) for the retired tab stamp | `route`, two imports, duplicate `account-workspace` import | certain |
| C2 `bot-panel-shell.component.spec.ts:766-793` `it.each([[''], ['?from=gallery']])` | `?from=gallery` row only proves a retired stamp no longer matters | `renderShellWithStamp` navigation | probable |
| C3 `panel-action-button.component.ts::suppressedBlockerId`, `::suppressedBlockerReasonCode`, `visibleBlockers` filter, html:25-29, scss `.panel-action__blocker` | sole host `bot-toolbar.component.html:5-17` binds neither; hosts removed in `f01773ea` (#2794) | spec tests :134, :295 | certain dead in prod; test-only |
| C4 same component: `moveIsSupported`, `moveRequested`, `blockerMoves`, `requestMove`, `isSelfDispatchable`, `Router` inject, html:31-45, scss `.panel-action__moves/__move`; `bot-toolbar.component.ts:209` `noMoves = () => false`, html:10 | `noMoves` makes `blockerMoves()` always `[]`; `rg moveRequested` → declaration and one comment, no listener (`564493c8`, 2026-10-02) | spec :262, :277, :295, :313 (a11y), :327, :345 and helper consts; `movesForBlocker`, `BOT_COCKPIT_RECONCILE_ANCHOR` stay (`api/operator-blocker.contract.spec.ts:78-80`) | certain dead in prod; test-only |
| C5 `PanelActionTone` member `'primary'`, scss `.panel-action__button--primary` | backend `ToolbarActionView.tone` is `danger|warning|neutral` (`api/broker.types.ts:29310`); `rg "panel-action__button--primary\|tone: 'primary'"` → scss only | none | certain |
| C6 rider on C3/C4: seven tests `panel-action-button.component.spec.ts:134, 262, 277, 295, 313, 327, 345` | prove a retired host (kind 5) | helper consts | certain given C3/C4 |
| C7 `instrument-quote.component.ts::InstrumentQuoteView.name/exchange/logoSlug/currencySymbol`, copy L46-54, html:7-9 | the only caller sets `symbol/price/changePercent` | spec fixture fields | probable |
| C8 `panel-instrument-quote.component.ts::sourceLabel` | sole host binds `symbol` and `tickerQuote` only | none | probable |
| C9 `bot-order-records.component.ts::activated` | `[activated]="true"` constant (see slice A) | the `undefined` branch | certain |
| C10 conditional: `broker-v2-panel.types.ts::FEED_CONTINUITY_NOT_RECORDED`, `feedContinuityFor`, shell `ts:623-626` `panel === null ?` | `feed_continuity` is required in the schema; the header already reads it directly | `bot-current-run` "—" stays | conditional on every lane running a current build |
| C11 `panel-action-button.component.spec.ts:376-409` "focus after a cancelled confirmation" (2 tests) | duplicate of `typed-halt-confirm.component.spec.ts:212-223` | none | probable |
| C12 `bot-panel-shell.component.spec.ts:1690-1696` + six hook classes `bot-board__money/orders/setup/decisions/health/option-power` | class pins with no scss rule and no e2e | six class names | probable; owner call |
| C13 absence lines in mixed tests (spec:1687-1688, 1796, 2397-2398) | policy: whole tests only | none | optional |

### Kept on purpose

`CURRENT_RUN_POLL_MS` export (spec seam); the standalone branch `@if (!inWorkspaceHeader)` (~50 specs render without a slot); `PanelActionTrigger.reason` always null (contract passthrough); "IBKR · last bar" chip (names its source, says "last bar", not "live"); null change renders "—"; `track notice.kind` (≤ 2 kinds).

### Finding rows

| id path::symbol | lens | severity | evidence | disposition |
|---|---|---|---|---|
| C-F1 `bot-panel-shell.component.ts:972-975, 1431-1488` | 1/5 | **P2 by the letter (fails closed); owner may rate P1** | confirm re-sends the prepare step's `command_context.idempotency_key` to a different operation: `onActionRequested` mints one key via `commandTarget(target)` and stores that target in the draft; confirm re-sends `draft.target`. The ledger's unique key is `(broker, clerk_id, idempotency_key)` and `_require_attempt_context_match` raises `ClerkRoutingAttemptConflict` when `operation_kind` differs (`service.py:2300-2311, 2458-2483`; `routing.py:480-492`). Recovery cannot complete from this UI. Traced by reading; `test_routing_attempts.py` does not cover cross-operation reuse | fix: `this.commandTarget(draft.target)` at confirm; spec :1058-1059 pins the reuse and must change |
| C-F2 `ts:707-709`; html:24-32 | 3/5 | P2 | the whole page (Stop, Flatten, Settle) waits on `profile.hasValue()`; the profile feeds only the rail, which accepts null; no retry; failure shows "Loading bot control…" under a red banner | fix: render with null, or add Retry |
| C-F3 html:114 `tapeLabel` | 3 | P2 | "Tape · live" keyed on the bot running, not the feed; the pane carries its own stall notice | owner-ruling |
| C-F4 `spec:1560-1606` "recovers from a rotated Prepare token…" | 6 | P2 | asserts `tokens[2]).toBeDefined()`; the second snapshot has the same `surface_version: 1`, so `adoptVersionedSnapshot` drops it | fix: `surface_version: 2` and assert `tokens[2] === 'plan-token-18'` |
| C-F5 `startup-join-status.component.ts:27-51` | 6/4 | robustness | no spec drives `filling`; countdown is `deadline_ms - Date.now()` on the browser clock | add a fake-timer spec |
| C-F6 html:227-240 | 3 | robustness | Health panel for `bot_page == null` is an empty frame with no sentence | leave; waits on slice A |
| C-F7 `ts:722-727, 524-529, 737-745` | 5 | robustness | `currentRun` reloads every 5 s while running though its reader renders only when the "Runs" fold is open; `currentRun` params and the live-store effect depend on the `target()` object | leave-with-reason |
| C-F8 comments: "Codex review 2026-09-19", "H15/H30/H31", "story 48/80" | 7 | hygiene | cite a review and launch-run numbers, not an ADR or issue | swap for #2595/#2794/#2007 |
| C-F9 `history-failure.spec.ts:1-17`, `:84-192` vs `bot-panel-shell.component.spec.ts:100-208` | 7 | hygiene | stale size numbers; duplicated ~110-line `PANEL` that `testing/bot-panel-fixtures.ts::fakeBotPanelView` provides | fix on next touch |
| C-F10 test names `spec:914`, `:1941`, `:1802` | 6 | hygiene | misleading names | rename |
| C-F11 guard scope | 5 | robustness (pointer) | covers `v2-panel/` only; ~20 resource files elsewhere unscanned | owner-ruling |
| C-F12 live-stream `end` (slice D D-F4) | 3 | robustness, latent | `end` closes the stream for good; graceful-shutdown order makes delivery unlikely | leave-with-reason |

### Owner rulings

C-F1: rate P1 or P2, and approve the one-line fix (fresh key at confirm). C-F3: does "Tape · live" become the source name? Receipt "Next session opens": `local` (pinned) or `et` (`temporal-rigor.md` lists session open under `et`)? C12: delete the board class-name assertions and hook classes, or keep them as a layout-order pin? C-F2: should the bot page render without the panel profile? C3/C4/C6: confirm the retired host is gone, so the test-only members and their seven tests go together.

## Slice D — `v2-panel/lib/` + `operator-lens/` + `bot-run-history/` + `gallery/` (44 files, all read)

20 features: 13 implemented, 2 partial, 1 missing, 4 unverified. 12 cut candidates (9 certain). 17 finding rows: P2 4, robustness 6, hygiene 7. No P0/P1.

### Feature table

| # | feature (requirement) | entry point → reachable | normal / empty / failure | classification: evidence |
|---|---|---|---|---|
| R1 | Current-run evidence + IBKR continuity, Runs fold (#2574, #2794) | shell html:179-190 → yes | `not_recorded` never shown as zero | implemented: `bot-current-run.component.spec.ts` (5); wrappers have no spec |
| R2 | History link narrowed to this bot (#2574) | `operator-run-history.component.ts:28-31` → yes | — | unverified: spec hand-builds `HISTORY` |
| R3 | Order path: transaction rail (#2563, #2794) | `bot-order-records.component.html:29-37` → yes | empty branch unreachable from host (CC6) | implemented: `transaction-rail.component.spec.ts` (5) |
| R4 | Evidence timeline, paged on demand (#2563) | rail "View evidence" → yes | single-flight, stale response dropped, failure+retry | implemented: spec (4) |
| R5 | Select a transaction from the audit trail (#2563) | journal row → `selectTransaction` → yes | the rail then never refreshes (F-1) | partial: shell spec:1971 |
| R6 | Audit trail filter / reveal / empty / failure (#2563) | `bot-order-records.component.html:41-48` | failure double-message (F-8) | unverified: no journal-tail spec |
| R7 | Live bot panel store: REST + SSE + 5 s fallback, stall, not-found (#2353, #2794) | `bot-panel-shell.component.ts:739` → yes | `end` event and stop→start uncovered (F-4, F-5) | implemented: `bot-panel-live-store.service.spec.ts` (13) |
| R8a | Panel HTTP surface, 24 of 35 methods (#1912, #2351, #2874, #2854, #3098) | `BrokerV2PanelService` → yes | URL, escaping, frozen key, quiesce routing, single submit | implemented: `broker-v2-panel.service.spec.ts` |
| R8b | 11 methods with no HTTP-contract test (`getPanelProfile`, `getAccountMoney`, `getDeployView`, `getPanel`, `getLiveSnapshot`, …) | each has a prod caller | consumers mock the service | unverified |
| R9 | One money answer, never `$0` (PRD #2560 D12) | `account-money-state.ts` → yes | all states | implemented: spec (8, one duplicate H-3) |
| R10–12 | bot hues; ChartBar→candle; history empty vs unavailable (#2211) | yes | wrap/stability; fail-loud notice | implemented: specs (3, 2, 7) |
| R13 | Cohort drawer read + frozen fence (ADR 0051 D3, #2068) | `cohort-drawer-presentation.ts` → yes | cold directory, rebind, watermark | implemented: `cohort-flatten-drawer.component.spec.ts` L485-561 |
| R14 | Refusal → outcome/why/next; toast (#2067, #2102, #2607) | `panel-action-outcome.ts` → yes | nested, flat fleet, vocabulary | implemented: spec (22) |
| R15 | Wall store: bars, fills, transport, paged frames (#2328, #2330) | `alpaca-home.component.ts:258-262` → `?view=wall` | incomplete frame, refused, identity change | implemented: `gallery-live-store.service.spec.ts` (13) |
| R16 | Wall tells the operator whether candles are live | none: no host reads `status/bots/resolution/refusalReason` | a dead feed draws last candles unmarked | **missing** (F-3) |
| R17 | Candle renderer (#2560 D11) | `home-sparkline.component.ts:20` → yes | empty, flat, out-of-window marker | implemented: `candle-renderer.spec.ts` |
| R18 | Panel-profile read | shell `profile` resource → gates the whole page; payload unused | — | partial (CC5/CC11, F-2) |
| R19 | `feedContinuityFor` rolling-deploy shim | `types.ts:105-107` | generated type has `feed_continuity` required | unverified (conditional, CC12) |

### Cut candidates

| id path::symbol | evidence | orphans | confidence | kill list |
|---|---|---|---|---|
| CC1 `lib/broker-v2-panel.types.ts:13` `ChannelState` | `grep -rnw` → definition only | none | certain | #2708 "14 unused type aliases" (missed by #2754) |
| CC2 `:51` `ChannelHealthView` | same | none | certain | same |
| CC3 `:53` `ClerkCard` | same (generated `api/broker.types.ts:9078` does not use the alias) | none | certain | same |
| CC4 `:55` `ReadinessCheckView` | same | none | certain | same |
| CC5 `operator-lens/transaction-rail.component.ts:40` `profile` input | `grep -n profile` in ts/html/spec → declaration, import, false doc only; bound at `bot-order-records.component.html:31` | `PanelProfile` import, doc ts:25-28 | certain (rail); chain waits on A/C | no |
| CC6 rail `html:3-5,7,45`, `.scss:7-10`, `ts:50` `hasTransaction`/`.rail__empty` | host guards mount on `transaction_ref !== null` | `.rail__empty` SCSS | probable | no |
| CC7 `bot-run-history/bot-current-run.component.scss:125-128` `.run-history__heading`, `.run-history__tabs` | no markup | two rules | certain | no |
| CC8 `gallery/lib/candle-renderer.ts` last-price-tag path (`layoutTag`, `drawLastPriceTag`, `roundedRectPath`, `showLastPriceTag`, tag config fields, `ChartScale.tagHalfHeight`) | only host forces it off (`home-sparkline.component.ts:64`); the rest is `candle-renderer.spec.ts` | spec `layoutTag` describe, one test, fill-count re-base; host one-line edit | certain dead; needs-a-run for the re-base | #2708 rows 5-6 (kept by #2754 "a surviving marker test counts its fill") |
| CC9 `gallery-live-store.service.ts` `resolution` accessor, `resolutionState` | `.resolution()` read only at spec line 153 | spec line | certain | #2708 rows 3-4 (kept by #2754) |
| CC10 same file `refusalReason`, `refusalReasonState` | store + spec `:529` only | spec line; false comment `gallery.types.ts:77-80` | certain | same |
| CC11 `broker-v2-panel.service.ts:177-183` `getPanelProfile` + shell `profile` resource + `bot-order-records` input + `PanelProfile` alias | dead chain behind CC5 | shell `isLoaded`/`loadError`, 6 shell specs, 3 e2e stubs, backend `fleet_compatibility_reads.py:21` pointer | probable | no |
| CC12 `types.ts:105-107` `feedContinuityFor` (keep the constant) | old-build shim | caller becomes `panel.feed_continuity` | conditional | family of #2708 rows 21-22 |

### #2708 rows in this slice against master

Gone: frame age / frame-stale, hover crosshair, `barIndexAtX`, `getLiveChart`, `laneUrl`, `accountUrl`, `snapshotAge`, `__TEST__`, `fmtDateNy`, `diffBps` (in `format.ts`: see slice H), `toleranceBand`, the `/actions/quiesce` fallback. Present and kept by the owner addendum: gallery `bots()`/`status()`. Present and kept only by #2754's "surviving tests": CC8, CC9, CC10. 4 of the 14 unused aliases remain: CC1–CC4.

### Kept on purpose

Gallery `bots()`/`status()` and what only they expose (`withReportedFeed`, `UNREPORTED_FEED`, `LaneBotView`, `upsertBots`, `dropRemoved`, `GalleryBotView`, `GalleryFeedView`) — kept by the #2708 addendum, no production host reads them (ruling 2); `BOT_HUES`, `MONEY_*` exports (used in-file); `ChartScale`/`PlotRect`/`CandleRendererConfig`; `OptionBuyingPowerCheck`, `LeadInBar`, `BotPageView` (test fixtures); `PanelActionTrigger.reason` (contract field); `HISTORY_EMPTY_NOTICE_CODES` (fail-loud set whose one member cannot occur on history; comment says so); `QUIESCE_ACTIONS` (keyed by the generated union).

### Finding rows

| id path::symbol | lens | severity | evidence | disposition |
|---|---|---|---|---|
| D-F1 `lib/bot-panel-live-store.service.ts:91,136-158,255-258` `selectedRail` | 3 | P2 | after one `selectTransaction`, `adopt()` overwrites `panel.rail` of every later snapshot with the rail captured at selection; the Order path stays frozen with no staleness mark; no spec | fix (re-read on `journal_tail_seq`, or mark "as of") |
| D-F2 `bot-panel-shell.component.ts:537-540,707-716`; html:24-27,32,168 | 3/5 | P2 | the page (incl. Stop/Flatten) does not mount until `/api/brokers/{broker}/panel-profile` answers; the payload is read by nothing (CC5). Fails closed | fix (drop the gate with CC5/CC11) |
| D-F3 `gallery-live-store.service.ts:310-325,462-476`; `home-sparkline.component.ts:37` | 3 | P2 | `status` never read; bootstrap error dropped by `void error;`; invalid frames dropped silently; a dead wall draws last candles unmarked or "No bars yet today". P1 only if the owner treats Wall candles as live state | owner-ruling (surface status, or cut machinery) |
| D-F4 `services/versioned-snapshot-stream.ts:66-68`; `bot-panel-live-store.service.ts:245-249` | 3 | P2, needs-a-run | an SSE `end` event closes the stream for good: no reconnect, no poll, no banner; server emits `end` when a hub stops (`surface_hub.py:297,391`) | fix (reconnect on `end` or "stream ended") |
| D-F5 `bot-panel-live-store.service.ts:102-117,160-166`; `gallery-live-store.service.ts:217-256` | 3 | robustness | `stop()` keeps state; a restart for another bot/account is not seen as an identity change (Home: Wall → List → switch account → Wall shows old candles until bootstrap) | fix |
| D-F6 `bot-panel-live-store.service.ts:121,170-185,318-321`; `gallery-live-store.service.ts:246,503-506` | 5 | robustness | bootstrap and 5 s fallback poll use raw `firstValueFrom(http.get)`: no timeout, no in-flight guard | fix |
| D-F7 `transaction-evidence-timeline.component.ts:46-54` | 5 | robustness | effect tracks the whole fleet-directory signal; any directory load blanks entries and re-reads page one | fix |
| D-F8 `journal-tail.component.html:36-38` + `bot-order-records.component.html:41-47` | 3 | robustness | failed read renders "No journal entries." under the parent's alert | fix (same as A-F7) |
| D-F9 `bot-panel-live-store.service.ts:64-69` | 3 | robustness | any 404 with no body message reads "No such bot is on this account." | leave-with-reason |
| D-F10 `gallery-live-store.service.ts:92-97,281-288` | 5 | robustness | `mergeBarsBySymbol` never trims; a tab open across days packs all bars into one canvas | leave-with-reason |
| D-H1 `panel-action-outcome.ts:96-99` vs `account-money-state.ts:81-84` | 7 | hygiene | two identical private non-empty-string helpers; `failedRead` re-derives message/why/next-step beside `deriveActionRejection` | leave / fold into `shared/errors/refusal-body.ts` |
| D-H2 `transaction-rail.component.spec.ts:111,121,131,137` | 6 | hygiene | `runAction` stubbed and asserted `not.toHaveBeenCalled()` can't fail (never injected); `.evidence-drawer` null is a tombstone | fix (drop four lines) |
| D-H3 `account-money-state.spec.ts:36-41` | 6 | hygiene | same input as `:30-34`, asserts only `.kind`; survivor asserts `{kind, view}` | fix (delete weaker) |
| D-H4 `candle-renderer.spec.ts:126-181` | 6 | hygiene | four `draw` tests assert only `not.toThrow()` on a stub ctx | leave-with-reason |
| D-H5 stale comments (`candle-renderer.ts:7-9`, `service.ts:144-150`, `transaction-rail.component.ts:25-28`, `cohort-drawer-presentation.ts:22-23`, `gallery.types.ts:77-80`, `gallery-live-store.service.ts:157`) | 7 | hygiene | false or nonexistent names | fix with the nearest cut |
| D-H6 shell `html:179-190` over `operator-run-history.component.html:1-15` | 7 | hygiene | two nested `<details>` folds for one content | owner-ruling |
| D-H7 `transaction-rail.component.spec.ts:172-183` | 6 | hygiene | pins `aria-label` (accessibility, out of scope) | owner-ruling |

### Owner rulings

CC8/CC9/CC10: do the last-price-tag path, `resolution()` and `refusalReason()` go (the addendum names only `bots()`/`status()`)? D-F3: surface Wall state on Home, or cut the machinery behind the kept accessors and its seven tests? CC5/CC11/D-F2: does the bot page stop reading `panel-profile`? D-F4: reconnect on SSE `end`, or say "stream ended"? Keep the never-firing `HISTORY_EMPTY_NOTICE_CODES` set? Two folds for run evidence (D-H6)?

## Slice E — `v2-panel/dual-pane-chart/` + `v2-panel/cohort-flatten/` + `shared/` (flatten and halt tickets) + `bot-end/` (48 files, all read)

19 features: 18 implemented, 1 partial. 4 cut candidates (2 certain). 25 finding rows: **P1 1 (reproduced, filed)**, P2 7, robustness 6, hygiene 5. No P0.

**What was checked for "sends the one request it claims".** Cohort: `confirm()` freezes `pending.legs` at review, clears `pending` synchronously (a second click returns), mints one `crypto.randomUUID()` key and sends exactly the reviewed legs; `retry()` re-sends the same `SentWave` object (spec asserts `toBe` identity). Extended ticket: `confirmSend()` nulls `reviewed` before emitting (a double click is a no-op) and sends the frozen `{limit_price, quote_observed_at_ms, band_override?}`. A re-sent extended flatten re-drives the same durable EXIT by plan version token, so the P1 below is a belief problem, not a duplicate-order path.

**Feed-notice verification (known-gaps §9 F12).** STALLED, ERRORED and RECOVERING all render through one state-agnostic branch driven by backend `show_notice` / `attention_required` (`dual-pane-chart.component.ts:353-357`, html:118-170; backend `chart_projection_service.py:262-312`). The live pane tests ERRORED and STARTING plus the `show_notice=false` contrast; STALLED only on the position line; RECOVERING has no spec of its own. No P1 there. "Refreshes every 5s" is hard-coded (H-2).

### Feature table

| feature (requirement) | entry → reachable | normal / empty / failure | classification: evidence |
|---|---|---|---|
| Typed / plain confirm dialog (#2560 story 80; ADR 0051) | 8 hosts → yes | token gate, plain mode, Escape; no once-guard inside (hosts guard, R-5) | implemented: `typed-halt-confirm.component.spec.ts` |
| Safe-flatten plan card (#2839, #2007) | shell html:83; custody desk | the static "No order has been submitted" is wrong after an unknown-outcome send | **partial (E-F1, P1)**: `safe-flatten-plan.component.spec.ts` (4); gap is an unknown-outcome send |
| Extended-hours flatten ticket (#2007, ADR 0068) | shell html:85 → PRE/POST | band refusal never confirmable; stale quote blocks send; price-check failure leaves "Pricing…" (E-F3) | implemented: `extended-flatten-ticket.component.spec.ts` ("sends exactly the price and quote it was reviewed against", band, stale quote, changed position) |
| Flatten fills / slippage (#2007) | shell html:103 | rows only for Clerk-measured fills | implemented: spec (3) |
| Next attempt (recovery status) (#2504) | lane bell, custody desk, health groups | hides itself with no status | implemented: spec (3) |
| Cohort flatten drawer: review, typed confirm, one POST, outcome, same-key retry (ADR 0051, #1909, #2068, #2912) | `alpaca-home.component.html:115` | load failure says "read failed"; transport failure "unknown"; typed refusal no retry; close mid-flight leaves state (E-F2) | implemented: `cohort-flatten-drawer.component.spec.ts` |
| Cohort group / leg selection / 64 cap (ADR 0051 D2) | `app-cohort-flatten-group` | disabled leg stays visible with its blocker | implemented |
| Cohort outcome per leg (ADR 0051 D5) | `app-cohort-flatten-outcome` | short response names unanswered legs; summary folds "unknown" into "failed" (E-F6) | implemented |
| Cohort operator-copy closed map (owner 2026-09-23) | `cohort-flatten-confirmation.ts` | — | implemented: closed-key test |
| Bot-end editor + popover (#2607, #2858, #2663) | bot page end card; Deploy end section | missing Popover API is a silent no-op (R-3) | implemented: `bot-end-fields.spec.ts` (5), `bot-end-card.component.spec.ts`, `alpaca-deploy-workflow.end.spec.ts` |
| Bot-end summary + refusal (#2607) | end card, Deploy, settle ticket | time-of-day only when backend words absent (H-3) | implemented |
| Market tape: live IBKR / delayed Polygon, interval, fill markers, Local/ET (#2202, #2355, #2794) | `bot-day-chart.component.html:2` | empty, loading, unavailable distinct | implemented: `dual-pane-chart.component.spec.ts` |
| Chart feed notice (STALLED / ERRORED / RECOVERING / STARTING) (#2355) | `liveFeedNotice` → html:118-170 | see above | implemented: spec "the chart's own feed line (#2355)" |
| Position pane for a bot that trades another asset (#2915) | `livePosition`/`histPosition` | gap per bar with no price | implemented: `dual-pane-chart-position.spec.ts` |
| Indicator overlay (fullscreen rail) (#2202, #2222) | `app-chart-indicator-rail` | calc failure visible only in the rail (E-F5) | implemented |
| Run lanes under the tape (#2808) | `lanes` input | off-bar marks kept for readers | implemented |
| Polygon history unavailable + Retry (#2202, #2211) | `historyUnavailable()` | one retry per click | implemented: `bot-panel-shell.history-failure.spec.ts` |
| Chart timezone persistence (#2808) | `TIME_ZONE_STORAGE_KEY` | unreadable storage makes construction throw (R-1) | implemented |

### Cut candidates

| path::symbol | evidence | orphans | confidence |
|---|---|---|---|
| `dual-pane-chart.component.scss:69` `.quote-line > strong` | `rg quote-line` → html L8-9, scss L53/59/69; every `<strong` in the template is inside a button, notice or chart-state, and a child component's `<strong>` cannot match `>` of the parent's scoped selector | one SCSS line | certain |
| `cohort-flatten-outcome.component.html:44` `[class.cohort-flatten__leg-outcome--unresolved]`; inert `cohort-flatten__summary` (L7), `cohort-flatten__not-attempted` (L30) | `rg` → html only; no scss, no global style, no e2e | the binding and two class attributes | certain it does nothing today; cut vs style is a call |
| `cohort-flatten-confirmation.spec.ts` "asks the operator to type FLATTEN" (L99-101) | constant pin; the drawer spec types the literal then clicks | one `it` | probable; money-path adjacent, keep |
| `dual-pane-chart.component.spec.ts` "names each source tab for the feed that draws it" (L161-169) | copy pin; 8 other specs click the tabs by that name | one `it` | probable; honesty copy, keep |

### Kept on purpose

`DUAL_PANE_CHART_FACTORY` (DI seam, 5 specs); `countPlottedFills`/`toSeriesMarkers` (used in the component; only the `export` is spec-only); `NextAttemptFacts` (the input's own type); `MAX_COHORT_FLATTEN_LEGS` (hand mirror of backend `max_length=64`, R-4); chart-indicator catalogue symbols (used by `strategy-view/`); `formatChartCrosshairTime`; all 31 `COHORT_FLATTEN_COPY` keys; the contrast test "says the read failed, never that there are no cohorts"; the closed-copy lint; all flatten, typed-confirm, extended-ticket and cohort walk-through specs (Sacred).

### Finding rows

| id path::symbol | lens | severity | evidence | disposition |
|---|---|---|---|---|
| **E-F1** `shared/safe-flatten-plan/safe-flatten-plan.component.html:22-27` | 3 | **P1, reproduced; filed** | card says "No order has been submitted." whenever not expired. `sendExtendedFlatten` keeps `preparedFlatten` on every failure (only success clears it, `bot-panel-shell.component.ts:1366`), so after a transport-level failure (`deriveActionRejection` outcome `unknown`) the card stays mounted while the POST may have been accepted; the Sell step reads "Failed". Run on `8784061c`: status-0 rejection → receipt "Unknown Action "Execute safe flatten" failed.", progress "Sell: Failed Action "Execute safe flatten" failed.", plan card present and still saying "No order has been submitted" | fix: host clears or replaces the plan on `unknown`, or the sentence comes from host state |
| E-F2 `cohort-flatten-drawer.component.ts:369-408` | 3/6 | P2 | `onClose()` clears `sent`/`outcome`/`dispatchFailure` but does not stop the in-flight `send()`; a late outcome lands in the closed drawer ("of 0 sent", or "retrying re-sends" with no Retry); no spec closes the drawer | fix (generation token) |
| E-F3 `extended-flatten-ticket.component.ts:120-143,197-207` | 3/6 | P2 | `awaitingPrice` clears only on a matching proposal; a failed price check leaves "Pricing…" with Review disabled | fix |
| E-F4 `extended-flatten-ticket.component.ts:129,236` | 4/3 | P2 | typed `511.289` passes `PLAIN_DECIMAL`; the pane shows "$511.29" while 511.289 is sent; the Clerk refuses (`RECOVERY_LIMIT_PRICE_INVALID`) so it fails closed | fix |
| E-F5 `dual-pane-chart.component.ts:437-442,448-453,493-503` + html:259-273 | 3 | P2 | `catchError(() => of({error}))` drops the error; `indicatorError` is bound only to the fullscreen rail; the earlier result set stays drawn | fix |
| E-F6 `cohort-flatten-outcome.component.ts:48-59` | 3 | P2 | backend folds `unknown` into `failed_count`; summary reads "failed N" while the leg row says "Outcome unknown" | fix |
| E-F7 `safe-flatten-plan.component.ts:49-53` | 4/5 | P2 | `expired = Date.now() >= plan().expires_at_ms` compares the browser clock to a server instant; the sibling ticket's comment says it avoids exactly that; the Clerk re-checks | leave-with-reason |
| E-F8 `safe-flatten-plan.component.html:104` + spec L41 | 7 | P2 | `{{ leg.symbol \| receiptLabel }}` breaks angular.md (symbols render through `app-asset-identity`): "SPY" becomes "Spy" (spec pins it), an OCC symbol becomes "Spy260619c00500000" | fix |
| E-R1 `dual-pane-chart.component.ts:84-87,574-576` | 5 | robustness | `typeof localStorage` does not guard the accessor throwing; field initializer → the chart and bot page fail to construct | fix |
| E-R2 `cohort-flatten-drawer.component.ts:343` | 3 | robustness | `crypto.randomUUID()` undefined outside a secure context; the click closes the dialog and sends nothing | pointer (module-wide) |
| E-R3 `bot-end-popover.component.ts:51-59` | 3 | robustness | `show()`/`hide()` guarded by `'showPopover' in panel`; without the API "Change end" does nothing, silently | leave-with-reason |
| E-R4 `cohort-flatten-confirmation.ts:35` | 5 | robustness | 64-leg cap hand-copied from the backend bound, no contract pin; fails closed (422) | leave-with-reason |
| E-R5 `typed-halt-confirm.component.ts:128-131`; `panel-action-button.component.ts:146-149` | 3 | robustness | `confirmed` emits on every click while mounted; safety is each host's read-and-clear; `confirm()` has no open-guard (saved by the shell's `actionPending()`) | pointer |
| E-R6 `cohort-flatten-drawer.component.spec.ts` | 6 | robustness | untested: `onClose` reset, Retry double click, double Flatten click | fix with E-F2 |
| E-H1..H5 | 6/7 | hygiene | comment "The Strategy Lab passes none" (component has one host); hard-coded "Refreshes every 5s" shown whenever `liveFeedNotice()` is null (also at `1m`, `NOT_EXPECTED`); `granularity="time"` renders `HH:MM:SS` with no date for a bot end and flatten fills; `engineIndicatorId` re-implements Python `_engine_indicator_id` with no parity test; `bot-chart-indicator.service.spec.ts` L16-18 echoes the value it just flushed | owner-ruling / leave |

Lens 4: no `new Date(string)`, `DatePipe`, `Intl.DateTimeFormat`, `toLocaleString`, 9:30/16:00 or fixed offsets in the slice. Lens 7: no `console.*`, `eslint-disable`, `@ts-ignore`, `any`, TODO.

### Owner rulings

Axe and focus specs (drawer spec :708-718, :321-336, :610-633; `typed-halt-confirm` keyboard-return tests; `bot-end-card.component.spec.ts:408`) under the 2026-10-12 ruling. "Refreshes every 5s": wanted at all? Keep or cut the two probable spec pins (FLATTEN constant, source-tab label)?

## Slice F1 — `broker-deploy-page/alpaca-deploy-workflow.*` (8 files, all read; 2,530-line host + four large specs)

23 features: 18 implemented, 2 partial, 2 unverified, 1 missing. 4 cut candidates. 23 finding rows: **P1 1 (reproduced, filed)**, P2 6 (one reproduced in part), robustness 2, hygiene 14. No P0. The `SUBMISSION_KEY_RE` lead resolved: the component reads it twice; only the `export` is spec-driven, and S:1353 pins it to the OpenAPI `submission_key` patterns. Every input and output of the 14 children the host binds was checked mechanically; all are read or emitted except `deploy-paper-access::accountId`.

### Feature table

| feature (requirement) | classification: evidence |
|---|---|
| Four steps What → How → Money → Confirm; columns ≥ 48rem (#2581, PRD #2560 D8) | implemented: spec "lays out What → How → Money → Confirm…" |
| Strategy choice; `?strategy=` / `?strategy_key=` deep link (#1702) | implemented: "changes the validated strategy…", "honors a changed strategy deep link on a reused route…" |
| Symbol pick, symbol-scoped readiness, retained-view staleness (#1777, #1778) | implemented: symbol-scope spec (15) |
| Strategy settings; Golden-scope gate on broker worlds (#2696) | implemented: "seeds an accepted Golden scope…", "blocks deployment while a strategy setting shows unparseable text" |
| Strategy permission line, host wiring (PRD #2560 D9) | implemented (host side): "keeps a strategy not yet allowed on this account open in What…" |
| Traded asset other than the signal's (#2912, #2915) | implemented (a failed early read is silent: F1-4): 9 tests |
| Option-structure bot: builder, 4 order terms, server review, blockers (#2913, ADR 0076) | implemented: `options.spec.ts` (30) |
| Where it trades: one broker world + Dry Run; Live never preselected (ADR 0059 D2/D11) | implemented: "never preselects Live…" |
| Position size and carryover (PRD #2560) | implemented (bounds hard-coded: F1-8) |
| Exit terms fixed for the bot's life, range-checked (H3) | implemented: "names an exit term that is typed but out of range…" |
| Bot end: default, Sell/Keep, no end, refusal, re-check at Deploy (#2607) | implemented: `end.spec.ts` (14) |
| Money wiring: review bound to exact settings, typed Live phrase (#2468, PRD #2560) | implemented: "gates Live on the exact typed phrase…" |
| Confirm: failing checks with fixes, Start refusal, "Check again" (#2901) | implemented: 7 recheck tests |
| Send once; a retry reuses the submission key and command (#2551) | implemented: "retries an uncertain Deploy under the same submission key and command", it.each "keeps the submission key after $kind…" |
| Recovery read by `?submission=`, "Check deployment status", key adoption | **partial: any HTTP 404 frees the key (F1-1, P1)**: ~12 tests (S:1089-1322); none feeds a fleet 404 to the read |
| Deploy again (`?from=`) (PRD #2560) | implemented: 8 tests |
| Golden Search handoff (`?golden_qualification=`) (#2696) | implemented: 9 tests |
| Per-account session draft (H9) | implemented: "keeps the form across leaving Deploy and coming back…" |
| Evidence-only durable override (decision 2026-08-24, #1702) | unverified for **Live** (Paper and Shadow named; code is mode-agnostic) |
| Lane-fence refusal / rebind handling (#2068, #2106) | **partial: the 409 branch reads only the nested FastAPI `detail`; the fleet body is flat (F1-3)** |
| Receipt, "Prepare a new deployment", focus (#2551) | implemented |
| Account switch while a Deploy is in flight (same component instance; H9) | **missing** (F1-2): no guard in `submit`; no spec |
| "What each exit step does" popover (PRD #2560) | unverified: no unit spec asserts it (`DEPLOY_VIEW` has no `exit_steps_summary`) |

### Cut candidates

| path::symbol | evidence | orphans | confidence |
|---|---|---|---|
| `deploy-paper-access.component.ts::accountId` (`input.required`, L60) + host binding `[accountId]="view.account_id"` at `alpaca-deploy-workflow.component.html:100` | `rg accountId` in the component ts/html → ts:60 (declaration), ts:81 (`target.accountId`, a different member); a mechanical scan of all 14 children found it the only unread input. Added 2026-08-23 (`f709701f`). Also found by reader F2 | ten spec sites in `deploy-paper-access.component.spec.ts` (setting an unknown input throws) | **certain** |
| `alpaca-deploy-workflow.component.ts::'clerk_routing_epoch_conflict'` (literal, C:2172) | `rg routing_epoch_conflict` → this line only; no backend emits it | none | probable; fix with F1-3 (it sits in a branch that cannot fire) |
| `alpaca-deploy-workflow.symbol-scope.spec.ts` "keeps the readiness pane rendered while the scoped view reloads" (SS:326-342) | asserts only that "Loading Deploy" is absent and the What heading present; `currentView()` falls back to `lastLoadedView`, so the loading branch is unreachable after a first load | one `it` | probable; not money path |
| `alpaca-deploy-workflow.component.spec.ts:719-720` inline `axe.run` | owner ruling on accessibility | none | needs ruling |

### Kept on purpose

`SUBMISSION_KEY_RE` export (real parity pin); fixtures `DEPLOY_VIEW`, `LIVE_DEPLOY_VIEW`, `scheduledEndView`, `previewedEnd`, `DEFAULT_END` (used by `tests/e2e/support/owner-walk-world.ts` and the deploy-tab spec); `laneReviewRequired` input; `refusalOutcome`/`REFUSAL_OUTCOMES`; every class member and module-level helper of the host (checked mechanically); every SCSS selector of the host.

### Finding rows

| id path::symbol | lens | severity | evidence | disposition |
|---|---|---|---|---|
| **F1-1** `checkSubmission` C:2227-2230; `recoveryNotCommitted` C:1436-1441 → `recoverySettled` C:1449-1454 → `releaseSubmission` C:1551 | 3 (gate fails open) | **P1, reproduced; filed** | the status read frees the idempotency key on any HTTP 404. The call goes through `operationUrl('bot_deploy_submission_read', …)` (a fleet operation), where `ClerkNotFound` / `ClerkLaneRetired` / `BrokerNotSupported` also answer 404 with a flat `{reason, message, next_step}` body (`app/broker/fleet/errors.py`). The route's real "never claimed" answer is a 404 with a string detail and no code (`routers/broker_v2_panel.py:571-575`). Run on `8784061c`: a flat `{reason:'clerk_not_found'}` 404 → "Not deployed" and the "Check deployment status" button gone (key freed) | owner-ruling: needs a typed code on that 404 (backend); interim, treat a 404 carrying a fleet `reason` as unknown |
| F1-2 `submit` C:2153; `acceptReceipt` C:2185-2194; `releaseSubmission` C:2198-2202; catch C:2160-2162 | 3/5 | P2 (cross-account write reproduced; the unsettled-key loss follows from the same lines and needs a second account holding an unsettled key) | the instance is reused across an account switch; `submit()` has no account check after `await deployBudgetBot(...)`. Run: a Deploy for PA9 in flight, switch to PA10, the answer arrives → PA10's submission key rotated (`keyOnBBeforeAnswer` ≠ `keyOnBAfterAnswer`) and `receipt` holds PA9's receipt (`receiptAccount: "PA9"`). `checkSubmission` guards this race (`key !== this.submissionKey()`); `submit` does not | fix: ignore a Deploy's answer for a draft that is no longer current |
| F1-3 `toDeployError` C:2496-2522; `settlesSubmission` C:168-170; `admissionFromError` C:2482-2486; conflict branch C:2171-2176 | 5/3 | P2 | all four read the nested FastAPI `error.error.detail`; fleet refusals are flat → `UNKNOWN_OUTCOME` even from the preview step before anything was sent; the lane-conflict branch cannot fire in production; the only spec for it (`alpaca-deploy-tab.component.spec.ts:141`) uses a nested fake. Fails closed | fix: use `deriveActionRejection` / `refusalBody` |
| F1-4 `pickedTradedAsset` C:747-760; `optionsWarningRead` C:777-783 | 3 | P2 | neither resource's `error()` is read anywhere; a failed read renders as "nothing to say" | fix |
| F1-5 `refreshExitTerms` C:662-665; `useDefaultEnd` C:989-992; `reload` C:2105-2109; `paperAccessChanged` C:2111-2114 | 3/5 | P2 | each calls `deployView.reload()` and ignores its boolean, which is `false` while a load is in flight (the hazard `scheduleSymbolScope` handles); a blur landing during the symbol-scope load is dropped; fails closed | fix |
| F1-6 `syncFrozenCommandDrift` C:1847-1867 | 3 | P2 | writes "The account changed. Nothing was sent" even while this attempt's Deploy is in flight | fix with F1-2 |
| F1-7 `validBudget` C:1388-1399 | 3 | robustness | the typed Live phrase is required only when `confirmation_text` is non-null; backend also checks | leave-with-reason |
| F1-8 `ticketForm` C:285-289, C:626-627 | 4/5 | P2 | exit-term ranges and quantity 1..100 are hand copies of `exit_terms.py` / `broker_bots.py:110` with no parity test; the backend `sizing_options[].min/max/default_quantity` are read by no non-spec code; fails closed | leave-with-reason or owner-ruling |
| F1-9 `submit` C:2148 | 3 | robustness | if `router.navigate` rejects, the catch shows "outcome unknown" although nothing left, with no "Check status" button | leave |
| F1-10..F1-23 | 6/7 | hygiene | `[target]="deployTarget(...)"` builds a new object per pass; three retired-feature absence lines in one mixed test (S:405-408); near-duplicate Deploy-key tests S:1200 / S:1851 / S:922 (money path: keep); `symbol-scope.spec.ts:360-367` cannot prove its own claim; six `axe.run` calls (S:719, 1931, 1952, 1962, E:463, O:400) vs AGENTS.md 2026-10-12; real 30 ms `setTimeout` in `options.spec.ts:203`; test-title mismatches; duplicate test helpers (`roomyViewport` ×3, `deferred` ×2, `stepRegion` ×3, `deployButton` ×4); `export` on `DRY_RUN_EXECUTION_MODE`; four file headers cite a "1k-line file rule" AGENTS.md does not have; unguarded `crypto.randomUUID()` (C:559, 2200, 2416); `as RunAdmissionDecision`/`detail as {…}` casts at the HTTP boundary; floating `router.navigate` promises | leave / owner-ruling |

Lens 4: nothing to fix in this slice.

### Owner rulings

F1-1: may the Frontend treat only the route's own "never claimed" answer as settled (needs a typed code on that 404 first)? Six `axe.run` tests: keep or retire? Three tombstone absence lines? Near-duplicate Deploy-key tests: keep all on the money path (default)? Use the backend-authored sizing bounds instead of the hard-coded 1..100?

## Slice F2 — `broker-deploy-page/deploy-*` children, draft store, `deploy-trades.ts`, `deploy-world.ts`, `option-deploy.fixtures.ts` (66 files, all read)

21 features: 15 implemented, 4 partial, 2 unverified. 18 cut candidates (9 certain). 22 finding rows: P2 4, robustness 6, hygiene 12. No P0/P1. All 14 directly composed children are in the host `imports` and rendered; every `output()` has a listener; the public methods `focusRefusal`, `focusDeploy`, `focus`, `refresh` are all called from the host. The draft store is in-memory only (no web storage in `broker-deploy-page`); a stale draft cannot launch with a stale budget or consent. No gate in the slice fails open (start-admission and readiness-section are display-only with the host gating; paper-access fails closed; evidence-override and traded-acknowledgement require the tick and reason). Hard-rule greps over all 66 files: zero hits for `console.`, `eslint-disable`, `@ts-ignore`, `as any`, `TODO`, `new Date`, `toLocale`, `Intl.`, `DatePipe`, `9:30`, `16:00`, fixed offsets, web storage; the only timers are money-step's two `setTimeout`s, both cleared in `onCleanup`.

### Feature table

| feature (requirement) | classification: evidence |
|---|---|
| Strategy choice (native select) (#2581, #2551) | implemented: host spec:1985 |
| Evidence chip (Accepted / Evidence only / Blocked) + blocked sentence (ADR 0021, #2607 D7) | unverified: no spec contains the chip words (R-8) |
| Experimental notice for Deployment Validation (#2607 D7) | implemented: `end.spec.ts:440` |
| One-time strategy permission, review then confirm (#2560 D9, ADR 0059 D2) | implemented: `deploy-paper-access.component.spec.ts` (12) |
| What it trades: same / another stock or ETF / options (#2912, #2913) | implemented: `deploy-traded-asset.component.spec.ts`; host spec:2557-2720 |
| Option structure summary, builder drawer, preview (#2915, ADR 0076) | implemented: `deploy-option-structure.component.spec.ts` (19) |
| Schema-driven strategy settings (#1701, #1709) | partial: schema min/max not enforced in the browser (R-7) |
| Where it trades, Position size, carryover (#2581, H17) | implemented: host spec:2249 |
| Option order terms (4 boxes) (#2915) | implemented: spec (12) |
| Bot end (#2607) | implemented: `end.spec.ts` |
| Money: facts, bar, shortcuts, typed-amount preview, Refresh (#2560 D12) | implemented: `deploy-money-step.component.spec.ts` (24) |
| Money: bounded price-wait re-check (#2559) | partial: stops after 15 asks and says nothing (R-4) |
| Funding answer + option review (#3099) | implemented: unknown figures worded, never zero |
| Confirm: review list, blockers, typed Live consent, Deploy (#2560 D8) | implemented: host spec:578 |
| "Every check" popover (#2581) | partial: host spec:568 asserts the popover and one label |
| "Last Start check" block (#2901) | unverified: no spec contains "Last Start check", "Start allowed", "Start blocked" |
| Evidence-only human override (decision 2026-08-24) | implemented: host spec:2065, 2089 |
| Untested traded-asset acknowledgement (#2912) | implemented: host spec:2575; `options.spec.ts:484,512` |
| Launch receipt (#2551, H12, H18) | partial: a `failed` receipt renders the success layout (R-1) |
| Session draft, per account, in memory (H9, #2551) | implemented: host spec:1816; `deploy-draft.store.spec.ts` |
| Step chrome (#2581) | implemented: host spec ~:540-565 |

### Cut candidates

| path::symbol | evidence | orphans | confidence |
|---|---|---|---|
| `option-deploy.fixtures.ts`: `OPTION_REVIEW_AWAITING` (L249-271), `OPTION_REVIEW_NOT_ASKED` (L273-302), `OPTION_REVIEW_BLOCKED_WITH_ROWS` (L363-464), `OPTION_REVIEW_ENTRY_SKIPPED` (L634-725), `MODE_REFUSAL` (L802-815), `BLOCKER_REFUSAL` (L817-830) | `grep -rnw <name> Frontend` → one hit each, the declaration; the file has three importers (`tests/e2e/options-ui-deploy.spec.ts:11`, `alpaca-deploy-workflow.options.spec.ts:38`, `deploy-money-step.component.spec.ts:15`) and none imports these names. 275 of 845 lines | none | certain |
| `deploy-paper-access.component.ts:60::accountId` | see slice F1 | host html:100; 12 spec render calls | certain |
| `deploy-evidence-override.component.scss`: `.evidence-override__heading > label` (L20-30), `h2` rules (L32-40), `.evidence-override__reason > span` (L65-70) | the markup has `<span aria-hidden>!</span>`, `<h3>`, and `<label>/<textarea>/<small>/<em>` under `__reason` — these rules match nothing today, so the "!" badge and heading are unstyled | none | certain dead; better re-pointed than deleted (a styling defect) |
| `deploy-launch-receipt.component.scss:23` `span` in `span, dt {}` | the html has no `<span>` | none | certain |
| `deploy-option-money-review.component.html:24` `[attr.data-state]` | nothing reads it | none | probable |
| eight tests: `deploy-traded-asset.component.spec.ts` :63, :26, :55, :69, :87 (duplicates of host spec:2612, 2557, 2650 and `options.spec.ts:207, 311, 404, 431, 662`); `deploy-option-order-terms.component.spec.ts:188` (survivor `options.spec.ts:329`); `deploy-paper-access.component.spec.ts:177` (identical to :122, `it.each` better); `deploy-draft.store.spec.ts:27` | each names its survivor | none | probable; money-adjacent, "when unsure, keep" |

### Finding rows

| id path::symbol | lens | severity | evidence | disposition |
|---|---|---|---|---|
| R-1 `deploy-launch-receipt.component.{html,scss}` | 3 | P2 | the backend authors `status:"failed"` receipts (`budget_deploy.py:585,600`: "{sid} did not launch" / "Nothing is running. Its budget is released."); the template has no tone: green disc, teal panel, and "Set aside $X" / "First deployed" rows for a bot that never launched; no spec for `failed` or `pending` | fix: tone by `status` |
| R-2 `deploy-paper-access.component.ts:196-218 toFailure` | 3/5 | P2 | parses only nested `{detail:{message…}}`; a typed fleet refusal (flat shape) falls to "No answer came back… Check your connection", which is false; fails closed | fix: use `refusalBody` |
| R-3 `deploy-draft.store.ts` + host restore | 3 | robustness | in-memory `Map`; restores `executionMode:'live'` if the owner had picked it (the typed phrase and fresh review are still required) against the store comment "Live is always the owner's own click" | owner ruling |
| R-4 `deploy-money-step.component.ts:70-71,237-247` | 3 | P2 | the price re-check stops after 15 asks and the "Wait for a fresh IBKR price…" line stays unchanged | fix (one sentence) |
| R-5 `deploy-parameters-section.component.ts:111-116,147-157` | 3 | P2 (fails closed) | a stale "invalid parameter" flag survives a strategy switch to a same-named field and keeps Deploy disabled over a valid default | fix: reset on schema change |
| R-6 five `crypto.randomUUID()` sites | 5 | robustness | unguarded; siblings guard it | owner decides whether LAN-http is supported |
| R-7 `deploy-parameters-section.component.html:60-61` | 3 | robustness | `[attr.min]`/`[attr.max]` on `type="text"` are inert; `Number()` also accepts `0x10`, `1e3`; the server enforces | leave or add client check |
| R-8 `deploy-binding-strip.component.html:17-27` | 3 | robustness | `@default` shows "✓ Accepted evidence" for `accepted`, no selected strategy, and any future union value | fix: `@case ('accepted')` |
| R-9 `deploy-money-step.component.ts:173` | 3 | robustness | `risk_revision ?? 0`; the server refuses a mismatch | leave |
| R-10 `deploy-start-admission.component.html:9-12` | 3 | robustness | shows 4 of the 6 fact ages | owner ruling |
| R-11 `option-structure.scss:61`, `traded-asset.scss:60`, `evidence-override.scss:12,27,79`, `traded-acknowledgement.scss:12,27,71` | 7 | hygiene | `var(--surface-card)` is declared nowhere; tinted panel backgrounds are not drawn (needs a visual run) | fix: `--bg-surface` |
| R-12..R-22 | 6/7 | hygiene | comments citing "H9/H12/H17/H18" (ids defined in no ADR or doc); comments naming a nonexistent `AlpacaDeployDrawerComponent`; a binding-strip scss comment pointing at an absent docstring and spec; `provideFleetDirectory()` provided where unused; real `setTimeout` waits (500/20/20 ms); a tombstone line in the receipt spec; `deploy-draft.store.spec.ts` pulls the whole host graph for one regex; five exports with no outside importer (`DeploySizingUnit`, `deploySizingLabel`, `DeployParameterEntry`, `TradesOnTheWire`, `OptionOrderTermNotes`); a spec title promising "consent fresh" asserting less; fixture prose "several legs are not built yet" is stale (#3103); `defaultOpenGateIds()` re-opens gates the owner folded | leave / fix on touch |

### Owner rulings

Axe tests (`deploy-option-order-terms.component.spec.ts:171`, `deploy-option-structure.component.spec.ts:313`) vs the 2026-10-12 ruling. Is a restored Live selection acceptable? R-1: bug fix in this sweep or a later item? Child/host duplicates added after the kill lists: cut the child tests or keep for speed? R-10: show all six fact ages?

## Slice G — `broker-deploy-page/option-structure/` + `account-desk/` + `account-picture/` + `clerk-transaction-evidence-drawer/` (45 files, all read)

16 features: 9 implemented, 3 partial, 1 missing, 2 unverified, 1 dead mode. 8 cut candidates (2 certain). 21 finding rows: **P1 1 (filed)**, P2 6, robustness 6, hygiene 6, pointer 1. No P0. No kill-list row and no `known-gaps.md` entry names a file in this slice. The Frontend computes no number the backend owns except `insideBounds` (G-F10) and `SYMBOL` (G-F11). Lens-4 verdict on the local-calendar-day filter in the transaction history: **not a finding** — `recorded_at_ms` is an instant, not a trading date.

### Feature table

| feature (requirement) | classification: evidence |
|---|---|
| Structure builder drawer: named structure or Custom, edit legs, add/remove up to `max_legs` (ADR 0076 #17, #2915) | implemented: builder spec "fills the legs of a named structure…", "calls an edited structure Custom…" |
| Row problems said in words, tied to their box (#2915) | implemented: `option-structure.spec.ts` `it.each` "holds no legs while a row has %s" |
| "What this picks right now" preview with bounded self-asking (ADR 0076 #17-18) | implemented: builder spec "asks again after the wait the server names and no sooner…", "stops asking by itself after a bounded run…", "asks nothing while the page is hidden…" |
| "Use this structure" gated on the server's verdict (ADR 0076 #17) | implemented: "will not use a structure the server marks red", "…not judged yet", "…whose ask failed" |
| Figures withdrawn (or restated "read at") past `figures_good_until_ms` (ADR 0076 #17) | partial (G-F03) |
| Option order terms pre-checked against catalogue ranges | implemented (a TS copy of the Python rule, G-F10): `option-structure.spec.ts` |
| Preview request built from the page; "nothing is asked yet" reasons (#2913) | implemented |
| Compact preview in Deploy (#2913) | unverified from this slice |
| Transaction history table: load whole window, client search/sort/filter, per-row evidence (#2560 Activity; ADR 0033) | **partial (G-F01)**: reached from `components/brokers/alpaca-desk/activity/alpaca-activity-records.component.html:24` |
| In-component period control Today / 30D / 60D reading `getPortfolioHistory` | **dead mode (C2)**: the only host binds `[showScopeControl]="false"` |
| Local-calendar-day filter on "Recorded (local time)" | implemented (registered by name, so not dead): "derives a local calendar day only while matching canonical timestamp values" |
| External-order acknowledgement from the receipt (fence frozen at render) (ADR 0033, #2106) | implemented: five tests incl. "sends the generation shown at render, not the one current when the receipt is opened" |
| Receipt evidence drawer: custody timeline A0..A3, facts, clocks, 3 accordions (ADR 0033) | partial: renders markers no producer writes (G-F02, C5) |
| Custody clocks 4 of 12, A1 stage, origin/observed epoch rows (ADR 0033) | unverified: no producer in `PythonDataService/app` |
| Account picture card (#3101) | implemented: `account-picture.component.spec.ts` (7) incl. "says an unknown figure is unknown, never zero" |
| Option endings (assigned / exercised / expired) shown in Activity rows (ADR 0076 #22) | **missing** (G-F04): `option_event` is served on every row and no Frontend code reads it |

### Cut candidates

| id path::symbol | evidence | orphans | confidence |
|---|---|---|---|
| C1 `option-structure-preview.ts::PREVIEW_ASK_AGAIN_FLOOR_MS` (L43) | `rg` → the definition only | the named import `ASK_AGAIN_FLOOR_MS` on L12 (the constant stays: `ask-again.ts:16,27`) | **certain** |
| C2 `account-desk-transaction-history.component.*` `showScopeControl` mode | `rg showScopeControl` → component, its spec, and the single host binding `false`; `getPortfolioHistory` callers: service def, this component, its spec | `HistoryScope`, `SCOPE_OPTIONS`, `SCOPE_CONFIG`, `selectedBrokerHistory` resource, `rangeUnavailable`, html 8-19/26-31, scss `.scope-control*`, one spec test, and it strands `brokers.service.ts::getPortfolioHistory` and operation `portfolio_history_read` (route and catalog snapshot stay) | probable (precedent #2708 "modes no host turns on") |
| C3 `clerk-transaction-evidence-drawer.component.scss` `.raw-receipt`, `.event-receipt` (L31, 33, 35, 36, 44) | the classes exist only in `clerk-receipt-evidence-accordions.component.html`, a different component with its own encapsulation; the accordions' own scss already carries the rules | none | **certain** |
| C4a `option-structure.spec.ts:206-210` "an option preview's figures" | one case; survivor builder spec:327-337 asserts the same ages and more | `quoteAgeSeconds` import | probable |
| C4b `option-structure-builder.component.spec.ts:607-616` | survivor :719-731 asserts the same count and more | the `as unknown as` cast | probable |
| C5 drawer `epochProvenance`/`recordedEpochs`/`isReceiptRecord`, `.epoch-provenance`, 4 of 12 `custodyClocks` rows, A1 stage | `rg origin_epoch\|observed_epoch\|clerk_boot_id\|epoch_seq PythonDataService/app` → 0; the four clock fields are never set by either `ClerkCustodyTimeline` constructor | spec `boot-a / 1, boot-b / 2` | probable for epochs; conditional for clocks/A1 (waits on the Python schema; ADR 0033 text is IBKR-era) |
| C6 `presentFeed` cases `reconnecting`, `rebuilding`, `stale`, `offline_but_saved`, `corrupt` + `.feed-status--attention` | Python emits only `live` and `projection_unavailable` | exhaustive-switch typing | needs-a-run; waits on the contract |
| C7 `option-structure-preview.ts::PREVIEW_ASK_AGAIN_LIMIT` | a second name for `ASK_AGAIN_LIMIT` | two specs repoint | probable (not delete-only) |

### Kept on purpose

`matchesLocalDateMs` (registered by name via `filterService.register('sameLocalDateMs', …)`, a string-dispatch registry counts as a caller); `ASK_AGAIN_CEILING_MS` (live inside `askAgainWaitMs`); `option-structure.fixtures.ts` (5 specs and e2e); `option-preview-display.ts::ageSeconds` (used by `exit-estimate`); type-only exports in `option-structure.ts`; `FigureRowComponent.showProvenance`; `optionExpiryAnchorMs` (has a parity spec naming the Python file).

### Finding rows

| id path::symbol | lens | severity | evidence | disposition |
|---|---|---|---|---|
| **G-F01** `account-desk-transaction-history.component.html:36` | 3 | **P1, filed** | the error banner is `@else if (store.errorMessage() && !store.hasLastGood())`; with a last-good feed the next branch (`:41`) renders the old rows and the old feed banner. The store keeps `rowsState`/`feedState` on failure and only sets `errorState` (`store.service.ts:134-137`); Python hardcodes `feed_state="live"` (`sqlite_clerk_transaction_projection.py:128,468`), so after a failed refresh — the refresh that follows a recovery action — the table stays headed "History is current". No spec combines `errorMessage` set with `hasLastGood` true | fix: render the error and Retry above the table whenever set |
| G-F02 `clerk-receipt-custody-timeline.component.ts:57-69` | 3 | P2 (borderline P1) | the template says "A missing marker is shown as not recorded, never inferred", but the stage after the last recorded one reads "Awaiting"; the SQLite projection never sets `broker_write_started_at_ms`, so A1 reads "Broker write started: Awaiting" for every order with A0 and no ack, including an unknown-outcome order; the drawer spec fixture sets all 12 clocks (a shape no producer emits) | fix |
| G-F03 `option-preview-panel.component.html` figures block | 3/1 | P2 | the server contract says a page past `figures_good_until_ms` shows when the quotes were read in place of the figures or asks again (`option_preview.py:459-462`); the panel does neither, and a `resolved` answer names no wait | fix or owner ruling |
| G-F04 `api/clerk-transaction-history.types.ts` + `toTableRow` | 5/1 | P2 (owner ruling: missing feature) | generated `ClerkTransactionSummaryRow` carries `option_event: OptionEndingView`; the hand-typed Frontend type omits it and no reader exists; an assignment/exercise/expiry row shows "No request details recorded" | owner-ruling |
| G-F05 `clerk-receipt-custody-timeline.component.ts:33-36` | 3 | P2 | "The Clerk durably accepted the instruction." is shown as A0 Recorded for externally observed orders too | fix |
| G-F06 `clerk-receipt-evidence-accordions.component.html:11-12` | 3 | P2 | the "Execution" column of the Instrument row renders the instruction's symbol as an observed execution fact | fix |
| G-F07..F09 | 3/4 | robustness | "Manual" for an absent origin while the table renders blank; blank `·` for a missing side/type and raw float prices; a sub-cent `fee` run through the `currency` pipe shows "$0.00" (no authored string; `authored-usd.pipe.ts` header names this) | fix / owner-ruling |
| G-F10 `option-structure.ts:419-443` `insideBounds`/`decimalPlaces` | 4 | P2 | TS re-implementation of `PythonDataService/app/options/bounds.py::Bounds.fault` comparing JS doubles where Python compares `Decimal`; no ADR 0068 row, no parity test naming the Python file; fails closed and the server re-judges | fix: add the row and a parity spec |
| G-F11 `option-structure.ts:473` `SYMBOL` | 7 | hygiene | the same regex is at `components/brokers/alpaca-history/bot-history.service.ts:55`, copying `app/broker/contract/models.py:65` with no parity test | fix |
| G-F12..F21 | 3/5/6/7 | robustness/hygiene | both catches drop the cause (`catch {`, `void error;`) on a custody acknowledgement; unguarded `crypto.randomUUID()` in a field initializer; store spec has no failure case; drawer spec fixture populates unproduced clocks; `account-picture.component.spec.ts:8,143` cannot fail under UTC (no TZ pin); ten-state `it.each` over one branch; PrimeNG `<p-column-filter type="date">` (not `app-date-field`); classes in templates with no rule (`.picks__flag`, `.account-picture__groups`, `.receipt-accordions`); a Deploy-agnostic module imports from a component file; hand-written duplicate of generated types already drifted (`option_event`, `lag_is_lower_bound`) | leave / fix |

### Owner rulings

Option endings in Activity: build a reader for `option_event`, or record that Activity does not show them? Custody drawer: retire the epoch rows, the four never-populated clocks and the A1 stage, or keep until Python drops the fields? Options preview: withdraw figures past `figures_good_until_ms`? `showScopeControl`: cut under the #2708 precedent (strands `getPortfolioHistory`)? Three axe-core cases in the builder spec under the 2026-10-12 ruling.

## Slice H — `broker-options-chain/`, `broker-options-surface/`, `deployment-budget/`, `exit-estimate/`, `fee-attribution/`, `funding-answer/`, `money-bar/`, `lib/manual-order-navigation`, `format`, `operation-error` (33 files, all read) + module-wide jobs

13 features: 9 implemented, 2 partial, 2 unverified. 8 cut candidates (4 certain). 20 finding rows: P1-candidate 1 (rated P2, see below), P2 8, robustness 5, hygiene 6. No P0.

**Module-wide job A — routes and rendering.** 87 `@Component`/`@Directive` declarations under `components/broker/**`. `loadComponent`/`loadChildren` into the module exist only in `app/app.routes.ts` (`:437` bot page, `:523` options chain, `:530` options surface). Routed 3; rendered by a parent template 84, each with at least one non-spec template hit. The "rendered nowhere" list is **empty**.

**Module-wide job B — kill list #2708 against master.** All cut by `2b6f0752` (#2754) except `format.ts::diffBps` (still on master, test-only; kept by the owner addendum): `alpaca-account-activation`, `fmtDateNy`, `toleranceBand`, `getLiveChart`, `laneUrl`/`accountUrl`, `snapshotAge`, `__TEST__`, `submissionFinished`, the per-bot fee mode, the `/actions/quiesce` fallback and the channel-name fallback are gone. Rows 21-22 were conditional; the restart confirmation is recorded in the cut commit and cannot be re-verified from the tree. Home's clear-outcome `busy` is bound (`home-finished.component.html:72`).

**Module-wide job C — `docs/known-gaps.md`.** Only two entries name a Frontend path: F12 (stale LIVE chart bars, anchor `dual-pane-chart.component.html`) is **closed** (2026-09-24, #2355/#2401) and sits in the open list — a docs-sweep candidate; F5 (refusal ordering) carries a stale half-sentence ("preview … unused for refusal shaping" — Deploy calls `previewStartAdmission`). F3, F17, F14, T2/O4, F7/F8 are unverified or true from this module. No entry names a file in slice H, so H-F1 below is untracked.

### Feature table

| feature (requirement) | classification: evidence |
|---|---|
| Options Chain (live) (AGENTS.md IBKR decision 2026-09-16) | **partial**: the body is unreachable — `canStream()` needs `health().connected`, which is never populated (H-F1) |
| Options Surface (3D) (PR #481) | **partial**: same gate (H-F1); the spec covers two helpers only |
| Engine reprice column (chain) (#2764) | unverified: no spec; silent blank on any failure |
| `absDiff` / `deltaAbsBand` | implemented: `format.spec.ts` |
| Shared formatters (ADR 0022) | implemented: wording owned by `shared/timestamp/timestamp-display.spec.ts` |
| Money bar, 4 sizes (PRD #2560 D6/D12) | implemented: "draws each slice at the backend's basis points, with no arithmetic of its own", "never states a shortfall the backend did not send" |
| "This bot's money" card (PRD #2560 D6; #3097, #3100) | implemented: `deployment-budget.component.spec.ts` |
| Funding answer (#3097, #3099) | implemented (no own spec): unknown = no amount, no `$` |
| Exit estimate (#3100) | implemented: spec (5) |
| Fee attribution card (PRD #2560) | implemented: "never turns an unavailable projection into no fees" |
| Manual-order link: build / parse (H20) | build implemented; parse **unverified** (no spec imports `parseManualOrderTicketQuery`) |
| `extractServerMessage` (#2067/#2081/#2102) | implemented: `operation-error.spec.ts` (5); 10+ consumers in `components/options-backtest/*` |

### Cut candidates

| path::symbol | evidence | orphans | confidence |
|---|---|---|---|
| `broker-options-surface.component.spec.ts` L59-74 `describe('projected line-count cap budget')` | mock theater: `project` is a lambda declared inside the test; no import of `projectedLines`/`overCap`/`LINE_CAP` | nothing | **certain** |
| `broker-options-chain.component.ts::setupLoading` (L104, writes L217, L233) | `grep -rn setupLoading` outside the surface → only those 3 lines; the chain html has 0 hits | decl + 2 sets | **certain** |
| `broker-options-chain.component.scss` `.error{}` L78-82 | no `class="error"` / `[class.error]` in the chain html (errors use `app-section-error`) | 5 lines | **certain** |
| `broker-options-chain.component.scss` `.disabled-banner a{}` L252-256 | no `<a>` / `routerLink` in the html | 5 lines | **certain** |
| `format.spec.ts` L9-13 `fmtBrokerExpiryDate(null)` | wrapper is `formatTimestampDisplay(ms, {mode:'date-et'})`; null → `—` owned by `timestamp-display.spec.ts:55,127` | spec import only | probable |
| `format.ts::diffBps` + `format.spec.ts:5,33-37` + docstring | test-only export; the assertion pins pre-fix behaviour | import + 5 spec lines | probable — **#2708 row 11, kept by the addendum** unless the assertion is dropped |
| Resume branch: chain html:87-94 + ts:283-289; surface html:113-117 + ts:373-376 | `paused` becomes true only in `stopStream()`, which nulls `currentStream` first, so Resume is disabled or never drawn | `togglePause`, `paused` branch | needs-a-run; fix-vs-cut is the owner's call |
| `fee-attribution` lifetime mode (`period === null`) and `headingLevel` default 3 | one host binds `period` and `headingLevel=2` | `BrokersService.getFeeAttribution` default null; spec cases 1/3/4 use the default (case 3 is the "unavailable is not zero" spec) | probable; money-path spec adjacent, leave |

### Kept on purpose

`echarts-gl.d.ts` (no types ship with `echarts-gl`; `surface.ts:443` imports it under `strict`); `pickStrikesAroundAtm`/`intersectAll` (called by `setupSurface`; only the `export` is for the spec); `MoneyBarSize`, `MoneyBarNotes`, `BotOpenPnl` (type the component's own inputs); `fmtSignedQuantity`; `deltaAbsBand`/`ToleranceBand`/`absDiff`; `operation-error.ts` (consumers in `options-backtest`); `$safeNavigationMigration(...)` ×8 (compiler built-in); the `LINE_CAP` pre-check.

### Finding rows

| id path::symbol | lens | severity | evidence | disposition |
|---|---|---|---|---|
| **H-F1** `services/broker-health.service.ts:55-60` / both pages | 1/3 | P2 (strict reading: fails closed; the reader flagged it as a P1 candidate) — **top finding** | both options pages gate their whole body on `health()`; `BrokerHealthService.health` is written only by `refresh()`, called only by `start()` and its own interval; `grep -rniE "broker-health\|BrokerHealthService" Frontend/src Frontend/tests` → the two pages (inject + read), the service, its spec — **no `start()` caller**. `8f66ef63` (#2167, 2026-09-15) removed the shell's poll when `/api/broker/health` 404'd, deferring the pages' reads to #2077; the 2026-09-16 owner decision retained IBKR market data and the route is back (`routers/broker.py:88`), but the Frontend poll was never restored. Both pages — in the app menu — say "Broker is disconnected. Connect IB Gateway… refresh the Status page", a page that no longer exists. No spec renders either page | owner-ruling: restore (start the poll + a render spec) or retire; do not cut without the owner |
| H-F2 `surface.ts:213-218,230-242` | 5 | robustness (latent behind H-F1) | the auto-load effect retries `loadExpirations()` with no backoff on every failed or empty read | fix |
| H-F3 `chain.ts:200-205,344-370` | 4 | P2 | reprice uses `selectedExpiry()` not `snap.expiry_ms`; changing Expiry while streaming reprices new keys at the wrong expiry → false red `\|ΔΔ\|` | fix |
| H-F4 `chain.ts:358` `q.iv ?? 0.2` | 4/3 | P2 | a made-up 20% vol feeds the engine when IBKR sent no IV; the comparison ignores `greeks_source` | fix |
| H-F5 both pages' Pause/Resume | 1/2 | P2 | Pause drops the stream and clears the picture; Resume is disabled or not drawn | owner-ruling |
| H-F6 `surface.html:140-145` | 2/5 | P2 | `label="Setup failed"` — `SectionErrorComponent` has no `label` input; `canRetry` defaults true with no `(retry)` → an inert Retry button | fix |
| H-F7 `surface.ts:99,214-218,277-329` | 1/5 | P2 | editing Symbol only does `symbol.set`; `allExpirations`/`readyExpiries`/`readyStrikes` stay the old symbol's; Load Surface streams the new symbol with the old expiries | fix |
| H-F8 `surface.ts:296-304` | 3 | P2 | with no snapshot, "spot" is the median strike of the nearest expiry, captioned "Spot $x" | fix |
| H-F9 `surface.ts:210,429-453` | 3/5 | P2 | `renderCharts()` floating promise; a failed `import('echarts')` = blank canvases + an unhandled rejection | fix |
| H-F10 `chain.html:175-181` | 3 | P2 | `freshness="live · last tick …"` whenever a snapshot exists, including `streamStatus()==='error'` | fix |
| H-R1..R5 | 3/5/1 | robustness | per-keystroke `loadStrikes` with no debounce; `catch {}` with a comment and no log on the reprice path (`quantlib.service.ts:94` throws on a GraphQL `errors` body); "monthly" expiries thinned by a 6-day gap (daily expiries give one); the 100-line cap is the whole shared IBKR allocation; `deployment-budget.component.spec.ts:313-315,494,496` absence lines for strings that exist nowhere | owner-ruling / fix |
| H-H1..H6 | 6/7 | hygiene | no spec renders either options page (why H-F1 went unseen for a month); `parseManualOrderTicketQuery` untested; `FormsModule`/`[ngModel]` ×4 and `$any($event.target)` ×3 vs angular.md; a "follow-up to PR 2" comment with no issue; `debounceMs` signal never set; `Intl.NumberFormat` built per call (~8×/row per 500 ms) | leave / fix on touch |

Nothing to fix, by lens: lens 3 money cards (unavailable is never zero); lens 4 time (`date-et` for expiry and fee period start, `et` for NY stamps, shared `app-timestamp-display` elsewhere; no `new Date(string)`, `DatePipe`, `Intl.DateTimeFormat`, `toLocale*`, fixed offsets or hard-coded session times).

### Cross-module pointers

`services/broker-sse.ts:101-117`: a named server `error` frame sets `status='error'` but never `source.close()`, so the EventSource auto-reconnects (~3 s) and the server re-errors until Pause or leave. `services/broker-health.service.ts:52` docstring ("The shell calls this once on app boot") is false. `market-data-feed.service.ts:30`: `expirations(symbol)` is not `encodeURIComponent`'d. Components used only from `components/brokers/` (later wave): `account-desk-transaction-history`, `account-picture`, `alpaca-deploy-workflow`, `fee-attribution`, `cohort-flatten-drawer`. Python wave (#2706): `routers/brokers.py:324-340` and `services/alpaca_fee_reconciliation.py:27-48` keep a per-bot read whose Frontend caller was cut in #2754.

### Owner rulings

Options pages: restore (start the health poll, then fix H-F2..F10) or retire — the reader recommends a one-line restore plus a render spec, not a cut. `diffBps`: drop the pre-fix assertion and the function, or keep under "test-only helpers follow their tests"? Pause: relabel to Stop, or really pause? Is a 100-line surface cap the right ceiling for a diagnostic page on the shared allocation? `deployment-budget.component.spec.ts` absence lines: tombstones or keep? `FeeAttribution` lifetime mode: cut with a spec re-point, or leave?
