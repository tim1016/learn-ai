> **Status:** Archived — prompt snippet, not a design doc.
> **Do not use as implementation authority.**
> Current authority: `.claude/rules/angular.md` (Angular/UI conventions).
> Archived because: verbatim LLM prompt for a one-off dark-theme design session; no ongoing decision authority.

# Claude Design — Quant/Lab dark theme, follow-up pass

This prompt picks up where a prior session left off. Read the **"What's already done"** section before proposing changes — it constrains the work and lists the tokens/components you should extend rather than re-invent.

## Project context

- Angular 21, zoneless, standalone components, `ChangeDetectionStrategy.OnPush`, Signals, SCSS only.
- Primary working directory: `/home/inkant/Documents/learn-ai/Frontend/`.
- Design tokens live in `src/app/styles/_tokens.scss` and are emitted both as SCSS vars and CSS custom properties.
- UI components: PrimeNG (tabs, buttons, accordion, divider, tooltip). Icons: PrimeIcons 7.
- **Do not introduce Tailwind utilities** into component templates. The repo has Tailwind installed but the convention is SCSS + tokens.
- **Every component must follow** `/home/inkant/Documents/learn-ai/.claude/rules/angular.md` (signals, `input()`/`output()` functions, `@if`/`@for`, no `*ngIf`, no `ngClass`/`ngStyle`, no `standalone: true` attribute — it's the default).

## What's already done (do not redo)

1. **Design tokens extended** in `src/app/styles/_tokens.scss`:
   - Text: `--text-primary`, `--text-secondary`, `--text-subtle` (NEW, 4.6:1 AA on `#0f1117`), `--text-muted`.
   - Page shell: `--page-pad-x`, `--page-pad-y`, `--sidebar-w`.
   - Badge variants: `--badge-{neutral,warn,info}-{bg,border,text}` (outline-style, low visual weight).
   - Tab three-state: `--tab-fg-{active,inactive,disabled}`, `--tab-accent`.
2. **Sidebar rebuilt**: `src/app/shell/app-sidebar.component.{ts,scss}`.
   - Inline SCSS `styles: [...]` (silently dead because `inlineStyleLanguage` wasn't set) replaced with an external `.scss` file.
   - Two-tier hierarchy, 3px active accent bar + bg, outline "deprecated" badge, pinned footer with top border, subtle separators, overflow-clipped.
   - New NAV group **Documentation** consolidates `/strategy-docs`, `/indicator-docs`, `/data-lab-docs`, `/data-quality-docs`, `/docs/indicator-reliability-methodology`.
   - Icon swaps to uniform outlines: Options `pi-sliders-h`, Research `pi-compass`, Portfolio `pi-briefcase`, Documentation `pi-book`.
3. **Shared page header component**: `src/app/shared/page-header/page-header.component.{ts,scss}`.
   - `<app-page-header [title] [subtitle] [eyebrow]>` with `[slot=actions]` content projection.
   - Subtitle uses `--text-subtle` for WCAG AA. Replaces hand-rolled `.page-header` blocks.
   - **Only the Engine Lab page has migrated to it so far.** Rollout pending — see Task 1 below.
4. **Engine Lab (`/engine`) alignment fixed**: the double-padded `:host { padding: 1.5rem 2rem }` was deleted; `.main` in `app.component.ts` now uses `padding: var(--page-pad-y) var(--page-pad-x)`.
5. **Redundant in-page docs nav removed** from `lean-engine-docs.component.html` (the two floating "Engine Lab | Strategy Docs" links — docs are reached via the sidebar Documentation group now).

## Constraints

- `--text-muted` is still used by many components for *labels* and *captions* (lower-visual-weight text where AA-large is acceptable). **Do not globally swap `--text-muted` → `--text-subtle`**. Only lift contrast on *body-sized* text (subtitles, metadata, helper text that a user is meant to read).
- `.nav-link` class has a specific meaning in `app-sidebar.component.scss`. If you need another nav-link-like element elsewhere, name it differently.
- Every change must pass `npx eslint Frontend/src/ --max-warnings 0` and `npx tsc --noEmit`.
- Accessibility: AXE must pass, WCAG AA minimums for focus states, color contrast, ARIA labels.

## Work to do — prioritized

### Task 1 — Migrate all route components to `<app-page-header>` (highest value)

The shared component exists but only the Engine Lab page uses it. Roll it out. Files that currently have their own hand-rolled `<header class="page-header"><h1>…</h1><p class="subtitle">…</p></header>`:

- `components/data-lab/data-lab.component.html` + `.scss`
- `components/data-lab/data-lab-docs/data-lab-docs.component.html` + `.scss`
- `components/data-quality/data-quality.component.html` + `.scss`
- `components/data-quality/data-quality-docs/data-quality-docs.component.html` + `.scss`
- `components/indicator-validation/indicator-docs/indicator-docs.component.html` + `.scss`
- `components/indicator-validation/indicator-report/indicator-report.component.html` + `.scss`
- `components/strategy-lab/strategy-lab.component.html` + `.scss`
- `components/strategy-lab/strategy-docs/strategy-docs.component.html` + `.scss`
- `components/options-strategy-lab/options-strategy-lab.component.html` + `.scss`
- `components/lean-engine/lean-engine-docs/lean-engine-docs.component.html` (keep local — subtitle contains `<code>` inline HTML, so just swap `$text-muted` → `$text-subtle` on the `.subtitle` rule — **already done**, but verify).

For each: (a) import `PageHeaderComponent`, (b) replace the `<header>` markup with `<app-page-header title="…" subtitle="…" />`, (c) delete the matching local `.page-header { h1, .subtitle { … } }` rules from the component's SCSS.

### Task 2 — Unify route-component `:host` padding app-wide

Over 30 route-level components set their own `:host { padding: … }` with inconsistent values. With the new `.main` in `app.component.ts` providing `var(--page-pad-y) var(--page-pad-x)`, most per-route paddings stack redundantly (double inset) or fight the app shell.

Per component, decide:
- **Top-level route component** (loaded directly by the router in `app.routes.ts`): remove `:host { padding }`, rely on `.main`.
- **Sub-route component** nested inside another page (e.g. `expiration-ribbon`, `benchmark-scorecard`, `data-lab-chart`): keep its own padding — it's not a route, it's a widget.

Start with the 11 files listed in Task 1 (those are all top-level routes). Then audit the remaining `:host { padding }` blocks under `components/**`.

### Task 3 — Global PrimeNG tab override

`<p-tabs>` is used in three route components (`lean-engine`, `portfolio`, `data-lab-docs-card`). Each has its own `::ng-deep .p-tabs { … }` block. The deep overrides duplicate logic and diverge slightly.

Move the PrimeNG tab theming into a **single global** override in `src/styles.css` (or a new `src/styles/_prime-overrides.scss`). Use the three-state tokens:

```css
--tab-fg-active:    var(--text-primary);
--tab-fg-inactive:  var(--text-subtle);
--tab-fg-disabled:  var(--text-muted);
--tab-accent:       var(--accent);
```

Spec:
- Uniform icon size (16px), gap between icon and label (6px), padding (12px vertical / 16px horizontal).
- Active: `color: var(--tab-fg-active)`, `border-bottom: 2px solid var(--tab-accent)`.
- Inactive: `color: var(--tab-fg-inactive)`, no underline, hover lifts to `var(--text-primary)`.
- Disabled: `color: var(--tab-fg-disabled)`, `opacity: 0.6`, `cursor: not-allowed`.

After the global override lands, delete the per-component `::ng-deep .p-tabs { … }` blocks.

### Task 4 — Shared badge / chip component

Today the app has ad-hoc badge markup (the sidebar "deprecated" outline, `.tag` in `styles.css`, `.source-chip` in engine, status chips in availability card, etc.) with mismatched colors, radii, and sizes.

Build `src/app/shared/badge/badge.component.ts`:

```ts
<app-badge variant="neutral|warn|info|success|danger" size="sm|md">
  {{ label }}
</app-badge>
```

Variants should consume the existing `--badge-{variant}-{bg,border,text}` tokens (add `success` and `danger` variants using `--bull` / `--bear` with low-alpha). Default size `sm` is text-xs uppercase outline style. Migrate at least the sidebar's `.nav-link-badge`, engine's `.source-chip`, and the `tag` class in `styles.css` to this component.

### Task 5 — Spacing scale audit

Tokens already define `--space-1` … `--space-8` (4px grid). Component SCSS mostly uses raw `rem`/`px` values (`1.25rem`, `0.875rem`, `16px`, etc.). Audit the 30+ component SCSS files and replace raw spacing with tokens where the value maps cleanly (`0.25rem` → `var(--space-1)`, `0.5rem` → `var(--space-2)`, etc.). **Do not force-fit** off-scale values (`0.375rem`, `0.625rem`) — if they're load-bearing, flag them in a follow-up note for redesign rather than rounding.

### Task 6 — Background-layer audit (three tiers)

Tokens define three surface layers: `--bg-canvas` (page), `--bg-surface` (card), `--bg-elevated` (hover / secondary elevated), `--bg-hover` (interactive hover). Many components use these correctly; some use raw hex or arbitrary rgba. Audit for consistency, especially:
- Options chain viewer (`options-chain-v2/`)
- Strategy builder
- Engine results hero cards (`engine-results/`)

Goal: every visible surface maps to one of the four layer tokens.

### Task 7 — Visual regression harness (scope this, don't necessarily build it)

Propose (don't build) a lightweight visual-regression setup: Storybook-lite or Playwright page-snapshot tests for the sidebar, page header, tab bar, badges, and each route's above-the-fold view. This is a scope/approach proposal, not a deliverable — pick one tool, justify the pick, outline the first 5 stories / snapshots, and note CI implications.

## Out of scope (explicitly)

- Don't touch backtest engine logic, chart components, or data services.
- Don't rename routes or add/remove pages.
- Don't introduce Tailwind, ng-zorro, or any other UI library.
- Don't swap PrimeIcons for Lucide or Heroicons in this pass (possible future work, but out of scope now — PrimeIcons is the house set).

## Deliverable format

For each task, produce:
1. A summary of what changed, file by file.
2. Screenshots or descriptions of before/after for the visible surface.
3. A short note in the PR description about any tolerance/override added.

Respect the scientific-rigor ethos of this repo: no cleanups that drift behavior, minimal surface area per change, and no backwards-compat shims.
