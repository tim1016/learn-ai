> **Status:** Archived — stale plan, pipeline superseded.
> **Do not use as implementation authority.**
> Current authority: FastAPI service in `PythonDataService/` (Polygon.io-based pipeline).
> Archived because: current data pipeline is Polygon-based; CSV pipeline plan superseded.

# DIA Minute RTH Data Pipeline — Cleanup, Validation & Frontend Plan

**Context:** Analysis of `DIA_minute_rth_2024-03-28_to_2026-03-28.csv` (195,390 rows, 501 trading days) revealed fabricated bars on half-days, fractional volumes in recent data, misleading cumulative VWAP, and opaque indicator warmup. This plan addresses all issues and adds a full-stack validation UI with before/after comparison and raw/clean data access.

**Why:** These deficiencies distort EMA/MACD/RSI/ADX calculations and cause TradingView mismatch. Fixing them is prerequisite to reliable backtesting and indicator validation.

**How to apply:** Implement in three layers — Python cleanup pipeline, .NET proxy, Angular frontend with validation dashboard and documentation page.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  Frontend: /data-quality                                    │
│  ┌─────────────┐  ┌──────────────────┐  ┌────────────────┐ │
│  │ Config Form  │  │ Validation Report│  │  Data Viewer   │ │
│  │ ticker/dates │  │ before vs after  │  │  raw | clean   │ │
│  │ clean opts   │  │ per-step metrics │  │  CSV download  │ │
│  └──────┬──────┘  └────────┬─────────┘  └───────┬────────┘ │
│         │                  │                     │          │
│  ┌──────┴──────────────────┴─────────────────────┴────────┐ │
│  │ /data-quality-docs — Documentation Page                │ │
│  │ Step-by-step cleanup explanation, libraries, formulas  │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────┬───────────────────────────────┘
                              │ HTTP (direct to Python)
┌─────────────────────────────▼───────────────────────────────┐
│  PythonDataService: /api/data-quality/*                     │
│                                                             │
│  POST /analyze       → run pipeline, return before/after    │
│  GET  /raw-csv       → stream raw (uncleaned) CSV           │
│  GET  /clean-csv     → stream cleaned CSV                   │
│  GET  /docs          → return cleanup step descriptions     │
└─────────────────────────────────────────────────────────────┘
```

---

# Part A — Python Cleanup Pipeline

## New Router: `/api/data-quality`

**File:** `PythonDataService/app/routers/data_quality.py`

### Endpoint 1: `POST /api/data-quality/analyze`

**Request:**
```json
{
  "ticker": "DIA",
  "from_date": "2024-03-28",
  "to_date": "2026-03-28",
  "timespan": "minute",
  "multiplier": 1,
  "session": "rth",
  "volume_fix": "round",       // "round" | "drop" | "nullify"
  "recompute_indicators": true,
  "indicators": [ ... ]        // optional, same format as /api/dataset/generate-csv
}
```

**Response:**
```json
{
  "ticker": "DIA",
  "from_date": "2024-03-28",
  "to_date": "2026-03-28",
  "raw_summary": {
    "total_bars": 195390,
    "trading_days": 501,
    "bars_per_day_distribution": { "390": 501 },
    "date_range": ["2024-03-28", "2026-03-27"],
    "zero_volume_bars": 2014,
    "flat_bars_ohlc_equal": 4693,
    "flat_with_volume": 2679,
    "fractional_volume_bars": 9608,
    "fractional_volume_date_range": ["2026-02-23", "2026-03-27"],
    "vwap_above_high": 4078,
    "vwap_below_low": 4127,
    "ohlc_violations": 0,
    "duplicate_timestamps": 0,
    "weekend_bars": 0,
    "intraday_gaps": 0,
    "big_moves_1pct": 8,
    "big_moves_2pct": 1,
    "early_close_days": ["2024-07-03", "2024-11-29", "2024-12-24", "2025-07-03", "2025-11-28", "2025-12-24"],
    "missing_holidays": ["2024-03-29", "2024-05-27", "..."]
  },
  "clean_summary": {
    "total_bars": 192256,
    "trading_days": 501,
    "bars_per_day_distribution": { "390": 495, "210": 6 },
    "zero_volume_bars": 0,
    "flat_bars_ohlc_equal": 2679,
    "fractional_volume_bars": 0,
    "vwap_above_high": 0,
    "vwap_below_low": 0,
    "ohlc_violations": 0,
    "duplicate_timestamps": 0,
    "indicators_recomputed": true,
    "warmup_days_used": 10
  },
  "steps": [
    {
      "order": 1,
      "name": "NYSE Session Filter",
      "library": "pandas_market_calendars",
      "description": "Removed bars outside valid NYSE RTH minutes (handles early-close days)",
      "bars_before": 195390,
      "bars_after": 192256,
      "bars_removed": 3134,
      "details": {
        "early_close_days_trimmed": 6,
        "fabricated_bars_removed": 1080
      }
    },
    {
      "order": 2,
      "name": "Fractional Volume Fix",
      "library": "pandas",
      "description": "Rounded 9,608 fractional volume values to nearest integer",
      "bars_before": 192256,
      "bars_after": 192256,
      "bars_removed": 0,
      "details": {
        "fractional_bars_fixed": 9608,
        "method": "round"
      }
    },
    {
      "order": 3,
      "name": "VWAP Recomputation",
      "library": "pandas (manual TP*Vol cumsum)",
      "description": "Replaced vendor cumulative VWAP with session-reset VWAP: TP=(H+L+C)/3, cumsum(TP*V)/cumsum(V)",
      "bars_before": 192256,
      "bars_after": 192256,
      "bars_removed": 0,
      "details": {
        "vwap_violations_before": 8205,
        "vwap_violations_after": 0
      }
    },
    {
      "order": 4,
      "name": "Zero-Volume Flat Bar Removal",
      "library": "pandas",
      "description": "Dropped bars where volume=0 AND O=H=L=C (stale price carry-forward)",
      "bars_before": 192256,
      "bars_after": 191322,
      "bars_removed": 934,
      "details": {}
    },
    {
      "order": 5,
      "name": "OHLC Integrity Enforcement",
      "library": "pandas",
      "description": "Enforced high=max(O,H,L,C), low=min(O,H,L,C). Sorted by timestamp, removed duplicates.",
      "bars_before": 191322,
      "bars_after": 191322,
      "bars_removed": 0,
      "details": {
        "ohlc_corrections": 0,
        "duplicates_removed": 0
      }
    },
    {
      "order": 6,
      "name": "Timezone Normalization",
      "library": "pandas (zoneinfo)",
      "description": "Converted UTC timestamps to America/New_York for correct session grouping across DST boundaries",
      "bars_before": 191322,
      "bars_after": 191322,
      "bars_removed": 0,
      "details": {
        "edt_days": 332,
        "est_days": 169
      }
    },
    {
      "order": 7,
      "name": "Indicator Recomputation",
      "library": "pandas-ta",
      "description": "Dropped all vendor indicator columns and recomputed from cleaned OHLCV with proper warmup",
      "bars_before": 191322,
      "bars_after": 191322,
      "bars_removed": 0,
      "details": {
        "indicators_dropped": 25,
        "indicators_recomputed": 25,
        "warmup_bars_fetched": 3900,
        "warmup_days": 10
      }
    }
  ],
  "raw_data_token": "uuid-for-raw-csv-download",
  "clean_data_token": "uuid-for-clean-csv-download"
}
```

### Endpoint 2: `GET /api/data-quality/raw-csv?token={token}`

Streams the **raw (uncleaned)** data as CSV using the token from the analyze response. Data is cached in-memory or on disk with a TTL (e.g. 30 minutes).

### Endpoint 3: `GET /api/data-quality/clean-csv?token={token}`

Streams the **cleaned** data as CSV using the token from the analyze response.

### Endpoint 4: `GET /api/data-quality/docs`

Returns a JSON array describing each cleanup step with library, formula, rationale. Used by the frontend documentation page so content is single-sourced from Python.

```json
[
  {
    "order": 1,
    "name": "NYSE Session Filter",
    "library": "pandas_market_calendars",
    "library_url": "https://github.com/rsheftel/pandas_market_calendars",
    "problem": "Polygon returns 390 bars per day regardless of whether it was an early-close day. Half-days (Jul 3, Nov 29, Dec 24) get ~180 fabricated bars with volume=0 and stale O=H=L=C prices after market close at 13:00 ET.",
    "fix": "Build a set of valid NYSE RTH minutes using pandas_market_calendars. Inner-join the dataframe against this set to keep only bars that fall within actual trading hours.",
    "rules": [
      "Normal day: 9:30 → 16:00 ET (390 minutes)",
      "Early close: 9:30 → 13:00 ET (210 minutes)"
    ],
    "code": "nyse = mcal.get_calendar('NYSE')\nschedule = nyse.schedule(start_date, end_date)\nvalid_minutes = mcal.date_range(schedule, frequency='1min')\ndf = df[df['ts_ny'].isin(valid_minutes)]",
    "impact": "Eliminates zero-volume fabricated bars, false flat candles after close, false end-of-day indicator bleed"
  },
  {
    "order": 2,
    "name": "Fractional Volume Fix",
    "library": "pandas",
    "library_url": null,
    "problem": "From 2026-02-23 onward (25 trading days, 9,608 bars), volume values are decimal (e.g. 142086.485768). Share volume must be integer. This likely results from Polygon's data interpolation or delayed settlement adjustments.",
    "fix": "Three options depending on strategy: (A) drop those days entirely for volume-sensitive strategies, (B) round to nearest integer for price-only indicators, (C) set volume to NaN and recompute volume-dependent indicators only on valid data.",
    "rules": [
      "Option A: df = df[df['volume'] % 1 == 0]",
      "Option B: df['volume'] = df['volume'].round().astype('int64')",
      "Option C: df.loc[df['volume'] % 1 != 0, 'volume'] = None"
    ],
    "code": "df['volume'] = df['volume'].round().astype('int64')  # default: round",
    "impact": "Prevents broken VWAP, OBV, MFI, CMF, ADX calculations on fractional volume"
  },
  {
    "order": 3,
    "name": "VWAP Recomputation",
    "library": "pandas (manual computation)",
    "library_url": null,
    "problem": "Polygon's VWAP is session cumulative (accumulates across the full day), not per-bar. This means VWAP can be far outside a single bar's high-low range. 8,205 bars had VWAP outside [low, high].",
    "fix": "Recompute VWAP using typical price TP = (H+L+C)/3, then cumulative sum within each session: VWAP = cumsum(TP * Volume) / cumsum(Volume). Reset at each session open.",
    "rules": [
      "TP = (High + Low + Close) / 3",
      "VWAP = cumsum(TP * Volume) / cumsum(Volume), grouped by trading date",
      "If volume = 0, VWAP = NaN"
    ],
    "code": "tp = (df['high'] + df['low'] + df['close']) / 3\ndf['vwap'] = (tp * df['volume']).groupby(df['date']).cumsum() / df['volume'].groupby(df['date']).cumsum()",
    "impact": "VWAP violations drop from 8,205 to near-zero. Anchored VWAP strategies become reliable."
  },
  {
    "order": 4,
    "name": "Zero-Volume Flat Bar Removal",
    "library": "pandas",
    "library_url": null,
    "problem": "Bars with volume=0 and O=H=L=C are stale price carry-forwards, not real market activity. They cause false smoothing in moving averages and momentum indicators.",
    "fix": "After session filtering, drop any remaining bars where volume=0 AND open=high=low=close.",
    "rules": [
      "Drop if: volume == 0 AND open == close AND high == low"
    ],
    "code": "df = df[~((df['volume'] == 0) & (df['open'] == df['close']) & (df['high'] == df['low']))]",
    "impact": "Removes stale bars that would otherwise dilute EMA/RSI calculations with non-market data points"
  },
  {
    "order": 5,
    "name": "OHLC Integrity Enforcement",
    "library": "pandas",
    "library_url": null,
    "problem": "Vendor data can occasionally have high < open or low > close due to trade correction or aggregation bugs. Defensive enforcement ensures downstream calculations never see impossible candles.",
    "fix": "Force high = max(O,H,L,C) and low = min(O,H,L,C). Sort by timestamp and remove any duplicates.",
    "rules": [
      "high = max(open, high, low, close)",
      "low = min(open, high, low, close)",
      "Sort ascending by timestamp",
      "Drop duplicate timestamps"
    ],
    "code": "df = df.sort_values('timestamp').drop_duplicates('timestamp')\ndf['high'] = df[['open','high','low','close']].max(axis=1)\ndf['low'] = df[['open','high','low','close']].min(axis=1)",
    "impact": "Prevents rare vendor glitches from causing NaN propagation in indicators"
  },
  {
    "order": 6,
    "name": "Timezone Normalization",
    "library": "pandas + zoneinfo (stdlib)",
    "library_url": null,
    "problem": "Raw timestamps are UTC. TradingView and most trading platforms compute indicators in exchange timezone (America/New_York). UTC day boundaries differ from ET day boundaries, especially around DST transitions.",
    "fix": "Convert UTC timestamps to America/New_York, derive trading date from the localized timestamp, and use this date for session grouping (VWAP reset, daily aggregation).",
    "rules": [
      "EDT (Mar-Nov): UTC-4, market opens 13:30 UTC",
      "EST (Nov-Mar): UTC-5, market opens 14:30 UTC",
      "Trading date = New York date of bar timestamp"
    ],
    "code": "df['ts'] = pd.to_datetime(df['unix_ts'], unit='ms', utc=True)\ndf['ts_ny'] = df['ts'].dt.tz_convert('America/New_York')\ndf['date'] = df['ts_ny'].dt.date",
    "impact": "Prevents mis-grouped sessions at DST boundaries (affecting daily VWAP, first/last bar detection)"
  },
  {
    "order": 7,
    "name": "Indicator Recomputation",
    "library": "pandas-ta",
    "library_url": "https://github.com/twopirllc/pandas-ta",
    "problem": "Exported indicators have hidden warmup history — all have values from row 0, including EMA-200 which needs 200+ bars to converge. The warmup data is not visible in the export, so indicator values in the first N rows cannot be independently verified.",
    "fix": "Drop all vendor indicator columns. Fetch extra historical bars BEFORE the requested start date (warmup window). Compute indicators on the full extended dataset, then trim to the requested window.",
    "rules": [
      "EMA-200: 10 extra trading days (~3,900 minute bars)",
      "MACD (12,26,9): 5 extra trading days",
      "RSI-14 / ADX-14: 3 extra trading days",
      "Bollinger Bands (20,2): 3 extra trading days",
      "Use the maximum warmup across all requested indicators"
    ],
    "code": "indicator_cols = [c for c in df.columns if c not in CORE_COLS]\ndf = df.drop(columns=indicator_cols)\n# Fetch warmup bars, compute via pandas-ta, then slice",
    "impact": "Indicator values become verifiable and reproducible. TradingView matching improves significantly."
  }
]
```

---

## New Service: `PythonDataService/app/services/data_quality_service.py`

Core cleanup logic, called by the router. Follows the existing service pattern (module-level singleton like `dataset_service.py`).

**Key methods:**

```python
class DataQualityService:
    def analyze(self, ticker, from_date, to_date, timespan, multiplier, session, volume_fix, indicators) -> dict:
        """Run full pipeline, return before/after summaries + step details."""

    def _compute_raw_summary(self, df: pd.DataFrame) -> dict:
        """Compute quality metrics on uncleaned data."""

    def _step1_session_filter(self, df, from_date, to_date) -> tuple[pd.DataFrame, dict]:
        """Filter to valid NYSE RTH minutes. Returns (cleaned_df, step_report)."""

    def _step2_fix_volume(self, df, method: str) -> tuple[pd.DataFrame, dict]:
        """Fix fractional volume. Returns (cleaned_df, step_report)."""

    def _step3_recompute_vwap(self, df) -> tuple[pd.DataFrame, dict]:
        """Recompute session-reset VWAP. Returns (cleaned_df, step_report)."""

    def _step4_remove_flat_bars(self, df) -> tuple[pd.DataFrame, dict]:
        """Remove zero-vol flat bars. Returns (cleaned_df, step_report)."""

    def _step5_ohlc_integrity(self, df) -> tuple[pd.DataFrame, dict]:
        """Enforce OHLC rules. Returns (cleaned_df, step_report)."""

    def _step6_normalize_tz(self, df) -> tuple[pd.DataFrame, dict]:
        """Convert to NY timezone. Returns (cleaned_df, step_report)."""

    def _step7_recompute_indicators(self, df, indicators) -> tuple[pd.DataFrame, dict]:
        """Drop and recompute indicators. Returns (cleaned_df, step_report)."""

    def _compute_clean_summary(self, df: pd.DataFrame) -> dict:
        """Compute quality metrics on cleaned data."""

    def _cache_dataframes(self, raw_df, clean_df) -> tuple[str, str]:
        """Cache both dataframes, return download tokens (UUID)."""
```

**New dependency:** Add `pandas_market_calendars` to `requirements.txt` and `requirements-light.txt`.

---

# Part B — Frontend Components

## Component 1: Data Quality Dashboard

**Route:** `/data-quality` (lazy-loaded)
**File:** `Frontend/src/app/components/data-quality/data-quality.component.ts`

### Layout

```
┌──────────────────────────────────────────────────────────────┐
│  Data Quality Analysis                          [Docs →]     │
├──────────────────────────────────────────────────────────────┤
│  ┌─ Config ────────────────────────────────────────────────┐ │
│  │ Ticker: [DIA    ]  From: [2024-03-28]  To: [2026-03-28]│ │
│  │ Timespan: [minute ▾]  Session: [RTH ▾]                 │ │
│  │ Volume Fix: (●) Round  ( ) Drop  ( ) Nullify           │ │
│  │ [x] Recompute indicators                               │ │
│  │                               [Run Analysis]           │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─ Summary Cards ─────────────────────────────────────────┐ │
│  │ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │ │
│  │ │ Raw Bars  │ │Clean Bars│ │ Removed  │ │  Steps Run │  │ │
│  │ │ 195,390   │ │ 191,322  │ │  4,068   │ │     7      │  │ │
│  │ │           │ │          │ │  (2.1%)  │ │            │  │ │
│  │ └──────────┘ └──────────┘ └──────────┘ └────────────┘  │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─ Tabs ──────────────────────────────────────────────────┐ │
│  │ [Step-by-Step Report] [Before / After] [Data Viewer]    │ │
│  └────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

### Tab 1: Step-by-Step Report

PrimeNG **Timeline** or **Accordion** showing each cleanup step:

```
┌─ Step 1: NYSE Session Filter ──────────────────────────────┐
│ Library: pandas_market_calendars                            │
│ Bars: 195,390 → 192,256  (removed 3,134)                   │
│ ┌────────────────────────────────────────────┐              │
│ │ ████████████████████████████░░ 98.4% kept  │              │
│ └────────────────────────────────────────────┘              │
│ Details:                                                    │
│   • 6 early-close days trimmed (Jul 3, Nov 29, Dec 24...)  │
│   • 1,080 fabricated after-close bars removed               │
├─ Step 2: Fractional Volume Fix ────────────────────────────┤
│ Library: pandas                                             │
│ Method: round                                               │
│ Bars: 192,256 → 192,256  (0 removed, 9,608 fixed)          │
│ ...                                                         │
└────────────────────────────────────────────────────────────┘
```

### Tab 2: Before / After Comparison

PrimeNG **Table** with side-by-side metrics:

```
┌──────────────────────────┬───────────┬───────────┬─────────┐
│ Metric                   │    Raw    │  Cleaned  │  Delta  │
├──────────────────────────┼───────────┼───────────┼─────────┤
│ Total bars               │  195,390  │  191,322  │ -4,068  │
│ Trading days             │      501  │      501  │      0  │
│ Zero-volume bars         │    2,014  │        0  │ -2,014  │
│ Flat bars (O=H=L=C)      │    4,693  │    2,679  │ -2,014  │
│ Fractional volume bars   │    9,608  │        0  │ -9,608  │
│ VWAP > high violations   │    4,078  │        0  │ -4,078  │
│ VWAP < low violations    │    4,127  │        0  │ -4,127  │
│ OHLC violations          │        0  │        0  │      0  │
│ Duplicate timestamps     │        0  │        0  │      0  │
│ Bars/day: 390 (normal)   │      501  │      495  │     -6  │
│ Bars/day: 210 (early)    │        0  │        6  │     +6  │
│ Indicators recomputed    │       no  │      yes  │    —    │
│ Warmup days used         │  unknown  │       10  │    —    │
└──────────────────────────┴───────────┴───────────┴─────────┘
```

Delta column uses green (improvement) / red (regression) / gray (unchanged) tags.

### Tab 3: Data Viewer

Toggle between raw and cleaned data with CSV download buttons:

```
┌────────────────────────────────────────────────────────────┐
│  View: (●) Raw Data  ( ) Cleaned Data                      │
│                                                            │
│  [Download Raw CSV]  [Download Clean CSV]                   │
│                                                            │
│  ┌─ PrimeNG Table (paginated, sortable, filterable) ─────┐│
│  │ timestamp          │ open   │ high  │ low   │ close  │ ││
│  │ 2024-03-28 09:30   │398.06  │398.26 │397.94 │398.04  │ ││
│  │ 2024-03-28 09:31   │398.05  │398.43 │398.05 │398.31  │ ││
│  │ ...                │        │       │       │        │ ││
│  └────────────────────────────────────────────────────────┘│
│  Showing 1-50 of 195,390          [< 1 2 3 ... 3908 >]    │
└────────────────────────────────────────────────────────────┘
```

- **Raw Data** view: shows original data as fetched from Polygon
- **Cleaned Data** view: shows post-pipeline data
- Both are paginated (virtual scroll or server-side pagination for 190k+ rows)
- Download buttons trigger CSV streaming from Python `/api/data-quality/raw-csv` and `/clean-csv`

---

## Component 2: Data Quality Documentation Page

**Route:** `/data-quality-docs` (lazy-loaded)
**File:** `Frontend/src/app/components/data-quality-docs/data-quality-docs.component.ts`

Follows the existing documentation page pattern (like `PortfolioDocsComponent` with PrimeNG Accordion + KatexDirective).

### Layout

```
┌──────────────────────────────────────────────────────────────┐
│  Data Quality Pipeline Documentation             [← Back]   │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─ Overview ──────────────────────────────────────────────┐ │
│  │ This pipeline cleans raw Polygon.io minute-level OHLCV  │ │
│  │ data to produce reliable, indicator-ready datasets.     │ │
│  │                                                         │ │
│  │ Pipeline order:                                         │ │
│  │  1. NYSE Session Filter                                 │ │
│  │  2. Fractional Volume Fix                               │ │
│  │  3. VWAP Recomputation                                  │ │
│  │  4. Zero-Volume Flat Bar Removal                        │ │
│  │  5. OHLC Integrity Enforcement                          │ │
│  │  6. Timezone Normalization                              │ │
│  │  7. Indicator Recomputation                             │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ▸ Step 1: NYSE Session Filter                               │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ Problem                                                 │ │
│  │ Polygon returns 390 bars/day even on early-close days.  │ │
│  │ Half-days get ~180 fabricated bars...                    │ │
│  │                                                         │ │
│  │ Library: pandas_market_calendars                        │ │
│  │ https://github.com/rsheftel/pandas_market_calendars     │ │
│  │                                                         │ │
│  │ Rules                                                   │ │
│  │  • Normal day: 9:30 → 16:00 ET (390 minutes)           │ │
│  │  • Early close: 9:30 → 13:00 ET (210 minutes)          │ │
│  │                                                         │ │
│  │ Code                                                    │ │
│  │ ┌────────────────────────────────────────────┐          │ │
│  │ │ nyse = mcal.get_calendar('NYSE')           │          │ │
│  │ │ schedule = nyse.schedule(start, end)        │          │ │
│  │ │ valid = mcal.date_range(schedule, '1min')   │          │ │
│  │ │ df = df[df['ts_ny'].isin(valid)]            │          │ │
│  │ └────────────────────────────────────────────┘          │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  ▸ Step 2: Fractional Volume Fix                             │
│  ▸ Step 3: VWAP Recomputation (with formula rendering)       │
│  │   VWAP = Σ(TP · V) / Σ(V)  where  TP = (H+L+C)/3       │
│  ▸ Step 4: Zero-Volume Flat Bar Removal                      │
│  ▸ Step 5: OHLC Integrity Enforcement                        │
│  ▸ Step 6: Timezone Normalization                             │
│  ▸ Step 7: Indicator Recomputation + Warmup Table             │
│                                                              │
│  ┌─ Libraries Used ───────────────────────────────────────┐  │
│  │ Library                    │ Purpose          │ Version │  │
│  │ pandas_market_calendars    │ NYSE schedule    │ latest  │  │
│  │ pandas-ta                  │ Indicators       │ latest  │  │
│  │ pandas                     │ Data wrangling   │ ≥2.2    │  │
│  │ zoneinfo (stdlib)          │ DST handling     │ 3.13    │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

Content is fetched from `GET /api/data-quality/docs` so documentation stays in sync with the actual pipeline implementation. The component renders it using PrimeNG Accordion panels with code blocks and KatexDirective for formulas.

---

# Part C — Routing & Navigation

### New Routes in `app.routes.ts`

```typescript
{
  path: 'data-quality',
  loadComponent: () => import('./components/data-quality/data-quality.component')
    .then(m => m.DataQualityComponent)
},
{
  path: 'data-quality-docs',
  loadComponent: () => import('./components/data-quality-docs/data-quality-docs.component')
    .then(m => m.DataQualityDocsComponent)
}
```

### Navigation Menu

Add under existing menu structure (near Data Lab):

```typescript
{
  label: 'Data Quality',
  items: [
    { label: 'Quality Analysis', routerLink: '/data-quality' },
    { label: 'Pipeline Docs', routerLink: '/data-quality-docs' }
  ]
}
```

---

# Part D — Files to Create / Modify

### New Files

| File | Purpose |
|------|---------|
| `PythonDataService/app/routers/data_quality.py` | FastAPI router with 4 endpoints |
| `PythonDataService/app/services/data_quality_service.py` | Cleanup pipeline logic + caching |
| `Frontend/src/app/components/data-quality/data-quality.component.ts` | Dashboard component |
| `Frontend/src/app/components/data-quality/data-quality.component.html` | Dashboard template |
| `Frontend/src/app/components/data-quality/data-quality.component.scss` | Dashboard styles |
| `Frontend/src/app/components/data-quality-docs/data-quality-docs.component.ts` | Docs page |
| `Frontend/src/app/components/data-quality-docs/data-quality-docs.component.html` | Docs template |
| `Frontend/src/app/components/data-quality-docs/data-quality-docs.component.scss` | Docs styles |

### Modified Files

| File | Change |
|------|--------|
| `PythonDataService/app/main.py` | Register `data_quality_router` |
| `PythonDataService/requirements.txt` | Add `pandas_market_calendars` |
| `PythonDataService/requirements-light.txt` | Add `pandas_market_calendars` |
| `Frontend/src/app/app.routes.ts` | Add 2 lazy routes |
| `Frontend/src/app/app.component.ts` | Add menu items |

---

# Part E — Implementation Priority

### Phase 1: Backend Pipeline (do first)
1. Add `pandas_market_calendars` dependency
2. Create `data_quality_service.py` with all 7 step methods
3. Create `data_quality.py` router with `/analyze`, `/raw-csv`, `/clean-csv`, `/docs`
4. Register router in `main.py`
5. Test with existing DIA data

### Phase 2: Frontend Dashboard
1. Create `DataQualityComponent` with config form + summary cards
2. Add Step-by-Step Report tab (Accordion with progress bars)
3. Add Before/After comparison tab (PrimeNG Table)
4. Add Data Viewer tab with raw/clean toggle + CSV download
5. Wire up routes and navigation

### Phase 3: Documentation Page
1. Create `DataQualityDocsComponent`
2. Fetch content from `/api/data-quality/docs`
3. Render with Accordion + KatexDirective for formulas
4. Add libraries table

---

# If You Only Do 3 Things

1. **NYSE calendar filtering** — removes ~3,134 fake bars
2. **Recompute VWAP** — fixes 8,205 out-of-range violations
3. **Fix fractional volume** — handles 9,608 corrupted bars

These alone eliminate almost all TradingView mismatch problems.

---

**Data source:** DIA minute data from Polygon.io (Starter plan, 2-year max history)
**New dependency:** `pandas_market_calendars` (`pip install pandas_market_calendars`)
