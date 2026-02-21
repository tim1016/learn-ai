# Options Chain Page — TradingView-Style Implementation Plan

## Goal

Build a new **Options Chain** page that mirrors the TradingView options chain UI (screenshot reference). This replaces or significantly upgrades the existing `TickerExplorerComponent` at `/options-chain` with a professional, dark-themed chain viewer featuring:

- Scrollable **expiration date ribbon** with month headers (Feb, Mar, Apr... Jan '27)
- Symmetrical chain table: **Calls** on the left, **Strike + IV%** in the center, **Puts** on the right
- All Greeks displayed: **Vega, Theta, Gamma, Delta** (not just delta/theta like current)
- **Price** column (day close / last trade)
- **Volume bars** — inline colored bar visualizations inside volume cells (blue for calls, red for puts)
- **ATM indicator** — current underlying price tooltip near the ATM strike
- **Sorted by strike** with ability to sort (click arrow on Strike column header)
- **Historical drill-down** — click any contract cell to open an overlay showing historical OHLCV chart for that option contract

---

## Screenshot Analysis — What TradingView Shows

### Layout (top → bottom)
1. **Header bar**: Ticker search (`Q SPY`), tab buttons (`Chain` | `Strategy builder` | `Strategy finder` | `Volatility`), view toggle (`By expiration` | `By strike`)
2. **Expiration ribbon**: Horizontal scrollable strip grouped by month. Each date is a clickable pill/chip. Selected date is highlighted. Dates are NOT consecutive — only dates with available contracts are shown (e.g., Feb has 23,24,25,26,27 but Mar jumps from 6 → 13 → 20 → 27 → 31)
3. **Section labels**: "Calls" (left) and "Puts" (right)
4. **Column headers**:
   - Calls side (right-to-left toward center): `[?]`, Vega, Theta, Gamma, Delta, Price, Ask, Bid, Volume
   - Center: `↑ Strike`, `IV, %`
   - Puts side (left-to-right from center): Volume, Bid, Ask, Price, Delta, Gamma, Theta, Vega, `[?]`
5. **Data rows**: ~10 strikes visible, centered around ATM (689 for SPY at $689.43)
6. **Volume bars**: Colored bar charts inside each Volume cell (proportional to relative volume). Blue bars for calls, red/pink bars for puts.
7. **ATM tooltip**: `SPY 689.43` label appears near the strike closest to current price

### Data Fields per Row (per side) — Adapted for Starter Plan

TradingView shows Bid/Ask columns, but **Polygon Starter plan does NOT include `last_quote` data** (bid/ask requires a higher tier). We adapt the column layout:

| Field | TradingView Label | Our Polygon Source | Available? |
|-------|------------------|--------------------|------------|
| Volume | Volume | `day.volume` from snapshot | Yes |
| Bid | Bid | `last_quote.bid` | **NO — Starter plan excludes quotes** |
| Ask | Ask | `last_quote.ask` | **NO — Starter plan excludes quotes** |
| Price | Price | `day.close` from snapshot (last trade price) | Yes |
| Delta | Delta | `greeks.delta` | Yes |
| Gamma | Gamma | `greeks.gamma` | Yes |
| Theta | Theta | `greeks.theta` | Yes |
| Vega | Vega | `greeks.vega` | Yes |
| Strike | Strike | `details.strike_price` | Yes |
| IV % | IV, % | `implied_volatility` (× 100 for %) | Yes |
| Open Interest | OI | `open_interest` | Yes |

**Our adapted column layout** (replacing Bid/Ask with OI since we have it):

```
CALLS                                                          PUTS
Vega | Theta | Gamma | Delta | Price | OI | Volume || ↑Strike | IV% || Volume | OI | Price | Delta | Gamma | Theta | Vega
```

This keeps the mirror symmetry and fills the space where Bid/Ask would be with Open Interest, which is arguably more useful for analysis anyway.

---

## Data Source Plan

### What We Have (Polygon Starter Plan)

| API | What It Gives Us | Limitation |
|-----|-----------------|------------|
| `list_snapshot_options_chain` | Live Greeks, IV, OI, day OHLCV for a given ticker + expiration | Only returns **live/unexpired** contracts. No bid/ask on Starter. |
| `list_options_contracts` | Contract metadata: all available strikes, expirations, contract types | No pricing/greeks — metadata only |
| `fetch_aggregates` (on option ticker) | Historical OHLCV for a specific option contract | Can look back 2 years — **this is our historical advantage** |

### What We Need for the UI

1. **Expiration dates list** → `list_options_contracts` with no expiration filter, group by `expiration_date` to discover all available dates
2. **Chain data for selected expiration** → `list_snapshot_options_chain` with expiration filter (live data)
3. **Underlying price** → From snapshot's `underlying_asset` data
4. **Historical drill-down** → `fetch_aggregates` on the option contract ticker (e.g., `O:SPY260220C00689000`) to get OHLCV history

### Historical Data Advantage — Contract Drill-Down

This is our differentiator from TradingView's basic chain view. When a user clicks on any call or put cell in the chain:

1. We already know the **option contract ticker** from the snapshot (e.g., `O:SPY260220C00689000`)
2. We call `fetch_aggregates(ticker=<option_ticker>, timespan='day', from=<2_years_ago>, to=<today>)` to get historical daily OHLCV
3. We display this in a **slide-out overlay panel** with:
   - Candlestick chart of the option's price history (reusing existing `CandlestickChartComponent`)
   - Volume histogram below the chart (reusing existing `VolumeChartComponent`)
   - Summary stats: high, low, avg volume, price range
   - Contract details: strike, expiration, type, underlying ticker

This lets traders see how an option's premium has moved over time — something TradingView's chain view doesn't show inline.

---

## Tailwind CSS v4 Setup

Tailwind CSS is **not currently installed** in the Frontend project. Install it before building the options chain page.

### Installation (3 steps)

**Step 1 — Install packages:**
```bash
cd Frontend
npm install tailwindcss @tailwindcss/postcss postcss --force
```

**Step 2 — Create `.postcssrc.json`** in `Frontend/` root:
```json
{
  "plugins": {
    "@tailwindcss/postcss": {}
  }
}
```

**Step 3 — Add import to `src/styles.css`** (before any other styles):
```css
@import "tailwindcss";
```

That's it. Tailwind v4 uses a CSS-first config — no `tailwind.config.js` needed. The PostCSS plugin scans your templates automatically.

### Color Palette for Options Chain (Tailwind classes)

| UI Element | Tailwind Class | Hex Equivalent |
|---|---|---|
| Page background | `bg-slate-900` | `#0f172a` |
| Table row background | `bg-slate-800` | `#1e293b` |
| Row hover | `hover:bg-slate-700` | `#334155` |
| Header text | `text-slate-400` | `#94a3b8` |
| Body text | `text-slate-200` | `#e2e8f0` |
| Strike column bg | `bg-slate-800/80` | — |
| Strike text | `text-white font-bold` | — |
| Call volume bars | `bg-blue-500` | `#3b82f6` |
| Put volume bars | `bg-red-500` | `#ef4444` |
| ATM row highlight | `bg-amber-500/10` | — |
| ATM badge text | `text-amber-400` | `#fbbf24` |
| Positive change | `text-emerald-400` | `#34d399` |
| Negative change | `text-red-400` | `#f87171` |
| Price text | `text-slate-100` | `#f1f5f9` |
| Greek text | `text-slate-300` | `#cbd5e1` |
| Border/divider | `border-slate-700` | `#334155` |
| Muted/empty cells | `text-slate-500` | `#64748b` |
| Selected chip | `bg-blue-600 text-white` | — |
| Unselected chip | `bg-slate-700 text-slate-300` | — |
| Month label | `text-slate-400 text-xs uppercase` | — |

### Usage Pattern in Templates

Tailwind classes go directly on HTML elements in the template:
```html
<tr class="hover:bg-slate-700" [class.bg-amber-500/10]="isAtm(row.strike)">
  <td class="text-slate-300 text-right px-2 py-1">{{ formatGreek(call?.greeks?.vega) }}</td>
  ...
  <td class="text-white font-bold text-center bg-slate-800/80 border-x border-slate-700">
    {{ row.strike | number:'1.0-0' }}
  </td>
  ...
</tr>
```

Volume bar container:
```html
<td class="relative px-2 py-1">
  <div class="absolute inset-y-0 right-0 bg-blue-500/30" [style.width.%]="getVolumeBarWidth(...)"></div>
  <span class="relative text-slate-200">{{ formatNumber(call?.day?.volume) }}</span>
</td>
```

### Tailwind + PrimeNG Coexistence

Tailwind and PrimeNG work side-by-side. Use Tailwind for:
- Layout (flexbox, grid, spacing, padding)
- Typography (font sizes, weights, colors)
- Custom elements (volume bars, badges, chip styling overrides)
- Dark theme colors on non-PrimeNG elements

Use PrimeNG for:
- Interactive components (Table, Drawer, Skeleton, Tooltip)
- Component behavior (sorting, scrolling, slide-out)

PrimeNG's Aura dark theme + Tailwind's dark-prefixed classes both activate from the same `.app-dark` class toggle.

---

## PrimeNG v21 Component Mapping

Every UI element maps to a specific PrimeNG component. PrimeNG v21.1.1 renamed several modules from earlier versions — corrected names below.

### v21 Rename Watch-List

| Old Name (v17-) | New Name (v19+) | Import Path |
|------------------|-----------------|-------------|
| `SidebarModule` | `Drawer` | `primeng/drawer` |
| `TabMenuModule` / `TabViewModule` | `Tabs` | `primeng/tabs` |

### Component → UI Element Mapping

| UI Element | PrimeNG Component | Import | Why This One |
|---|---|---|---|
| **Options chain table** | `Table` | `primeng/table` | `pSortableColumn` for strike sorting, `pTemplate` for custom header/body rows, `[scrollable]` with fixed `scrollHeight`, `[ngClass]` on `<tr>` for ATM/ITM row highlighting, column `colspan` for Calls/Puts header groups |
| **Expiration date ribbon** | `SelectButton` inside `ScrollPanel` | `primeng/selectbutton` + `primeng/scrollpanel` | `SelectButton` renders a row of toggle-able pills/chips. Wrap in `ScrollPanel` for horizontal overflow scrolling when many expirations exist. Group by month using `@for` with month headers above each `SelectButton` group |
| **Expiration date chips** | `Chip` | `primeng/chip` | Alternative to `SelectButton` for individual date pills within each month group — lighter visual weight, supports custom styling per chip. Use inside the `ScrollPanel` as clickable date indicators |
| **Slide-out contract overlay** | `Drawer` | `primeng/drawer` | `position="right"`, `[(visible)]` two-way binding, `[modal]="true"` for backdrop dimming, `[style]="{ width: '500px' }"`. Replaces custom CSS slide-out — handles z-index, escape-to-close, click-outside automatically |
| **Underlying summary bar** | `Toolbar` | `primeng/toolbar` | Three-zone layout (`#start`, `#center`, `#end` templates). Ticker+price on left, contract count on right. Dark-themed via CSS |
| **Ticker search input** | `InputText` | `primeng/inputtext` | `pInputText` directive on `<input>`, combine with a `Button` for the "Fetch" action. Simple — no autocomplete needed since user knows the ticker |
| **Loading — table skeleton** | `Skeleton` | `primeng/skeleton` | Shimmer placeholders matching the table row layout. `animation="wave"`, custom `width`/`height` per cell. Show skeleton table while chain data loads |
| **Loading — spinner** | `ProgressSpinner` | `primeng/progressspinner` | Small spinner inside overlay panel while historical aggregates load. Lighter than a full skeleton for single-area loading |
| **Strike range control** | `InputNumber` | `primeng/inputnumber` | `[showButtons]="true"` with `min=5`, `max=50`, `step=5`. Compact +/- stepper to set how many strikes to show around ATM |
| **Volume bars** | Pure CSS `<div>` | n/a | Percentage-width div inside each volume `<td>`. Not a PrimeNG component — pure CSS gives full control over bar direction (right-to-left for calls, left-to-right for puts) and colors. `ProgressBar` would work but can't mirror direction easily |
| **Cell hover tooltips** | `Tooltip` | `primeng/tooltip` | `pTooltip="Click for historical data"` on clickable call/put cells. Lightweight directive, no extra DOM elements |
| **ATM price badge** | `Tag` | `primeng/tag` | `severity="warn"` for the orange ATM label `SPY 689.43`. Positioned absolutely near the ATM strike row |

### Corrected Import Block

```typescript
// Core data display
import { Table } from 'primeng/table';
import { Toolbar } from 'primeng/toolbar';
import { Tag } from 'primeng/tag';

// Expiration ribbon
import { SelectButton } from 'primeng/selectbutton';
import { ScrollPanel } from 'primeng/scrollpanel';
import { Chip } from 'primeng/chip';

// Inputs & controls
import { InputText } from 'primeng/inputtext';
import { InputNumber } from 'primeng/inputnumber';
import { Button } from 'primeng/button';

// Overlays & feedback
import { Drawer } from 'primeng/drawer';         // was SidebarModule
import { Tooltip } from 'primeng/tooltip';
import { Skeleton } from 'primeng/skeleton';
import { ProgressSpinner } from 'primeng/progressspinner';
```

> **Note on v21 imports**: PrimeNG v21 exports standalone components directly (e.g., `Table`, `Drawer`), not `XxxModule` wrappers. Import the component class directly into your `imports: [...]` array.

### What Each Component Replaces from Custom Code

| Previously Custom | Now PrimeNG |
|---|---|
| Hand-rolled `<table>` with manual sorting | `p-table` with `pSortableColumn` + `p-sortIcon` |
| Custom CSS slide-out panel with `position: fixed` + `@keyframes slideIn` | `Drawer` with `position="right"` — handles animations, backdrop, escape-to-close |
| Manual horizontal overflow div for expirations | `ScrollPanel` with `SelectButton` or `Chip` groups |
| Custom loading spinner CSS | `ProgressSpinner` + `Skeleton` |
| Custom tooltip positioning | `pTooltip` directive |

### Dark Theme Strategy with PrimeNG

PrimeNG v21 uses the **Aura** preset (already configured in `app.config.ts`). For dark mode on this page only:

**Option A — CSS class toggle (recommended)**:
Set `darkModeSelector: '.app-dark'` in the PrimeNG config. The options chain component adds `.app-dark` to `document.documentElement` on init and removes it on destroy. All PrimeNG components on this page automatically switch to dark palette.

```typescript
// In options-chain.component.ts
ngOnInit() {
  document.documentElement.classList.add('app-dark');
}

ngOnDestroy() {
  document.documentElement.classList.remove('app-dark');
}
```

**Option B — Component-scoped SCSS only**: Override PrimeNG CSS variables inside `:host` scope. More isolated but more manual work.

**Decision**: Option A — cleanest approach, PrimeNG handles all component dark styling automatically.

---

## Architecture Decision

### Replace vs. New Component

**Decision: Replace the existing `TickerExplorerComponent`** at the `/options-chain` route.

Reasoning:
- The current component has the right data flow (snapshot → filter → display)
- But the UI is basic (light theme, no volume bars, missing columns)
- A complete rewrite of the template + styles is needed, but the TS logic is solid

### Dark Theme

The TradingView screenshot uses a **dark theme**. Options:

**Option A**: Make the options chain page dark-themed standalone (page-level dark background). This is easiest and matches the reference closely. Other pages stay light.

**Option B**: Add a global dark mode toggle. This is scope creep — defer.

**Decision: Option A** — standalone dark theme for this page only. Apply dark background/text colors via the component's own SCSS.

---

## Implementation Phases

### Phase 1: Frontend Types & Service Updates

No backend changes needed — the existing snapshot API already returns everything we need (Greeks, IV, OI, day OHLCV). We just need to add a method to discover expirations.

**Files to modify**:
- [market-data.service.ts](Frontend/src/app/services/market-data.service.ts) — Add `getOptionsExpirations(ticker)` method

**New service method** — `getOptionsExpirations(ticker)`:
Uses `getOptionsContracts` with `expiration_date_gte=today` to get all future contracts, then extracts unique `expirationDate` values to build the expiration ribbon. Called once on ticker change.

### Phase 2: Frontend Component — Complete Rewrite

This is the largest phase. The `TickerExplorerComponent` template and styles are completely rewritten.

#### Phase 2a: Expiration Date Ribbon

**What it does**: Horizontal scrollable bar showing all available expiration dates, grouped by month, with a month header row above and individual date chips below.

**UI Structure**:
```
┌──────────────────────────────────────────────────────────────────┐
│  Feb          Mar                    Apr     May    Jun   ...    │
│ [23][24][25]  [2][3][4][5][6][13]... [2][17] [15]  [18]  ...   │
└──────────────────────────────────────────────────────────────────┘
```

**Signals**:
```typescript
availableExpirations = signal<string[]>([]);    // All expiration dates
selectedExpiration = signal<string | null>(null); // Currently selected date
expirationsLoading = signal(false);

// Computed: group expirations by month for ribbon display
expirationsByMonth = computed(() => {
  // Returns: { label: 'Feb', year: 2026, dates: ['2026-02-23','2026-02-24',...] }[]
});
```

**Behavior**:
- On ticker change/submit → fetch all expirations
- Default select the nearest expiration date
- On date click → fetch chain snapshot for that expiration
- Scroll horizontally with overflow; optionally add < > arrow buttons

#### Phase 2b: Chain Table — Core Layout

**Column structure** (adapted — no bid/ask):

```
CALLS                                                    PUTS
Vega | Theta | Gamma | Delta | Price | OI | Volume | ↑Strike | IV% | Volume | OI | Price | Delta | Gamma | Theta | Vega
```

Note: TradingView shows calls columns in **reverse order** (Vega is leftmost for calls, but rightmost for puts). This creates a mirror effect where Greeks are on the outside and price/volume are near the center.

**Row structure**:
```typescript
interface ChainRow {
  strike: number;
  call: SnapshotContractResult | null;
  put: SnapshotContractResult | null;
  isAtm: boolean;
}

chainRows = computed<ChainRow[]>(() => {
  // Combine calls and puts by strike
  // Mark ATM row
  // Sort by strike ascending
});
```

**Clickable rows**: Each call/put cell is clickable. Clicking opens the historical overlay for that contract.

**ATM indicator**: A small floating label `SPY 689.43` positioned near the ATM strike row.

#### Phase 2c: Volume Bars

**What they are**: Inside each Volume cell, a horizontal bar whose width is proportional to that contract's volume relative to the max volume across all visible contracts.

**Implementation**:
```typescript
maxCallVolume = computed(() => Math.max(...chainRows().map(r => r.call?.day?.volume ?? 0)));
maxPutVolume = computed(() => Math.max(...chainRows().map(r => r.put?.day?.volume ?? 0)));

getVolumeBarWidth(volume: number | null, max: number): number {
  if (!volume || !max) return 0;
  return (volume / max) * 100; // percentage
}
```

**Template** (Tailwind classes — no custom SCSS needed):
```html
<!-- Call volume (bar grows right-to-left, aligned to right edge) -->
<td class="relative px-2 py-1 text-right cursor-pointer hover:bg-slate-700"
    (click)="openContractOverlay(call, 'call')" pTooltip="Click for history">
  <div class="absolute inset-y-0 right-0 bg-blue-500/30 rounded-l"
       [style.width.%]="getVolumeBarWidth(call?.day?.volume, maxCallVolume())">
  </div>
  <span class="relative text-slate-200 text-xs">{{ formatNumber(call?.day?.volume ?? null) }}</span>
</td>

<!-- Put volume (bar grows left-to-right, aligned to left edge) -->
<td class="relative px-2 py-1 text-left cursor-pointer hover:bg-slate-700"
    (click)="openContractOverlay(put, 'put')" pTooltip="Click for history">
  <div class="absolute inset-y-0 left-0 bg-red-500/30 rounded-r"
       [style.width.%]="getVolumeBarWidth(put?.day?.volume, maxPutVolume())">
  </div>
  <span class="relative text-slate-200 text-xs">{{ formatNumber(put?.day?.volume ?? null) }}</span>
</td>
```

The bar is `absolute` behind the text (`relative` span sits on top). `bg-blue-500/30` and `bg-red-500/30` give semi-transparent colored bars matching the TradingView style.

#### Phase 2d: Dark Theme Styling

The page uses Tailwind utility classes for all colors and layout. The `:host` block is minimal:

```scss
:host {
  display: block;
  min-height: 100vh;
}
```

The template root `<div>` carries the Tailwind dark classes:
```html
<div class="bg-slate-900 text-slate-200 min-h-screen p-5">
  ...
</div>
```

**All colors use Tailwind's `slate` / `blue` / `red` / `amber` / `emerald` palettes** — see the Tailwind Color Palette table in the setup section above. No custom hex values anywhere.

#### Phase 2e: Sort by Strike

The Strike column header has a sort icon (↑). Clicking toggles ascending/descending sort.

```typescript
strikeSortDirection = signal<'asc' | 'desc'>('asc');

sortedChainRows = computed(() => {
  const rows = this.chainRows();
  const dir = this.strikeSortDirection();
  return [...rows].sort((a, b) => dir === 'asc' ? a.strike - b.strike : b.strike - a.strike);
});
```

### Phase 3: Historical Contract Overlay (Drill-Down)

This is the key differentiating feature. Clicking any call or put cell opens an overlay panel showing that option contract's historical data.

#### 3a: Overlay Panel UI — PrimeNG `Drawer`

Uses PrimeNG `Drawer` (was `Sidebar`) for the slide-out panel. No custom CSS positioning needed.

```html
<p-drawer [(visible)]="overlayVisible()" position="right"
          [modal]="true" [style]="{ width: '520px' }"
          (onHide)="closeOverlay()">
  <ng-template #header>
    <span class="overlay-title">
      {{ selectedContract()?.ticker }} — {{ selectedContract()?.contractType }}
      ${{ selectedContract()?.strikePrice }} ({{ selectedContract()?.expirationDate }})
    </span>
  </ng-template>

  @if (overlayLoading()) {
    <p-progressSpinner [style]="{ width: '50px', height: '50px' }" />
  } @else if (overlayAggregates().length > 0) {
    <app-candlestick-chart [data]="overlayAggregates()" [ticker]="selectedContract()?.ticker ?? ''" />
    <app-volume-chart [data]="overlayAggregates()" />

    <div class="overlay-summary">
      <!-- Summary stats + contract details -->
    </div>
  } @else {
    <p>No historical data available for this contract.</p>
  }
</p-drawer>
```

**Panel contents**:
```
┌─────────────────────────────────────────────────┐
│  ✕  O:SPY260320C00690000 — Call $690 (Mar 20)  │  ← Drawer header template
│─────────────────────────────────────────────────│
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │     Candlestick Chart (price history)     │   │  ← CandlestickChartComponent
│  └──────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────┐   │
│  │     Volume Histogram                      │   │  ← VolumeChartComponent
│  └──────────────────────────────────────────┘   │
│                                                  │
│  Summary:                                        │
│  High: $8.50  Low: $0.45  Avg Vol: 12,340       │
│  Current: $5.85  OI: 21,902  IV: 12.1%          │
│                                                  │
│  Contract: Call | Strike: $690 | Exp: 2026-03-20 │
│  Underlying: SPY @ $689.43                       │
└─────────────────────────────────────────────────┘
```

#### 3b: Data Fetching

When user clicks a contract cell:

```typescript
selectedContract = signal<{
  ticker: string;           // e.g., 'O:SPY260320C00690000'
  contractType: string;     // 'call' | 'put'
  strikePrice: number;
  expirationDate: string;
  snapshot: SnapshotContractResult;  // Current snapshot data
} | null>(null);

overlayLoading = signal(false);
overlayAggregates = signal<StockAggregate[]>([]);
overlayError = signal<string | null>(null);

async openContractOverlay(contract: SnapshotContractResult, side: 'call' | 'put'): Promise<void> {
  if (!contract?.ticker) return;

  this.selectedContract.set({
    ticker: contract.ticker,
    contractType: side,
    strikePrice: contract.strikePrice!,
    expirationDate: contract.expirationDate!,
    snapshot: contract,
  });

  this.overlayLoading.set(true);
  this.overlayError.set(null);

  try {
    // Fetch historical daily bars for this option contract
    // Uses existing getOrFetchStockAggregates — works with option tickers too
    const fromDate = getMinAllowedDate(); // 2 years ago
    const toDate = new Date().toISOString().slice(0, 10);

    const result = await firstValueFrom(
      this.marketDataService.getOrFetchStockAggregates(
        contract.ticker, fromDate, toDate, 'day', 1
      )
    );

    this.overlayAggregates.set(result.aggregates);
  } catch (err) {
    this.overlayError.set(err instanceof Error ? err.message : String(err));
  } finally {
    this.overlayLoading.set(false);
  }
}

closeOverlay(): void {
  this.selectedContract.set(null);
  this.overlayAggregates.set([]);
}
```

#### 3c: Reusing Existing Chart Components

The overlay reuses the existing charting infrastructure:
- `CandlestickChartComponent` — already built for market-data page, accepts `StockAggregate[]` data
- `VolumeChartComponent` — already built, accepts same data format

These work with any ticker (stock or option) since they just render OHLCV data.

#### 3d: Overlay Styling

No custom positioning/animation CSS needed — `Drawer` handles slide-in animation, backdrop, z-index, and escape-to-close automatically. We only style the inner content:

```scss
.overlay-summary {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 16px;
  font-size: 13px;
}

.overlay-title {
  font-weight: 600;
  font-size: 14px;
}
```

Dark theme is handled globally by PrimeNG's `.app-dark` class — the Drawer inherits it automatically.

### Phase 4: Polish & UX Enhancements

#### 4a: Strike Range Filtering
Currently shows ALL strikes. TradingView shows ~10-15 around ATM. Add a control to limit visible strikes:

```typescript
strikeRange = signal(10); // Show ±10 strikes around ATM

visibleRows = computed(() => {
  const rows = this.sortedChainRows();
  const atmIdx = rows.findIndex(r => r.isAtm);
  if (atmIdx === -1) return rows;
  const range = this.strikeRange();
  return rows.slice(Math.max(0, atmIdx - range), atmIdx + range + 1);
});
```

#### 4b: Auto-Scroll to ATM
When chain loads, scroll the table so the ATM row is centered in the viewport.

#### 4c: Underlying Summary Bar
Replace the current card with a compact bar that includes:
- Ticker + price
- Change + change%
- Number of contracts
- Last updated timestamp

#### 4d: Loading States
Skeleton loader or shimmer effect while chain data is fetching. The expiration ribbon stays interactive even while the chain table reloads.

#### 4e: Click Hints
Subtle hover effect on call/put cells to indicate they're clickable. Cursor changes to pointer. Tooltip: "Click for historical data".

---

## Component Structure

```
Frontend/src/app/components/options-chain-v2/
├── options-chain.component.ts       # Main component (replaces ticker-explorer)
├── options-chain.component.html     # Template (includes p-drawer for overlay)
├── options-chain.component.scss     # Dark-themed styles
└── expiration-ribbon/
    ├── expiration-ribbon.component.ts
    ├── expiration-ribbon.component.html
    └── expiration-ribbon.component.scss
```

**Why this structure?**
- `expiration-ribbon`: Complex scrolling + month grouping logic with `ScrollPanel` + `Chip`/`SelectButton` — warrants extraction as a child component with `input()`/`output()` for selection
- **No separate `contract-overlay/`**: The overlay is just a `<p-drawer>` in the main template with a few chart components inside — not enough logic to justify a separate component
- **No separate `volume-bar/`**: Volume bars are pure CSS `<div>` elements inside table cells — a dedicated component would add overhead for what's essentially a styled `[style.width.%]` binding
- Main component: Ticker search (`InputText`), chain table (`Table`), overlay (`Drawer`), data fetching, orchestration

---

## Data Flow

```
User enters ticker → fetchExpirations(ticker)
                          ↓
              Polygon: list_options_contracts(underlying_ticker, expiration_date_gte=today)
                          ↓
              Extract unique expiration_dates → populate ribbon
                          ↓
              Auto-select nearest expiration date
                          ↓
User clicks expiration → fetchChainSnapshot(ticker, expiration)
                          ↓
              Polygon: list_snapshot_options_chain(ticker, expiration)
                          ↓
              Returns: underlying info + contracts with Greeks, IV, OI, day OHLCV
                          ↓
              Build chainRows: group by strike, pair calls/puts, find ATM
                          ↓
              Render table with volume bars, ATM highlight, dark theme
                          ↓
User clicks a call/put cell → openContractOverlay(contract)
                          ↓
              Polygon: fetch_aggregates(option_ticker, day, 2yr range)
                          ↓
              Slide-out panel with candlestick chart + volume + stats
```

---

## Files to Create

| File | Purpose | PrimeNG Components Used |
|------|---------|------------------------|
| `options-chain-v2/options-chain.component.ts` | Main component — table, overlay, data fetching | `Table`, `Drawer`, `Toolbar`, `Tag`, `InputText`, `InputNumber`, `Skeleton`, `ProgressSpinner`, `Tooltip` |
| `options-chain-v2/options-chain.component.html` | Template — chain table + `<p-drawer>` for overlay | — |
| `options-chain-v2/options-chain.component.scss` | Dark theme overrides, volume bar CSS, layout | — |
| `options-chain-v2/expiration-ribbon/expiration-ribbon.component.ts` | Expiration date ribbon — scroll + select | `ScrollPanel`, `Chip` or `SelectButton` |
| `options-chain-v2/expiration-ribbon/expiration-ribbon.component.html` | Ribbon template with month groups | — |
| `options-chain-v2/expiration-ribbon/expiration-ribbon.component.scss` | Ribbon styles — chip sizing, month headers | — |

All paths relative to `Frontend/src/app/components/`.

## Files to Modify

| File | Change |
|------|--------|
| `Frontend/src/app/services/market-data.service.ts` | Add `getOptionsExpirations()` method |
| `Frontend/src/app/app.routes.ts` | Update `/options-chain` to load new component |
| `Frontend/src/app/app.component.ts` | Update menu item if label changes |
| `Frontend/src/app/app.config.ts` | Add `darkModeSelector: '.app-dark'` to PrimeNG config |
| `Frontend/src/styles.css` | Add `@import "tailwindcss";` at top |

## Files to Create (Infrastructure)

| File | Content |
|------|---------|
| `Frontend/.postcssrc.json` | `{ "plugins": { "@tailwindcss/postcss": {} } }` |

**Note**: No backend or Python changes needed. The existing snapshot API already returns Greeks, IV, OI, and day OHLCV. The historical drill-down uses the existing `getOrFetchStockAggregates` query which already works with option tickers.

---

## Execution Order

```
Phase 0: Install Tailwind CSS v4 + configure PostCSS               (small)
Phase 1: Service — add expirations method                          (small)
Phase 2a: Expiration ribbon (ScrollPanel + Chip/SelectButton)      (medium)
Phase 2b: Chain table core layout (p-table + pSortableColumn)      (large)
Phase 2c: Volume bars (Tailwind absolute/relative divs in cells)   (small)
Phase 2d: Dark theme — .app-dark toggle + Tailwind slate palette   (medium)
Phase 2e: Sort by strike (pSortableColumn — mostly free)           (small)
Phase 3a: Drawer overlay + historical data fetching                (medium)
Phase 3b: Chart integration in Drawer (reuse existing components)  (small)
Phase 4: Polish — InputNumber range, ATM scroll, Tooltip hints     (medium)
```

**Total: ~6 new files, ~3 modified files**

Significantly fewer files than the original plan because PrimeNG handles the overlay (`Drawer`), scrolling (`ScrollPanel`), and loading states (`Skeleton`, `ProgressSpinner`) that would otherwise require custom components.

---

## What We're NOT Building (Scope Limits)

| Feature | Why Not | Future? |
|---------|---------|---------|
| Bid/Ask columns | Polygon Starter plan does not include `last_quote` data | Upgrade to higher plan |
| Strategy builder tab | Massive scope — separate feature | Yes, Level 3 |
| Strategy finder | Requires pattern matching engine | Yes, Level 3 |
| Volatility tab (smile/skew chart) | Separate visualization feature | Good candidate for next iteration |
| By strike view toggle | Different data grouping — defer | Later |
| Full historical chain reconstruction | Would need to fetch aggs for every contract at every strike — too many API calls | Could batch with caching |
| Real-time WebSocket updates | Requires WS infrastructure | Future |

---

## Key Technical Decisions

1. **No backend changes**: The existing API surface is sufficient. Snapshot gives us Greeks/IV/OI/OHLCV, aggregates gives us historical data. No new endpoints needed.

2. **Bid/Ask omitted**: Polygon Starter plan docs explicitly state "`last_quote` is only returned if your current plan includes quotes." We replace Bid/Ask columns with **Open Interest** which is available and arguably more useful for chain analysis.

3. **Expiration discovery**: Use `list_options_contracts` (not snapshot) to find all expirations, because snapshot only returns live data for one expiration at a time. The contracts endpoint can return metadata for ALL future expirations quickly.

4. **Dark theme via `.app-dark` + Tailwind**: Add `darkModeSelector: '.app-dark'` to PrimeNG config. The options chain component toggles this class on `<html>` during init/destroy. PrimeNG components auto-switch to dark palette. All custom elements use Tailwind's `slate`/`blue`/`red`/`amber`/`emerald` color palettes — no custom hex values anywhere in the SCSS.

5. **`Drawer` for historical overlay**: PrimeNG's `Drawer` (`position="right"`) replaces ~40 lines of custom CSS for slide-in animation, backdrop, z-index, escape-to-close. The overlay content is just chart components + summary text — not enough to warrant a separate Angular component.

6. **`p-table` for chain, not raw `<table>`**: Gets us `pSortableColumn` + `p-sortIcon` for free, `pTemplate` for custom header/body rows, `[scrollable]` with `scrollHeight` for fixed-height viewport with scroll, and `[ngClass]` row styling for ATM/ITM highlighting. No virtual scroll needed — typical chain has 20-60 strikes.

7. **Volume bar rendering**: Tailwind `absolute`/`relative` divs (not `ProgressBar`). `ProgressBar` can't render right-to-left bars for the calls side mirror layout. `bg-blue-500/30` and `bg-red-500/30` with `[style.width.%]` binding gives full directional control.

8. **`ScrollPanel` + `Chip` for expiration ribbon**: `ScrollPanel` provides the horizontal overflow container. Within it, dates grouped by month, each rendered as a `Chip` component. `SelectButton` is an alternative but `Chip` gives individual click handling and more visual flexibility per date.

9. **ATM detection**: Compare underlying price from snapshot to strikes. Nearest strike = ATM. Show `Tag` component with `severity="warn"` positioned near the ATM strike row.

10. **Historical overlay reuses existing charts**: `CandlestickChartComponent` and `VolumeChartComponent` already accept `StockAggregate[]`. Option tickers work with the same `getOrFetchStockAggregates` GraphQL query.

11. **Component naming**: `options-chain-v2` directory to avoid conflicts during development. Route updated at the end to swap in the new component.
