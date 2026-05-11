# Claude Design — Indicator picker prototype (2026-05-10)

> **Status:** Active — pass this prompt into Claude Design (`frontend-design` skill) to produce a standalone HTML/CSS prototype of the new indicator picker.
> Companion plan: `.claude/plans/i-would-like-to-logical-dijkstra.md` (Phase 3).
> After delivery: Claude Code ports the prototype into a typed Angular standalone component at `Frontend/src/app/shared/indicator-picker/`.

## Project context

- Angular 21, zoneless, standalone, `ChangeDetectionStrategy.OnPush`, Signals.
- Frontend stack: PrimeNG + PrimeIcons 7, custom SCSS using design tokens in `Frontend/src/app/styles/_tokens.scss`. Tailwind v4 is available globally but pages don't lean on it heavily.
- The picker will live in the right rail of a 3-column IDE layout primitive (`.ide-grid` / `.ide-rail-right`) that's already landed in `Frontend/src/styles.scss`. The rail is **~360px wide** at the 3-col breakpoint and reflows under main at 2-col. **Do not redesign the rail or the page.** Design only the picker.
- Hosted on three pages: Data Lab (`/data-lab`), Feature Runner (`/research-lab/feature-runner`), Signal Runner (`/research-lab/signal-runner`).

## The design task

Produce a standalone HTML/CSS prototype of a multi-instance technical indicator picker. The picker will be reimplemented as an Angular 21 standalone component and mounted in the right rail of the three pages listed above. You are designing **only the picker** — not the page layout, not the rail container, not any surrounding chrome.

### What the picker does

Users select one or more technical indicators (RSI, MACD, Bollinger Bands, EMA, ATR, etc.) to compute over a time series. Some indicators support multiple instances with different parameters (e.g. an EMA ribbon = EMA(5), EMA(10), EMA(20), EMA(50) — four instances of the same indicator). The catalog has ~80 entries grouped into 4 categories: trend, momentum, volatility, volume.

### The data contract (consume, don't redefine)

```ts
interface IndicatorParamConfig {
  name: string;            // e.g. "length"
  type: 'int' | 'float';
  default: number;
  min: number;
  max: number;
  description: string;
}

interface IndicatorInfo {
  name: string;            // "rsi", "macd", "bbands"
  category: string;        // "trend" | "momentum" | "volatility" | "volume"
  description: string;
  configurable_params: IndicatorParamConfig[];
}

interface IndicatorPreset {
  name: string;
  instances: Array<{ indicator: string; params: Record<string, number> }>;
}
```

**Inputs:** `indicators: IndicatorInfo[]`, `activeKeys: string[]`, `presets: IndicatorPreset[]`
**Outputs:** `(add)`, `(addInstance)`, `(preview: { name: string; active: boolean })`

### What's new vs. the old picker

1. **A FIFTH facet alongside the 4 categories: "Overlay vs. Sub-panel"** — toggle pills that filter the visible set.
   - **Overlay** = draws on the price chart (EMA, BBands, VWAP).
   - **Sub-panel** = its own pane below (RSI, MACD, ATR).
   - Treat this as orthogonal to category (i.e. user can have both an Overlay filter and a Category filter active).

2. **Hover preview.** When the user hovers an indicator name for >300ms, show an inline mini-chart preview (≈60px tall) of how the indicator looks on a sample series. Use a static sine-wave-with-noise sample for the prototype. The Angular component will swap in real data later. Preview disappears on mouse-out.

3. **Presets.** Above the category list, surface 4–6 named presets that add multiple instances at once. Clicking a preset emits one `addInstance` event per instance. Initial set:
   - "EMA ribbon 5/10/20/50"
   - "Bollinger triple (10, 20, 50)"
   - "RSI + MACD combo"
   - "ATR + ADX trend strength"

### What NOT to design

- **Page layout.** The IDE shell (left rail / main / right rail) is owned by Claude Code. The picker drops into a ~360px-wide right-rail column with vertical scroll.
- **Inline parameter editing.** Parameters live in a separate modal (already exists). The picker only browses + adds.
- **Fuzzy search, keyboard nav, recents, favorites.** Out of scope for this revision.
- **Any data-fetching, persistence, or service code.** Pure HTML/CSS only.

### Constraints

- **Dark theme.** Use CSS custom properties (`--bg-surface`, `--bg-elevated`, `--text-primary`, `--text-secondary`, `--text-muted`, `--accent`, `--border`, `--bull`, `--bear`) so tokens drop in. Token reference: `Frontend/src/app/styles/_tokens.scss`.
- **Portable to a typed Angular component**: classes scoped under a single root selector, interactive state expressible as a single component signal, no JS frameworks in the prototype.
- **Width**: optimize for 320–400px. Stack vertically. Use disclosure widgets (`<details>` or equivalent) for category sections so the rail doesn't scroll endlessly.
- **Output**: one `.html` file with all CSS in a `<style>` block, plus mock `IndicatorInfo[]` data and the presets array as JS consts for the demo.

## Deliverable

A single self-contained `.html` file showing the picker in its empty state, its filtered state (with one facet selected), and its hover-preview state. Save to:

`docs/design/indicator-picker-prototype-2026-05-10.html`

## After delivery (Claude Code picks up)

1. Read the prototype.
2. Create `Frontend/src/app/shared/indicator-picker/indicator-picker.component.{ts,html,scss}`. Standalone, OnPush, container-query-aware.
3. Reuse `IndicatorCatalogService` (already loads the `IndicatorInfo[]` from `/api/dataset/available`).
4. Define `INDICATOR_PRESETS` and the Overlay/Sub-panel name map (a static record keyed by indicator name) as module-level consts in a sibling file.
5. Wire `(add)`, `(addInstance)`, `(preview)` outputs.
6. Vitest spec: render with stub catalog, facet pills filter the visible set, hover after 300ms emits `preview`, preset click emits batch `addInstance`.
7. Mount in `/_ide-sandbox` for visual verification before wiring into Data Lab / Feature Runner / Signal Runner (Phase 4 in the plan).
