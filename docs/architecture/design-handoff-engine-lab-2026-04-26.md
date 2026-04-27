# Design handoff — Engine Lab (and shared ticker picker)

**Date**: 2026-04-26
**Owner**: Inkant
**For**: `frontend-design` agent (Claude Design)
**Scope**: `Frontend/src/app/components/lean-engine/` (all tabs) + the
shared `Frontend/src/app/shared/ticker-range-picker/` used by both
Engine Lab and Data Lab.

---

## 1. Context & goals

The Engine Lab page (`/lean-engine`) is the cockpit for running and
reviewing LEAN-compatible backtests. It has four to five tabs:
**Configure**, **Results**, **History**, **Docs**, and (feature-flagged)
**Replay**. It currently scrolls vertically on every reasonable
viewport, has inconsistent surface tones between cards, and is missing
two pieces of functionality the user expects.

**Design goals — verbatim from user**:

1. Production-readiness card stays always-visible — it is the lead.
2. Everything else: progressive disclosure. Show the verdict, hide the
   detail behind expand / hover / drawer.
3. **No vertical scroll on large screens** (≥ 1440 wide × ≥ 900 tall).
   Scroll on smaller viewports is acceptable.
4. Density is the win, not whitespace.
5. Aesthetic: keep the current TV-terminal dark mode. Tokens in
   `Frontend/src/app/styles/_tokens.scss`. Do **not** introduce new
   colours — pull from tokens.

---

## 2. What's already shipped (pre-design baseline)

So the designer isn't re-solving solved problems:

- **Chart palette token-aligned.** `engine-chart` was using drifted hex
  (`#0f1117`, `#00c896`, `#e5334e`). Now uses `$bg-surface`,
  `$bull (#26a69a)`, `$bear (#ef5350)`, `$border-light`, `$accent`. The
  chart panel now reads as the same surface tone as every other card
  on the page. (`engine-chart.component.ts` and `.scss`.)
- **History toolbar label.** "sorted by **executedat** desc" is now
  "sorted by **Run date**, newest first". Direction label is
  context-aware (numeric columns read "highest first" / "lowest
  first"). (`engine-history.component.ts` + `.html`.)

The user perceived three different shades of dark on the Results tab
("dark chart on a white background"); that was almost certainly the
hex-drift inconsistency above. After the fix the page should read as
**one consistent dark canvas**. Designer should re-confirm on visual
audit.

---

## 3. Page-level shell consistency

**Issue**: every route should share a single page-shell wrapper —
consistent gutter, consistent header, consistent background. There is
already an `<app-page-header>` (`Frontend/src/app/shared/page-header/`),
but no enforced page-shell composition.

**Designer task**:

- Spec a shell component (working name: `app-page-shell`) that
  composes header + optional toolbar slot + content slot, and replaces
  the various ad-hoc per-page wrappers.
- Or: keep `<app-page-header>` as-is and document a CSS contract
  (`.page` wrapper class with the gutter, max-width, vertical rhythm,
  and bottom hairline) that every route must use.

The `Engine Lab` h1 itself is already rendered with `$text-primary`
(20.8:1 contrast, AAA). The user perceived the title as "muted" —
likely the long subtitle ("LEAN-compatible backtest engine with full
statistics, trade history, and research journal") flattens visual
hierarchy. Recommend either an eyebrow + shorter subtitle, or stronger
title weight/scale. Designer's call.

---

## 4. Configure tab — ticker-range picker rebuild

**This is the largest piece of work in the brief** and the user's
most specific complaint. Both Engine Lab and Data Lab use the same
`<app-ticker-range-picker>` component but stuff their own extra
controls *around* it, and the picker's internal layout has no
semantic grouping. The result is fields appearing next to each other
that have no conceptual relationship.

### 4.1 Current state (with file pointers)

`Frontend/src/app/shared/ticker-range-picker/ticker-range-picker.component.html`
lays out a flat grid:

```
┌──────────┬────────────────┬──────────────┬────────┐
│ Ticker   │ From → To      │ Resolution   │ Span   │
└──────────┴────────────────┴──────────────┴────────┘
┌─────────────────────────────────────────────────────┐
│ Quick ranges: [1W][1M][3M][6M][1Y][2Y]    ☐ Auto…  │
└─────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────┐
│ Cache availability strip (per-day cells + legend)   │
└─────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────┐
│ Advisories                                          │
└─────────────────────────────────────────────────────┘
```

Then *outside* the picker, each consumer adds its own controls:

- **Data Lab** appends an `.options-row` with three fieldsets:
  `Bar timeframe (Auto/Manual)`, `Session (Regular/Extended)`, and
  `API pacing (Auto-chunk)`. (`data-lab.component.html:156–217`.)
- **Engine Lab** appends its own `.options-card` with `Strategy /
  Fill mode / Initial cash / Commission`.
  (`lean-engine.component.html:78–122`.)

### 4.2 What's wrong with the current grouping

User's words, paraphrased:

- **Ticker has no relation to From → To** but they sit side-by-side
  with identical visual weight. They're different concerns:
  *what instrument* vs *what time window*.
- **Resolution is deeply tied to Range** (1-min bars over 2 years is
  ~200k rows; daily bars are 500). They should live together.
- **Span is a derivation of Range** — it should sit *inside* the
  range group, not in a fourth column.
- **Quick ranges** (`1W / 1M / 3M / 6M / 1Y / 2Y`) belong with the
  date inputs they shortcut. They're currently in a separate row.
- **Auto-resolution** exists in Data Lab (`autoBarTimeframe` signal,
  `data-lab.component.ts:379`) but lives *outside* the picker. Engine
  Lab doesn't have it at all. It should sit *with* the resolution
  buttons since "auto" is a mode of the resolution control.
- **RTH / Extended toggle**: Data Lab has it; Engine Lab is missing
  it entirely. User considers it "very real and very important".
  Logically it belongs to the time-window group (a session is a
  filter on the time window), not to a separate "options" block.
- **Availability legend is dumb**: it always renders all five
  swatches neutrally. If the only gaps are weekends, "weekend"
  should light up. If everything is on disk, "complete" should light
  up. The user wants the *dominant* state highlighted so they can
  tell at a glance whether the picker is happy.

### 4.3 Proposed grouping (starting point — designer to refine)

Three semantic groups inside the picker, plus the existing
availability + advisory blocks:

**Group A — Instrument** (`what`)
- Ticker combobox

**Group B — Time window** (`when`)
- From → To inputs
- Span readout (right-aligned inside this group)
- Quick-ranges chip row (1W / 1M / 3M / 6M / 1Y / 2Y)
- **Session: Regular / Extended** (new — see backend dep §6.1)

**Group C — Sampling** (`how dense`)
- Resolution buttons (1m / 1h / 1d)
- **Auto / Manual** toggle (new in picker — lift from Data Lab)
- Auto readout when on

**Group D — Availability** (existing strip, unchanged structure)
- Per-day cells
- Smart legend: highlight the dominant state (see §4.5)

**Group E — Advisories** (existing, unchanged)

The designer should decide:
- Visual separators between groups: gap, hairline, eyebrow label,
  card-within-card?
- Are A/B/C cards or are they regions inside one card?
- Mobile: do groups stack or collapse into accordions?

### 4.4 What stays *outside* the picker

These are page-specific and do not belong inside a shared component:

- **Data Lab**: API pacing / Auto-chunk
- **Engine Lab**: Strategy selector, Fill mode, Initial cash,
  Commission

These are run-execution settings, not data-window settings. They
should live in their own card on each consuming page. Designer can
spec how that card should look (likely matching the picker's outer
chrome for visual consistency).

### 4.5 Smart availability legend

Today every legend swatch renders neutrally. Proposed behaviour:

```
Cache availability summary signals (computed):

  if summary.complete === summary.weekdays         → state: "complete"
  else if summary.partial > 0 && summary.hole === 0 → state: "partial"
  else if summary.hole > 0                          → state: "hole"
  else if all gaps are weekends                     → state: "weekend"
  else                                              → state: "missing"
```

The dominant state's legend chip should be highlighted (e.g. tinted
background + bold label); the others remain dim. Designer chooses the
visual treatment. The computation is a small `computed()` I'll add
once the visual is specified.

### 4.6 Picker is shared — keep it shared

Important: Engine Lab and Data Lab **must use the same picker**.
Avoid the temptation to fork. The picker takes inputs (`hideResolution`,
`showAutoFetch`, `availability`) — extend that API to also gate the
new groups (e.g. `hideSession`, `hideAutoResolution`) so each page can
opt in/out without forking.

---

## 5. Results tab

### 5.1 Production Readiness card (lead element)

`Frontend/src/app/components/lean-engine/readiness-score-card/`

User likes the UX, wants it **more compact**. Currently:

- Score ring (110 × 110 SVG)
- Headline (label + grade chip + signal chip + verdict paragraph)
- Expand toggle
- Always-visible dimension strip (5 pills horizontally)
- Expanded detail (per-dimension blocks with sub-scores)

**Designer task**: tighten without losing at-a-glance readability.
Options to consider:

- Tabular dimension breakdown (score / weight / verdict in three
  columns) instead of pills
- Sidebar layout: ring + grade fixed left, dimensions tabular right
- Move the verdict paragraph into a tooltip on the grade chip; show
  only a one-line summary in the card head

Constraint: ring + grade + verdict-summary must remain visible
without expansion.

### 5.2 Hero metrics row — biggest density win

`engine-results.component.html:22–82`

Currently:
- 6 cards × ~120 px tall (Net Profit / Max DD / Sharpe / Sortino /
  Profit Factor / Win Rate)
- Secondary 2-card row (Expectancy + Trade Summary)
- Sharpe-divergence card
- Fee Analysis (4 more cards)
- LEAN statistics dashboard
- Trade log (already collapsed)

This is the bulk of the vertical bloat. **Strategies for the
designer**:

- Collapse the six hero cards into a single dense KPI strip
  (~60 px tall) with the verdict on hover. The colour-coded
  border-top stripe stays.
- Move "Fee Analysis", "Trade Sharpe vs Portfolio Sharpe", and the
  secondary Expectancy/Trade Summary row into the Production
  Readiness card's expanded detail, OR behind a "Details" drawer.
- Trade log stays collapsed by default — already correct.

### 5.3 Chart placement

`engine-chart` is now token-aligned (see §2). The remaining choice:
edge-to-edge inside the tab panel, or carded with surface chrome
matching other cards. User dislikes "no horizontal padding" — that
likely means they want it carded. Designer's call.

---

## 6. History tab

`Frontend/src/app/components/lean-engine/engine-history/`

Currently a hand-rolled `<table>` with a custom toolbar and a
column-chooser dropdown. Default-on columns: Date, Strategy, Symbol,
Range, Net P&L, Sharpe, Max DD, Win %, Trades, plus a Replay button.

**Designer task**:

- Migrate the table presentation to PrimeNG `<p-table>` with sortable
  headers, density toggle (compact / normal), sticky header on
  vertical scroll, row hover, zebra striping (optional). Keep the
  existing column-chooser logic and persisted prefs.
- Define the header treatment: uppercase eyebrow style, slight
  contrast lift on column header bg.
- Define row hover state and the "click row to load" affordance
  (currently no visual hint that the row is clickable).
- Notes column inline-edit: keep the on-click → input pattern, but
  spec the focus ring and confirmed/saved feedback.

The "white-on-dark contrast" the user reported on this tab was
almost certainly the same chart-hex drift; should be resolved after
§2. Designer to re-audit on a refresh.

---

## 7. Docs tab

`Frontend/src/app/components/lean-engine/lean-engine-docs/`

User reports contrast issues. Likely the same root cause as History
(now fixed). Designer should:

- Audit prose contrast against `$bg-canvas` — body text must hit AA.
- Audit code blocks (`<pre>`, `<code>`) — the global `code` style in
  `styles.css:48` uses `$bg-elevated`, which can disappear inside an
  already-elevated card.
- Confirm benchmark scorecard surface (it has its own subdirectory)
  matches the rest of the app.

---

## 8. Backend dependencies (this is not all frontend work)

Two of the user's asks need backend support:

### 8.1 RTH / Extended-hours session toggle on Engine Lab

`PythonDataService/app/routers/engine.py` does **not** accept a
`session` parameter. The strategies hardcode `_is_rth(bar.end_time)`
checks against 9:30–16:00 ET (per the docstring at line 729-730).
Adding a UI toggle without engine support would silently do nothing.

**Required Python work** (separate PR, not the designer's concern but
flagged here so this brief doesn't ship a UI for a feature that
doesn't exist):

- Add `session: Literal['rth', 'extended'] = 'rth'` to the engine
  request model.
- Plumb it through to the strategy filter so `_is_rth` becomes
  `_in_session(bar.end_time, session)`.
- Document strategy-by-strategy whether extended-hours is even
  meaningful (some strategies have RTH-only assumptions baked into
  their logic).

The designer should design the toggle assuming the backend will
catch up. The frontend can ship the toggle disabled (with a tooltip
explaining "extended hours coming soon") if the backend isn't ready
yet.

### 8.2 Auto-resolution port from Data Lab

The auto-resolution math (`pickAutoBarTimeframe(days)`) lives in
Data Lab today but is generic. To use it inside the shared picker
component, the function should move to a shared util
(`Frontend/src/app/utils/`). Trivial mechanical move, not design
work.

---

## 9. Out of scope

- **Replay tab** — feature-flagged, separate concern.
- **Strategy Builder** and other sibling routes — handle in a
  follow-up brief if needed.
- **Indicator catalog** at the bottom of Data Lab — separate brief.
- **Behavioural changes** beyond the layout/grouping above
  (e.g. don't redesign the strategy picker's data model — only its
  visual surface if needed).

---

## 10. Open questions for the designer

1. Picker groups (§4.3): cards-within-card, hairlines, or just gap?
2. Hero metrics (§5.2): single dense strip vs. a 3-column "split"
   layout (outcome / risk / edge-quality columns)?
3. Production Readiness expansion (§5.1): does the dimension strip
   stay always-visible, or collapse on small viewports?
4. History (§6): density default — compact or normal?
5. Mobile breakpoint behaviour: what's the smallest viewport we
   support, and what collapses to accordion vs. stays inline?
6. Should the page-shell wrapper (§3) become its own component, or
   stay a CSS contract on the host?

---

## 11. Files the designer will touch

Most likely:

- `Frontend/src/app/shared/ticker-range-picker/*` — main rework
- `Frontend/src/app/shared/page-header/*` — title hierarchy tweak
- `Frontend/src/app/components/lean-engine/lean-engine.component.{html,scss}`
- `Frontend/src/app/components/lean-engine/engine-results/*`
- `Frontend/src/app/components/lean-engine/readiness-score-card/*`
- `Frontend/src/app/components/lean-engine/engine-history/*`
- `Frontend/src/app/components/lean-engine/lean-engine-docs/*`
- `Frontend/src/app/components/data-lab/data-lab.component.{html,scss}`
  — picker API consumer, may need plumbing changes when picker grows
  the new toggles
- (possibly) a new `Frontend/src/app/shared/page-shell/` if the
  designer specs a wrapper component

Tokens to consume from: `Frontend/src/app/styles/_tokens.scss`. Do
not introduce new hex.
