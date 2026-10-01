# Kill list — dead code in the Frontend broker surfaces (#2708)

Part of the lean-and-mean map (#2700). Plan, don't cut.

- **Read at:** `87b8e261021ec673c2e7c80448c0973bccd45378`, which is both `origin/master` on 2026-09-30 and the map's charting SHA. Every `file:line` below is at that SHA.
- **Area:** `Frontend/src/app/components/broker/`, `components/brokers/`, `fleet/`, `shell/`. That is 178 non-spec `.ts` files plus their templates and styles.
- **Bar:** dead means nothing reachable uses it. A symbol whose only callers are specs is dead. Dead beats sacred, so each item's tests are rows here too.

## How the evidence was gathered

All tools were throwaway scripts in the session scratchpad. Nothing was added to the repo.

1. **Import graph from `src/main.ts`.** Static and dynamic `import()` were followed, including every `loadComponent` in `app.routes.ts`, with specs excluded. Of the 178 area files, 5 are unreachable: the activation component (row 1), two spec-support files and two ambient `.d.ts` files (see "Considered and kept").
2. **How components are reached.** Each `@Component` selector was matched against every non-spec template, inline or `.html`. Each class was also checked against route, `createComponent`, dialog and outlet references. Only `app-alpaca-account-activation` has no reach.
3. **TypeScript `findReferences`.** I ran the language service on the worktree's sources, using the main checkout's `node_modules/typescript` (6.0.3) read-only, over the whole `Frontend/src` program. For each class member and each top-level declaration, it counted references from non-spec files only. Members not referenced in TypeScript were then checked against templates: the bare name in the component's own template, or `.name` anywhere else.
4. **Inputs and outputs.** I listed optional `input()`s that no host template binds, and `output()`s that no host listens to.
5. **Signals.** I listed writable `signal()`s that are never `.set`/`.update`d.
6. **Old-build shims.** I grepped for comments describing retired-mode or old-build compatibility. Each hit was then traced by hand.

## Kill list

### A. Dead component

| # | Path | Kind | Evidence |
|---|---|---|---|
| 1 | `Frontend/src/app/components/brokers/alpaca-desk/alpaca-account-activation.component.ts`, `.html`, `.scss` | component | No template, route or opener uses selector `app-alpaca-account-activation` (`.ts:21`). Its only importer is its own spec. Its one host, `alpaca-desk-account-state`, was deleted in `fc1138d9` (#2562, "remove the Overview desk's orphaned pieces"), and this child was missed. |
| 2 | `Frontend/src/app/components/brokers/alpaca-desk/alpaca-account-activation.component.spec.ts` (whole file) | test, kind 5 | It only exercises row 1. |

### B. Branches kept for a retired mode

| # | Path | Kind | Evidence |
|---|---|---|---|
| 3 | `components/broker/v2-panel/gallery/lib/gallery-live-store.service.ts`: the wall state Home never reads, namely `bots` (`:194`) / `botsState` (`:186`), `upsertBots` / `dropRemoved` / `withReportedFeed` / `UNREPORTED_FEED` / `LaneBotView` (`:122-157`, including the pre-#2330 "older build omits `feed`" shim), `resolution` (`:197`) / `resolutionState` (`:189`), `status` (`:206`) / `statusState` (`:190`) and every `.set` on it, `frameStaleState` (`:191`) with `trackFrameAge` / `clearFrameAge` / `frameAgeTimer` (`:224`, `:338-350`) / `FRAME_STALE_AFTER_MS` (`:30`), and `refusalReason` (`:212`) / `refusalReasonState` (`:192`, `.set` at `:265`, `:394`, `:490`) | retired-mode state | The store's only injector is `alpaca-home.component.ts:124,141`. Home reads only `barsBySymbol` and `markersBySid` (`alpaca-home.component.html:68,85`) and calls only `start` / `stop` (`.ts:258,262`). `findReferences` finds no non-spec reader of the rest. That state fed the retired Gallery page, whose routes now redirect to Home's wall (`app.routes.ts:411,428`). The transport (bootstrap, SSE, paging, refusal → poll fallback, lane isolation) stays. |
| 4 | `gallery-live-store.service.spec.ts` cases whose subject is only that dead state: "gives a bot from a lane that reports no feed an attention-required unknown feed…", "reads stale once the newest frame ages past twice the poll interval…" and "never reports live while a relay opens then aborts the stream…" | test, kind 5 | They assert only `bots()` / `status()`, which row 3 removes. Other cases in this file prove live transport through those same accessors; see Hazard H1. |
| 5 | `components/broker/v2-panel/gallery/lib/candle-renderer.ts`: the interactive-tile paths `barIndexAtX` (`:171`), `layoutTag` (`:191`), `roundedRectPath` (`:231`), `drawCrosshair` (`:368`), `drawLastPriceTag` (`:393`), `draw`'s `hoverIndex` parameter (`:439`) and its branch (`:449-451`), the `showLastPriceTag` branch (`:452`), the config keys `crosshairColor`, `crosshairDash`, `guideDash`, `tagTextColor`, `tagFont`, `tagPaddingX`, `tagHeight`, `tagRadius` and `showLastPriceTag`, and `ChartScale.tagHalfHeight` (`:34`, set at `:153,166`) | retired-mode branch | The only non-spec caller of `draw` is `home-sparkline.component.ts:73`, which passes `hoverIndex = null` and sets `showLastPriceTag: false` (`:64`). Hover and the last-price tag belonged to the retired Gallery tile. `barIndexAtX` has spec callers only. |
| 6 | `candle-renderer.spec.ts`: `describe('barIndexAtX')` (`:106`), `describe('layoutTag')` (`:144`) and the `draw` case "paints the last-price tag by default but omits it when showLastPriceTag is false" (`:223`) | test, kind 5 | They exercise only row 5. The `:201` case "…markers plus an active hover index" drops its hover argument and keeps its marker half. |
| 7 | `components/broker/fee-attribution/fee-attribution.component.ts`: the per-bot mode, meaning the `strategyInstanceId` input (`:33`) and its uses (`:43`, `:49-50`) | retired-mode branch | The sole host, `alpaca-activity-page.component.html:47`, never binds it, so `sid` is always `null`. The bot-page host `<app-fee-attribution … [strategyInstanceId]=…>` was removed in `ce945674` (#2563, "bot page as one view"). |
| 8 | `fee-attribution.component.spec.ts`: "asks for the deployment authority and keeps account uncertainty visible on a bot" (`:45`), plus the helper's `strategyInstanceId` parameter (`:22`, `:27`, `:94`) | test, kind 5 | It proves only the per-bot mode from row 7. |
| 9 | `components/brokers/alpaca-desk/alpaca-order-entry.component.ts`: the `submissionFinished` output (`:102`) and its three emits (`:260`, `:335`, `:376`) | unlistened output | The sole host, `alpaca-manual-order-host.component.html:17-25`, binds no `(submissionFinished)`. Its listener `(submissionFinished)="refreshDesk()"` went with the Overview desk in `363d2d32` (#2562). Removing the emits changes no order behavior. |
| 10 | `components/brokers/alpaca-home/home-clear-outcome.component.ts`: the `busy` input (`:21`) and its template bindings `[disabled]="busy()"` / `[attr.aria-busy]="busy()"` (`home-clear-outcome.component.html:37,39`) | dead input | The sole host, `home-finished.component.html:67`, never binds it, so it is always `false`. See Hazard H5. |

### C. Unused functions, members and types

| # | Path | Kind | Evidence |
|---|---|---|---|
| 11 | `components/broker/format.ts`: `fmtInteger` (`:46`), `fmtSignedInteger` (`:50`), `fmtPercentDefault` (`:67`), `fmtSignedNumber` (`:71`), `fmtDateNy` (`:131`) | function | Zero references anywhere in the repo, specs included. |
| 12 | `components/broker/format.ts`: `fmtDurationRemaining` (`:141`), `fmtElapsedSince` (`:150`), `diffBps` (`:170`), `toleranceBand` (`:202`) | function (spec-only) | Their only callers are in `format.spec.ts`. `ToleranceBand` stays because `deltaAbsBand` uses it. The `toleranceBand` doc comment (`:200`) cites `ibkr-frontend-implementation-plan.md` §9.2, which does not exist at this SHA. |
| 13 | `format.spec.ts`: `describe('fmtDurationRemaining')` (`:22`), `describe('fmtElapsedSince')` (`:36`), `describe('diffBps and toleranceBand (kept for unbounded scalars)')` (`:105`) | test, kind 5 | They test only row 12. |
| 14 | `components/broker/v2-panel/lib/broker-v2-panel.service.ts`: `getLiveChart` (`:553`) | service method | `findReferences` finds no non-spec caller. The live chart now arrives inside `getLiveSnapshot` (`BotPanelLiveSnapshot.live_chart`). Only a spec mock key names it (`bot-panel-shell.component.spec.ts:548`). |
| 15 | `fleet/clerk-scoped-url.ts`: `laneUrl` (`:25`), `accountUrl` (`:33`) | function (spec-only) | `operationUrl` replaced them; `operation-url.ts:3-10` says so. The only callers are `resource-target.spec.ts:57,59`. `clerkScope` stays. |
| 16 | `components/broker/broker-options-chain/broker-options-chain.component.ts`: `snapshotAge` (`:178`); `components/broker/broker-options-surface/broker-options-surface.component.ts`: `snapshotAge` (`:154`) | computed property | It is read nowhere: no TypeScript reference and no template use. These pages show the IBKR read-only feed and stay; only this unread computed goes. |
| 17 | `broker-options-surface.component.ts`: `__TEST__` (`:587`) | test-only export | It re-exports `pickStrikesAroundAtm` (`:512`) and `intersectAll` (`:534`), which are already exported. Its only user is the spec (`:2`, `:4`). |
| 18 | `components/broker/account-desk/account-desk-transaction-history-store.service.ts`: `filters` (`:55`) | property (spec-only) | The template reads other `store.*` members but not this one. Only the store spec reads it (`:85`). `filtersState` stays. |
| 19 | `components/brokers/alpaca-desk/alpaca-sqlite-custody.component.ts`: `requireClerkId` (`:117`) | private method | Never called. |
| 20 | Type aliases with no non-spec reference: `broker-v2-panel.types.ts` `OperatorBlocker` (`:40`), `OperatorConfirmationCopy` (`:41`), `StationApplicability` (`:46`), `DutyOutcomeView` (`:56`), `FeedContinuityEventView` (`:61`), `ReadinessCheckView` (`:65`), `ExitTerms` (`:68`); `broker-v2-panel.service.ts` `DeployBotReceipt` (`:80`, the old `AlpacaPaperDeployReceipt`), `MoneyParts` (`:107`); `brokers/alpaca-history/bot-history.service.ts` `FleetBotHistoryGap` (`:16`), `BotHistoryRun` (`:17`); `cohort-flatten/cohort-flatten-confirmation.ts` `CohortFlattenCopy` (`:118`); `gallery/lib/gallery.types.ts` `GalleryResetEvent` (`:102`, plus its mention at `:8`) | type | `findReferences` finds 0 non-spec references for each. Two specs import `ReadinessCheckView` (`bot-details.component.spec.ts:11`, `operator-readiness.component.spec.ts:11`). `panel-action-button.component.ts:22` uses an `OperatorBlocker` from elsewhere, not this alias. |

### D. Conditional: old-build shims (cut once the precondition holds)

When unsure on the money path, keep. These are dead only once no process on an old build is left running. The cutting agent can check that from container start times. The code alone cannot tell.

| # | Path | Kind | Evidence and precondition |
|---|---|---|---|
| 21 | `broker-v2-panel.service.ts`: the quiesce fallback in `runAction`, meaning `LEGACY_ACTIONS_KEY_SUFFIX` (`:59`), `IDEMPOTENCY_KEY_MAX_LENGTH` (`:60`), `isUnroutedNotFound` (`:65`) and the `try/catch` (`:377-390`) | old-build shim, **money path** | It fires only on an unrouted 404 from a coordinator or clerk without `/actions/quiesce`, so only on a build older than `57c72e21` (2026-09-23, #2351). **Precondition:** the fleet coordinator and every clerk have been started since 2026-09-23. Its test, `broker-v2-panel.service.spec.ts:556` ("falls back to /actions under a derived key when the quiesce route is not deployed yet"), goes with it. |
| 22 | `components/broker/v2-panel/bot-page/bot-connections.component.ts`: the `channelName` fallback (`:35-39`) | old-build shim | `ChannelHealthView.name` has been a required `str` since `ee7ae7a4` (2026-09-17) (`PythonDataService/app/schemas/broker_v2_panel.py:277`). The `\|\|` default fires only for an older clerk. **Precondition:** every clerk has been started since 2026-09-17. No spec. |

## What the cuts orphan

- `Frontend/src/app/api/alpaca.types.ts:98`, `AlpacaDeskSelectionSummary`: its only user is row 1. It sits in #2709's area but dies with this cut.
- `Frontend/src/app/services/brokers.service.ts:232-240`, the `strategyInstanceId` parameter of `getFeeAttribution` and its `strategy_instance_id` query branch: its only caller passes `null` after row 7. The mock in `alpaca-activity-page.component.spec.ts:92` names a `_sid` argument and loses it.
- `AlpacaDeskStateResponse.choices`: after row 1 no Frontend code reads it. `staged_choice` stays, because `alpaca-settings-page.component.ts:406` reads it. This is a backend pointer, below.
- The `bot_chart_live` catalog operation: no Frontend caller after row 14. Backend pointer, below.
- No fixture, conftest or config file in the area is orphaned. Row 1's `.scss` is the only stylesheet that goes.

## Hazards the cutting PR must carry

- **H1. Gallery store tests that lose their observable.** Row 3 removes `bots()`, `status()`, `resolution()` and `refusalReason()`. In `gallery-live-store.service.spec.ts`:
  - Seven cases prove live transport **only** through those accessors: "replaces all state on a newer-epoch snapshot…", "bootstraps via REST, opens the stream…", "falls back to 5s polling…", "stops reconnecting and polls instead when the lane refuses the stream", "clears prior state when starting against a different account…", "isolates same-account Clerks…" and "clears state and scopes the reconnect cursor…". Each must either switch its assertion to `barsBySymbol()` / `markersBySid()` or to the HTTP/SSE expectations it already makes, or be deleted.
  - Seven more mix live bar or marker assertions with dead lines. For those, drop only the dead lines.

  Switching the assertion touches the map's "no rewriting surviving tests" rule; see the note to the map.
- **H2. Line-level spec edits, not whole tests.** In these specs a dead symbol shares a test with live assertions:
  - `resource-target.spec.ts:57,59`, inside "rejects incomplete outbound command and account URL targets".
  - `account-desk-transaction-history-store.service.spec.ts:85`, inside "discards a late period response…".
  - The spec imports from rows 17 and 20.
  - The `getLiveChart` mock key at `bot-panel-shell.component.spec.ts:548`.

  Drop only those lines and keep the rest of each test.
- **H3. Run the full Frontend suite.** Rows 7, 9 and 10 change template bindings, and `ng test` runs the template compiler (AOT), which catches a stale binding. Run the full `ng test`, not only the specs for the touched files, before pushing.
- **H4. Money-path files.** Rows 9 and 14 touch money-path files but change no order or custody outcome. Row 21 changes stop/flatten routing, so it ships only once its precondition is verified. Under the map's gate rule, a PR that includes row 21 is a money-path PR.
- **H5. A possible bug behind row 10.** The clear-outcome Retry and Dismiss buttons are never disabled while a retry is in flight, because no host binds `busy`. If that is a bug, the fix is to bind it in `home-finished`, which is a separate issue. The cut keeps today's behavior either way.
- **H6. The IBKR feed is sacred.** Nothing here touches the IBKR read-only feed or its plumbing. The options chain and surface pages (`app-menu.ts:58-59`) and the live tape stay.
- **H7. Re-check before cutting.** Kill lists age. Re-check every row at the cutting SHA, because the account-workspace slices (#2560) are still landing in this area.
- None of these Frontend cuts regenerate an OpenAPI or GraphQL snapshot. The backend pointers below would.

## Considered and kept

- **Bookmark redirect routes.** `BrokerLaneUnavailableComponent` (`fleet/broker-lane-unavailable.component.ts`) and the redirect guards (`alpacaSurfaceRedirectGuard`, `brokerClerkRedirectGuard`, `homeRedirectGuard`) are reachable by URL from `app.routes.ts:316-614`. They stay. The route table itself belongs to #2709.
- **Spec-support files.** `alpaca-deploy-workflow.fixtures.ts`, `fleet/fleet-directory-testing.ts` and `fleet/node-builtins.d.ts` stay. The last one exists only for `lane-fence-freeze.contract.spec.ts`. These live or die with their specs (#2732, #2733).
- **`echarts-gl.d.ts`.** It is live, through the dynamic `import('echarts-gl')` at `broker-options-surface.component.ts:448`.
- **The three `*.snapshot.json` files.** `operation-url.ts:21` reads the catalog at runtime, and the vocabulary snapshots are CI contract gates (`.github/workflows/ci.yml:303-343`). Gates belong to #2716.
- **Constant values that are still used.** `PanelInstrumentQuoteComponent.sourceLabel` (`:31`) is never overridden, and `BrokerOptionsSurfaceComponent.debounceMs` (`:102`) is never written, but both values still reach the screen or the wire. They are constants, not dead behavior.
- **Over-exported symbols.** Exports used only inside their own file are not dead.

## Pointers outside this area

- **#2709 (rest of the Frontend).** Three environment keys are read nowhere in the repo: `liveRunnerDaemonUrl`, `flags.replayInLeanEngine` and `flags.botCockpitStateStream`. They sit in `Frontend/src/environments/environment.ts:12-15` and both `.example` files. Also see the orphans listed above.
- **#2706 (dead HTTP routes).**
  - The `…/bots/{sid}/chart/live` routes, scoped and unscoped (`PythonDataService/app/routers/broker_v2_panel.py:946-969`): no Frontend caller after row 14.
  - The `strategy_instance_id` filter on fee attribution (`app/routers/brokers.py:406`): no Frontend caller after row 7.
  - `AlpacaDeskStateResponse.choices`: no Frontend reader after row 1.
- **#2701 / #2702 (clerk, fleet).**
  - `bot_chart_live` (`app/broker/alpaca/clerk/fleet_adapter.py:565`).
  - 17 more catalog operations have no Frontend caller at all: `attention_read`, `bot_authority_facts`, `bot_decision_evidence`, `bot_history_read`, `configuration_events`, `configuration_owner_read`, `configuration_owner_update`, `custody_bot_snapshot`, `custody_bot_timeline`, `custody_command_read`, `custody_pnl_attribution`, `custody_reconcile`, `custody_runs_stop`, `lane_account_quiet_read`, `lane_go_live_release`, `lane_ibkr_bar_check`, `lane_stop_all_bots`.

  The coordinator or scripts may call some of these, and several are money-path (`lane_stop_all_bots`, `custody_runs_stop`, `custody_reconcile`). Check their non-Frontend callers before calling any of them dead.
- **#2732 / #2733 (Frontend specs).** Any spec kill there that takes `lane-fence-freeze.contract.spec.ts` also takes `fleet/node-builtins.d.ts`.

## Not reviewed

- **Fallbacks for server fields.** I did not audit `??` / `||` fallbacks for fields the backend now always sends, apart from the two old-build shims in section D, which comments flagged.
- **Template branches.** I did not audit dead `@if` / `@switch` branches driven by server enums inside templates.
- **SCSS.** Unused SCSS selectors were not reviewed.
- **Inputs on routed components.** Optional inputs on route-level components, which router param binding may fill, were not checked by the bound-input scan.
- **Template-name collisions.** The template check matches names, not symbols, so a member whose name collides with an unrelated template expression may have been kept wrongly. That error only ever keeps code, never cuts it.
- **Docs.** I did not sweep the docs for pages that describe the cut pieces (#2711, #2713).
- **E2E specs.** Specs under `Frontend/tests/e2e` belong to #2734.
