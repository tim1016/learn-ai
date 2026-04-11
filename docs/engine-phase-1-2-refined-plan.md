# Engine Lab — Phase 1 & 2 Refined Implementation Plan

**Date:** 2026-04-10  
**Direction:** The Engine Lab becomes the single home for backtesting. Strategy Lab is deprecated. All desirable Strategy Lab features (charts, replay, LEAN statistics) migrate into the Engine. Phase 1 & 2 (Insights + Alpha Models) are built directly into the Engine, not the Strategy Lab.

---

## Current State: Strategy Lab vs Engine Lab

### Strategy Lab (DEPRECATED after this work)

The Strategy Lab has two modes and several visual components:

**Backtest Mode** — runs a strategy via the `/api/backtest/run` endpoint, which goes through the .NET `BacktestService` and returns:
- Candlestick chart with trade markers (entry/exit arrows) via `StrategyLabChartComponent` (Lightweight Charts)
- Indicator overlays (SMA, EMA lines drawn over the price chart)
- Volume sub-panel below the candlestick chart
- Additional sub-panels for RSI, MACD, etc. (driven by `ChartIndicatorResult[]`)
- Quality report modal (gaps, missing sessions, flat bars)
- LEAN Statistics panel (25+ portfolio metrics, trade stats) via `LeanStatisticsComponent`
- Trade log table with expandable indicator snapshots
- Cumulative PnL from trade list
- ZIP download of full backtest data

**Replay Mode** — loads historical bars and plays them forward one-by-one:
- `ReplayControlsComponent`: Play/Pause, speed selection (1x, 2x, 5x, 10x, 50x), seek slider, progress bar, current timestamp display
- `ReplayChartComponent`: Candlestick chart that builds bar-by-bar as replay progresses, with indicator overlays and trade markers appearing as they happen
- `ReplayEngineService`: Manages the bar-by-bar playback state machine (load, play, pause, seekToPercent, step, reset)
- `ReplayIndicatorService`: Loads pre-computed indicator series, slices them to the current replay position
- `ReplayStrategyService`: Loads trades, filters to those visible at the current replay time, tracks active position

### Engine Lab (CURRENT)

The Engine Lab runs strategies via the `/api/engine/backtest` endpoint using the Python LEAN-parity engine. It currently has:
- Dynamic strategy selection from a JSON Schema-driven registry
- Dynamic parameter form (auto-generated from strategy's `params_schema`)
- Data availability checker (shows what's on disk vs what needs fetching)
- Resolution toggle (minute / daily)
- Fill mode selector (signal_bar_close / next_bar_open)
- Commission per order control
- `EngineResultsComponent`: Summary KPI cards (initial cash, final equity, net profit, fees, trades, win rate) + LEAN Statistics panel + trade log table + CSV download
- `EngineHistoryComponent`: Lists past backtest runs from the database
- `LeanEngineDocsComponent`: Inline documentation tab
- Quick date range presets (1D, 7D, 15D, 1M, 3M, 6M, 12M, 2Y)
- Auto-fetch toggle for pulling missing Polygon data on the fly

**What's missing from Engine Lab compared to Strategy Lab:**
1. No candlestick chart with trade markers
2. No indicator overlay visualization
3. No volume chart
4. No sub-panels (RSI, MACD, etc.)
5. No replay mode
6. No equity curve chart
7. No quality report modal
8. No ZIP download

---

## What We're Building: The Enhanced Engine Lab

After Phase 1 & 2, the Engine Lab becomes a complete strategy analysis workspace with three major sections:

### Section A: Run & Configure (existing, enhanced)
What exists today — strategy picker, parameters, date range, fill mode, commission — plus new Alpha Model selection.

### Section B: Results & Charts (migrate from Strategy Lab + new Insight panels)
Everything visual that makes the Strategy Lab useful, plus entirely new Insight-driven analysis.

### Section C: Replay (migrate from Strategy Lab)
Bar-by-bar replay with insight emission visualization.

---

## Phase 1: Insight Data Model + Backend Integration

### Backend Work (Python)

**New files:**
```
PythonDataService/app/engine/framework/
├── __init__.py
├── insight.py           # Insight, InsightType, InsightDirection, InsightScore
├── insight_manager.py   # InsightManager — tracks, scores, and reports on insights
└── insight_scorer.py    # DefaultInsightScoreFunction
```

**Changes to existing files:**
- `engine/strategy/base.py` — Add `emit_insight()` to `StrategyContext`, add `InsightManager` to context
- `engine/engine.py` — Add insight scoring step in the main loop; include insights in `BacktestResult`
- `engine/strategy/algorithms/spy_ema_crossover.py` — Emit insights alongside existing `set_holdings()` calls (dual-mode: trades + predictions)
- `routers/engine.py` — Extend the `/api/engine/backtest` response to include `insights[]` and `insight_summary`

**New API response shape (additions to `EngineBacktestResponse`):**
```json
{
  "insights": [
    {
      "id": "abc-123",
      "symbol": "SPY",
      "direction": "up",
      "period_minutes": 75,
      "magnitude": 0.00042,
      "confidence": 0.72,
      "generated_time": "2024-06-03T10:30:00",
      "close_time": "2024-06-03T11:45:00",
      "reference_value": 526.92,
      "reference_value_final": 527.59,
      "source_model": "EmaCross_5_10_RSI14",
      "tag": "EMA5=526.8127 EMA10=526.5432 RSI=58.71",
      "score": {
        "direction": 1.0,
        "magnitude": 0.38,
        "is_final": true
      }
    }
  ],
  "insight_summary": {
    "total_insights": 63,
    "direction_accuracy": 0.698,
    "avg_magnitude_score": 0.42,
    "avg_confidence_emitted": 0.67,
    "insights_by_quarter": { ... },
    "confidence_calibration": [ ... ],
    "time_of_day_accuracy": { ... }
  }
}
```

### What This Gives the User

**Before Phase 1:** "I ran SPY EMA Crossover. It made 63 trades, 69.8% win rate, 11.27% return."

**After Phase 1:** All of the above, PLUS:

> "Of those 63 trades, the strategy made 63 structured predictions. 44 were directionally correct (69.8%). The model predicted an average magnitude of 0.042% per trade, but actual winning moves averaged 0.087% — it consistently underestimates how big the wins will be. Confidence calibration shows the model is slightly underconfident in the 0.60-0.70 band (actual win rate 72% vs stated 65%). Morning predictions (9:30-11:00) were 78% accurate vs 60% accuracy during the lunch hour."

This transforms the results page from a report card into a diagnostic tool.

---

## Phase 2: Alpha Models + Engine Charts + Replay Migration

### Backend Work (Python)

**New files:**
```
PythonDataService/app/engine/framework/alpha/
├── __init__.py
├── alpha_model.py              # AlphaModel base class
├── ema_cross_alpha.py          # SpyEmaCrossover as Alpha Model
├── rsi_reversal_alpha.py       # RSI Reversal as Alpha Model  
├── composite_alpha.py          # Run multiple alphas together
└── registry.py                 # Alpha model registry (extends strategy registry)
```

**Changes to existing:**
- `routers/engine.py` — New endpoint `/api/engine/backtest` accepts `alpha_models[]` parameter for composite runs; extend response with `chart_data` (OHLCV bars + indicators for chart rendering)
- Strategy registry — Alpha Models are registered alongside classic strategies; the UI shows both

**New API response additions for chart data:**
```json
{
  "chart_bars": [
    { "t": 1717416600, "o": 526.50, "h": 527.10, "l": 526.30, "c": 526.92, "v": 12500 }
  ],
  "chart_indicators": [
    { "id": "ema5", "panel": "main", "type": "line", "color": "#ff9800", "data": [...] },
    { "id": "rsi14", "panel": "rsi", "type": "line", "color": "#2196f3", "data": [...] }
  ],
  "equity_curve": [
    { "t": 1717416600, "equity": 100000, "cash": 100000, "holdings": 0 }
  ]
}
```

### Frontend Work (Angular) — The Enhanced Engine Lab UI

Here's what the user sees, tab by tab:

#### Tab 1: Configure & Run (enhanced existing)

Current Engine Lab form + two additions:
- **Alpha Model selector** — dropdown that shows both classic strategies (SpyEmaCrossover, SMA Crossover, etc.) and Alpha Models (EmaCross Alpha, RSI Alpha, Composite). When an Alpha Model is selected, additional dropdowns appear for Portfolio Construction and Risk Management (Phase 3 & 4 placeholders, defaulting to "Direct Execution" for now)
- **Composite mode toggle** — when enabled, lets the user pick 2-3 Alpha Models to run simultaneously

#### Tab 2: Results (enhanced existing + new Insight panels)

This tab becomes the core analysis workspace. It's organized in collapsible sections:

**Section 2a: KPI Summary Cards** (existing, kept as-is)
- Initial Cash → Final Equity → Net Profit → Total Fees → Trades → Win Rate
- These are your at-a-glance numbers

**Section 2b: Strategy Chart** (MIGRATED from Strategy Lab `StrategyLabChartComponent`)
This is the big visual payoff — the Lightweight Charts candlestick chart that the Strategy Lab has today, now inside the Engine:
- Candlestick price chart with trade entry/exit markers (green △ for entry, red ▽ for exit)
- Indicator overlays on the main chart (EMA5, EMA10 lines)
- Volume histogram below the price chart
- Sub-panels for RSI, MACD, etc.
- Quality report button (gap detection, missing sessions)

**NEW: Insight markers on the chart** — each Insight is drawn as a colored band on the price chart:
- Green semi-transparent band = UP prediction that was correct
- Red semi-transparent band = UP prediction that was wrong
- Band spans from `generated_time` to `close_time`
- Hovering a band shows: direction, magnitude, confidence, score

This is the single most powerful visual addition — you can now SEE when your model predicted correctly vs incorrectly, overlaid directly on the price action.

**Section 2c: Equity Curve** (MIGRATED from Strategy Lab + enhanced)
- Line chart of portfolio equity over time
- Drawdown shading below the equity line
- Trade markers on the equity curve showing where each trade's PnL landed

**Section 2d: Insight Analysis Panel** (NEW — the Phase 1 payoff)

This is entirely new and doesn't exist in the Strategy Lab. It's a collapsible panel below the charts with several sub-views:

**Insight Accuracy Table:**
```
#   | Time          | Symbol | Direction | Magnitude | Confidence | Actual    | Dir Score | Mag Score
1   | 2024-04-01 10:15 | SPY | UP       | 0.042%    | 0.72       | +0.087%   | 1.0       | 0.52
2   | 2024-04-01 14:30 | SPY | UP       | 0.038%    | 0.65       | -0.021%   | 0.0       | 0.0
...
```
Each row is one Insight with its prediction and actual outcome. Sortable, filterable, downloadable as CSV.

**Confidence Calibration Chart:**
A line chart with confidence buckets (0.5-0.6, 0.6-0.7, etc.) on the X-axis and actual win rate on the Y-axis. A perfect model follows the diagonal. Above the diagonal = underconfident. Below = overconfident. This tells the user whether to trust the model's confidence scores for position sizing.

**Direction Accuracy Over Time:**
A rolling window chart showing what percentage of the last 10 (or 20) predictions were directionally correct. Helps spot when the model starts degrading mid-backtest.

**Time-of-Day Heatmap:**
A heatmap showing prediction accuracy by hour of day and day of week. Reveals temporal patterns — "this strategy is 78% accurate in the morning but only 60% after lunch."

**Magnitude Analysis:**
- Scatter plot: predicted magnitude (X) vs actual magnitude (Y)
- Shows whether the model over- or under-estimates moves
- Regression line shows the systematic bias

**Section 2e: LEAN Statistics** (existing `LeanStatisticsComponent`, kept as-is)
The full 25+ field portfolio statistics panel that already works.

**Section 2f: Trade Log** (existing, enhanced)
Current trade table + new column for "Insight Score" showing the direction/magnitude score for each trade's associated insight.

#### Tab 3: Replay (MIGRATED from Strategy Lab)

The replay mode moves wholesale from Strategy Lab into Engine Lab. Components to migrate:
- `ReplayControlsComponent` → `engine/replay-controls/`
- `ReplayChartComponent` → `engine/replay-chart/`
- `ReplayEngineService` (already a standalone service, no move needed)
- `ReplayIndicatorService` (same)
- `ReplayStrategyService` (same)

**NEW in Engine Replay: Insight Emission Visualization**

As the replay plays forward bar-by-bar, insights appear in real time:
- When the Alpha Model emits an insight, a glowing marker appears on the chart with the predicted direction
- The insight band begins drawing forward from the emission point
- When the insight period expires, the band fills in green (correct) or red (wrong)
- A running accuracy counter in the replay controls shows: "Predictions: 12 | Correct: 9 | Accuracy: 75%"

This transforms replay from "watch the candles build" into "watch the model think, predict, and get scored in real time." It's the most engaging way to understand how the strategy behaves.

#### Tab 4: History (existing, kept as-is)
Past backtest runs from the database. No changes needed.

#### Tab 5: Docs (existing, enhanced)
Current docs tab + new section explaining Insights, Alpha Models, and how to interpret the new panels.

---

## How This Enhances the User Experience

### Before (Strategy Lab)
The user runs a backtest and sees:
1. A candlestick chart with entry/exit markers — "what happened"
2. KPI cards — "how much money it made"
3. LEAN stats — "risk-adjusted metrics"
4. Trade log — "list of trades"

They know the strategy made money, but they don't know *why* it worked on some trades and not others. They can't see the model's reasoning or confidence. They can't compare two strategies' predictions. They have to switch between Strategy Lab and Engine Lab to get different views of the same data.

### After (Enhanced Engine Lab)
The user runs a backtest and sees everything above PLUS:
1. **Insight bands on the chart** — "when did the model predict correctly vs incorrectly, visually overlaid on price"
2. **Confidence calibration** — "should I trust the model when it says 80% confident?"
3. **Direction accuracy over time** — "is the model degrading?"
4. **Time-of-day heatmap** — "when is the model at its best and worst?"
5. **Magnitude scatter** — "does the model accurately estimate the size of the move?"
6. **Per-insight table** — "for each individual prediction, what was the model thinking and what actually happened?"
7. **Replay with live insight scoring** — "watch the model think in real time"

One page. One workflow. No switching between labs.

### Concrete User Stories

**Story 1: "Why did my strategy lose money in Q3?"**
- Before: Look at trade log, squint at dates, try to correlate with chart. Manual and slow.
- After: Open the Direction Accuracy Over Time chart. See the rolling accuracy dropped from 75% to 52% in August. Click on that period in the Insight table. Notice that all the wrong predictions happened between 12:00-14:00. Check the time-of-day heatmap — confirms lunch-hour degradation. Actionable: suppress signals from 12:00-14:00.

**Story 2: "Should I increase my position size when confidence is high?"**
- Before: No way to answer this — the Strategy Lab doesn't track confidence.
- After: Open the Confidence Calibration chart. See that 0.70-0.80 confidence predictions are actually 83% accurate (underconfident — good!). But 0.80+ predictions are only 60% accurate (overconfident — bad!). Actionable: size positions by confidence, but cap it at 0.80.

**Story 3: "I have two strategies — which one should I trust?"**
- Before: Run each in separate tabs, manually compare Sharpe ratios.
- After: Select Composite Alpha mode. Pick EmaCross + RsiReversal. Run once. See both models' insights scored side by side. "When both agree → 82% accuracy. When they disagree → 51% accuracy." Actionable: only trade when both models agree.

**Story 4: "I want to understand how this strategy behaves bar by bar"**
- Before: Switch to Strategy Lab's Replay tab. Different interface, different data source, can't see LEAN-parity results.
- After: Click the Replay tab in Engine Lab. Same LEAN-parity engine, same data. Watch candles build, see insights appear with their predicted direction, watch them score green or red as the period expires. Running counter: "12/16 correct (75%)." Scrub backward and forward with the slider.

---

## Migration Path: Strategy Lab → Engine Lab

### Components to Migrate (copy + adapt)

| Strategy Lab Component | Engine Lab Destination | Changes Needed |
|---|---|---|
| `StrategyLabChartComponent` | `engine/engine-chart/` | Change input types from `BacktestResponse` to `EngineBacktestResponse`; add Insight band rendering |
| `ReplayControlsComponent` | `engine/replay-controls/` | Add insight counter to the controls bar |
| `ReplayChartComponent` | `engine/replay-chart/` | Add insight marker rendering as bars appear |
| `QualityModalComponent` | Already shared in `data-lab/` | Just import it |
| `LeanStatisticsComponent` | Already shared (Engine imports it) | No changes |
| `BacktestResultsComponent` | Merge into enhanced `EngineResultsComponent` | Extract chart and trade log rendering |

### Services (no migration needed)
- `ReplayEngineService`, `ReplayIndicatorService`, `ReplayStrategyService` — these are already standalone services. The Engine Lab just imports and uses them.

### Strategy Lab Deprecation
After migration is complete:
1. Add a deprecation banner to Strategy Lab: "This lab is deprecated. Use Engine Lab for all backtesting."
2. Strategy Lab route can redirect to Engine Lab after a grace period
3. Eventually remove the Strategy Lab components entirely

---

## Implementation Order

### Phase 1A — Backend Insights (2-3 days)
1. Create `insight.py`, `insight_manager.py`, `insight_scorer.py`
2. Add `emit_insight()` to `StrategyContext`
3. Add insight scoring step to `BacktestEngine`
4. Modify `SpyEmaCrossoverAlgorithm` to emit insights (dual-mode)
5. Run parity test — confirm zero regression
6. Extend `/api/engine/backtest` response with `insights[]` and `insight_summary`

### Phase 1B — Engine Chart Migration (2-3 days)
1. Copy `StrategyLabChartComponent` into `engine/engine-chart/`
2. Adapt inputs to work with `EngineBacktestResponse` data shape
3. Have the backend return `chart_bars[]` and `chart_indicators[]` in the engine response
4. Wire into `EngineResultsComponent` — chart renders below KPI cards
5. Add equity curve chart (line chart from `equity_curve[]`)

### Phase 1C — Insight Panels (2-3 days)
1. Create `InsightPanelComponent` — the collapsible Insight Analysis section
2. Build the Insight Accuracy Table (sortable, filterable)
3. Build the Confidence Calibration Chart (Lightweight Charts line chart)
4. Build the Direction Accuracy Over Time chart (rolling window line)
5. Build the Time-of-Day Heatmap (simple HTML/SVG grid)
6. Build the Magnitude Scatter Plot

### Phase 2A — Alpha Models (2-3 days)
1. Create `AlphaModel` base class and `EmaCrossAlphaModel`
2. Port RsiReversal as a second Alpha Model
3. Create `CompositeAlphaModel` for multi-alpha runs
4. Register Alpha Models in the strategy registry
5. Extend `/api/engine/backtest` to accept `alpha_models[]` for composite mode
6. Update Engine Lab form to show Alpha Models in the strategy dropdown

### Phase 2B — Replay Migration (2-3 days)
1. Add Replay tab to Engine Lab
2. Import `ReplayControlsComponent` and `ReplayChartComponent` (copy from Strategy Lab, adapt)
3. Wire replay services to Engine Lab's data flow
4. Add insight emission markers to the replay chart
5. Add running accuracy counter to replay controls
6. Add deprecation banner to Strategy Lab

### Phase 2C — Insight Chart Bands + Polish (1-2 days)
1. Add insight prediction bands to the main candlestick chart (semi-transparent colored rectangles)
2. Hover tooltip on bands showing prediction details and score
3. Connect chart time range to insight table filtering (click a time range on chart → filter insights)
4. ZIP download that includes insights data
5. End-to-end testing of the full flow

**Total: ~12-17 days of focused work**

---

## File Structure After Phase 1 & 2

```
Frontend/src/app/components/lean-engine/
├── lean-engine.component.ts           # Main page (enhanced with new tabs)
├── lean-engine.component.html
├── lean-engine.component.scss
├── engine-results/                    # Enhanced results (existing)
│   ├── engine-results.component.ts
│   ├── engine-results.component.html
│   └── engine-results.component.scss
├── engine-chart/                      # NEW — migrated from Strategy Lab
│   ├── engine-chart.component.ts      # Candlestick + indicators + insight bands
│   ├── engine-chart.component.html
│   └── engine-chart.component.scss
├── engine-equity-chart/               # NEW — equity curve visualization
│   ├── engine-equity-chart.component.ts
│   ├── engine-equity-chart.component.html
│   └── engine-equity-chart.component.scss
├── insight-panel/                     # NEW — all insight analysis views
│   ├── insight-panel.component.ts     # Container with collapsible sections
│   ├── insight-table.component.ts     # Per-insight accuracy table
│   ├── confidence-chart.component.ts  # Calibration line chart
│   ├── accuracy-timeline.component.ts # Rolling accuracy over time
│   ├── time-heatmap.component.ts      # Time-of-day accuracy heatmap
│   └── magnitude-scatter.component.ts # Predicted vs actual scatter
├── replay-controls/                   # MIGRATED from Strategy Lab
│   ├── replay-controls.component.ts   # Play/pause/seek + insight counter
│   └── replay-controls.component.html
├── replay-chart/                      # MIGRATED from Strategy Lab
│   ├── replay-chart.component.ts      # Candlestick replay + insight markers
│   └── replay-chart.component.html
├── engine-history/                    # Existing, no changes
├── lean-engine-docs/                  # Existing, add insight docs
└── models/
    └── insight.model.ts               # NEW — TypeScript Insight interfaces

PythonDataService/app/engine/framework/
├── __init__.py
├── insight.py
├── insight_manager.py
├── insight_scorer.py
├── alpha/
│   ├── __init__.py
│   ├── alpha_model.py
│   ├── ema_cross_alpha.py
│   ├── rsi_reversal_alpha.py
│   ├── composite_alpha.py
│   └── registry.py
```

---

## Summary: What Changes for the User

| What they do today | What they do after Phase 1 & 2 |
|---|---|
| Go to Strategy Lab for charts, go to Engine Lab for LEAN-parity numbers | One place: Engine Lab has everything |
| See: "63 trades, 69.8% win rate" | See: "63 predictions, 69.8% directionally correct, 42% magnitude accuracy, underconfident in 0.6-0.7 band" |
| Manually compare Sharpe ratios between strategies | Run Composite Alpha → see where models agree (82% accuracy) vs disagree (51%) |
| Watch replay: candles build, trade markers appear | Watch replay: candles build, predictions appear with direction, bands fill green/red as they score |
| No way to know which predictions were wrong or why | Insight table: every prediction with its score, filterable by time, confidence, accuracy |
| Strategy Lab has charts, Engine has numbers | Engine has both: charts with insight bands + all LEAN statistics |
