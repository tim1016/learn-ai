# Engine Lab — progressive-revelation redesign

- **Date:** 2026-07-19
- **Status:** Design approved, pending spec review
- **Scope:** Frontend only (`Frontend/src/app/components/lean-engine/**`, one new `shared/` component)
- **Route affected:** `/engine` (`LeanEngineComponent`)

## Context

The Engine Lab (`/engine`) validates one strategy across the Python and LEAN
engines. Since the split of "SPY EMA crossover strategy" into an **EMA
crossover signal**, and since a dedicated **Strategy Validation** page
(`/strategy-validation`) now owns reference-code and validation evidence, the
Lab page carries surfaces that no longer earn their place.

Today the **Workbench** tab (`lean-engine.component.html`) renders a full-width
top strip (params form / LEAN algorithm card / launcher card) followed by a
**three-rail IDE grid** (`.ide-grid`):

- **Left rail** (`.ide-rail-left`, 260–320px): Engine · ticker+range picker ·
  Strategy queue · Execution.
- **Main** (`.ide-main`): run banner → run bar → `engine-run-report` (or a
  placeholder).
- **Right rail** (`.ide-rail-right`, 230–320px): `validation-evidence-card` +
  Algorithm `<details>` + Gotchas `<details>`.

### Problem

1. **Charts are width-starved.** When results render, `engine-results` sits
   inside the center rail (`minmax(440px, 1fr)`), squeezed between a ~300px left
   rail and a ~300px right rail. On a 1440px viewport that leaves ~800px for the
   main column, and `engine-results` *then* splits it 1.6/1 (chart +
   readiness card) — so the price chart gets ~500px. The **right rail is the
   main width thief.**
2. **Redundant validation surfaces.** The `validation-evidence-card` and the
   LEAN reference-code now duplicate the Strategy Validation page.
3. **Config never gets out of the way.** Every input stays fully expanded even
   after it is set and a run is in flight, competing with results for the eye.
4. **Copy is bespoke ×3.** Three hand-rolled clipboard implementations
   (launcher command in `lean-engine.component.ts`; `quantconnect-reference-code`;
   `lean-engine-docs` copy blocks), no shared affordance, inconsistent feedback.

## Goals

- Once results exist, the **price chart and equity curve take most of the
  page width.**
- Inputs live in a **collapsible side nav**; configured sections collapse to a
  one-line summary, and the whole nav collapses to an icon rail.
- **One vertical-scroll page.** No new tabs, no modals. (Existing top-level
  tabs — Workbench / History / Strategy detail — are retained.)
- **One shared copy affordance:** a hover-revealed copy icon; click copies and
  confirms.
- Push validation/reference concerns to the Strategy Validation page; keep the
  Lab focused on *running* and *reading* results.

## Non-goals

- No changes to the History tab, the run-dock, the run/SSE pipeline, or the
  `engine-run-report` data contract.
- No backend/GraphQL/Python changes. This is a presentational restructuring.
- No inline LEAN Python in the Lab (explicitly deferred to Strategy Validation
  — see Decision D).

## Locked decisions

| # | Decision | Choice |
|---|---|---|
| A | Config nav collapse | **Auto-collapse configured sections + whole-rail icon strip.** Auto-collapse fires on first successful run; user can pin open/closed (persisted). |
| B | Chart layout in results | **Full-width, price + equity stacked** (both visible; drop the in-chart Price/Equity toggle in results mode). |
| C | Validation-evidence card | **Dropped** from the Lab (Strategy Validation page owns it). Data-policy fact preserved — see Risk R1. |
| D | Strategy tab LEAN Python | **Link out only** — thin tab with a deep-link to `/strategy-validation?strategy=<key>`. No inline source. |

## Design

### Two modes, one page

Both modes are the **Workbench** tab; the difference is driven by whether a run
has completed (`completedRunId()` is set) plus the nav's pinned state.

```
SETUP MODE (no results)                RESULTS MODE (after a run)
┌ Workbench | History ───────────┐     ┌ Workbench | History ──────────────────┐
│ CONFIG NAV   │ MAIN            │     │▸│ RUN SUMMARY  SPY·2024·min·ema_cross  │
│ ▾ Engine     │ [Run bar]       │     │C│ ┌ Hero KPI strip (7) ─────────────┐  │
│ ▾ Time window│ ┌ setup ───────┐│     │O│ ┌ PRICE CHART  full-width, tall ──┐  │
│ ▾ Strategy   │ │ placeholder  ││     │N│ │                                 │  │
│ ▾ Execution  │ │ + data policy││     │F│ └─────────────────────────────────┘  │
│ (▾ LEAN)     │ └──────────────┘│     │▸│ ┌ EQUITY CURVE full-width ────────┐  │
│ [◀ collapse] │                 │     │ │ ▸ Readiness ▸ Fees ▸ LEAN ▸ Ledger │
└──────────────┴─────────────────┘     └─┴────────────────────────────────────┘
   right rail REMOVED                    nav auto-collapsed to icon rail;
                                         each configured section = 1-line summary
```

### 1. Config nav (new primitive `config-section` + inline wiring)

**Refinement over the original plan:** rather than a monolithic
`engine-config-nav` child that would need ~10 two-way `model()` bindings plumbed
back to the parent (fragile), the nav is built from a small reusable
**`config-section`** collapse primitive (header + editorial index + collapsed
summary + projected body). The actual input controls stay bound *inline* to the
parent's signals via `<ng-content>`, so folding a section never plumbs form
state through a child. The parent owns only the open-state map + summaries +
the auto-collapse effect. This is lower-risk and keeps `data-testid`s intact.

- **File:** `Frontend/src/app/components/lean-engine/config-section/config-section.component.{ts,html,scss,spec.ts}`
- **Sections (accordion, top→bottom):**
  1. **Engine** — the engine `<select>` (python / lean / both).
  2. **Time window** — `app-ticker-range-picker` (symbol + date range +
     resolution + availability).
  3. **Strategy** — the strategy queue list **and** the strategy parameters
     form (today's `params-card`), merged here.
  4. **Execution** — fill mode · initial cash · commission/order.
  5. **LEAN** *(conditional — only when `engine() !== 'python'`)* — the audited
     LEAN template summary + `View copyable QuantConnect code →` link + the
     launcher card (status, command, Check/Copy actions).
- **Per-section collapse:** each section header carries a `configured?` flag
  (computed from whether its inputs hold non-default/valid values). When
  configured **and** collapsed, the header shows a **one-line summary**:
  - Engine → `Python (in-process)` / `LEAN (sidecar)` / `Both`
  - Time window → `SPY · 2024-01-02 → 2024-06-28 · minute`
  - Strategy → `ema_crossover_signal` (+ `n params` if any set)
  - Execution → `signal close · $100k · $1.00/order`
  - LEAN → launcher status pill (`Ready` / `Not running`)
- **Whole-rail collapse:** a `[◀]`/`[▶]` toggle collapses the rail to a slim
  **icon strip** (one icon per section, reusing each section's existing
  `pi-*`). Hover/click an icon → expand that section as a popover (or re-open
  the full rail, whichever is simpler to land first; popover is the target).
- **Auto-collapse:** on the transition `completedRunId(): null → value`,
  collapse every *configured* section and the rail. Pinned-open/closed is a
  user override persisted to `localStorage` (key `engineLab.configNav`,
  mirroring the run-dock's persistence pattern).
- **Responsive:** below ~900px the nav is not a side rail — it renders as a
  single **top collapsible bar** above the main content (CSS-only via the grid
  breakpoints already in `lean-engine.component.scss`).

### 2. Main area — full-width stacked charts

- **`engine-results.component.html`:** replace the `.results-split` (chart +
  readiness) with a full-width vertical stack:
  `run summary → hero KPI strip → price chart → equity curve → drawers → ledger`.
- **Readiness card** (`app-readiness-score-card`) moves from the side into the
  existing drawer row (alongside Fees & LEAN-stats drawers) — or a full-width
  band directly under the equity curve. Target: a collapsible drawer to keep
  the scroll short.
- **`engine-chart.component`:** add a `layout` input (`'stacked' | 'tabbed'`,
  default `'tabbed'` to preserve any other caller). In `'stacked'` the tab
  switcher is hidden and **both** `#priceChart` and `#equityChart` render
  (remove the `display:none` gating). `engine-results` passes `layout="stacked"`.
  - Price container min-height ~560px; equity ~320px (from today's 480/300).
  - Both charts must `resize()` to the now-wider container — verify the
    lightweight-charts `ResizeObserver`/`applyOptions({ width })` path fires on
    the mode switch and on nav collapse/expand (width changes).

### 3. Right rail removed — rehoming its content

- **`validation-evidence-card`** — deleted. Once unmounted from the Lab it had
  no remaining consumer (the Strategy Validation page renders its own evidence),
  so keeping it would be dead code. Component + spec removed.
- **Algorithm pseudocode** (`strategy.algorithm_pseudocode`) → moved to the
  **Strategy tab**.
- **Gotchas** (`strategy.gotchas`) → already in the Strategy tab; the right-rail
  duplicate is removed.

### 4. Strategy tab (edit `strategy-detail-tab`)

- Keeps: display name, description, **Validation contract** (registry key,
  resolutions), **Known gotchas**.
- Adds: **Algorithm** (pseudocode `<pre>`, migrated from the deleted right
  rail) and a prominent **`View LEAN code on Strategy Validation →`** button
  (`routerLink="/strategy-validation" [queryParams]="{ strategy: detail.name }"`).
- No inline LEAN Python source.

### 5. Shared copy affordance (new `shared/copy-button`)

- **File:** `Frontend/src/app/shared/copy-button/copy-button.component.{ts,html,scss}`
- **API:** `text = input.required<string>()`; optional `label = input('Copy')`,
  `copiedLabel = input('Copied')`, `variant = input<'icon' | 'button'>('icon')`.
- **Behavior:** shows a `pi-copy` icon; on click copies `text()`, swaps to
  `pi-check` + copied label for ~1.6s (timer via `setTimeout`, cleared on
  destroy). Clipboard-unavailable → emit an error state and render the
  fallback message ("Copy was blocked — select and copy manually"), ported
  from `quantconnect-reference-code.component.ts`.
- **Hover reveal (`variant='icon'`):** icon `opacity: 0` at rest; visible on
  parent `:hover`, on `:focus-within`, and always on coarse pointers
  (`@media (pointer: coarse)`). The `<button>` is always present in the DOM and
  keyboard-focusable with an `aria-label` — so it is reachable and named even
  when visually hidden (AXE / WCAG AA).
- **Adoptions (replace bespoke impls):**
  - Launcher command block → `copy-button` (drop `copyLeanLauncherCommand()` +
    `leanLauncherCopied` from `lean-engine.component.ts`).
  - `quantconnect-reference-code` header + provenance SHA-256 values.
  - Run-id chip in `run-report.component.html`.
  - `lean-engine-docs` copy blocks (`copyToClipboard`/`copiedKey` → the shared
    component).

## State & data flow

No new data sources. The nav consumes/relays existing `lean-engine.component`
signals: `engine`, `rangeState`/`resolution`, `selectedStrategyName` +
`paramValues`/`paramEntries`, `fillMode`/`initialCash`/`commissionPerOrder`,
and the LEAN template/launcher signals. `completedRunId()` drives the
setup→results transition and the auto-collapse. `strategy-detail-tab` continues
to receive `StrategyInfo` and emit `closed`/`configure`.

## Accessibility

- Config-nav accordion headers are `<button>`s with `aria-expanded`; the
  icon-rail collapse toggle has an `aria-label` reflecting state.
- Copy button: always-focusable, `aria-label` present even when the icon is
  visually hidden; copied state announced via `aria-live="polite"`.
- Charts keep their existing empty-state text; the stacked layout adds no
  keyboard traps.
- Must pass AXE and WCAG AA (color contrast on summary lines uses `$text-subtle`
  / `$text-secondary`, both AA at their sizes per existing tokens).

## Testing

- **`engine-config-nav`** (`.component.spec.ts`): renders sections; a configured
  section collapses to its summary string; the icon-rail toggle hides section
  bodies; auto-collapse fires when a `completedRunId` input flips from null.
  Assert rendered output (Angular Testing Library), not private signals.
- **`copy-button`**: click writes to a mocked `navigator.clipboard` and swaps to
  the copied label; clipboard-unavailable renders the fallback; button exposes
  an accessible name.
- **`engine-chart`**: `layout="stacked"` renders both containers (no
  `display:none`) and hides the tab switcher.
- **`strategy-detail-tab`**: shows the algorithm pseudocode and the Strategy
  Validation deep-link with the correct `strategy` query param.
- Existing `engine-results` / `run-report` specs updated for the full-width
  stack (no `.results-split`).

## Risks & watch-outs

- **R1 — data-policy fact loss.** The dropped `validation-evidence-card` is the
  only pre-run surface stating the **bar-consolidation** data policy (e.g.
  minute → 15m for EMA crossover). Preserve it as a one-line note in the setup
  placeholder (`validation-stage-placeholder`) and rely on the run report's
  existing `barsNotice` post-run. This is a strict-equivalence detail
  (numerical-rigor) and must not silently vanish.
- **R2 — chart resize.** lightweight-charts must recompute width when the nav
  collapses/expands and when switching to the stacked layout. Verify the resize
  path; add a `ResizeObserver` on the chart container if the current width sync
  is tab-switch-only.
- **R3 — "configured?" heuristics.** Defining when a section counts as
  configured (esp. Strategy params with optional fields) needs care so sections
  don't collapse prematurely or refuse to collapse. Start conservative: a
  section is configured when its required inputs are valid and non-default.
- **R4 — component extraction churn.** Moving inputs into `engine-config-nav`
  touches a large template; keep the diff mechanical (move, don't rewrite) and
  preserve `data-testid`s (`engine-select`, `run-btn`) so existing e2e/specs
  stay green.

## Out of scope

- No new top-level tabs or modals; History and the run-dock are untouched.
- No inline LEAN Python in the Lab.
- No backend, GraphQL, or Python changes.

## Files

**New**
- `components/lean-engine/config-section/config-section.component.{ts,html,scss,spec.ts}`
- `shared/copy-button/copy-button.component.{ts,html,scss,spec.ts}`

**Deleted**
- `components/lean-engine/validation-evidence-card/*` (orphaned once dropped
  from the Lab).

**Edited**
- `components/lean-engine/lean-engine.component.{html,scss}` — Workbench
  restructure; mount the config nav; delete the right rail + top strip.
- `components/lean-engine/engine-results/engine-results.component.{html,scss}` —
  full-width stacked charts; readiness → drawer.
- `components/lean-engine/engine-chart/engine-chart.component.{ts,html,scss}` —
  `layout` input; stacked rendering; resize on width change.
- `components/lean-engine/strategy-detail-tab/strategy-detail-tab.component.{html,scss}` —
  algorithm pseudocode + Strategy Validation deep-link.
- `components/engine-lab/run-report/run-report.component.html` — run-id chip
  uses `copy-button`.
- `components/lean-engine/lean-engine-docs/*` — copy blocks → `copy-button`.
- `components/strategy-validation/quantconnect-reference-code/*` — copy header +
  SHA values → `copy-button`.
- `components/lean-engine/validation-stage-placeholder/*` — new `dataPolicyNote`
  input preserves the bar-consolidation fact (R1) on the pre-run stage.
