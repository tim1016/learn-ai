# Claude Design — Data Lab follow-up pass (2026-04-24)

This prompt covers a focused redesign of the **Data Lab** page (`/data-lab`) and a consolidation of indicator documentation across the app. Read the **"What's already done"** section first — it lists what just landed and constrains what to revisit. Areas marked **"Open question for design"** are explicit asks.

## Project context

- Angular 21, zoneless, standalone, `ChangeDetectionStrategy.OnPush`, Signals, SCSS only.
- Primary working directory: `Frontend/`.
- Design tokens: `src/app/styles/_tokens.scss`. UI: PrimeNG + PrimeIcons 7.
- No Tailwind in templates. `.claude/rules/angular.md` is binding.

## What's already done (this session)

### 1. Indicator metadata consolidated into a single source of truth

- **New** `Frontend/src/app/shared/indicators/indicator-reference.ts` — canonical UI-facing indicator metadata for all 33 indicators.
  - Exports `INDICATOR_REFERENCE` (record), `INDICATOR_REFERENCE_LIST` (frozen array), `getIndicatorReference(key)`, and `CATEGORY_META` (color + label per `IndicatorCategory`).
  - Each entry carries: `key`, `displayName`, `category` (`trend|momentum|volatility|volume`), `panelType`, `formulaLatex`, `library`, `outputColumns`, `defaultParams`, `params[]` (mirrors PythonDataService `INDICATOR_CONFIGS`), `description`, narrative (`quickWhy`/`quickAnalogy`/`quickImpact`), `interpretation[]`, `recommendedTimeframes`, `dataNotes[]`, `timeframeBehavior`, `relatedIndicators[]`, `professionalRef`, optional `checkQuestion`/`checkAnswer`.
- **Deleted** the redundant page `components/indicator-validation/indicator-docs/` (route `/indicator-docs` now redirects to `/data-lab-docs`).
- **Deleted** stale planning files: `DATA_LAB_INDICATORS_FULL.md`, `INDICATORS.md`, `indicator-planning.md`.
- **Deleted** `Frontend/src/app/shared/indicator-docs.ts` — its `INDICATOR_QUICK_INFO` shape is now re-exported from the consolidated reference for backward-compat (consumers should migrate to `getIndicatorReference()` over time).
- **Rewired**:
  - `shared/indicator-tooltip` reads from the consolidated reference.
  - `data-lab-docs` no longer carries an inline 900-line indicator array; it consumes `INDICATOR_REFERENCE_LIST`.
  - Sidebar Documentation group: removed duplicate "Indicator Docs" entry; "Indicator Reference" → `/data-lab-docs` is the single canonical link.
  - `indicator-validation` page header link: "Calculation Reference" → "Indicator Reference" (now points to `/data-lab-docs`).

**Layering note carried in the file's header comment:** per CLAUDE.md rule 5 ("Python owns all math"), this TS file is the **UI documentation** truth. The **calculation truth** still lives in `PythonDataService` (pandas-ta), and the **param contract truth** (validated bounds) is `INDICATOR_CONFIGS` in `dataset_service.py`. The TS `params[]` array mirrors INDICATOR_CONFIGS for offline rendering of defaults/bounds. They must be kept in sync by hand — a CI parity test was explicitly declined.

### 2. Active Indicators redesigned

Old: bare `name` + inline number inputs in a tight grid.

New: two-component split.

- `components/data-lab/active-indicator-card/` — presentational card with:
  - 4px **left color rail** colored per `CATEGORY_META[category].color`.
  - Full display name (e.g. "Exponential Moving Average (EMA)") instead of the bare key.
  - Category chip (color = rail) + Panel chip ("Overlay"/"Sub-panel") + per-param chips (`length=14`, `std=2.0`).
  - Reset / Configure / Remove icon buttons.
  - Whole card is the click target → opens the modal. Keyboard-focusable, `role="button"`.
  - Preserves the existing per-entry timeframe-warning icon.
- `components/data-lab/indicator-config-modal/` — single shared `<p-dialog>` instance:
  - Header with category chip + panel + recommended timeframes.
  - **Why use it** (with analogy + impact). **Parameters** with min/max/step pulled from backend `ParamConfig`, descriptions inline, "Reset to defaults" action. **Formula** (KaTeX, displayMode). **How to read it** (interpretation bullets). **Caveats** (timeframe-behavior callout + dataNotes). **Related indicators** as add-to-active chips. **Provenance** footer.
  - Click-to-open (not hover-to-open) — pushed back on hover-modals as a usability antipattern; hover stays as the lightweight overlay tooltip.
- The old "Active Entries" grid in `data-lab.component.html:616-665` was replaced; the modal is mounted once at the end of the template, driven by a `configuringIndex: signal<number|null>` and three event handlers (`onModalParamChange`, `onModalReset`, `addInstanceByName`).

### 3. Options Companion contract estimate

- New computed signals on `DataLabComponent`: `optionsContractEstimate()` returns `{ contracts, expiries, bars }` based on date range × strikes-each-side × calls/puts × expiry mode × ticker daily-vs-weekly. `optionsContractEstimateSeverity()` classifies into `ok | warn | danger` at thresholds `< 5,000 | 5,000–20,000 | ≥ 20,000` contracts.
- Renders below the "Include options companion files" checkbox as a colored callout: `~ 312 contracts across 26 expiries · ~ 121,680 option bars (ATM ± 5 · calls + puts)`.
- Helpers `countWeekdays(from, to)` and `barsPerTradingDay(timespan, multiplier)` added at the top of `data-lab.component.ts` — holiday calendar deliberately ignored (~5% overcount, well within the precision of "rough estimate" decisions).

### 4. Bar timeframe default

- Verified the Bar timeframe selector already defaults to **1 min** via `timespan='minute' + multiplier=1` resolving through `activeBarTimeframe()` to the `'1m'` option. No code change required.

## Constraints

- `npx tsc --noEmit` passes (verified). `npx eslint src/` adds **0 errors / 0 new warnings** in changed files. Pre-existing warnings in `data-lab.component.ts/.html` and `quality-modal.component.html` are unchanged and out of scope here.
- Frontend tests not run — Vitest container wasn't up locally.
- The card and modal are dark-theme only; no light-mode variant (the rest of Data Lab is dark-only too).
- The card colors hard-code hex values for category rail/chip (`#3b82f6`/`#a855f7`/`#f59e0b`/`#10b981`) — these were chosen to match common trading-app conventions but **were not run through `_tokens.scss`**.

## Open questions for design

1. **Category palette**. The four hex values above (trend = blue, momentum = purple, volatility = amber, volume = green) are placeholders. Should they:
   - move into `_tokens.scss` as `--ind-cat-{trend,momentum,volatility,volume}` and resolve through CSS custom properties?
   - be re-mapped to existing semantic tokens (e.g. `--accent-blue`, `--accent-purple`)?
   - be tuned for AA contrast against `--bg-elevated` (currently the chip text uses the rail color directly — fine on dark backgrounds but unverified for AAA)?
2. **Card density**. Each card currently shows up to ~5 chips on one wrap line: category, panel, and `n` param chips. With 8 EMAs in the default setup the grid can feel busy. Worth either: (a) hiding the panel chip (least informative); (b) collapsing identical-named indicators into a "ribbon" group; (c) leaving as-is and trusting the `entries-grid` to wrap nicely. Currently (c).
3. **Modal vs side-panel**. A dialog interrupts flow when the user is iterating on parameters and watching the chart below it. A right-anchored slide-out panel ("inspector") might be a better fit for repeated tweaks. Out of scope for this round; would touch:
   - `indicator-config-modal/` (rename + restructure as inspector)
   - `data-lab.component.scss` (reserve right-side gutter)
   - Trade-off: chart real-estate vs context preservation.
4. **Hover preview vs click**. Right now hover triggers the existing 340 px shared `<app-indicator-tooltip>` on the *catalog* (bottom of page) entries. Click triggers the new modal on *active* entries. Cohesion-wise, should the catalog entries also gain click-to-modal so the catalog → activate flow lets users "preview" without committing? Pure UX question — no code is presupposed.
5. **Estimate threshold tuning**. The 5k/20k contract thresholds were declared "ok" by the user. They're conservative for short windows on weekly-only tickers and aggressive for daily-expiry tickers (SPY, QQQ) over multi-month windows where 20k is easy to exceed. Worth re-tuning per-ticker class? Currently global.
6. **"Related indicators" affordance**. The modal renders related indicators as `[+ EMA] [+ DEMA]` chips that immediately add a new active instance with defaults. Should these instead show a confirmation, a parameter pre-fill mini-form, or just navigate the modal to the related indicator? Currently: instant-add + stays in the modal.

## What I'd like a fresh pair of eyes on

- Whether the **Active Indicators section header** ("Active Indicators") is doing enough work — most pages on Data Lab have unstyled `h3` headers and the section now feels heavier than its title.
- Card hover/focus state — currently a flat `bg-hover` change; design might want a more pronounced affordance signaling "this whole card is clickable."
- Whether the modal should pre-populate params **from the current entry's values** (it does) versus from `INDICATOR_REFERENCE.params[i].default` (it doesn't, until the user clicks Reset). The current behavior is correct, but design might want the modal to surface the *delta* from defaults more visibly.
- The `options-estimate` callout — it's clean but visually competes with the `optionsResolutionWarning` info-callout right below it. Could merge or stack-rank for stronger hierarchy.

## Files touched in this session

```
Frontend/src/app/shared/indicators/indicator-reference.ts   (new, ~960 lines)
Frontend/src/app/components/data-lab/active-indicator-card/  (new)
Frontend/src/app/components/data-lab/indicator-config-modal/ (new)
Frontend/src/app/components/data-lab/data-lab.component.{ts,html,scss}
Frontend/src/app/components/data-lab/data-lab-docs/data-lab-docs.component.ts (-905 lines)
Frontend/src/app/shared/indicator-tooltip/indicator-tooltip.component.ts
Frontend/src/app/shell/app-sidebar.component.ts
Frontend/src/app/app.routes.ts
Frontend/src/app/components/indicator-validation/indicator-validation.component.html

Deleted:
  Frontend/src/app/shared/indicator-docs.ts
  Frontend/src/app/components/indicator-validation/indicator-docs/
  Frontend/src/app/components/data-lab/{DATA_LAB_INDICATORS_FULL,INDICATORS,indicator-planning}.md
```

---

# Round 2 — Acted on the design brief (same date)

All ten of the brief's items landed in this codebase. Notes on each:

### §1 — Category palette → tokens
- New tokens in `_tokens.scss`: `--ind-cat-{trend,momentum,volatility,volume}` and `*-soft` variants, hex values matched to the brief (`#4d8dff`, `#a78bfa`, `#f2ad3d`, `#26a69a`).
- `CATEGORY_META` now exports CSS-var refs (`var(--ind-cat-trend)`, `var(--ind-cat-trend-soft)`) — the four hex strings no longer survive a grep of components/. Re-tunable design-only.

### §2a — Drop panel chip from cards
Done. Chip removed; panel still appears in the modal header. Visual diff: each card lost its "Overlay"/"Sub-panel" chip.

### §2b — Numbered ribbon group (≥ 4)
- New `active-indicator-group/` component. Used when ≥ 4 consecutive entries share an indicator key (after sort).
- Group card has rail + display name + count badge + category chip + per-pill clickable summary + hover-revealed × on each pill + right-aligned `[+ Add]` + group-level Reset/Remove.
- The default 8-EMA setup now collapses to a single group card.

### §3 — Inspector pin mode
- Pin button in the dialog header (`pi pi-thumbtack` ↔ `pi pi-arrow-right`). Persisted to `localStorage('data-lab.indicator-config.mode')`.
- When pinned: PrimeNG dialog renders right-anchored, 360 px wide, full-height, no overlay, no dismiss-on-mask. The page reserves a right gutter via a `--inspector-w` CSS custom property the modal sets on `:root`. `.data-lab` consumes it via `padding-right: calc(1rem + var(--inspector-w, 0px))`.
- Esc still closes from either mode.

### §4 — Catalog preview mode
- Each catalog item now has an `ⓘ Preview` icon button next to the existing `+` button.
- Click opens the modal in `mode="preview"` with a synthetic entry seeded from `INDICATOR_CONFIGS` defaults. Param state is local to the modal until the user commits.
- Footer in preview: `[Cancel] [+ Add to active with these params]`. The `Add to active` button emits an `addPreview({key, params})` event; the parent calls `entries.update(...)` with the user's chosen params.
- Header has a `Preview` chip in `--accent` color so the user always knows they're not editing an active entry.

### §5 — Per-ticker class thresholds
`optionsContractEstimateSeverity` now branches: daily-expiry tickers (SPY/QQQ/IWM/…) use `< 2k / 2k–8k / ≥ 8k`; weekly-only use `< 5k / 5k–20k / ≥ 20k`. Uses the existing `DAILY_EXPIRY_TICKERS` set; no backend dependency.

### §6 — Related indicator undo affordance
- Modal tracks per-key 3-second timeouts in a local `pendingUndo: signal<Set<string>>`.
- Tri-state chip via `relatedChipState(key)`:
  - `idle` → `[+ EMA]` (default, accent-soft)
  - `pending` → `[✓ Added · undo]` (`--bull-soft`)
  - `active` → `[✕ Remove EMA]` (`--bear-soft`)
- Click during `pending` removes the just-added entry. After 3 s, returns to `idle`. `active` state is computed from the parent's `activeIndicatorKeys` input.
- Modal does not navigate when a related chip is clicked.

### §A — Active Indicators sub-toolbar
- Eyebrow header `ACTIVE INDICATORS · {count}` with mono-styled count, `--text-subtle`, uppercase, 0.06em letter-spacing, 1 px border-bottom.
- Right side: `Sort` dropdown (Order added / Category / Name A→Z) + `[+ Add indicator]` outline button (in `--accent`).
- Sort drives a display-only `sortedEntriesView` computed; underlying `entries()` order is preserved so removeEntry/openConfigure indices stay correct.
- `[+ Add indicator]` scrolls to the catalog (`#data-lab-indicator-catalog`). The full focused-search popover is **not** in this round — see "Deferred / open" below.

### §B — Card hover/focus + chevron
- Card hover: 1 px `--accent` border, `translateY(-1px)`, soft shadow, chevron brightens to `--accent` and slides 1 px right. Focus-visible: 2 px outline `--accent` with offset (replaces hover ring). Active: settles back to baseline.
- `pi pi-arrow-right` chevron added to the right-side actions row (kept inline with the action buttons rather than absolutely positioned to avoid overlap).
- Internal action buttons stop-propagate clicks so the card's "open modal" gesture doesn't fire.

### §C — Delta-from-defaults
- Card: 4 px `--warn` rail dot when any param diverged. Param chips render `length=21*` with the asterisk in `--warn` and the chip border lifted to `--warn`. Reset button on the card is disabled when no params have diverged.
- Modal: Reset button shows count (`Reset to defaults (3 changes)`) and is disabled when nothing has diverged. Each diverged param row gets a `↺` button positioned to the right of the input that resets that single param to its default.

### §D — Merge companion-plan callouts
- Single `.companion-plan` card replaces the previous three stacked callouts.
- Layout: eyebrow row (`COMPANION PLAN · {OK|WARN|DANGER}`), 2-column `<dl>` rows for Contracts / Option bars / Resolution / Expiry coverage (rows omitted when not applicable), and a footer with `[− Strikes / + Strikes]` stepper (always visible in warn/danger) and `[Switch to weekly only]` (only for daily-expiry tickers in warn/danger; flips expiry mode to `nearest_within_days` with `maxDte=7`).
- Severity tints route to existing `--info-soft` / `--warn-soft` / `--bear-soft` — no new tokens needed.

---

## Cross-cutting

- `INDICATOR_QUICK_INFO` now carries `@deprecated` JSDoc pointing consumers to `INDICATOR_REFERENCE` / `getIndicatorReference`.
- `tsc --noEmit` passes. `eslint` adds 0 new errors. Pre-existing warnings in `data-lab.component.ts/.html` and `quality-modal.component.html` are unchanged.
- Visual-regression coverage was not added — flagged for a follow-up if Storybook is wired in.

## Deferred / open

These were called out in the brief but consciously not implemented in this round; they're isolated and can ship as separate PRs:

1. **§A — Focused-search popover** for the `[+ Add indicator]` button. Currently the button scrolls to the existing catalog. The popover would need an inline search input + filtered list anchored to the button — out of scope for the layout pass, in scope for a future round.
2. **§3 — Inspector mode persists across active-card clicks.** Currently the dialog hides on Esc/close from either mode. Persisting the inspector "open" while the user clicks different cards is a follow-up; needs the parent to track "inspector-mode-active" state separately from the modal's own `visible` model.
3. **Sort persistence to localStorage.** §A's sort dropdown resets on page reload.
4. **Cross-cutting note 3 from the brief — Storybook coverage.** None of the new states (default/modified/hover/focus card; modal/inspector/preview modes) are snapshotted.
5. **Open follow-up 1 from the brief — Catalog visual pass.** The catalog at the bottom of Data Lab is now visually heavier than the active section it feeds. Reduction of padding + emphasis on the `Add` affordance is a follow-up.
6. **Open follow-up 4 from the brief — Inline timeframe-mismatch notice** at the section header level rather than per-card.

## Files added / changed (round 2)

```
Added:
  Frontend/src/app/components/data-lab/active-indicator-group/  (new sub-component for ribbon group)

Modified:
  Frontend/src/app/styles/_tokens.scss                                   (+18 lines: ind-cat tokens)
  Frontend/src/app/shared/indicators/indicator-reference.ts              (CATEGORY_META → var refs; @deprecated INDICATOR_QUICK_INFO)
  Frontend/src/app/components/data-lab/active-indicator-card/*           (drop panel chip, hover/focus, chevron, modified marker)
  Frontend/src/app/components/data-lab/indicator-config-modal/*          (mode input, preview state, pin/inspector, related-undo)
  Frontend/src/app/components/data-lab/data-lab.component.{ts,html,scss} (sub-toolbar, sort, group view, preview wiring, companion-plan merge, per-ticker thresholds)
```

