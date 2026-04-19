# Quant Trading Lab — Design System

A dark-mode, data-dense design system for a **quantitative options research and backtesting workbench**. Built for traders, researchers, and quants who need to ingest market data, validate indicators, build and test strategies, manage portfolios, and run a LEAN-style backtest engine.

---

## Product context

**Quant Trading Lab** is an internal research platform made up of three services:

| Service | Stack | Role |
|---|---|---|
| **Frontend** | Angular 21 + PrimeNG (Aura dark preset) + Tailwind v4 + Apollo | The operator UI — the surface this design system is built from |
| **Backend** | .NET / C# + HotChocolate (GraphQL) + EF Core | Portfolio, validation, strategy execution, market data orchestration |
| **PythonDataService** | FastAPI + pandas + QuantLib | Polygon.io ingestion, options pricing, research/ML, LEAN-compatible backtest engine |

### Product surfaces

The Frontend app is a **single workbench** with five top-level sections:

1. **Stocks** — Market Data, Ticker Explorer, Technical Analysis, Stock Analysis, Strategy Lab, Strategy Validation, Indicator Validation, Data Lab
2. **Data Quality** — Pipeline quality analysis + docs
3. **Options** — Options Chain, Strategy Builder, Options Strategy Lab, Pricing Lab, Options History, Snapshots
4. **Engine** — "Lean Engine" backtester (LEAN-compatible output + TradingView parity diagnostics)
5. **Portfolio** — Dashboard, Positions, Risk Panel, Scenario Explorer, Strategy Attribution, Reconciliation, Validation
6. **Research Lab** — Feature runner, Signal runner, Batch runner, Signal/Feature reports, Robustness, Experiment history

### Source material

- **Codebase (primary source of truth):** `tim1016/learn-ai` on GitHub, `master` branch.
  - `Frontend/src/app/styles/_tokens.scss` — canonical design tokens
  - `Frontend/src/app/app.component.ts` — PrimeNG menubar, all navigation
  - `Frontend/src/app/components/*` — per-surface templates + SCSS
  - `Frontend/src/app/app.config.ts` — PrimeNG Aura dark preset, selector `.app-dark`
- **Icon font:** PrimeIcons (`primeicons@7`) — `pi pi-*` classes, loaded from CDN.
- **Component library:** PrimeNG 21 (Menubar, Table, Tabs, Drawer, DatePicker, Autocomplete, Tooltip, etc.).
- **Charts:** Chart.js + lightweight-charts (candlestick / financial).
- **Math rendering:** KaTeX (for options math docs, Black-Scholes, indicator formulas).

---

## Content fundamentals

The voice is **precise, technical, and operator-oriented** — this is a tool for people who already know what an ITM put or a Sharpe ratio is. No hand-holding, no emoji, no marketing polish. But every page has a collapsible "How to use this page" guide so new operators don't get lost.

**Tone:** matter-of-fact, terse, literate.

- **Sentence case** for headings. Never title case. Never all-caps *except* eyebrow labels (uppercase with 0.04–0.06em tracking).
- **Imperative voice** for actions: "Record Trade", "Take Snapshot", "Fetch Data", "Run Backtest".
- **Second person implicit** — labels address the operator but usually omit "you": "Type a ticker", "Keep the range short for minute data".
- **Parenthetical clarifications** are heavy. `VWAP = volume-weighted average price (institutional fair-value benchmark)`. `SMA Crossover (deprecated)`. `Data is 15-minute delayed (Polygon Starter plan)`.
- **No emoji.** Ever. Use PrimeIcons (`pi pi-chart-line`, `pi pi-shield`, `pi pi-wallet`).
- **Tickers are monospace + uppercase**: `AAPL`, `SPY`, `GLD`.
- **Numbers are tabular-nums**, aligned right in tables, formatted to fixed decimals (`$` + 2dp for prices, 3dp for Sharpe/Sortino, 2dp + `%` for return/drawdown, `1.0-0` for volume).
- **Inline code** for identifiers, params, column names: `bars_fetched`, `holidayId`, `ema_12`.
- **"Deprecated" / "v2" / "beta" tags** are explicit and preserved — legacy features are labeled, not hidden.

**Example phrasing (verbatim from the app):**

> *"Type a ticker (e.g. AAPL, MSFT, GLD) in the text box. Pick From and To dates. Keep the range short for minute data (1–5 days) or longer for daily data. Click Fetch Data. Results are cached in the database so the next fetch for the same range is instant."*

> *"Data Coverage: 94% — 17,320 / 18,400 expected bars — 32 / 34 trading days"*

> *"Note: All data is 15-minute delayed and limited to the past 2 years (Polygon Starter plan)."*

---

## Visual foundations

### Canvas
Deep charcoal (`#0f1117`) full-bleed. No gradients, no images, no patterns on the page background. A quiet surface so that price action, charts, and colored cells pop.

### Surfaces (layered depth via tone, not shadow)
1. `--bg-canvas` `#0f1117` — page background
2. `--bg-surface` `#161922` — cards, config panels
3. `--bg-elevated` `#1c1f2e` — nested panels, inputs, strategy sub-cards
4. `--bg-hover` `#232738` — row hover, button hover

Shadows are minimal: `0 1px 3px rgba(0,0,0,0.2)` on cards, `0 4px 12px rgba(0,0,0,0.4)` on popovers/session panels.

### Color vibe
**Semantic first.** Every color means something. Blue `#3b82f6` = accent/active/link. Emerald `#00c896` = bull/win/profit/OK. Red `#e5334e` = bear/loss/error. Amber `#f59e0b` = warn/ATM/partial. No decorative hues.

### Typography
System sans stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, ...`) for UI. `ui-monospace` for tickers, params, code, and column identifiers. The codebase pulls `katex/dist/katex.min.css` for math rendering in docs. **No web fonts are shipped** — this system follows suit. (If we want a branded display face later, we'd add it here; flagging as an intentional omission, not a substitution.)

- Tight letter-spacing (`-0.02em`) on large headings.
- Eyebrow labels: `0.65rem`, `font-weight: 700`, `text-transform: uppercase`, `letter-spacing: 0.04–0.06em`, color `--text-muted`.
- Data values: `font-variant-numeric: tabular-nums` for alignment.
- Dense line-heights (`1.2` for hero values, `1.5–1.6` for body, `1.7–1.8` for tables).

### Spacing
4-px grid. Cards use `1.25rem` internal padding; page container is `1200–1400px` max-width with `1–2rem` horizontal padding. Grids use `gap: 0.5–1rem` for dense controls, `1.5rem` for major sections.

### Backgrounds & imagery
**None.** This is a data tool. There are no hero images, illustrations, repeating patterns, or gradients on page backgrounds. The *only* decorative pattern in the entire system is the **diagonal hatch** on ITM option-chain cells (`repeating-linear-gradient(-45deg, transparent, transparent 4px, rgba(16,185,129,0.12) 4px, rgba(16,185,129,0.12) 5px)`) — a functional visual encoding, not decoration.

### Animation
**Sparse and fast.** `0.15s` default transition on hover/focus. `0.1s` on active/press (with `transform: scale(0.98)`). One keyframe animation in the codebase: a 6px slide-down fade for the session panel (`animation: slideDown 0.2s ease-out`). No bounces, no springs, no page transitions.

### Hover / focus / press
- **Hover:** darker surface (`--bg-hover`) OR slightly lighter text color. Never opacity changes on buttons.
- **Focus:** `border-color: --accent` + `box-shadow: 0 0 0 3px rgba(59,130,246,0.15)` — a subtle blue halo on inputs.
- **Press:** `transform: scale(0.98)` on primary buttons. No color change.
- **Disabled:** `opacity: 0.55` + `cursor: not-allowed`.

### Borders
Hairline `1px solid var(--border)` (`#2a2e3e`) everywhere. `--border-light` (`#353a4d`) for table dividers and emphasized hovers. Dashed `1px dashed --border-light` for file-upload targets.

### Corner radii
- `4px` — table cells' inline tags, chips, small badges
- `6px` — inputs, buttons, callouts, standard panels
- `8px` — cards, form sections
- `10px` — primary cards (controls-card, config-card)
- `9999px` — pills (date presets, status counts)

### Cards (the workhorse)
```
background: var(--bg-surface);
border: 1px solid var(--border);
border-radius: 10px;
padding: 1.25rem;
box-shadow: 0 1px 3px rgba(0,0,0,0.2);
```
Variant: **hero/stat cards** tint themselves with `--bull-soft` / `--bear-soft` and a matching `border-color: rgba(bull|bear, 0.25)` when the value is positive/negative.

### Callouts
Left-border accent is used **only on callouts**, never on generic cards. Patterns:
- Info: `background: #eff6ff; border-left: 3px solid #2563eb;` (light — used on dark surfaces intentionally as an insert)
- Warn: `background: #fffbeb; border-left: 3px solid #d97706;`
- Error: `background: rgba(229,51,78,0.12); border-left: 3px solid var(--bear);`
- Deprecation: `background: rgba(255,152,0,0.08); border: 1px solid rgba(255,152,0,0.3);` (full border, not just left)

### Transparency / blur
Used sparingly. Semantic tints are `rgba(color, 0.08–0.15)` to soften backgrounds without adding a new color. No `backdrop-filter: blur(...)` in the codebase.

### Layout rules
- **Top-level nav is a horizontal Menubar** (PrimeNG `p-menubar`) with dropdown groups. Always sticky-top-ish and always present.
- Page containers: `max-width: 1200px` for dense tools (Strategy Lab, Data Lab); `1400px` for the Engine lab with its wider charts.
- Two-column control grids: `grid-template-columns: 1.5fr 1fr` — collapses to one column below 768px.
- Hero metric rows: `grid-template-columns: repeat(5, 1fr)` for the financial dashboard stat strip.

---

## Iconography

**PrimeIcons (`primeicons@7`) is the sole icon system.** It's a CDN-available icon font used pervasively via `<i class="pi pi-*">`. The codebase never ships custom SVGs for UI icons and never uses emoji.

- Stocks group: `pi-chart-line`, `pi-chart-bar`, `pi-list`, `pi-wave-pulse`, `pi-search`, `pi-camera`, `pi-wrench`, `pi-check-square`, `pi-book`, `pi-verified`, `pi-chart-scatter`, `pi-database`
- Data Quality: `pi-shield`, `pi-check-circle`
- Options: `pi-objects-column`, `pi-table`, `pi-th-large`, `pi-calculator`, `pi-history`
- Engine: `pi-cog`, `pi-play`
- Portfolio: `pi-wallet`
- Research Lab: `pi-search`
- Tracked: `pi-eye`

Inline status/hint icons:
- `pi-exclamation-triangle` (amber) — deprecation / warn
- `pi-info-circle` (blue) — info callouts
- `pi-bookmark-fill` (blue) — active session
- `pi-sort` — table sort

**For this design system, load PrimeIcons from CDN:**
```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/primeicons@7.0.0/primeicons.css">
```

No logo file exists in the upstream repo (the `<title>` is just `"Books & Authors"` — an artifact of an older codebase state). The wordmark **"Quant Trading Lab"** used throughout this design system is a placeholder lockup (a geometric candle glyph + monospace wordmark) to flag for the user.

---

## Index

```
README.md                    ← you are here
SKILL.md                     ← agent-skill entry point (cross-compatible with Claude Code)
colors_and_type.css          ← CSS variables: colors, type, spacing, radii, shadows

preview/                     ← small cards shown in the Design System tab
  00-brand.html              ← wordmark + tagline
  01-colors-canvas.html      ← canvas & surface stack
  02-colors-text.html        ← text colors
  03-colors-semantic.html    ← bull / bear / warn / accent
  04-colors-options.html     ← ITM / OTM / ATM options encoding
  05-type-scale.html         ← heading & body sizes
  06-type-specimens.html     ← eyebrow / stat / hero / mono
  07-spacing.html            ← 4px spacing scale
  08-radii.html              ← corner radii
  09-shadows.html            ← elevation / shadow stack
  10-buttons.html            ← primary / secondary / ghost / preset pills
  11-inputs.html             ← text inputs / selects / checkboxes
  12-cards.html              ← stat cards, hero cards
  13-callouts.html           ← info / warn / error / deprecation
  14-badges.html             ← pills, tags, parity badges
  15-table.html              ← trade log table row styles
  16-iconography.html        ← PrimeIcons used across the app

ui_kits/
  frontend/
    README.md                ← kit overview
    index.html               ← interactive recreation of the workbench
    TopNav.jsx               ← PrimeNG-style menubar
    Card.jsx, StatCard.jsx   ← surface primitives
    Button.jsx, Input.jsx    ← form primitives
    OptionsChain.jsx         ← ITM/OTM/ATM chain table
    TradeLog.jsx             ← win/loss trade table
    StrategyLab.jsx          ← controls card + hero metrics screen
    PortfolioDashboard.jsx   ← summary cards + metrics + trade form
```

---

## Caveats & known substitutions

- **No logo in upstream repo.** The wordmark + candle glyph is a placeholder for review.
- **No web fonts shipped.** The system uses the native system sans stack — matches the codebase. If a branded display face is desired, flag it.
- **Some callouts use light backgrounds on dark surfaces** (copied from the upstream SCSS — this is an inherited inconsistency, preserved as-is). Consider adding a dark-aware callout variant.
- **PrimeNG component styling is not re-implemented** — only the *visual vocabulary* of the overrides is captured. Components like `p-datepicker`, `p-autocomplete`, `p-tabs` should be used directly via PrimeNG Aura dark.
