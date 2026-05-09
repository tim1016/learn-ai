> **Status:** Archived — stale plan for deprecated feature.
> **Do not use as implementation authority.**
> Current authority: `docs/architecture/engine-authority-map.md` ("Deprecated engines" table).
> Archived because: Strategy Lab is deprecated per engine-authority-map.md; improvement plans are moot.

# Strategy Lab — UI/UX Improvement Plan

**Component:** `Frontend/src/app/components/strategy-lab/`
**Current state:** 604-line template, 547-line TS, 589-line SCSS. Single monolithic component handling both Backtest and Replay modes.
**Design system:** Dark theme, PrimeNG + Tailwind CSS, custom SCSS tokens (`$bg-canvas: #0f1117`, `$accent: #3b82f6`, `$bull: #00c896`, `$bear: #e5334e`)

---

## Phase 1 — Layout restructure & progressive disclosure (highest impact, lowest risk)

These changes reorganize the existing controls without adding new features. They touch only the template and SCSS — no new signals, no API changes.

### 1.1 Two-column parameter layout

**Current:** Everything stacks vertically in `.controls-card` — ticker, dates, timespan, checkboxes, strategy, params, buttons all in a single column flow (lines 36–311). Users scan top-to-bottom through ~15 controls before finding "Run Backtest".

**New layout:**

```
┌─────────────────────────────────────────────────────────────┐
│ Left column (60%)              │ Right column (40%)          │
│                                │                             │
│ INSTRUMENT                     │ DATE RANGE                  │
│ ┌────────────────────────────┐ │ ┌─────────┐ ┌─────────┐   │
│ │ Ticker [AAPL___________▼]  │ │ │ From    │ │ To      │   │
│ └────────────────────────────┘ │ └─────────┘ └─────────┘   │
│                                │ Presets: 7d 30d 3m 6m 1y  │
│ STRATEGY                       │                             │
│ ┌────────────────────────────┐ │ DATA STATUS                 │
│ │ EMA Crossover + RSI   [▼] │ │ ✅ 500 sessions on disk     │
│ │ ┌──────────────────────┐   │ │ ⚠ 23 missing → [Fetch]    │
│ │ │ Fast EMA: 5          │   │ │ Source: Local cache         │
│ │ │ Slow EMA: 10         │   │ │                             │
│ │ │ RSI: 50-70           │   │ │                             │
│ │ │ Exit: 5 bars         │   │ │                             │
│ │ └──────────────────────┘   │ │                             │
│ └────────────────────────────┘ │                             │
│                                │                             │
│ ▶ Advanced (Resolution, Fill,  │                             │
│   Session, Warmup)             │                             │
├────────────────────────────────┴─────────────────────────────┤
│ [▶ Run Backtest]                          [Reset] [Save cfg] │
└──────────────────────────────────────────────────────────────┘
```

**Implementation:**

In `strategy-lab.component.html` (lines 36–311), wrap the `.controls-card` body in a CSS grid:

```html
<div class="controls-card">
  <div class="controls-grid">
    <!-- Left column -->
    <div class="col-instrument">
      <div class="section-label">Instrument</div>
      <!-- Ticker input (existing, move here) -->

      <div class="section-label" style="margin-top: 1rem">Strategy</div>
      <!-- Strategy select + params (existing lines 163–297, move here) -->
    </div>

    <!-- Right column -->
    <div class="col-daterange">
      <div class="section-label">Date Range</div>
      <!-- From/To date pickers (existing lines 42–115, move here) -->
      <!-- Quick presets (existing lines 118–125, move here) -->

      <div class="section-label" style="margin-top: 1rem">Data Status</div>
      <!-- New data-status component (see §1.3) -->
    </div>
  </div>

  <!-- Advanced settings (collapsed by default) -->
  <!-- Run / Reset buttons (sticky footer) -->
</div>
```

SCSS addition:

```scss
.controls-grid {
  display: grid;
  grid-template-columns: 1.5fr 1fr;
  gap: 1.5rem;

  @media (max-width: 768px) {
    grid-template-columns: 1fr;
  }
}

.section-label {
  font-size: 0.7rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: $text-muted;
  margin-bottom: 0.5rem;
}
```

### 1.2 Progressive disclosure — "Advanced Settings" collapsible

**Current:** Resolution (Timespan + Multiplier), Session, Forward-fill, and Warmup checkboxes are always visible (lines 127–161). These are secondary controls that add visual noise for the 80% workflow of "pick symbol, pick strategy, run".

**Change:** Collapse them behind a toggle. Add a new signal:

```typescript
// strategy-lab.component.ts
showAdvanced = signal(false);
```

In template, replace the two `.options-row` blocks (lines 127–161) with:

```html
<!-- Advanced Settings Toggle -->
<button class="advanced-toggle" (click)="showAdvanced.set(!showAdvanced())">
  <i class="pi" [class.pi-chevron-right]="!showAdvanced()" [class.pi-chevron-down]="showAdvanced()"></i>
  Advanced Settings
  <span class="advanced-summary" *ngIf="!showAdvanced()">
    {{ multiplier() }}{{ timespan() === 'minute' ? 'm' : timespan() === 'hour' ? 'h' : 'd' }}
    · {{ session() === 'rth' ? 'RTH' : 'Extended' }}
    · {{ forwardFill() ? 'Fill' : 'No fill' }}
    · {{ warmup() ? 'Warmup' : 'No warmup' }}
  </span>
</button>

@if (showAdvanced()) {
  <div class="advanced-panel" @slideDown>
    <!-- Existing options-row blocks (lines 127-161) move here unchanged -->
  </div>
}
```

SCSS:

```scss
.advanced-toggle {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  background: none;
  border: 1px solid $border;
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  color: $text-secondary;
  font-size: 0.82rem;
  cursor: pointer;
  width: 100%;
  margin-top: 0.75rem;
  transition: all 0.15s;

  &:hover { background: $bg-hover; color: $text-primary; }

  .advanced-summary {
    margin-left: auto;
    font-weight: 400;
    color: $text-muted;
    font-size: 0.78rem;
  }
}

.advanced-panel {
  border: 1px solid $border;
  border-radius: 6px;
  padding: 0.75rem;
  margin-top: 0.5rem;
  background: $bg-elevated;
}
```

### 1.3 Data status component (replaces buried text)

**Current:** No explicit data availability display in the template — it's implicitly part of the pipeline info callout that only appears *after* running the backtest (lines 320–331).

**Change:** Add a small inline data-status block in the right column that shows availability *before* running. This requires a new lightweight API call or a computed signal.

Add to the TS:

```typescript
dataStatus = signal<{ available: number; missing: number; source: string } | null>(null);
```

Populated by debounced `effect()` that calls an existing or new lightweight endpoint when ticker or date range changes. Template:

```html
<div class="data-status">
  @if (dataStatus(); as ds) {
    <div class="status-row">
      <i class="pi pi-check-circle" style="color: var(--bull)"></i>
      <span>{{ ds.available }} sessions on disk</span>
    </div>
    @if (ds.missing > 0) {
      <div class="status-row warn">
        <i class="pi pi-exclamation-circle" style="color: var(--warn)"></i>
        <span>{{ ds.missing }} missing</span>
        <button class="fetch-link" (click)="fetchMissing()">Fetch now</button>
      </div>
    }
    <div class="status-source">Source: {{ ds.source }}</div>
  } @else {
    <span class="text-muted">Enter dates to check</span>
  }
</div>
```

### 1.4 Sticky CTA footer inside the controls card

**Current:** "Run Backtest" and "Generate CSV" are inline at the bottom of the card (lines 299–310), visually identical weight, and disconnected from the controls they act on.

**Change:** Pin them to the bottom of `.controls-card` with a top-border separator. Make "Run Backtest" the dominant CTA. Add "Reset" as a ghost button.

```html
<div class="controls-footer">
  <div class="footer-left">
    <button class="btn-primary btn-lg" (click)="runBacktest()" [disabled]="loading()">
      @if (loading()) {
        <i class="pi pi-spin pi-spinner"></i> Running...
      } @else {
        <i class="pi pi-play"></i> Run Backtest
      }
    </button>
    <button class="btn-ghost" (click)="resetForm()">Reset</button>
  </div>
  <div class="footer-right">
    <button class="btn-secondary btn-sm" (click)="generateZip()" [disabled]="loading() || !result()">
      <i class="pi pi-download"></i> CSV
    </button>
  </div>
</div>
```

SCSS:

```scss
.controls-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-top: 1px solid $border;
  margin-top: 1rem;
  padding-top: 1rem;

  .footer-left { display: flex; gap: 0.75rem; align-items: center; }
}

.btn-lg {
  padding: 0.65rem 1.5rem;
  font-size: 0.95rem;
  font-weight: 700;
}

.btn-ghost {
  background: none;
  border: none;
  color: $text-secondary;
  font-size: 0.82rem;
  cursor: pointer;
  padding: 0.5rem 0.75rem;
  &:hover { color: $text-primary; }
}
```

Add `resetForm()` method to the TS that resets all signals to their defaults.

---

## Phase 2 — Trades table toggle & results polish

### 2.1 Collapsible trade log

**Current:** The trade table renders immediately with all rows visible (lines 398–448). For 128-trade runs this is a wall of data that pushes the chart out of view.

**Change:** Wrap in a collapsible panel, collapsed by default.

Add signal:

```typescript
showTradeLog = signal(false);
```

Replace lines 398–448:

```html
@if (r.trades.length > 0) {
  <div class="trade-log-panel">
    <button class="panel-toggle" (click)="showTradeLog.set(!showTradeLog())">
      <i class="pi" [class.pi-chevron-right]="!showTradeLog()" [class.pi-chevron-down]="showTradeLog()"></i>
      <span>Trade Log</span>
      <span class="panel-badge">{{ r.trades.length }} trades</span>
    </button>

    @if (showTradeLog()) {
      <div class="table-wrapper">
        <!-- Existing <table> unchanged (lines 402–445) -->
      </div>
    }
  </div>
}
```

SCSS:

```scss
.trade-log-panel {
  margin-top: 1.5rem;
}

.panel-toggle {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  background: $bg-surface;
  border: 1px solid $border;
  border-radius: 8px;
  padding: 0.75rem 1rem;
  color: $text-primary;
  font-size: 0.9rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;

  &:hover { background: $bg-hover; }

  .panel-badge {
    margin-left: auto;
    font-size: 0.75rem;
    font-weight: 500;
    background: $bg-elevated;
    color: $text-secondary;
    padding: 2px 8px;
    border-radius: 10px;
  }
}
```

### 2.2 Results summary redesign — hero metrics row

**Current:** 12 stat cards in a uniform auto-fit grid (lines 334–383). All cards have identical visual weight; the most important numbers (final equity, drawdown, Sharpe) don't stand out.

**New structure:** A "hero row" of 5 primary KPIs at the top, then the remaining 7 in a secondary row below.

```html
<!-- Hero metrics -->
<div class="hero-metrics">
  <div class="hero-card" [class.positive]="r.total_pnl_pct > 0" [class.negative]="r.total_pnl_pct <= 0">
    <span class="hero-label">Net P&L</span>
    <span class="hero-value">{{ formatPct(r.total_pnl_pct) }}</span>
    <span class="hero-sub">${{ r.total_pnl_pts.toFixed(2) }}</span>
  </div>
  <div class="hero-card negative">
    <span class="hero-label">Max Drawdown</span>
    <span class="hero-value">{{ formatPct(r.max_drawdown_pct) }}</span>
  </div>
  <div class="hero-card">
    <span class="hero-label">Trades</span>
    <span class="hero-value">{{ r.total_trades }}</span>
    <span class="hero-sub">{{ r.winning_trades }}W / {{ r.losing_trades }}L</span>
  </div>
  <div class="hero-card">
    <span class="hero-label">Win Rate</span>
    <span class="hero-value">{{ (r.win_rate * 100).toFixed(1) }}%</span>
  </div>
  <div class="hero-card">
    <span class="hero-label">Sharpe</span>
    <span class="hero-value">{{ r.sharpe_ratio.toFixed(3) }}</span>
  </div>
</div>

<!-- Secondary metrics -->
<div class="stats-grid">
  <!-- Avg Win, Avg Loss, W/L Ratio, Profit Factor, Expectancy, Fees remaining -->
</div>
```

SCSS:

```scss
.hero-metrics {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 0.75rem;
  margin-bottom: 0.75rem;

  @media (max-width: 768px) {
    grid-template-columns: repeat(2, 1fr);
  }
}

.hero-card {
  background: $bg-surface;
  border: 1px solid $border;
  border-radius: 8px;
  padding: 1rem;
  text-align: center;

  .hero-label {
    display: block;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: $text-muted;
    margin-bottom: 0.25rem;
  }

  .hero-value {
    display: block;
    font-size: 1.4rem;
    font-weight: 700;
    line-height: 1.2;
  }

  .hero-sub {
    display: block;
    font-size: 0.75rem;
    color: $text-secondary;
    margin-top: 0.15rem;
  }

  &.positive .hero-value { color: $bull; }
  &.negative .hero-value { color: $bear; }
}
```

### 2.3 Quieter quick-range presets

**Current:** Five bright blue `.preset-btn` buttons in a horizontal row (lines 118–125) that draw disproportionate attention.

**Change:** Restyle as small ghost pills underneath the date inputs, highlight only the active one.

Add signal:

```typescript
activePreset = signal<string | null>(null);
```

Update the existing `setPresetRange()` and `setPresetMonths()` methods to also set `activePreset`:

```typescript
setPresetRange(days: number) {
  // ... existing logic ...
  this.activePreset.set(`${days}d`);
}
setPresetMonths(months: number) {
  // ... existing logic ...
  this.activePreset.set(`${months}m`);
}
```

Template:

```html
<div class="date-presets">
  @for (p of [{ label: '7d', fn: 'setPresetRange', arg: 7 }, ...]; track p.label) {
    <button
      class="preset-pill"
      [class.active]="activePreset() === p.label"
      (click)="p.fn === 'setPresetRange' ? setPresetRange(p.arg) : setPresetMonths(p.arg)"
    >{{ p.label }}</button>
  }
</div>
```

SCSS:

```scss
.date-presets {
  display: flex;
  gap: 4px;
  margin-top: 0.4rem;
}

.preset-pill {
  background: transparent;
  border: 1px solid $border;
  border-radius: 12px;
  padding: 2px 10px;
  font-size: 0.72rem;
  font-weight: 500;
  color: $text-muted;
  cursor: pointer;
  transition: all 0.15s;

  &:hover { border-color: $text-secondary; color: $text-secondary; }

  &.active {
    background: $accent;
    border-color: $accent;
    color: #fff;
  }
}
```

---

## Phase 3 — Strategy card & trust indicators

### 3.1 Strategy card with structured preview

**Current:** Strategy selection is a bare `<select>` dropdown (lines 164–174) followed by raw parameter inputs (lines 178–297). The user sees "ema_crossover_rsi" in snake_case and a wall of number inputs with no description.

**Change:** Replace with a card that shows the strategy's logic at a glance.

Add a `strategyMeta` computed signal that returns display info for the selected strategy:

```typescript
strategyMeta = computed(() => {
  const s = this.strategyName();
  const meta: Record<string, { display: string; timeframe: string; rules: string[]; }> = {
    ema_crossover_rsi: {
      display: 'EMA Crossover + RSI',
      timeframe: '15m',
      rules: [
        `Entry: EMA${this.emaCrossoverFastPeriod()} crosses EMA${this.emaCrossoverSlowPeriod()}`,
        `Filter: RSI ${this.emaCrossoverRsiMin()}–${this.emaCrossoverRsiMax()}`,
        `Min gap: ${this.emaCrossoverMinGap()}`,
        `Exit: hold ${this.emaCrossoverExitBars()} bars`,
      ],
    },
    // ... similar for other strategies
  };
  return meta[s] ?? { display: s, timeframe: '', rules: [] };
});
```

Template:

```html
<div class="strategy-card">
  <div class="strategy-card-header">
    <div>
      <select [(ngModel)]="strategyName" class="strategy-select">
        <!-- options unchanged -->
      </select>
    </div>
    <button class="btn-ghost btn-sm" (click)="showStrategyParams.set(!showStrategyParams())">
      {{ showStrategyParams() ? 'Hide params' : 'Edit params' }}
    </button>
  </div>

  <div class="strategy-rules">
    @for (rule of strategyMeta().rules; track rule) {
      <div class="rule-line">• {{ rule }}</div>
    }
  </div>

  @if (showStrategyParams()) {
    <div class="strategy-params">
      <!-- Existing @switch param blocks (lines 179–296), unchanged -->
    </div>
  }
</div>
```

SCSS:

```scss
.strategy-card {
  background: $bg-elevated;
  border: 1px solid $border;
  border-radius: 8px;
  padding: 0.75rem 1rem;
  margin-top: 0.75rem;
}

.strategy-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}

.strategy-rules {
  .rule-line {
    font-size: 0.8rem;
    color: $text-secondary;
    line-height: 1.6;
  }
}
```

### 3.2 LEAN parity indicator

**Current:** No visible parity status anywhere. The engine's biggest selling point (bit-exact LEAN reproduction) is invisible to the user.

**Change:** A small badge in the pipeline info callout.

Template addition (inside the `.info-callout` after backtest runs, line 320):

```html
<div class="parity-badge" [class.validated]="isParityValidated()" [class.baseline]="!isParityValidated()">
  @if (isParityValidated()) {
    <i class="pi pi-verified"></i>
    LEAN Parity ✅
    <span class="parity-detail">Validated: {{ parityReference() }}</span>
  } @else {
    <i class="pi pi-info-circle"></i>
    Baseline Mode
    <span class="parity-detail">No LEAN reference for {{ ticker() }}</span>
  }
</div>
```

TS: the parity check can be a simple signal computed from the ticker and strategy — currently only SPY + ema_crossover_rsi has a validated reference (the 63-trade test from `test_spy_validation.py`).

```typescript
isParityValidated = computed(() =>
  this.ticker().toUpperCase() === 'SPY' && this.strategyName() === 'ema_crossover_rsi'
);
parityReference = computed(() =>
  this.isParityValidated() ? 'SPY × EMA Crossover RSI (63 trades, bit-exact)' : ''
);
```

---

## Phase 4 — Input polish & micro-UX

### 4.1 Searchable symbol input

**Current:** Bare `<input type="text">` for ticker (line 40), 80px wide. No validation, no suggestions.

**Change:** Replace with PrimeNG `p-autocomplete` or a custom dropdown. Source suggestions from a static JSON of common tickers or an API endpoint (if the `MarketDataService` supports symbol search).

```html
<div class="input-group ticker-group">
  <label>Symbol</label>
  <p-autoComplete
    [(ngModel)]="ticker"
    [suggestions]="tickerSuggestions()"
    (completeMethod)="searchTickers($event)"
    [minLength]="1"
    placeholder="SPY"
    [style]="{ width: '100%' }"
  />
</div>
```

If a symbol-search endpoint doesn't exist yet, start with a hardcoded list of ~20 common symbols as a static filter — upgradeable later.

### 4.2 Inline tooltips for non-obvious controls

**Current:** "Fill Mode", "Resolution", "Session" have no explanatory text except `option-hint` spans that add visual noise.

**Change:** Replace hint spans with PrimeNG tooltips on the label:

```html
<label>
  Session
  <i class="pi pi-info-circle tooltip-icon" pTooltip="RTH = Regular Trading Hours (09:30–16:00 ET). Extended adds pre/post-market." tooltipPosition="top"></i>
</label>
```

SCSS:

```scss
.tooltip-icon {
  font-size: 0.7rem;
  color: $text-muted;
  cursor: help;
  margin-left: 4px;
  vertical-align: middle;
}
```

### 4.3 RTH vs Extended Hours — explicit in results

**Current:** Session choice is made before running but not reflected in results.

**Change:** Add to the pipeline info callout (line 323):

```html
<span class="session-tag">{{ session() === 'rth' ? 'RTH' : 'Extended' }}</span>
```

### 4.4 Terminology cleanup

Replace:
- "Weekdays on disk" → "Trading sessions available"
- "Forward-fill gaps" → "Fill missing bars" (with tooltip for the detail)
- "Indicator warm-up" → "Pre-warm indicators" (with tooltip)

---

## Phase 5 — Component extraction (code health)

The current component is 547 lines of TS and 604 lines of HTML. This phase extracts reusable pieces without changing behavior.

### 5.1 Extract `BacktestControlsComponent`

Move the entire controls grid (lines 36–311) into `strategy-lab/backtest-controls/backtest-controls.component.ts`. The parent passes in signals via `model()` for two-way binding and receives `runBacktest` / `generateZip` events via `output()`.

### 5.2 Extract `BacktestResultsComponent`

Move the results section (lines 317–448) into `strategy-lab/backtest-results/backtest-results.component.ts`. Input: `BacktestResponse` signal. Contains the hero metrics, secondary metrics, chart host, and trade log panel.

### 5.3 Extract `StrategyCardComponent`

The strategy dropdown + rules preview + parameter form becomes its own standalone component. Input: `strategyName` model, outputs: parameter changes. This enables reuse if strategy selection appears elsewhere (e.g., a "compare strategies" feature).

### 5.4 Shared SCSS tokens

Currently the design tokens are duplicated across `strategy-lab.component.scss` and `strategy-lab-chart.component.scss`. Extract to a shared partial:

```
Frontend/src/app/styles/_tokens.scss
```

Import in each component's SCSS:
```scss
@use '~styles/tokens' as *;
```

---

## Implementation order & effort estimates

| Step | Description | Files touched | Effort |
|---|---|---|---|
| **1.2** | Advanced Settings collapsible | `.html`, `.ts`, `.scss` | ~1 hr |
| **2.1** | Trade log toggle (collapsed default) | `.html`, `.ts`, `.scss` | ~30 min |
| **2.3** | Quiet preset pills | `.html`, `.scss`, `.ts` (add `activePreset` signal) | ~30 min |
| **1.1** | Two-column grid layout | `.html`, `.scss` | ~2 hr |
| **1.4** | Sticky CTA footer + Reset | `.html`, `.ts`, `.scss` | ~45 min |
| **2.2** | Hero metrics row | `.html`, `.scss` | ~1 hr |
| **3.1** | Strategy card | `.html`, `.ts`, `.scss` | ~2 hr |
| **4.2** | Tooltips (replacing hint spans) | `.html`, `.scss` | ~30 min |
| **4.4** | Terminology cleanup | `.html` | ~15 min |
| **3.2** | Parity badge | `.html`, `.ts`, `.scss` | ~30 min |
| **1.3** | Data status component | `.html`, `.ts`, `.scss` + possible API | ~2 hr |
| **4.1** | Searchable symbol input | `.html`, `.ts` | ~1.5 hr |
| **4.3** | RTH/Extended in results | `.html`, `.scss` | ~15 min |
| **5.1–5.4** | Component extraction + shared tokens | New files, refactor | ~3 hr |

**Total estimate:** ~15 hours of focused work across all 5 phases.

**Recommended batching:**
- **Sprint 1 (quick wins, ~3 hr):** Steps 1.2, 2.1, 2.3, 4.2, 4.4 — immediately reduces visual noise and hides the trade table.
- **Sprint 2 (layout, ~4 hr):** Steps 1.1, 1.4, 2.2 — the big layout restructure that changes how the page feels.
- **Sprint 3 (strategy + trust, ~3 hr):** Steps 3.1, 3.2, 4.3 — makes the strategy readable and the parity claim visible.
- **Sprint 4 (polish + refactor, ~5 hr):** Steps 1.3, 4.1, 5.1–5.4 — data status, symbol search, code cleanup.

---

## Signals to add (summary)

| Signal | Type | Default | Purpose |
|---|---|---|---|
| `showAdvanced` | `signal<boolean>` | `false` | Toggle advanced settings panel |
| `showTradeLog` | `signal<boolean>` | `false` | Toggle trade log table |
| `showStrategyParams` | `signal<boolean>` | `false` | Toggle strategy parameter editing |
| `activePreset` | `signal<string \| null>` | `null` | Track which date preset is active |
| `dataStatus` | `signal<DataStatus \| null>` | `null` | Pre-run data availability |
| `tickerSuggestions` | `signal<string[]>` | `[]` | Autocomplete suggestions |

---

## What this plan does NOT include

- Backend/API changes (everything is frontend-only except the optional data-status endpoint in §1.3)
- New strategies or parameter definitions
- Chart component redesign (the `strategy-lab-chart` using lightweight-charts is solid)
- Replay mode changes (left as-is; could get the same treatment in a follow-up)
- Mobile-specific redesign (responsive breakpoints are added, but this isn't a mobile-first pass)
