# PRD — Strategy Lab Workbench and Results Experience

- **Date:** 2026-08-09
- **Status:** Ready for issue approval
- **Product surfaces:** Strategy Lab Workbench, History, persisted run Results
- **Delivery posture:** Four Terra-sized tracer bullets; each issue must be independently verifiable and labeled `ready-for-agent`
- **Builds on:** Strategy Lab redesign spec (2026-08-09), the persisted run-detail contract, the Python and LEAN engine adapters, and the shared `lightweight-charts` v5 component
- **Authority:** Python authors every numerical value and curve. .NET transports persisted values. Angular renders, formats, filters, and lays out those values without calculating trading results.

---

## 1. Executive summary

Strategy Lab currently spends most of its first viewport on page identity,
editable configuration, a terse verdict strip, and a full-width statistics
grid. The chart—the evidence that explains what the strategy did—begins below
those layers. Completed results also reuse the Workbench report tree, so the
Results surface retains editable controls and run actions that do not belong in
a read-only report.

The chart compounds the problem. Price, equity, and indicator panes are
separate chart instances whose visible logical ranges are relayed manually.
They can appear synchronized while still using different timestamp indexes,
so time ticks and vertical gridlines need not occupy the same pixels. The
displayed equity curve is mark-to-market portfolio value sampled by the
producing engine, while the buy/sell markers represent completed trades. The
line therefore moves while a trade is open instead of stepping only when profit
or loss is achieved.

This PRD reorganizes the same evidence into two focused products:

1. **Workbench** configures and starts a validation.
2. **Results** explains one immutable persisted run.

Results put one synchronized multi-pane chart in the first viewport and use a
compact left rail for a prominent grade, core statistics, the read-only run
configuration, and a collapsed **More statistics** disclosure. The chart uses
one native `lightweight-charts` pane tree and therefore one real time scale.
Its primary equity pane displays a Python-authored realized-equity staircase,
while the native mark-to-market curve remains the canonical input for risk
statistics and audit evidence.

Existing runtime-generated Strategy Lab history may be deleted instead of
backfilled. Committed golden fixtures, contract fixtures, reference outputs,
Oracle outputs, and test datasets are immutable and are never cleanup targets.

## 2. Product problem

### 2.1 The first viewport prioritizes framing over evidence

The current page renders a title, icon, descriptive subtitle, large vertical
gap, tab row, configuration rail, verdict line, and statistics strip before the
chart. On a desktop viewport, only the chart header and the top edge of the
price pane are visible without scrolling.

The sidebar already identifies the active product as Strategy Lab. Repeating
the page name and a descriptive sentence does not help the user complete the
page's job. “Diagnostic complete” restates the obvious state of a results page.
The verdict strip repeats the grade, trade count, and parity information that
also exist in the statistics or evidence model.

### 2.2 Workbench and Results have different jobs but share one tree

Workbench inputs are intentionally editable. Results configuration is
historical evidence and must be read-only. Sharing the same report composition
forces Results to inherit Workbench concerns such as re-run, editable controls,
and collapsible configuration behavior.

The desired actions are unambiguous:

- Workbench: **Run validation**.
- Results: **Back to workbench**.

Returning to Workbench restores the exact persisted configuration so the user
can modify it intentionally. Results never mutates a persisted run.

### 2.3 The displayed equity series answers the wrong visual question

The current curve is engine-native mark-to-market equity. Python records
portfolio value after every input bar; LEAN persists its native Strategy Equity
series at LEAN's emitted cadence. That evidence is correct for continuous risk
measurement, but it does not visually answer, “What profit or loss was achieved
at each sell?”

The primary Results chart needs a distinct **Realized equity** series:

- begin at initial cash;
- remain flat while a position is open;
- step at each completed trade's exit timestamp;
- add that trade's persisted net account-currency P&L exactly once;
- include a persisted synthetic terminal exit when the producer uses one;
- remain flat for a zero-trade run.

Risk metrics continue to use their existing canonical mark-to-market or
compatibility-ledger inputs. The UI must not imply that Sharpe, Sortino, or
maximum drawdown were calculated from the realized staircase.

### 2.4 “Synchronized” panes do not share one time scale

The current chart creates one chart instance per pane, adds hidden whitespace
anchors, and relays logical ranges with animation-frame scheduling. This is
inherently weaker than one chart with multiple panes:

- equity can contain timestamps between strategy candles;
- each chart can therefore have a different logical index map;
- each chart independently chooses time ticks and gridlines;
- resize and `fitContent` behavior must be coordinated manually.

The installed `lightweight-charts` v5.2 API supports native panes. One chart
instance can place price, equity, and indicator series at pane indexes while
retaining independent price scales. That is the required synchronization
boundary.

### 2.5 Statistics are repeated instead of organized

The report currently presents core metrics at the top, a separate LEAN
statistics dashboard below the chart, and fee/sequencing content that repeats
portfolio and trade Sharpe. Some top-level fields are incomplete; for example,
the report projection currently supplies no persisted expectancy value.

The new Results rail has exactly one statistics location. It does not discard
native LEAN fields or parity receipts. It places frequent metrics in the default
view and less frequent evidence behind **More statistics**.

## 3. Goals

1. Put meaningful price, signal, and realized-equity evidence in the first
   viewport on desktop.
2. Give every chart pane one actual horizontal time scale, identical tick
   positions, and coincident vertical gridlines.
3. Make realized trade outcomes visually correspond to sell markers.
4. Preserve canonical mark-to-market risk statistics and their provenance.
5. Separate editable Workbench configuration from immutable Results evidence.
6. Present the grade as the dominant statistics element.
7. Provide one statistics location for Python and LEAN runs without hiding
   native evidence.
8. Remove redundant presentation while retaining the underlying data.
9. Make the implementation smaller by deleting manual chart synchronization
   and shared Workbench/Results branching.
10. Reset legacy runtime history safely without touching scientific fixtures.

## 4. Non-goals

- Changing strategy signals, order timing, sizing, fills, commission, or trade
  pairing.
- Recomputing Sharpe, Sortino, drawdown, profit factor, or the run grade in
  Angular or .NET.
- Forcing pointwise parity between Python-native and LEAN-native mark-to-market
  curves.
- Removing LEAN native analysis, calculation receipts, parity evidence, or the
  trade ledger.
- Rebuilding Data Lab in this delivery.
- Changing the canonical Alpaca Broker V2 product.
- Deleting or regenerating any committed golden fixture or vendored reference.

## 5. Users and jobs

### Researcher configuring a run

- Select an engine, instrument, time window, strategy, and execution settings.
- Start validation with one consistently named action.
- See honest running progress and failure guidance.
- Return from a result with the run's exact inputs restored for intentional
  modification.

### Researcher reading a result

- See the strategy's entries and exits against price immediately.
- See achieved profit or loss step at the corresponding exits.
- Compare price, equity, and indicators at one exact time position.
- Read the grade and important statistics without scanning a full-width matrix.
- Recall precisely which configuration produced the evidence.
- Expand the complete statistics and engine receipts only when needed.
- Inspect validation analytics, native analysis, and the full trade ledger.

## 6. Product principles

1. **Evidence is the page thesis.** The chart begins at the top of Results.
2. **One surface, one job.** Workbench edits; Results explains.
3. **One horizontal clock.** Multiple panes share one chart time scale rather
   than approximating synchronization between charts.
4. **Name the curve honestly.** “Realized equity” means exit-booked P&L;
   “mark-to-market equity” means continuously valued holdings.
5. **Prominence follows decision value.** Grade and core metrics are visible;
   detailed receipts are available but collapsed.
6. **Reorganize; do not erase evidence.** Redundant copy may disappear, but
   unique statistics and receipts retain an accessible home.
7. **Python owns the numbers.** UI density work never becomes a pretext for
   client-side trading arithmetic.
8. **Strict new-run contract.** Runtime history is reset instead of adding
   permanent legacy display branches.

## 7. Information architecture

### 7.1 Strategy Lab shell

The standalone page-title header and descriptive subtitle are removed. The
active application navigation already communicates “Strategy Lab.”

The first content row is the tab list:

- **Workbench**
- **History**

The tab row uses the minimum height required for an accessible target and has no
large blank region above or below it.

### 7.2 Workbench

Workbench owns the editable configuration and run lifecycle. Its primary action
is always **Run validation**. When both engines are selected, secondary text may
state `Python + LEAN`; the action label does not change to “Run both engines.”

On successful persistence, Workbench navigates to the canonical Results route
for the new run. It does not embed the complete Results component tree beneath
the form.

### 7.3 History

Selecting a completed run navigates to that run's Results route. History does
not repopulate editable configuration and render results within its own tab.

### 7.4 Results

Results begins with a compact **Back to workbench** action and no promotional
header. The action returns to Workbench with the result's exact persisted
configuration restored.

Desktop Results use two columns:

```text
┌─ Results rail ──────────┬─ Shared-time-scale evidence ───────┐
│ Grade + core statistics│ Price + signals                    │
│ More statistics        │ Realized equity staircase          │
│ Run configuration      │ Indicator panes                    │
│                        │                                    │
└────────────────────────┴────────────────────────────────────┘
```

The chart column begins at the same vertical position as the statistics rail.
Validation analytics, LEAN native analysis, and the trade ledger follow below
the chart as collapsed deep dives.

At narrow widths, the Results rail becomes a compact block above the chart.
Core statistics stay visible; configuration and More statistics remain
collapsible. The chart retains a usable minimum height and horizontal gestures
must not be captured by nested page scrolling.

## 8. Results rail

### 8.1 Grade-led statistics

The grade is the rail's signature element:

- a large letter grade;
- the composite score expressed as `n / 100`;
- one grade-colored rule or edge;
- accessible text that does not rely on color.

The Results page does not render the phrase “Diagnostic complete.” The fact that
the persisted Results route loaded successfully communicates completion.

### 8.2 Core statistics

The default rail view shows a compact two-column ledger grouped by meaning:

**Returns**

- Net P&L
- Total return or canonical persisted return field
- Profit factor

**Risk-adjusted**

- Sharpe
- Sortino
- Maximum drawdown

**Activity**

- Total trades
- Win rate, including wins and losses
- Total fees

Missing values render as unavailable, never as zero. Every displayed metric is
a persisted producer-authored value or a direct typed projection of one.

### 8.3 More statistics

One **More statistics** button expands the remaining evidence in the same rail.
It contains, when available:

- remaining portfolio statistics;
- remaining trade statistics;
- runtime statistics;
- fee and sequencing details not already present in configuration;
- Python/LEAN calculation provenance;
- LEAN native parity receipt;
- cross-engine input, metric, readiness, and trade-reconciliation receipts;
- run ID, execution timestamp, duration, and source metadata.

The disclosure may scroll within the page, but it must not create a second
always-visible results dashboard or duplicate a metric already shown above.

### 8.4 Read-only run configuration

Configuration follows statistics and uses semantic label/value rows, not
disabled inputs:

- engine selection that produced this row;
- instrument and venue identity;
- start and end dates;
- strategy-bar timeframe and session;
- strategy name and parameters;
- fill mode;
- initial cash;
- commission/brokerage policy;
- data source, adjustment policy, and fixture identity when relevant.

Opaque audit tokens and fixture hashes remain exact. Human-readable labels use
the shared receipt-label rules where applicable.

## 9. Equity and statistics authority

### 9.1 Two different curves, two different purposes

The persisted report contract carries:

1. **Mark-to-market equity** — the existing producer-native curve used by
   canonical risk metrics, validation analytics, and audit notices.
2. **Realized equity** — a Python-authored display projection over completed
   trade P&L, used by the primary Results equity pane.

Angular never derives one from the other.

### 9.2 Realized-equity definition

Let `E0` be persisted initial cash and `pnl_i` be the persisted net
account-currency P&L of completed trade `i`, ordered by exit timestamp and stable
trade number. The producer emits:

```text
realized_equity_0 = E0
realized_equity_i = E0 + Σ pnl_j, for j = 1..i
```

Each new value is timestamped at `trade_i.exit_ms_utc`. The curve is rendered
with step interpolation. The producer supplies a starting point at the run's
first covered chart/equity timestamp and, when needed for a visible terminal
flat segment, a terminal point at the run's last covered timestamp.

All timestamps are `int64 ms UTC`. Accumulation uses the producer's canonical
accounting precision and explicit `atol=1e-6, rtol=0` validation for accumulated
P&L. Equal exit timestamps use stable trade-number order and result in one
deterministically aggregated timestamp point.

The final realized value must equal persisted initial cash plus persisted total
closed-trade P&L within the pinned tolerance. A synthetic terminal exit is a
completed trade for this projection and is visibly identified in the trade
ledger.

### 9.3 Risk metrics remain canonical

Sharpe, Sortino, maximum drawdown, and any other mark-to-market or compatibility
metric retain their existing canonical inputs and formulas. Their help text and
provenance identify the producing contract. No metric is silently recomputed
from the realized staircase.

The implementation adds the realized-equity concept to the math source registry
and engine authority map in the same PR as its canonical producer.

## 10. Persisted report contract

The runtime report envelope advances to a strict version that includes both
curve identities and typed metadata. Existing runtime runs are removed, so the
new reader does not require a permanent legacy branch or a best-effort fallback
that could mislabel mark-to-market points as realized equity.

The boundary guarantees:

- explicit report schema version;
- explicit curve identity and cadence;
- `int64 ms UTC` timestamps;
- strictly increasing output timestamps after deterministic equal-exit folding;
- finite numerical values;
- point-count/downsample receipts for each curve;
- mark-to-market errors remain distinguishable from realized-curve errors;
- primary statistics have typed nullable fields rather than an unstructured
  Angular dictionary;
- parity and native-statistics receipts retain their producer identity.

.NET parses and exposes this stored envelope without computing P&L, returns,
ratios, or grades.

## 11. Native multi-pane chart

### 11.1 One chart instance

The shared chart creates exactly one `IChartApi`. Series are assigned to native
pane indexes:

- pane 0: price candles, volume, strategy overlays, and trade markers;
- pane 1: realized-equity area/line series with step interpolation;
- pane 2+: oscillator and secondary indicator series.

The chart owns one time scale, one visible range, one `fitContent` operation,
and one resize lifecycle. Native panes retain separate y-scales and controlled
heights.

### 11.2 Alignment contract

After initial fit, scroll, zoom, resize, indicator addition/removal, and expanded
mode transitions:

- every vertical time gridline crosses every pane at the same x-coordinate;
- the bottom time ticks describe those same gridlines;
- a trade marker and realized-equity step with the same timestamp occupy the
  same x-coordinate;
- there is one vertical crosshair time coordinate;
- only the bottom time axis is visible.

The old hidden-anchor series, per-pane chart array, range-subscription relay,
reentrancy flag, and animation-frame synchronization are deleted.

### 11.3 Attribution

The in-chart TradingView attribution logo is disabled. An accessible
TradingView attribution link is retained in an application About/licensing
surface so the chart canvas remains visually quiet without dropping required
attribution.

## 12. Result actions and navigation

- Workbench exposes **Run validation**.
- Results exposes **Back to workbench**.
- Results does not expose Re-run.
- Back to workbench restores the persisted configuration and makes it editable.
- Changing the restored strategy/configuration clears any stale result identity.
- Browser Back and direct bookmarked Results URLs remain functional.
- Pending parity may continue polling in Results without changing layout or
  introducing a headline strip.

## 13. Runtime-history reset

The user has authorized deletion of all existing runtime-generated Strategy Lab
runs because the report contract is intentionally forward-only.

Before deletion, the implementation must resolve and report exact counts for:

- persisted Strategy Lab/engine/LEAN execution rows in scope;
- dependent persisted trades;
- dependent parity verdicts;
- runtime-generated run artifacts associated with those rows.

Deletion uses explicit identifiers or verified table relationships. It must not
delete ticker identity, cached market bars, source code, or any path outside the
resolved runtime-artifact roots.

These locations and artifact classes are categorically excluded:

- `PythonDataService/tests/fixtures/**`;
- `contracts/fixtures/**`;
- `docs/references/golden-fixtures/**`;
- `references/**`;
- normalized/raw Oracle fixtures committed under test fixture roots;
- any other Git-tracked fixture or reference artifact.

Tests assert the cleanup selector cannot match fixture/reference roots. The
runtime deletion is reported separately when executed, including whether the
deleted data is recoverable from a database or artifact backup.

## 14. Loading, missing, and failure states

- Workbench strategy-loading errors do not render a stale Results tree.
- Results loading retains the rail/chart skeleton dimensions to avoid layout
  jumps.
- A missing realized curve renders an explicit “Realized equity unavailable”
  notice; it never substitutes the mark-to-market curve under the realized
  label.
- A missing mark-to-market curve can leave the realized curve visible while
  risk/validation notices honestly report their unavailable evidence.
- A malformed strict report envelope fails visibly and does not guess at field
  meanings.
- Truncated trade-ledger evidence remains labeled; primary full-run statistics
  and the persisted realized curve continue to represent the complete run.

## 15. Accessibility and responsive behavior

- Workbench/History tabs implement complete tab semantics and keyboard
  navigation.
- Back to workbench, More statistics, chart expansion, and indicator actions
  have accessible names and visible focus.
- Grade meaning is available in text and is not color-only.
- The statistics disclosure exposes `aria-expanded` and maintains focus.
- Read-only configuration uses semantic description-list structure.
- Chart panes have meaningful accessible labels, while duplicated canvas detail
  is not announced repeatedly.
- Desktop, tablet, and mobile layouts pass AXE and preserve WCAG AA contrast.
- Reduced-motion preferences are respected; this redesign requires no ambient
  animation.

## 16. Testing and evidence

### 16.1 Numerical contract

- Golden cases for winning, losing, break-even, fee-bearing, equal-exit-time,
  synthetic-exit, and zero-trade runs.
- Explicit accumulated-P&L tolerance `atol=1e-6, rtol=0`.
- Python and LEAN persistence paths produce the same realized-equity contract
  from equivalent closed-trade ledgers.
- Final realized equity reconciles to initial cash plus total persisted
  closed-trade P&L.
- Existing mark-to-market risk-statistics tests remain unchanged and passing.
- Golden fixtures are consumed read-only and are never regenerated to satisfy
  the implementation.

### 16.2 Transport contract

- Persisted report schema version and both curves round-trip through .NET and
  GraphQL.
- Timestamps remain JSON numbers in milliseconds.
- Missing/malformed fields produce typed, honest errors.
- Primary statistic fields and parity/native receipts retain nullability and
  provenance.

### 16.3 Chart behavior

- The chart factory is called once regardless of pane count.
- Series are created at the correct native pane indexes.
- Realized equity uses step interpolation.
- One `fitContent` and one resize path control all panes.
- Browser interaction tests cover initial fit, zoom, pan, resize, and indicator
  pane changes.
- A visual/pixel regression verifies coincident vertical gridlines and matching
  marker/step x-coordinates.
- The in-chart TradingView logo is absent and the accessible attribution link is
  present elsewhere.

### 16.4 Product behavior

- Workbench has no page-title/subtitle block and its tabs are the first content
  row.
- Workbench's primary action is Run validation for Python, LEAN, and both.
- Successful runs and History selections navigate to Results.
- Results has Back to workbench and no Run validation/Re-run control.
- Results restores exact configuration on return.
- Grade is prominent; Diagnostic complete is absent.
- Core statistics appear only once.
- Trade count appears under Activity; parity/native receipts appear under More
  statistics.
- Configuration is read-only semantic information on Results.
- Deep dives no longer duplicate the statistics dashboard.
- Responsive and AXE tests pass.

## 17. Success criteria

1. At a 1440×900 desktop viewport, the chart's price pane begins in the first
   viewport without scrolling.
2. Price, realized equity, and indicator panes use one native time scale and
   pass the alignment interaction regression.
3. Every realized-equity step reconciles to a completed trade exit and net P&L.
4. Canonical risk metrics remain unchanged by the new display curve.
5. Results contain no editable controls and Workbench contains no full Results
   tree.
6. The grade and core metrics are readable without expanding More statistics.
7. Every unique legacy statistic or receipt remains accessible in the unified
   disclosure or an explicitly retained deep dive.
8. Runtime history is reset with zero fixture/reference files changed or
   deleted.
9. No Angular or .NET trading arithmetic is introduced.

## 18. User stories

- **US-1:** As a researcher, I want Workbench and History at the top so the page
  starts with navigation rather than repeated identity copy.
- **US-2:** As a researcher, I want Run validation to mean the same thing for
  every engine choice.
- **US-3:** As a researcher, I want completed runs to open a dedicated Results
  page so configuration evidence cannot be mistaken for editable inputs.
- **US-4:** As a researcher, I want Back to workbench to restore the exact run
  inputs so I can make an intentional variation.
- **US-5:** As a researcher, I want achieved P&L to step at sell signals so the
  equity pane explains completed outcomes.
- **US-6:** As a researcher, I want all panes to share exact time ticks and
  gridlines so I can compare the same instant vertically.
- **US-7:** As a researcher, I want the grade to be the dominant statistic so I
  can orient quickly.
- **US-8:** As a researcher, I want one compact statistics location so Python
  and LEAN reports have a consistent reading pattern.
- **US-9:** As an auditor, I want all native metrics and parity receipts retained
  behind More statistics so density improvements do not erase evidence.
- **US-10:** As a maintainer, I want runtime history reset without compatibility
  branches while scientific fixtures remain immutable.

## 19. Delivery slices

### Slice 1 — Strict realized-equity report

Deliver one newly executed Python or LEAN run whose persisted Results report
contains both canonical mark-to-market equity and a reconciled realized-equity
staircase. Display the staircase in the existing equity pane, register the new
math concept and authority, and safely reset old runtime history without
touching fixtures.

### Slice 2 — Dedicated Workbench and Results navigation

Separate editable Workbench state from immutable Results composition. Put tabs
at the top, remove the title/subtitle block, navigate successful and historical
runs to Results, and restore exact configuration through Back to workbench.

### Slice 3 — One native multi-pane chart

Replace the multiple-chart synchronization mechanism with one native-pane
chart. Prove exact grid/tick alignment across interaction states, render realized
equity with step interpolation, and move TradingView attribution out of the
chart canvas.

### Slice 4 — Grade-led unified Results rail

Move the prominent grade, core statistics, More statistics disclosure, and
read-only configuration into the left Results rail. Remove the verdict strip,
duplicate statistics dashboard, and repeated fee/Sharpe fields while preserving
all unique metrics and receipts.

Slices 1 and 2 can start independently. Slice 3 consumes Slice 1's realized
curve. Slice 4 consumes the dedicated Results composition from Slice 2 and the
typed result fields established by Slice 1.

## 20. Definition of done

- All four slices meet their acceptance criteria and are demoable separately.
- The numerical and transport contracts are documented and tested.
- The chart uses one native time scale with no manual synchronization code.
- Workbench and Results have distinct component ownership.
- The first-viewport and responsive layouts match this PRD's hierarchy.
- Runtime history cleanup has an auditable target/count receipt.
- Git shows no deleted or modified golden/reference fixtures caused by cleanup.
- Relevant Python, Backend, Frontend, contract, accessibility, and browser tests
  pass.
- The final diff passes the repo's thermo-nuclear maintainability review.
