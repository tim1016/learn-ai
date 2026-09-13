# PRD — Data Lab workspace redesign

- **Date:** 2026-09-12
- **Status:** Ready for issue approval
- **Product surfaces:** Data Lab Explore, Build dataset, Validate, saved setups, run dock
- **Delivery posture:** Incremental child-route extraction with contract and compatibility gates; no big-bang rewrite
- **Source:** Two-player Sol/GLM plan tournament; audit receipt in `docs/audits/plan-tournament-2026-09-12-data-lab-ui-ux.json`
- **Authority:** Python authors calendars, sessions, output columns, workload estimates, and every numerical result. .NET transports and persists typed values. Angular owns interaction and presentation only.

---

## 1. Executive summary

Data Lab currently presents chart exploration, dataset construction, validation,
quality evidence, options companions, reference files, saved sessions, and run
progress in one three-rail workbench. The page's fixed 320px and 360px rails,
multiple scroll contexts, wide previews, and adjacent chart/export actions make
the interface dense and prone to overflow. Users must understand nearly the
entire product before completing any one job.

This PRD reorganizes Data Lab into one shared workspace with three focused child
routes:

1. **Explore** views charts and configures indicators.
2. **Build dataset** authors and generates a CSV/ZIP recipe.
3. **Validate** compares datasets and inspects validation evidence.

The shared shell retains ticker, range, saved setup, recipe, and run state across
route navigation. Explore is chart-first. Build dataset never mounts or fetches
the chart. Validate presents one evidence task at a time. The base page has one
document scroll and no page-level horizontal overflow.

Indicator discovery extends the existing shared picker with accessible search.
Color customization reuses the existing indicator override flow but permits only
named theme tokens. A compact, lazy news strip connects the selected ticker and
time window to existing news functionality without adding baseline density or
blocking the chart.

The redesign also closes existing authority violations. Python supplies the
canonical exchange window, projected columns, session count, and workload
estimates. Data Lab's touched wire and persistence boundaries migrate to `int64`
UTC milliseconds through an additive, consumer-inventoried compatibility plan.

## 2. Product problem

### 2.1 Three different jobs compete on one page

The current `DataLabComponent` combines:

- ticker and date-range selection;
- bar, session, adjustment, hygiene, options, and companion settings;
- chart preview and active indicators;
- CSV column preview and ZIP generation;
- pandas-ta versus TradingView validation;
- quality reports and past-chain inspection;
- saved sessions and run progress.

`Preview chart`, `Bundle as ZIP`, and `Generate CSV` appear in the same action
area even though they start different workflows. Chart exploration and dataset
generation can also assemble and submit overlapping requests through different
paths.

### 2.2 The page shell creates avoidable density and overflow

The global `.ide-grid` assigns fixed 320px and 360px rails around the main
canvas. At large widths those rails become independently sticky and scrollable;
at intermediate widths content is reflowed below the main area. Wide tables,
long output names, charts, and code-like evidence can still force clipping or
page-level horizontal scrolling.

### 2.3 Shared functionality exists but is incomplete or bypassed

- Data Lab already uses `app-asset-identity` and `app-ticker-range-picker`.
- The shared indicator picker supports facets, presets, categories, active
  counts, and add actions, but not text search.
- Data Lab retains dead search signals from an older inline catalog.
- The shared chart indicator rail and Alpaca V2 chart already demonstrate the
  color-override event and host-state flow, but the current picker accepts
  arbitrary colors.
- News already exists through `NewsService`, the Research Lab news components,
  and the Python news router.
- Data Lab bypasses `IndicatorCatalogService` with its own catalog request.

### 2.4 Frontend projections conflict with authority rules

Data Lab currently computes or approximates weekdays, bars, output columns,
options workloads, and session lengths in Angular. It also carries string date
requests, local date arithmetic, end-of-day timestamps, and Backend `DateTime`
session persistence. These are incompatible with the repository's Python math
authority and `int64 ms UTC` boundary policy.

## 3. Goals

1. Give Explore, Build dataset, and Validate one unambiguous job and primary
   action each.
2. Reduce default information density without removing existing capabilities.
3. Eliminate page-level horizontal overflow at supported viewport widths.
4. Make indicator discovery searchable and fully keyboard accessible.
5. Support theme-token-only colors for single- and multi-output indicators.
6. Reuse the shared ticker identity/range, indicator catalog, run, chart, news,
   timestamp, and error-state components.
7. Keep chart and news requests explicit so editing does not trigger surprise
   vendor usage.
8. Move authoritative session, column, and workload projections to Python.
9. Migrate touched temporal wires and saved-session timestamps to numeric UTC
   milliseconds without breaking unexamined consumers.
10. Preserve bookmarks, saved-session links, and existing workflows through
    tested ingress adapters.

## 4. Non-goals

- Changing indicator formulas, warmup behavior, output values, or tolerances.
- Changing dataset file formats except where a separately reviewed contract
  migration requires numeric timestamps.
- Removing quality, validation, options companion, cached snapshot, saved setup,
  or run-progress capabilities.
- Introducing a new charting, state-management, calendar, accessibility, or UI
  dependency.
- Adding live news polling or trading recommendations.
- Rebuilding or referencing deprecated IBKR bot-control or navigation surfaces.
- Changing the thirteen-indicator default without a separate product decision.

## 5. Users and jobs

### Researcher exploring data

- Select a ticker, authoritative time window, and timeframe.
- Inspect the chart without seeing export and validation controls.
- Search for, configure, show/hide, and recolor indicators.
- Know when the chart is stale before intentionally refreshing it.
- Inspect relevant news without leaving the research context.

### Researcher building a dataset

- Reuse an explored ticker, range, and indicator recipe.
- Configure bars, adjustments, hygiene, options, and companion files in a
  progressive form.
- Review Python-authored columns, sessions, dependencies, and estimated cost.
- Start exactly one durable ZIP-generation job and follow its progress.

### Researcher validating evidence

- Compare pandas-ta and TradingView CSVs in a focused surface.
- Inspect quality and reconciliation evidence without navigating chart controls.
- Download the validation report.

## 6. Product principles

1. **One route, one job.** Route boundaries carry the main cognitive split.
2. **Progressive disclosure.** Advanced and companion settings remain available
   without occupying the first viewport.
3. **Committed state drives paid work.** Draft edits never fetch automatically.
4. **One execution path.** Dataset generation has one mapper and one job-backed
   submission path.
5. **Theme tokens are the palette.** Stored presentation choices are stable
   token identifiers, not color literals.
6. **Color is never the only cue.** Line style, width, labels, and selected marks
   preserve meaning.
7. **Python owns projections.** UI simplification must not create new browser
   math.
8. **Calendar truth is explicit.** Date intent resolves through the canonical
   exchange calendar into numeric half-open windows.
9. **Quiet context, not another feed.** News is optional, bounded, and
   failure-isolated.
10. **Compatibility is a deliverable.** Old URLs and saved sessions are tested
    before the monolith is removed.

## 7. Information architecture

### 7.1 Routes

- `/data-lab/explore`
- `/data-lab/export`
- `/data-lab/validate`

`/data-lab` becomes the stable product entry and redirects to Explore after
normalizing any legacy query state. `/data-quality` redirects to Validate.

### 7.2 Shared shell

`DataLabComponent` becomes a small route shell containing:

- page header and `app-asset-identity`;
- compact scope summary and shared ticker/range controls;
- Explore, Build dataset, and Validate route tabs;
- Saved setups access;
- child `router-outlet`;
- one shell-owned `RunDockComponent`.

A component-scoped `DataLabWorkspaceStore` owns:

- committed ticker and numeric time window;
- draft scope and bar policy;
- stable indicator instances and parameters;
- approved series-color token IDs;
- companion-file settings;
- active saved session and schema version;
- last chart request signature and stale state;
- last dataset-plan receipt;
- news state;
- active generation run.

Server sessions and runs remain durable truth. The store keeps references and
draft state rather than duplicating persisted results.

### 7.3 Explore

Explore renders:

- compact scope bar;
- primary chart canvas;
- **Edit scope** drawer;
- **Indicators** drawer or bottom sheet;
- compact active-indicator summary and chips;
- explicit **Refresh chart** primary action;
- concise quality/provenance status;
- collapsed ticker-news section.

Scope or recipe edits mark the chart **Out of date**. The last good chart remains
visible while stale or refreshing. Only explicit refresh calls `/api/chart/data`.

The existing thirteen-indicator default remains, but the collapsed state reads
`13 active` rather than displaying thirteen large controls.

### 7.4 Build dataset

Build dataset never creates `DataLabChartComponent` and never calls the chart
endpoint. Its progressive recipe contains:

1. **Scope and bars** — ticker, numeric time window, timeframe, session, and
   adjustment policy.
2. **Indicators and columns** — shared recipe editor and Python-authored column
   plan.
3. **Companions and quality** — collapsed Options, Polygon/reference companions,
   `news.csv`, and quality settings.
4. **Review and generate** — canonical sessions, output columns, dependencies,
   warnings, estimate assumptions, and **Generate dataset ZIP**.

`RunSessionService.start()` is the only submission path. Remove the combined
preview/export action, synchronous ZIP UI path, and duplicate payload builders.
The run remains visible and cancellable while navigating among Data Lab routes.

### 7.5 Validate

Validate presents one readable column containing:

- pandas-ta CSV selection;
- TradingView CSV selection;
- **Run validation**;
- comparison report;
- report download.

Quality evidence and past-chain inspection remain reachable through focused
panels. `PastChainInspectorComponent` stays under the Options companion unless
an existing tested workflow proves it belongs in general validation.

## 8. Responsive and no-overflow contract

Base routes use normal document scrolling. Drawers, dialogs, and explicitly
labeled tables may scroll internally.

### Desktop, 1280px and wider

- Centered workspace canvas.
- Explore uses the chart plus an overlay drawer, never two permanent rails.
- Export may show a narrow review summary beside the form.
- Validate remains one column.

### Tablet, 768–1279px

- One base column.
- Context and indicator panels become modal drawers.
- Export review follows the form rather than occupying a side rail.

### Mobile, below 768px

- One column with a compact scope summary.
- Full-width chart and indicator bottom sheet.
- Vertical Export steps.
- Safe-area-aware primary action and 44px minimum touch targets.

### Layout requirements

- Every flexible grid or flex ancestor uses `min-width: 0`.
- Flexible tracks use `minmax(0, 1fr)`.
- Base views do not use viewport-height `max-height` or nested
  `overflow-y: auto`.
- Long filenames, column names, errors, and audit tokens use wrapping,
  truncation, or accessible disclosure appropriate to their meaning.
- Wide tables scroll only inside a focusable region with an accessible name and
  visible scroll affordance.
- Charts respond to their containing block, not the viewport.

At 320, 768, 1024, 1440, and 1920px, all three routes must satisfy:

```ts
document.documentElement.scrollWidth ===
  document.documentElement.clientWidth
```

## 9. Indicator search and configuration

Extend `shared/indicator-picker/indicator-picker.component.*` with an opt-in,
backward-compatible searchable-list presentation.

The search index includes:

- canonical key and display name;
- reference name and description;
- category and pane;
- aliases and parameter names.

Search combines with the existing pane/category facets. The component announces
result count and provides **Clear search** and **Clear filters**. Opening the
drawer focuses search. Arrow keys and Home/End move through results, Enter adds,
and Escape clears before closing. Focus returns to the invoking control.

Presets move behind a disclosure. Existing picker consumers retain their current
presentation until deliberately migrated. Data Lab deletes its dead catalog
signals and loads the catalog through `IndicatorCatalogService`.

Active indicator instances have stable, parameter-aware identity. Parameter
editing reuses the existing modal and validates min/max/type before apply.
Changing parameters preserves the editor on error and marks the chart stale.

## 10. Theme-only chart colors

Define a finite `ChartSeriesColorToken` registry backed by CSS custom properties
in `_tokens.scss`.

- Store token IDs, never hex, RGB, or arbitrary CSS strings.
- Resolve computed colors only at the Lightweight Charts boundary.
- Replace `<input type="color">` with an accessible named swatch radio group.
- Retain bull/bear/warn/regime colors for their semantic meanings.
- Use selected marks, labels, line styles, and widths as non-color cues.
- Use stable indicator-instance plus canonical result-output identity for
  multi-output mappings.
- Give secondary outputs deterministic adjacent eligible tokens.
- Replace touched chart, EMA, MACD, and Data Lab SCSS color literals with theme
  variables.

Before implementation, inventory the active Alpaca V2
`dual-pane-chart-indicators.ts` exports, callers, tests, and the shared
`ChartIndicatorResult` shape. Capture BBands and MACD fixtures. If the helpers
are broker-neutral and contract-correct, move them under `shared/trading-chart`
with fixture parity and update Alpaca imports. Otherwise, derive canonical
result identity from `IndicatorCatalogService` metadata plus the Python-authored
result plan. Data Lab must never import an Alpaca feature directory.

Add a programmatic palette test. For every eligible series token, resolve the
series and chart-surface colors under every supported light and dark theme,
alpha-composite translucent values, calculate WCAG relative luminance and
contrast, and fail below 3:1. AXE remains required for semantics but is not
accepted as graphical-contrast proof.

## 11. Ticker news

Explore contains a collapsed **Headlines for {ticker}** section below the chart.

- Load only when expanded or manually refreshed.
- Show at most five text-only headlines and a **View all** link.
- Follow the committed ticker and numeric UTC window.
- Do not poll.
- Never block, clear, or replace the chart.
- Handle idle, loading, empty, stale, error, and rate-limit states locally.
- Reuse `NewsService`, the Research Lab news presentation, `app-asset-identity`,
  `TimestampDisplayComponent`, and shared section-error behavior.
- Display publication timestamps using instant/local mode. Publication time is
  an instant, not a date-anchored field.
- Preserve Polygon sentiment's vendor-asserted provenance wherever sentiment is
  shown.

If the current service exposes only vendor string operators, add a small
app-owned numeric-window adapter. The UI submits `start_ms_utc` and exclusive
`end_ms_utc`; it does not construct vendor date strings.

## 12. Dataset plan and temporal authority

Add `POST /api/dataset/plan` to the Python service. Back it with reusable dataset,
options, chart, and canonical-calendar functions. The response contains:

- normalized recipe and output-column specifications;
- canonical output-column count;
- resolved numeric half-open session window;
- exact exchange sessions and session count;
- workload estimates explicitly typed as estimates;
- estimate assumptions and provenance;
- companion dependencies and warnings;
- allowed and recommended timeframes;
- exchange, calendar, timezone, and calendar-version metadata.

Extract one Python column-projection function and parity-test it against the
actual generated ZIP manifest. Angular renders the receipt unchanged and does
not calculate columns, bar counts, weekdays, session lengths, expiries, or
options-contract workloads.

Migrate the touched Data Lab request family to numeric `start_ms_utc` and
exclusive `end_ms_utc`. Resolve date-only intent through the canonical exchange
calendar, normally from session open to the following session open. Do not
construct `T23:59:59`, guess UTC offsets, or hard-code 390-minute sessions.

## 13. Saved-session migration gate

Before changing `DataLabSession` storage or GraphQL fields, inventory every:

- `DataLabSession` and `DataLabSessionInput` reference;
- EF model, migration, index, and query;
- GraphQL field and operation;
- generated client and schema snapshot;
- test and documented external contract.

Create a consumer matrix recording fields read, fields written, timestamp
expectation, compatibility strategy, and contract test.

Migration is additive:

1. Add numeric start, end, created, and updated fields.
2. Dual-read numeric authority with legacy fallback.
3. Make all new writes numeric only.
4. Derive any temporary deprecated GraphQL projection from numeric authority.
5. Version the existing session JSON envelope for colors and workspace state.
6. Backfill through the canonical calendar.
7. Move time-based indexes only after backfill and every consumer snapshot pass.
8. Remove legacy fields only after all inventoried consumers cut over.

If a consumer cannot be inventoried, initially scope the migration to Data
Lab-owned wires and retain a bounded deprecated read adapter.

## 14. Legacy URL migration

Create and test an ingress adapter before removing the monolith:

| Legacy input | Canonical destination |
|---|---|
| `/data-lab` | `/data-lab/explore` |
| `/data-quality` | `/data-lab/validate` |
| `?mode=explore` | `/data-lab/explore` |
| `?mode=build` or `?mode=export` | `/data-lab/export` |
| `?mode=validate` | `/data-lab/validate` |
| Legacy ticker, range, and trading-session query | Equivalent validated numeric scope on the chosen child route |
| Legacy indicator query/recipe | Decode through a bounded recipe-schema validator |
| `sessionId=<uuid>` | Explore unless the legacy mode explicitly selects Export or Validate |

Preserve only aliases discovered by the pre-implementation inventory. Unknown
keys are dropped. Invalid recipe entries are dropped with a visible warning.
URL state may populate the workspace but may never auto-fetch, auto-generate, or
bypass boundary validation.

## 15. Functional requirements

- **FR-001:** Data Lab exposes Explore, Build dataset, and Validate as lazy child
  routes under one shared shell.
- **FR-002:** Shared scope, recipe, saved-session, and active-run state survives
  child-route navigation.
- **FR-003:** Explore has one explicit chart-refresh action and preserves the last
  good chart while inputs are stale or refreshing.
- **FR-004:** Build dataset never mounts the chart or calls the chart endpoint.
- **FR-005:** Dataset generation uses one request mapper and one job-backed
  submission path.
- **FR-006:** The shared indicator picker provides accessible search combined
  with existing facets.
- **FR-007:** Indicator configuration supports stable instances and
  contract-derived multi-output identity.
- **FR-008:** Chart customization emits and persists only approved theme token
  IDs.
- **FR-009:** Every eligible palette token passes 3:1 graphical contrast against
  supported chart surfaces in light and dark themes.
- **FR-010:** News is lazy, bounded to five headlines, manual-refresh only, and
  failure-isolated.
- **FR-011:** Publication timestamps use instant/local display and sentiment
  retains vendor provenance.
- **FR-012:** Python authors session resolution, columns, timeframe advice,
  dependencies, and workload estimates.
- **FR-013:** Touched temporal wire and storage values use `int64 ms UTC`.
- **FR-014:** Every documented legacy URL shape reaches an equivalent validated
  child-route state.
- **FR-015:** Existing saved setups, run progress, quality, validation, options,
  companions, snapshots, and downloads remain reachable.
- **FR-016:** No supported viewport has page-level horizontal overflow.

## 16. States and edge cases

- Empty Explore provides a calm scope-and-refresh prompt.
- Non-trading windows and rejected timeframes show server-authored recovery
  choices.
- Catalog and news failures do not erase active recipes or chart state.
- Unsupported saved chart snapshots restore their configuration but mark the
  chart stale.
- A second conflicting export action is disabled while a run is active.
- Export cancellation remains available across Data Lab navigation.
- Invalid saved or URL-supplied indicator parameters do not execute.
- Drawers and dialogs trap focus, support Escape, and restore focus to their
  trigger.
- Status changes use polite live regions; blocking errors use alert semantics.
- Reduced-motion preferences and browser zoom remain usable.

## 17. Implementation sequence

### Phase 0 — dependency and compatibility gates

- Inventory indicator/result helpers and capture multi-output fixtures.
- Inventory every saved-session and GraphQL consumer.
- Freeze legacy URL/query normalization behavior in tests.
- Inventory every supported theme and chart-surface token.

### Phase 1 — shared seams and Python contract

- Add searchable-list mode to the shared indicator picker.
- Replace Data Lab's direct catalog request with `IndicatorCatalogService`.
- Establish shared result identity using either verified broker-neutral helpers
  or the catalog/server-plan fallback.
- Add the theme palette, swatch picker, resolver, and contrast tests.
- Add `DataLabWorkspaceStore` and a pure request mapper.
- Implement and contract-test dataset planning and numeric calendar resolution.

### Phase 2 — routes and shell

- Add `data-lab.routes.ts` and lazy child routes.
- Implement the tested legacy ingress adapter.
- Reduce `DataLabComponent` to the shell.
- Move Saved setups and RunDock into the shell.

### Phase 3 — Explore

- Extract chart orchestration into Explore.
- Bind numeric windows, theme overrides, and explicit timezone display.
- Add the indicator drawer, stale behavior, quality dialog, and lazy news strip.

### Phase 4 — Export and Validate

- Build the progressive Export recipe around the shared scope control and Python
  plan receipt.
- Submit only through `RunSessionService.start()`.
- Move companions and quality options into disclosures.
- Extract the CSV comparison and report workflow into Validate.
- Delete duplicate generation and client numerical projections.

### Phase 5 — persistence and cleanup

- Execute the additive Backend, GraphQL, and session migration after the consumer
  gate passes.
- Version and migrate saved workspace JSON.
- Remove obsolete monolith HTML/SCSS only after workflow and URL parity pass.
- Remove global `.ide-grid` rules only after confirming no remaining callers.
- Update `docs/known-gaps.md` and contract snapshots. Update math/engine authority
  registries only if implementation introduces or moves numerical authority.

## 18. Verification

### Frontend unit and interaction tests

- Search/facet intersection, empty state, clearing, focus, and keyboard behavior.
- Stable recipe/result identity with BBands and MACD multi-output fixtures.
- Only known color tokens can be emitted, persisted, and restored.
- Light/dark palette contrast against chart surfaces is at least 3:1.
- Workspace state survives child-route navigation.
- Editing marks a chart stale without fetching.
- Explore makes one chart request.
- Export starts one job and never calls chart.
- Dataset-plan receipts render without client recomputation.
- Session dual-read/single-write and JSON-version migration.
- Lazy news loading, manual refresh, errors, and provenance.
- Every legacy route and query mapping.

### Python and Backend tests

- Dataset-plan schema and boundary validation.
- Column projection versus generated ZIP manifest.
- DST, holiday, early-close, and non-trading-window cases.
- Numeric half-open chart and export requests.
- Estimate assumptions and provenance.
- Saved-session backfill, fallback, numeric-only writes, ordering/index behavior,
  and per-consumer GraphQL snapshots.

### End-to-end tests

- Legacy links preserve equivalent ticker, range, indicator, session, and saved
  session state.
- Explore search, add, configure, recolor, and refresh.
- Export review, generate, cancel, and cross-route run persistence.
- Validate upload, report, and download.
- AXE on every child route and modal surface.
- No page overflow at 320, 768, 1024, 1440, and 1920px with worst-case content.
- Visual states for drawers, bottom sheets, empty/loading/stale/error, and every
  supported light/dark theme.

## 19. Dependencies, assumptions, and risks

### Dependencies

- Existing Angular, Lightweight Charts, UI, signal, news, run, calendar, and test
  infrastructure is sufficient. No new dependency is required.
- Shared search/color seams and Python plan contracts land before route removal.
- Saved-session migration waits for the complete consumer inventory.

### Assumptions

- The thirteen-indicator default remains until product explicitly changes it.
- Explore and Export share an indicator recipe; colors remain presentation-only.
- Canonical date intent maps to session-open start and next-session-open exclusive
  end unless contract tests establish another accepted semantic.
- Multiple indicator instances are preserved when their parameters differ.
- The generated deliverable remains a ZIP, even when the user's primary goal is
  a CSV.

### Risks and mitigations

- **Hidden saved-session consumers:** require a repository-wide consumer matrix
  and per-consumer snapshots before cutover.
- **Feature-specific Alpaca helper coupling:** gate reuse and provide a shared
  catalog/server-plan fallback.
- **Bookmark breakage:** freeze an explicit ingress map before route extraction.
- **Color accessibility drift:** test computed tokens in every supported theme.
- **Accidental vendor cost:** prohibit auto-fetch and prove request counts.
- **Dual-path export behavior:** delete old actions after job-path parity passes.

## 20. Definition of done

1. Explore, Build dataset, and Validate are separate child routes with shared
   workspace and run state.
2. Every documented legacy link redirects with equivalent validated state.
3. Base layouts use one document scroll and have no horizontal overflow at the
   target widths.
4. Explore is chart-first and fetches only after explicit refresh.
5. Build dataset never mounts or fetches the chart.
6. Indicator discovery is searchable, keyboard accessible, and screen-reader
   usable.
7. Indicator colors persist as theme tokens only and pass the 3:1 palette test.
8. Multi-output identity is fixture-pinned without a Data Lab dependency on an
   Alpaca feature directory.
9. Displayed symbols use `app-asset-identity` except inside plain-text controls.
10. Data Lab sends and stores numeric UTC milliseconds.
11. Python's canonical exchange calendar owns session resolution.
12. Python authors output columns, workload estimates, dependencies, and
    timeframe advice.
13. Export has one mapper and one job-backed execution path.
14. News is optional, lazy, compact, correctly timestamped, and failure-isolated.
15. Every inventoried saved-session consumer passes migration tests before legacy
    fields are removed.
16. Unit, contract, integration, contrast, AXE, responsive, and end-to-end tests
    pass.
17. No deprecated IBKR surface is touched or restored.
