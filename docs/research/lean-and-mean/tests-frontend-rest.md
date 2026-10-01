# Kill list: the rest of the Frontend specs and e2e (#2734)

Part of map #2700. Read at **`6a4d7d39`** (`origin/master`, 2026-09-30). The map was charted at `87b8e261`. The blocking dead-code list (#2709) was read at `87b8e261`.

**Area.** Every `Frontend/src/**/*.spec.ts` outside the sibling tickets' folders, plus the Playwright suite run by `.github/workflows/frontend-e2e.yml` (`Frontend/tests/e2e/`). The sibling folders are `components/broker/` (#2732) and `components/brokers/`, `fleet/`, `shell/`, `components/research-lab/`, `components/data-lab/` and `components/strategy-lab/` (#2733). That leaves 151 spec files. 25 of them, and the spec cases #2709 names inside surviving files, belong to #2709 and are skipped (see "Skipped"). This list covers the other 126 spec files (about 22,400 lines, about 1,200 tests) and the 7 e2e specs. Paths are relative to `Frontend/src/app/` unless they start with `Frontend/`.

## How this was judged

1. A throwaway script (kept in scratch, never added to the repo) listed every `it`/`test` in each file with its `expect` lines. Each `expect` was tagged as authored-text, rendered-value, control/DOM, request (`expectOne`), or bare `toHaveBeenCalled`. Every test that asserts only text was read in full. So was every bare-call test, every `should create`, and every case where one spec looks like it repeats another one layer up or down.
2. **Copy pinning (kind 2)** means a test whose *every* assertion is prose the template authors: headings, empty-state sentences, explainer text, default labels. A test stays if any assertion proves one of these:
   - a control exists, is enabled or is disabled;
   - a request fires;
   - a data value is rendered (hashes, sizes, counts, prices, dates);
   - a data row or alert is present or absent.
3. **Receipt and formatter wording.** The map's ruling makes the `receiptLabel` pipe's own spec the one place receipt wording is proven. This list reads that ruling by analogy for the other canonical formatters too. For example, `artifact-receipt.ts`, `trading-range.ts`, `error-catalog.ts`, `plain-english.ts` and `param-range.ts` each have their own spec, and that spec stays as the one place their output wording is proven. A component spec that asserts only the wording a pipe or formatter produced is a duplicate (kind 3), and the row names the formatter spec that survives.
4. **Mixed tests stay whole.** A behavior test that also pins a sentence is not trimmed. The map rules out rewriting surviving tests.

## Kill list

### Kind 1: trivial

| # | Item | Evidence |
|---|---|---|
| T1 | `shared/charts/candlestick-chart/candlestick-chart.component.spec.ts` (whole file) | Its only test is `should create` → `expect(component).toBeTruthy()`. The component stays live (strategy-builder, past-chain-inspector, day-candles). |
| T2 | `shared/charts/line-chart/line-chart.component.spec.ts` (whole file) | Same: one `should create`. |
| T3 | `shared/charts/volume-chart/volume-chart.component.spec.ts` (whole file) | Same: one `should create`. |
| T4 | `app.component.spec.ts` › `should create` | `expect(fixture.componentInstance).toBeTruthy()`. The 18 tests after it render the shell. |
| T5 | `app.component.spec.ts` › `should contain a router-outlet` | Checks that a `router-outlet` tag exists. `keeps standard pages inset and declared workspaces full-bleed` navigates two routes through that outlet. |
| T6 | `components/pricing-lab/pricing-lab.component.spec.ts` › initialization › `creates the component` | `toBeTruthy()` on the instance. |
| T7 | same › `defaults ticker to SPY` | Restates a signal's initial literal. |
| T8 | same › `starts with no expirations, no contract, no result` | Restates three signal initial values (`[]`, `null`, `null`). |
| T9 | same › `defaults riskFreeRate to 0.05` | Restates a signal's initial literal. |
| T10 | `components/strategy-builder/strategy-builder.component.spec.ts` › initialization › `creates the component` | `toBeTruthy()` on the instance. |
| T11 | same › `defaults ticker to SPY` | Restates a signal's initial literal. |
| T12 | same › `starts with no legs and no analysis result` | Restates signal initial values. |
| T13 | same › `defaults riskFreeRate to 0.043` | Restates a signal's initial literal. |
| T14 | `components/options-lab/chain/options-lab-chain.component.spec.ts` › `creates the component with default state` | `toBeTruthy()` plus four signal initial literals (`'SPY'`, `'quick'`, `15`, `false`). The tests after it exercise each of these. |
| T15 | `shared/ticker-quote/ticker-quote.component.spec.ts` › `host has inline class` | Checks that a host CSS class mirrors the `mode` input. That is a styling pass-through. Inline-mode behavior is proven by `does not render name/exchange chip/caret icon in inline mode`. |
| T16 | same › `host has card class` | Same, for card mode. |
| T17 | `shared/markdown-drawer/markdown-drawer-host.component.spec.ts` › `is not visible initially` | Restates the service's initial signals. `renders nothing when no document is open` proves the user-visible outcome. |
| T18 | `shared/copy-button/copy-button.component.spec.ts` › `renders the label text in the button variant` | Checks that a `label` input is echoed as button text. |
| T19 | `shared/param-range/param-range-input.component.spec.ts` › `renders the field title` | Checks that a `title` input is echoed (`getByText("Crossover gap (bps)")`). |

### Kind 2: copy pinning

| # | Item | Evidence |
|---|---|---|
| C1 | `app.component.spec.ts` › `names the top-bar region for what it holds: each account, with its attention bell (review B minor 4)` | One assertion: `aria-label` is `'Accounts'`. It pins a renamed label word. |
| C2 | `components/data-lake-observatory/data-lake-observatory.component.spec.ts` › `renders an empty catalog honestly rather than as zeroed tables` | One assertion: `findByText(/The catalog holds no artifacts yet/)`. |
| C3 | same › `asks for a symbol before claiming anything about coverage` | One assertion: the sentence `'Name a symbol above to see which sessions are on disk.'`. |
| C4 | `components/golden-validation-workbench/golden-validation-workbench.component.spec.ts` › `shows an explicit absence when execution configuration was not recorded` | Only `getByText("Configuration")` and `getByText("Not recorded")`. |
| C5 | `components/lean-engine/validation-stage-placeholder/validation-stage-placeholder.component.spec.ts` › `names the decision-minute open rather than calling every other mode the next open (#2599)` | Only label text: `'Decision minute open'` is present and `'Next bar open'` is absent. |
| C6 | `shared/validation-scope/validation-scope-note.component.spec.ts` › `opens the explainer and states the summary-level validation contract` | Seven `document.body.textContent toContain(...)` checks on explainer prose. Opening and closing are proven by `closes the explainer from the dialog's own close control`, and the trigger by `names the trigger accessibly, marks it as a dialog opener, and tracks expansion`. |
| C7 | `shared/indicator-picker/indicator-picker.component.spec.ts` › `facets that filter a non-empty catalog down to nothing keep the filter advice` | Only advice sentences and the link's text. The clear action is proven by `shows an empty state with a combined clear action and clears both search and facets`. |
| C8 | `shared/page-guide/page-guide.component.spec.ts` › `renders pulls/why and the default summary label` | Pins the default label `'How this page works'` and echoes two input strings. |
| C9 | `shared/ticker-range-picker/parts/instrument-card.component.spec.ts` › `says a no-match search found no listed symbol` | One assertion: `textContent` contains `'No listed symbol matches that'`. |
| C10 | same › `distinguishes an empty lake from a search that matched nothing` | Only two sentence checks: `'the lake holds nothing yet'` is present and `'matching that'` is absent. |
| C11 | `components/data-lake-observatory/backfill-panel/lake-backfill-panel.component.spec.ts` › `honours a cap the data plane lowered rather than a hardcoded one` | One assertion: the sentence `'That window is 31 days; the data plane accepts at most 30.'`. The lowered-cap rule is proven by `shared/data-lake/trading-range.spec.ts` › `honours a cap the data plane lowered`. Blocking past the cap is proven by `blocks a window one day past the cap instead of letting the server refuse it`, which checks that no submit is sent. |
| C12 | `Frontend/tests/e2e/strategy-lab-analytical-manual.spec.ts` › `keeps unknown metric and stale contract context explicit` | One assertion: `getByText(/requested metric is not documented/i)`. |

### Kind 3: duplicates (the surviving stronger test is named)

| # | Item | Survivor (`file › describe › it`) |
|---|---|---|
| D1 | `components/data-lake-observatory/artifact-inspector/artifact-inspector.component.spec.ts` › `names a failed row diagnosis through the receipt-label pipe` | `shared/pipes/receipt-label.pipe.spec.ts` › `formats underscore, dot, dash, and uppercase receipt identifiers as title case`, plus `components/data-lake-observatory/lib/artifact-receipt.spec.ts` › `routes backend identifiers through the code channel, not verbatim`. The inspector test asserts only `'Provider Rate Limited'` and the backend's detail sentence. |
| D2 | same file › `names a missing row by the endpoint's own reason, without hedging` | Same survivors. It asserts only `'Artifact Not Found'` and the backend detail. |
| D3 | same file › `says a content hash is absent rather than showing a blank one` | `artifact-receipt.spec.ts` › `says an absent content hash is absent instead of showing an empty token`. The inspector test adds only the exact sentence. |
| D4 | same file › `surfaces a rejection reason instead of a blank panel` | `shared/data-lake/data-lake.service.spec.ts` › `names an unreachable data plane instead of surfacing a bare status 0`. That test owns `'The data plane did not respond.'`; the inspector test asserts only that sentence and `'Unavailable'`. |
| D5 | `components/data-lake-observatory/coverage-query-bar/coverage-query-bar.component.spec.ts` › `renders the adjustment vocabulary as operator language, not raw codes` | `receipt-label.pipe.spec.ts` › `formats underscore, dot, dash, and uppercase receipt identifiers as title case`. The option text is `{{ option \| receiptLabel }}` (`coverage-query-bar.component.html:45`). |
| D6 | `components/data-lake-observatory/storage-summary/lake-storage-summary.component.spec.ts` › `renders artifact kinds as operator language, not raw codes` | Same pipe spec. The cells are `kind.artifact_kind \| receiptLabel` (`lake-storage-summary.component.html:36-37`). |
| D7 | `components/data-lake-observatory/data-lake-observatory.component.spec.ts` › `surfaces a rejected window under its own reason code` | `components/data-lake-observatory/lib/coverage-board.spec.ts` › `names a rejected symbol as a problem instead of dropping it silently`, plus the pipe spec. The page test asserts only `'SPY · Range Too Large'` and the detail text. |
| D8 | `shared/errors/section-error.component.spec.ts` › `renders catalog copy when the error carries a known code` | `shared/errors/error-catalog.spec.ts` › `uses catalog copy when the code is mapped`. The component test re-asserts catalog wording (`'IB Gateway'`, `'Retry'`). |
| D9 | `shared/trading-chart/chart-indicator-rail.component.spec.ts` › ChartIndicatorRailComponent catalog load failure › `forwards a failed catalog load to its picker, which says so` | `shared/trading-chart/trading-chart.component.spec.ts` › `tells the rail a failed indicator catalog load, and its picker says so`. That test renders the real rail and picker, so it covers both hops. |
| D10 | same rail file › `keeps the neutral empty state when the catalog did not fail` | `trading-chart.component.spec.ts` › `raises no catalog failure in the rail by default`, which asserts the same no-alert and `'No indicators available'` through the real rail. |
| D11 | `shared/ticker-range-picker/ticker-range-picker.component.spec.ts` › `does not render multiplier dropdown by default` | `shared/ticker-range-picker/parts/sampling-card.component.spec.ts` › `does NOT render multiplier dropdown when availableMultipliers is empty`. The parent's pass-through is proven by `passes availableMultipliers through to the Sampling card`. |
| D12 | `shared/ticker-quote/ticker-quote.component.spec.ts` › `renders caret in card mode` | Same file › `shows pi-caret-up for positive in card mode` (and its down and flat siblings), which find the same `<i>` in card mode before checking its class. |
| D13 | `shared/symbol-picker/symbol-picker.component.spec.ts` › `renders the bound symbol` | `instrument-card.component.spec.ts` › `renders the current symbol and exchange`. The picker renders `InstrumentCardComponent` (`symbol-picker.component.ts:27`) and adds no rendering of its own. |
| D14 | `services/market-data.service.spec.ts` › getOrFetchStockAggregates › `should send POST to GraphQL endpoint` | Same describe › `should send correct variables`, which matches the same `expectOne(GRAPHQL_URL)` and asserts the body. |
| D15 | `Frontend/tests/e2e/account-desk-cutover.spec.ts` (whole file) | `app.routes.spec.ts` › `navigates the deprecated %s URL to %s`. That is a full-router test over all 7 of these paths plus 7 more. The e2e adds only the CI static server's SPA fallback (`http-server --proxy`), which is serving plumbing, not app behavior. |

### Kind 4: mock theater

| # | Item | Evidence |
|---|---|---|
| M1 | `components/data-lake-observatory/lib/data-lake-backfill.store.spec.ts` › `start() rides JobsService.onEvent() instead of opening its own stream` | Its only assertion is `expect(onEvent).toHaveBeenCalledWith('job-1', expect.any(Function))` on a `vi.fn()`. The outcome is proven by `a frame delivered through the registered handler folds the same as a direct ingestEvent() call`. |
| M2 | same › `reattach() also rides JobsService.onEvent() for the adopted job` | Its only assertion is that the `onEvent` mock was called. The outcome is proven by `a reattached panel shows every session so far, including failures the first panel saw (#2472)`, which uses the real `JobsService` and an SSE stub. |

### Kind 5: retired features

| # | Item | Evidence |
|---|---|---|
| R1 | `app.component.spec.ts` › `carries no Trader/Operator switch: each page has one view (PRD #2560 D2)` | Asserts that a retired control is absent (no `tablist`, no word "Operator"). The switch's code is gone, so nothing in the app can bring it back. |
| R2 | `shared/eyebrow-heading-retirement.contract.spec.ts` (whole file) | A textual scan of every template that keeps a retired markup pattern (#2183/#2184) from coming back. It checks styling, not behavior, and carries a path allowlist (`ALLOWED`). See Q-note 2. |

## Not cut, judged on purpose

These are recorded so the next reader does not re-litigate them.

- **Sacred money-path e2e stays.** `account-owner-walk-through.spec.ts` (deploy, stop, flatten, clear, typed-phrase Live), `alpaca-multi-clerk.spec.ts`, `alpaca-clerk-ui-correlation.spec.ts` and `account-first-navigation.spec.ts` drive real reloads, SSE revisions and routed URLs, which no unit spec can do. Their copy assertions sit inside behavior tests.
- **`shell-menubar.spec.ts`** proves breakpoint layout, which jsdom cannot measure.
- **`strategy-lab-analytical-manual.spec.ts` › `supports keyboard-addressable search, …`** stays: it drives keyboard search and filters.
- **Formatter specs stay as the canonical wording tests.** This covers `receipt-label.pipe.spec.ts`, `plain-english.spec.ts` (except #2709's `formatStrategySummary` case), `validation.spec.ts`, `trading-range.spec.ts`, `param-range.spec.ts`, `error-catalog.spec.ts`, `data-lake.service.spec.ts`, `markdown-slug.spec.ts` and `artifact-receipt.spec.ts`.
- **`timestamp-display.spec.ts`** stays (local / et / date-et modes). So does `components/legal/legal-notices-page` (it asserts that the license-attribution link exists and where it points) and `markdown-viewer` › `refuses a document from outside the served docs folder` (a security refusal).
- **Bare-call tests that prove an action fires stay.** These are the `engine-lab-run-history` re-read-on-completion tests, the `alpaca-live-verdict` cadence and back-off tests, and the `coverage-query-bar` / `grid-search-form` "never preflights / does not re-query" tests.
- **Service HTTP specs stay.** They assert the URL, method, params and body: the request contract.

## What the cuts orphan

- **Nothing.** No cut row is the last user of a fixture, mock, factory or `testing/` helper:
  - the chart specs (T1–T3) use only `TestBed`;
  - `Frontend/tests/e2e/support/owner-walk-world.ts` and `Frontend/tests/fixtures/alpaca-clerk-ui-correlation.fixture.ts` are not imported by D15 or C12;
  - every `fake*` helper the cut cases touch (`fake-symbol-catalog`, `fake-picker-world`) keeps 10 or more surviving importers.
- The three chart components lose their only spec. They are live, but they have no logic beyond wiring the chart library.
- After D15, `frontend-e2e.yml` runs 6 spec files. No Playwright config change is needed.

## Hazards the cutting PR must carry

- **H1. Same files as #2709.** `services/market-data.service.spec.ts` (D14 here, `calculateIndicators` there) and `data-lake-backfill.store.spec.ts` (M1/M2 here, `fetchedCount` there) also lose #2709 cases. Land both cuts in one PR, or rebase the second onto the first, and re-run `ng test` after resolving. These are semantic conflicts that git will not flag.
- **H2. Do not trim inside surviving tests.** Copy assertions inside mixed tests stay. The map rules out rewriting tests.
- **H3. Multi-line `it.each` blocks were judged by name.** There are 21, 12 of them in `app.routes.spec.ts`. The summarizer did not split their cases, though each was read by name. None is on this list.
- **H4. Re-check at your SHA.** Each survivor named in a duplicate row must still exist and still assert the named outcome before its duplicate is deleted. If a survivor itself falls to another ticket's cut, the duplicate stays.
- **H5. AXE coverage.** None of the cut tests is an AXE test. The `passes AXE` cases in data-lake-observatory and indicator-picker stay.
- **H6. Run the full frontend suite.** Run `podman exec my-frontend npm test` after the cut, so the AOT template compile still sees every surviving selector.

## Waits on the volatility-transport ruling

- Nothing in this area. `components/options-lab/volatility-stub/volatility-stub.component.ts` (`VolatilityStubComponent`) has no spec. No other spec in this area sits over the volatility-surface routes. The live edge IV30 and realized-vs-IV specs (`components/edge/realized-vs-iv/`, `components/edge/services/edge-api.service.spec.ts`) were judged as usual, and none of their tests is cut.

## Proven math, unused

- **`utils/black-scholes.ts` `lognormalCdf`.** It is unused options math, and #2709 (C14) lists it with its cases in `utils/black-scholes.spec.ts` (`should return 0 for x <= 0`, `should return value between 0 and 1`, `should be monotonically increasing`). It is unit-tested but has no parity fixture: `black-scholes.parity.spec.ts` does not cover it. The map should confirm whether #2709's C14 cut stands under the new volatility/options-math ruling. Nothing here cuts any Black-Scholes test, and the parity spec and its fixture `Frontend/src/testing/bs-parity/` stay.

## Pointers outside this area

- **#2716 (CI checks).** `frontend-e2e.yml` has a "Verify echarts + echarts-gl + zrender pinned set" step (`npm ls …`), a dependency check inside the e2e job. Its header comment also cites `Frontend/src/testing/operator_surface_fixtures/`, which no longer exists.
- **#2733 (shell, labs).** `account-first-navigation.spec.ts` › `offers Alpaca as Accounts alone, and retires the broker-wide surfaces` overlaps `app.routes.spec.ts` › `lands %s on the account list, in however many hops it takes` and the shell menu specs. It stays here because it also clicks the real built menu.
- **#2709.** It owns `app.routes.spec.ts` › `keeps the Clerk diagnostic gallery unlinked beneath the examples route` (B5's route).

## Q-notes for the map (readings, not new rules)

1. **The formatter reading** (judgment point 3) extends the `receiptLabel` ruling to every canonical formatter. If the owner meant the pipe only, rows D3, D4, D7 and D8 become kind 2 instead, and the formatter specs listed under "Not cut" would need re-judging as copy pins.
2. **R2 is a lint written as a spec.** As a test it fails the bar (it checks styling). Under the ★ gate rule ("lint/build/typecheck stays") it could be read as a kept lint. It is listed here as a test, because it runs inside `ng test` rather than ESLint.

## Skipped (owned by #2709)

These are whole spec files: A1, A5, A6, A8, A11, A12, A13, A15, A16, A18–A21, A23 and A26, plus Tier B (portfolio, `jobs/job-progress`, `_ide-sandbox`, `examples/alpaca-bot-control`, `api/alpaca-bot-control.contract.spec.ts`). Also skipped are the C1–C23 cases inside surviving specs:
- `calculateIndicators`, `runFeatureResearch` / `getExperiment`, `listActivities` / `listOrders`;
- the `connect` / `disconnect` / `isPaperConnected` cases;
- `dataPlaneHealth` / `ibkrApiEvidence`, `startTrustedRun`, `QUIET_LANE_ATTENTION_STATE`;
- the run-session signals;
- every `date-validation.spec.ts` case for the C13 helpers;
- `lognormalCdf`, `formatTimestampIsoInZone`, `chartSeriesColorVar` / `CHART_SERIES_SURFACE_HEX`;
- `fetchedCount`;
- the spec-strategy-runner C23 fields and `formatStrategySummary`.

## Not reviewed

- **Mixed tests were not read line by line.** These assert both behavior and prose, and their prose was left alone by rule, not judged. The ones with the most wording are `strategy-validation.component.spec.ts`, `golden-validation-workbench`, `walk-forward-study-*`, `grid-search-*` and `lake-backfill-panel`.
- **The body of each `it.each` case** (see H3).
- **Behavior duplicates between the service HTTP specs and the component specs that mock those services.** They were sampled, not exhaustively paired. One example is the `*-runs.service` URL-encoding tests against the components that call them.
- **The large e2e walk-throughs were judged at test level only.** Their individual `expect` lines (for example, 26 copy assertions in the Paper walk-through) sit inside sacred behavior tests and were not judged one by one.
