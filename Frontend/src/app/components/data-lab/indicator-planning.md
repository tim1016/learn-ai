# Data Lab — Improvement Plan

**Date:** 2026-04-04
**Scope:** UI, UX, documentation, validation, and architecture improvements
**Current state:** 35 indicators implemented (backend + frontend), session persistence, multi-timeframe chart with lightweight-charts, CSV export + validation report

---

## Current Inventory (What's Already Working)

| Area | Status |
|------|--------|
| 35 indicators computed via pandas-ta | ✅ |
| Dynamic indicator catalog from `/api/dataset/available` | ✅ |
| Multi-instance indicators with configurable params | ✅ |
| lightweight-charts with overlay + sub-panel rendering | ✅ |
| Two-layer LRU+TTL caching (resample + indicators) | ✅ |
| Session save/load/rename/delete (GraphQL → .NET) | ✅ |
| Chart snapshot persistence in sessions | ✅ |
| CSV export with streaming | ✅ |
| Metadata JSON + Column Descriptions CSV export | ✅ |
| Validation report (pandas-ta vs TradingView) | ✅ |
| 12 indicators documented with LaTeX formulas | ✅ |
| EMA ribbon with auto-color scale | ✅ |
| Forward-fill, warm-up, RTH/extended session filters | ✅ |
| Date pickers with market holidays | ✅ |
| Timeframe selection with bar-count validation | ✅ |

---

## Phase 1 — Documentation Hardening (High Priority)

The docs component currently covers 12 indicators. 23 are undocumented. This phase brings all 35 to parity and makes output schemas discoverable.

### 1.1 Complete Indicator Documentation (All 35)

**What:** Add `IndicatorDoc` entries to `data-lab-docs.component.ts` for every missing indicator.

**Missing indicators (23):**

| # | Indicator | Key | Category |
|---|-----------|-----|----------|
| 1 | SMA | `sma` | Overlay |
| 2 | DEMA | `dema` | Overlay |
| 3 | TEMA | `tema` | Overlay |
| 4 | WMA | `wma` | Overlay |
| 5 | HMA | `hma` | Overlay |
| 6 | KAMA | `kama` | Overlay |
| 7 | ZLMA | `zlma` | Overlay |
| 8 | RMA | `rma` | Overlay |
| 9 | ALMA | `alma` | Overlay |
| 10 | VWAP | `vwap` | Overlay |
| 11 | Keltner Channel | `kc` | Overlay |
| 12 | Donchian Channel | `donchian` | Overlay |
| 13 | Stochastic RSI | `stochrsi` | Sub-panel |
| 14 | Williams %R | `willr` | Sub-panel |
| 15 | ROC | `roc` | Sub-panel |
| 16 | Momentum | `mom` | Sub-panel |
| 17 | NATR | `natr` | Sub-panel |
| 18 | A/D Line | `ad` | Sub-panel |
| 19 | CMF | `cmf` | Sub-panel |
| 20 | MFI | `mfi` | Sub-panel |
| 21 | TSI | `tsi` | Sub-panel |
| 22 | Fisher Transform | `fisher` | Sub-panel |
| 23 | Squeeze | `squeeze` | Sub-panel |

**Per indicator, add:**

```ts
{
  name: 'sma',
  displayName: 'Simple Moving Average (SMA)',
  formulaLatex: '\\text{SMA} = \\frac{1}{n}\\sum_{i=0}^{n-1} C_{t-i}',
  description: 'Arithmetic mean of last n closes. Slowest-reacting MA but widely used as trend filter.',
  library: 'pandas-ta (ta.sma)',
  outputColumns: ['sma_{length}'],
  defaultParams: 'length = 20',
}
```

**Effort:** ~2–3 hours (formulae are standard, just transcribing)

### 1.2 Add Output Column Schema to Catalog API

**What:** Extend `/api/dataset/available` and `/api/chart/available-indicators` to return `output_columns` per indicator so the frontend never needs to hardcode column names.

**Backend change in `dataset_service.py`:**

```python
# In INDICATOR_CONFIGS, add output_columns field:
'macd': {
    'params': [...],
    'output_columns': ['macd_{fast}_{slow}_{signal}', 'macdh_{fast}_{slow}_{signal}', 'macds_{fast}_{slow}_{signal}'],
    'multi_output': True,
    'category': 'oscillator',
}
```

**Frontend benefit:** The `estimatedColumns` computed signal in `data-lab.component.ts` currently hardcodes multi-output detection for bbands, macd, supertrend, stoch, aroon, kc, donchian, adx. This can become fully dynamic.

**Effort:** ~2 hours backend, ~1 hour frontend refactor

### 1.3 Add "Known Behaviors" Section to Docs

**What:** Surface the data behavior notes (VWAP outside H/L, Supertrend NaN patterns, flat bars, Saturday UTC bars, forward-fill gaps) directly in the docs UI.

Currently these are tribal knowledge. Add a "Data Notes" accordion section in `data-lab-docs.component.html` with each behavior documented.

**Effort:** ~1 hour

---

## Phase 2 — Chart UX Improvements (High Priority)

### 2.1 Indicator Legend Panel

**Problem:** Once you have 8 EMA lines + Bollinger Bands + Supertrend on the chart, it's hard to know what's what. The EMA ribbon auto-colors help, but there's no persistent legend.

**Solution:** Add a collapsible legend overlay on the chart showing:

- Indicator name + params (e.g., "EMA(50)")
- Color swatch matching the line
- Eye icon to toggle visibility per series
- Click to highlight (increase opacity, dim others)

**Implementation:**

- Add a `<div class="chart-legend">` overlay in `data-lab-chart.component.html`
- Populate from `ChartIndicatorResult[]` that's already available
- Wire visibility toggles to `series.applyOptions({ visible })` on lightweight-charts

**Effort:** ~4 hours

### 2.2 Crosshair Data Panel (Tooltip)

**Problem:** The crosshair syncs across panels but doesn't show indicator values at the hovered bar.

**Solution:** Add a floating data panel that shows:

- OHLCV values for the hovered bar
- All indicator values at that timestamp
- Color-coded to match chart lines

**Implementation:**

- Subscribe to `chart.subscribeCrosshairMove()`
- Read data points from each series at the logical index
- Render in an absolutely-positioned overlay div

**Effort:** ~3 hours

### 2.3 Sub-Panel Reference Lines

**Problem:** Oscillators like RSI (30/70), Stochastic (20/80), CCI (-100/+100) need horizontal reference lines to be useful. Currently they render as plain line charts without overbought/oversold zones.

**Solution:** For each sub-panel, draw reference lines based on indicator type:

| Indicator | Reference Lines |
|-----------|----------------|
| RSI | 30, 70 |
| Stochastic | 20, 80 |
| StochRSI | 20, 80 |
| CCI | -100, +100 |
| Williams %R | -20, -80 |
| MFI | 20, 80 |
| ADX | 25 |

**Implementation:**

- Use `chart.addLineSeries()` with flat data at the reference value
- Style as dashed, low-opacity lines
- Define a `REFERENCE_LINES` map keyed by indicator name

**Effort:** ~2 hours

### 2.4 MACD Histogram Color Coding

**Problem:** MACD histogram should use 4 colors (rising positive, falling positive, falling negative, rising negative) to show momentum direction, not just a single color.

**Solution:** Compute histogram delta and apply per-bar colors:

```ts
// green:       hist > 0 && hist > prev_hist (rising positive)
// light green: hist > 0 && hist <= prev_hist (falling positive)
// red:         hist < 0 && hist < prev_hist (falling negative)
// light red:   hist < 0 && hist >= prev_hist (rising negative)
```

**Effort:** ~1 hour

### 2.5 Volume Bar Color Coding

**Problem:** If volume bars aren't already colored by bullish/bearish candles, they should be.

**Solution:** Color each volume histogram bar based on whether close >= open (bull = green) or close < open (bear = red). Already partially done with `DARK.bull`/`DARK.bear` — verify it's applied per-bar, not as a series default.

**Effort:** ~30 min (verification + fix if needed)

---

## Phase 3 — Indicator Management UX (Medium Priority)

### 3.1 Indicator Search / Filter

**Problem:** The indicator catalog shows all 151 pandas-ta indicators across 9 categories. Finding a specific one requires expanding categories and scrolling.

**Solution:** Add a search input above the catalog that filters indicators by name or description in real-time.

```html
<input type="text" placeholder="Search indicators..." (input)="filterIndicators($event)" />
```

**Effort:** ~1 hour

### 3.2 Indicator Presets / Templates

**Problem:** Setting up a common indicator combination (e.g., "Trend following: EMA ribbon + Supertrend + ADX + MACD") requires many clicks.

**Solution:** Add preset templates that load a curated set of indicators with one click:

| Preset | Indicators |
|--------|-----------|
| **Default** | 8 EMAs, BBands, Supertrend, MACD |
| **Trend Following** | EMA(20,50,200), Supertrend, ADX, MACD |
| **Mean Reversion** | BBands, RSI, Stochastic, CCI |
| **Volume Analysis** | VWAP, OBV, CMF, MFI, A/D |
| **Volatility** | BBands, ATR, Keltner, Squeeze, NATR |
| **Momentum** | RSI, MACD, StochRSI, ROC, TSI |

**Implementation:**

- Define presets as arrays of `IndicatorEntry[]`
- Add a dropdown or chip bar above the indicator catalog
- "Apply preset" replaces current entries (with confirmation if entries exist)

**Effort:** ~2 hours

### 3.3 Drag-and-Drop Indicator Reordering

**Problem:** Active indicators in the entries grid can't be reordered. Order affects CSV column sequence.

**Solution:** Add drag handles to `entry-card` elements using Angular CDK DragDrop.

**Effort:** ~2 hours

### 3.4 Indicator Grouping in Active Panel

**Problem:** With many active indicators, the entries grid is a flat list. Hard to distinguish overlays from oscillators.

**Solution:** Group active entries by category (Overlay vs Sub-panel) with section headers.

**Effort:** ~1 hour

---

## Phase 4 — Validation & Quality (Medium Priority)

### 4.1 Automated Validation for All 35 Indicators

**Problem:** The validation report (pandas-ta vs TradingView) is manual — user uploads two CSVs. No way to know which indicators have been validated and which haven't.

**Solution:** Add a validation status tracker:

- Markdown file or DB table tracking which indicators have been validated
- Status: ✅ Validated (match rate), ⚠️ Known divergence, ❌ Not yet validated
- Surface this status in the docs and indicator catalog UI

**Effort:** ~3 hours

### 4.2 Per-Indicator Validation Notes

**What:** For each validated indicator, document:

- Match rate vs TradingView (e.g., "99.2% within ±0.01")
- Known divergence causes (e.g., "TradingView uses different ALMA sigma default")
- Specific parameter combinations tested

**Where:** Add a `validationNotes` field to `IndicatorDoc` and render it in the docs accordion.

**Effort:** ~2 hours (documentation work, assumes validation data exists)

### 4.3 Quality Report Enhancements

**Problem:** The `QualityReport` in the chart response covers bar-level quality but not indicator-level quality.

**Solution:** Extend the quality report to include:

- Per-indicator NaN count and NaN percentage
- Warm-up bar count per indicator (how many bars until first valid value)
- Flag indicators where >50% of values are NaN (likely misconfigured length vs date range)

**Backend change:** Add `indicator_quality` to the chart response:

```python
{
  "indicator_quality": {
    "ema_200": {"nan_count": 199, "nan_pct": 2.1, "warmup_bars": 199},
    "supertl_10_3.0": {"nan_count": 4800, "nan_pct": 48.0, "note": "NaN during downtrends by design"}
  }
}
```

**Effort:** ~3 hours

---

## Phase 5 — Session & Export Improvements (Medium Priority)

### 5.1 Session Comparison

**Problem:** Can't easily compare two sessions (e.g., same ticker, different date ranges or indicator configs).

**Solution:** Add a "Compare" button that loads two sessions side-by-side, syncing their time scales if the date ranges overlap.

**Effort:** ~6 hours (significant chart layout work)

### 5.2 Export Enhancements

**What:** Add export options beyond CSV:

| Format | Use Case |
|--------|----------|
| **Parquet** | ML pipelines, faster load in Python/pandas |
| **JSON (OHLCV+indicators)** | API consumers |
| **Chart image (PNG)** | Reports, sharing |

**Implementation:**

- Parquet: `df.to_parquet()` in `dataset_service.py` — add a format param to the generate endpoint
- JSON: Already halfway there with metadata endpoint — add a full data JSON option
- PNG: `chart.takeScreenshot()` from lightweight-charts API

**Effort:** ~4 hours

### 5.3 Session Auto-Save

**Problem:** If you configure a complex indicator set and the browser crashes, you lose everything unless you manually saved a session.

**Solution:** Auto-save the current config to localStorage on every change (debounced 2s). On load, check for unsaved config and offer to restore.

**Effort:** ~2 hours

---

## Phase 6 — Performance & Architecture (Lower Priority)

### 6.1 Indicator Computation Parallelism

**Problem:** `calculate_dynamic_indicators()` computes indicators sequentially.

**Solution:** Use `concurrent.futures.ThreadPoolExecutor` to compute independent indicators in parallel. pandas-ta releases the GIL for numpy operations, so threading helps.

```python
with ThreadPoolExecutor(max_workers=4) as executor:
    futures = {executor.submit(calc_one, entry): entry for entry in entries}
    for future in as_completed(futures):
        result_df = future.result()
        df = pd.concat([df, result_df], axis=1)
```

**Effort:** ~2 hours

### 6.2 WebSocket for Real-Time Updates

**Problem:** Currently all data fetching is request/response. For live trading scenarios, the chart goes stale.

**Solution:** Add a WebSocket endpoint that pushes new bars + indicator updates as they arrive from the data source. lightweight-charts supports `series.update()` for real-time appends.

**Effort:** ~8–12 hours (significant new feature, requires data source with streaming support)

### 6.3 Cache Warming

**Problem:** First chart load for a ticker+range is slow because there's no cache.

**Solution:** Add a "warm cache" button or background job that pre-fetches common tickers at common timeframes on startup.

**Effort:** ~3 hours

### 6.4 Indicator Config Validation

**Problem:** Users can enter any value in indicator parameter inputs. Setting EMA length to 0 or -1 will crash pandas-ta.

**Solution:** The backend `INDICATOR_CONFIGS` already has `min`/`max` bounds. Add frontend enforcement:

- HTML `min`/`max` attributes (already present in template)
- Add explicit validation in `updateEntryParam()` — clamp values, show warning toast
- Backend: add validation before calling pandas-ta, return 400 with descriptive error

**Effort:** ~2 hours

---

## Phase 7 — Advanced Features (Lower Priority / Future)

### 7.1 Custom Indicator Builder

**What:** Let users define custom indicators using a formula DSL or Python expression, computed server-side.

**Example:** `custom_spread = ema(close, 10) - ema(close, 50)`

**Effort:** ~8–12 hours (formula parser, security sandboxing)

### 7.2 Indicator Alerts

**What:** Define alert conditions (e.g., "RSI crosses above 70") that trigger notifications.

**Effort:** ~6–8 hours (needs alert evaluation engine + notification system)

### 7.3 Multi-Ticker Overlay

**What:** Overlay multiple tickers on the same chart for correlation analysis.

**Effort:** ~6 hours (data normalization, color management)

### 7.4 Backtesting Framework

**What:** Define entry/exit rules using indicators and run a backtest over historical data.

**Effort:** ~20+ hours (separate project scope)

---

## Implementation Priority Matrix

| Phase | Effort | Impact | Priority |
|-------|--------|--------|----------|
| **Phase 1** — Documentation | ~6 hrs | High (usability, trust) | 🔴 Do first |
| **Phase 2** — Chart UX | ~10 hrs | High (daily workflow) | 🔴 Do first |
| **Phase 3** — Indicator Mgmt | ~6 hrs | Medium (convenience) | 🟡 Next sprint |
| **Phase 4** — Validation | ~8 hrs | Medium (confidence) | 🟡 Next sprint |
| **Phase 5** — Session/Export | ~12 hrs | Medium (workflow) | 🟡 Next sprint |
| **Phase 6** — Performance | ~15 hrs | Low-Med (scale) | 🟢 When needed |
| **Phase 7** — Advanced | ~40+ hrs | Low (nice-to-have) | 🔵 Future |

**Total estimated effort:** ~60 hours for Phases 1–5, ~55+ hours for Phases 6–7

---

## Suggested First Sprint (1–2 Weeks)

1. ✅ Complete all 35 indicator docs with formulas (Phase 1.1)
2. ✅ Add output column schema to API (Phase 1.2)
3. ✅ Indicator legend panel on chart (Phase 2.1)
4. ✅ Crosshair data panel (Phase 2.2)
5. ✅ Sub-panel reference lines (Phase 2.3)
6. ✅ MACD histogram 4-color coding (Phase 2.4)
7. ✅ Indicator search (Phase 3.1)

These 7 items cover the biggest usability gaps and can be completed in roughly 15–18 hours of focused work.