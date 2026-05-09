> **Status:** Archived — stale roadmap, work materially completed.
> **Do not use as implementation authority.**
> Current authority: `docs/architecture/engine-authority-map.md`.
> Archived because: Data Lab work completed; current state documented in engine-authority-map.md.

# Data Lab — Roadmap & Future Improvements

**Last updated:** 2026-03-29

## Current State

The Data Lab fetches minute-by-minute OHLCV data from Polygon.io, calculates configurable pandas-ta indicators (151 available across 9 categories), and exports as CSV with companion metadata JSON. A validation report engine compares pandas-ta output against TradingView CSV exports row-by-row.

### Deployment Blocker

`python-multipart` was added to `requirements-lock.txt` but Podman's build cache did not invalidate. To deploy:

```bash
podman compose down
podman build --no-cache -t learn-ai_python-service -f PythonDataService/Dockerfile PythonDataService/
podman compose up -d
```

---

## Planned Improvements

### 1. Polygon 07:00 ET Bar Filter (High Priority)

**Problem:** Polygon includes late-reported settlement trades in minute aggregates around 07:00-07:02 ET, inflating close prices by $4-6 on some bars. TradingView filters these out. This is the #1 source of indicator divergence — it poisons all downstream EMAs, with longer periods recovering more slowly.

**Solution:** Add a processing option to the Data Lab that:
- Detects bars in the 07:00-07:05 ET window where the close deviates >$1 from the previous bar's close
- Substitutes the contaminated close with the previous bar's close (or removes the bar entirely)
- Flags cleaned bars in the metadata JSON

**Files to modify:**
- `PythonDataService/app/services/dataset_service.py` — new `filter_0700_contamination()` function
- `PythonDataService/app/models/requests.py` — add `filter_0700: bool` to `DatasetGenerationRequest`
- `Frontend/src/app/components/data-lab/data-lab.component.ts` — expose checkbox option

### 2. Rendered Markdown for Validation Report

**Problem:** The validation report currently displays as raw markdown in a `<pre>` block. Tables and headings are not rendered.

**Solution:** Integrate a markdown renderer:
- Option A: `ngx-markdown` (Angular library wrapping `marked`)
- Option B: `marked` + `DomSanitizer` for lightweight rendering
- Render the report inline with proper table formatting, colored grade badges, and clickable hotspot timestamps

**Files to modify:**
- `Frontend/package.json` — add `ngx-markdown` or `marked`
- `Frontend/src/app/components/data-lab/data-lab.component.html` — replace `<pre>` with rendered output

### 3. Per-Day Gap Report

**Problem:** The current validation report doesn't detail where time gaps occur in the data.

**Solution:** Add a "Data Quality" section to the validation report showing:
- Total missing minutes vs expected (by session type)
- Largest N gaps with timestamps and durations
- Per-day breakdown: bars received vs expected, fill rate percentage
- Forward-fill statistics: how many bars were synthesized

**Files to modify:**
- `PythonDataService/app/services/validation_service.py` — add gap analysis functions
- `PythonDataService/app/services/dataset_service.py` — return gap stats from `preprocess_and_calculate()`

### 4. Batch Validation with Auto-Column Mapping

**Problem:** TradingView CSV exports have different column names than pandas-ta output. Currently the validation requires columns to match exactly.

**Solution:**
- Auto-detect TradingView column patterns (e.g. "EMA" → `ema_length*`, "BB Upper" → `bbu_*`)
- Support multi-indicator TradingView exports where all indicators are in one CSV
- Fuzzy matching on column names with confidence scores
- Show the column mapping in the validation report

**Files to modify:**
- `PythonDataService/app/services/validation_service.py` — enhance `_find_common_fields()` with fuzzy matching

### 5. Database Storage for Minute Bars

**Problem:** Every Data Lab request re-fetches from Polygon.io, even for the same ticker/date range. This is slow (minutes for 1-year ranges) and wastes API quota.

**Solution:**
- Store fetched minute bars in PostgreSQL using the existing `StockAggregate` entity (set `Timespan = "minute"`, `Multiplier = 1`)
- Before fetching, check which date ranges already exist in the DB
- Only fetch missing ranges from Polygon, merge with cached data
- Add a "Use Cache" toggle in the Data Lab UI

**Files to modify:**
- `Backend/Services/Implementation/MarketDataService.cs` — add minute bar caching logic
- `Backend/GraphQL/Mutation.cs` — add mutation for storing fetched bars
- `PythonDataService/app/routers/dataset.py` — accept pre-fetched bars from .NET backend
- `Frontend/src/app/components/data-lab/data-lab.component.ts` — add cache toggle

### 6. Indicator Warm-up Visualization

**Problem:** Users don't see where indicator warm-up ends and reliable data begins.

**Solution:**
- Mark the first N rows (per indicator) as "warm-up" in the CSV with a boolean column or separate metadata field
- In the validation report, exclude warm-up rows from accuracy calculations
- Show warm-up boundary in the Data Lab UI

---

## Known Data Behaviors (Not Bugs)

| Behavior | Explanation |
|----------|-------------|
| VWAP outside bar H/L | Polygon VWAP is daily rolling, not per-bar. Accumulates across the session. |
| Supertrend 48%/52% NaN | `supertl` is NaN during downtrends, `superts` during uptrends — by design. Use `supert` for the main line. |
| Flat bars (H=L=O=C) | Pre/post market bars with only 1 trade in the minute. ~10% of extended-hours data. |
| Saturday UTC bars | Friday after-hours trades timestamped past midnight UTC. Filtered when session='rth'. |
| 13K+ time gaps | Polygon doesn't return zero-trade minutes. Forward-fill option mitigates this. |

---

## Architecture Reference

```
Frontend (Angular)
  → Data Lab UI
    → POST /api/dataset/generate-csv (Python FastAPI)
    → POST /api/dataset/generate-metadata (Python FastAPI)
    → POST /api/dataset/validation-report (Python FastAPI)
  → GraphQL /graphql (.NET Hot Chocolate)
    → availableIndicators query → proxies Python /api/dataset/available

Python Service (FastAPI + pandas-ta)
  → dataset_service.py: chunked fetch, session filter, forward-fill, indicator calc
  → validation_service.py: row-by-row comparison, markdown report generation
  → Polygon.io REST API: list_aggs with auto-pagination
```
