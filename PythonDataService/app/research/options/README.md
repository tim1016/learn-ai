# Options IV Pipeline — Data Fetching Reference

## Overview

The IV (Implied Volatility) pipeline builds a **30-day constant-maturity IV time series** for each ticker. This requires fetching raw options contract data from Polygon.io, then deriving IV via Black-Scholes.

For **1 ticker over ~500 trading days**, the pipeline makes approximately **3,000–4,000 Polygon API calls**.

---

## Pipeline Stages & API Calls

### Stage 1: Stock Daily Bars

**Purpose**: Get the underlying stock's daily close prices (needed for strike selection and BS inputs).

| Detail | Value |
|--------|-------|
| **Polygon Method** | `client.list_aggs()` |
| **API Endpoint** | `GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}` |
| **Wrapper** | `PolygonClientService.fetch_aggregates()` |
| **Calls per ticker** | **1** (single call returns full date range) |
| **Returns** | `[{timestamp, open, high, low, close, volume, vwap}, ...]` |

**Example**: Fetch SPY daily bars from 2024-06-01 to 2026-02-28 → ~500 bars in 1 call.

---

### Stage 2: Find Bracket Expiries (per month)

**Purpose**: For each trading day, find two options expiration dates that **bracket** 30 DTE — one below 30 days and one above. This enables interpolation to a constant 30-day maturity.

| Detail | Value |
|--------|-------|
| **Polygon Method** | `client.list_options_contracts()` |
| **API Endpoint** | `GET /v3/reference/options/contracts` |
| **Wrapper** | `PolygonClientService.list_options_contracts()` |
| **Calls per ticker** | **~20** (one per unique month in the date range, cached) |
| **Key Parameters** | `underlying_ticker`, `as_of` (historical date), `expiration_date.gte`, `expiration_date.lte`, `strike_price.gte`, `strike_price.lte`, `contract_type=call` |
| **Returns** | Contract metadata: `{ticker, underlying_ticker, contract_type, strike_price, expiration_date}` |

**What it does**:
- For trade date 2025-06-01, the target is 30 DTE → 2025-07-01
- Searches for contracts expiring between 14–60 DTE (2025-06-15 to 2025-07-31)
- Uses `as_of=2025-06-01` to see contracts that existed on that date
- Applies ±5% ATM strike filter to keep results small (~200 contracts)
- Extracts unique expiry dates, picks closest below/above 30 DTE

**Example output**: low_expiry=2025-06-30 (29 DTE), high_expiry=2025-07-03 (32 DTE)

**Important**: Does NOT use `expired=True` here. The `as_of` parameter already handles historical lookup. Combining `expired=True` + `as_of` would return 0 results (this was the original bug).

---

### Stage 3: Fetch Contracts for Each Bracket Expiry (per month)

**Purpose**: For each bracket expiry date, find the specific ATM call, OTM put (~5% below), and OTM call (~5% above) contracts.

| Detail | Value |
|--------|-------|
| **Polygon Method** | `client.list_options_contracts()` |
| **API Endpoint** | `GET /v3/reference/options/contracts` |
| **Wrapper** | `PolygonClientService.list_options_contracts()` |
| **Calls per ticker** | **~40** (2 expiries × ~20 months) |
| **Key Parameters** | `underlying_ticker`, `expiration_date` (exact), `expired=True`, `strike_price.gte`, `strike_price.lte` |
| **Returns** | Same contract metadata as Stage 2 |

**What it does**:
- For expiry 2025-06-30 and stock close $592.71:
  - Search strikes within ±15% of ATM ($504–$682)
  - **ATM call**: strike closest to $592.71 → `O:SPY250630C00593000`
  - **OTM put**: strike ~5% below ATM ($563) → `O:SPY250630P00563000`
  - **OTM call**: strike ~5% above ATM ($622) → `O:SPY250630C00622000`

**Important**: Uses `expired=True` here (without `as_of`) because these are past-expiry contracts that need the expired flag to appear in results.

---

### Stage 4: Fetch Option Daily Bars (per trading day × 6 contracts)

**Purpose**: Get the daily OHLCV bar for each option contract on each trading day. The close/mid price is the input to the Black-Scholes IV solver.

| Detail | Value |
|--------|-------|
| **Polygon Method** | `client.list_aggs()` |
| **API Endpoint** | `GET /v2/aggs/ticker/{optionTicker}/range/1/day/{date}/{date}` |
| **Wrapper** | `PolygonClientService.fetch_aggregates()` |
| **Calls per ticker** | **~3,000** (6 contracts × ~500 trading days) |
| **Key Parameters** | `ticker` (option ticker like `O:SPY250630C00593000`), `from_date=to_date` (single day) |
| **Returns** | `{timestamp, open, high, low, close, volume, vwap, transactions}` |

**This is the bottleneck.** ~3,000 sequential API calls at ~75ms each ≈ **3–4 minutes per ticker**.

**What it does per trading day**:
For each of the 6 contracts (low bracket: ATM call, OTM put, OTM call; high bracket: same):
1. Fetch the single-day bar for the option contract
2. Extract price: prefer bid/ask midpoint, fallback to close (with volume ≥ 50 filter)
3. Parse strike from option ticker: `O:SPY250630C00593000` → strike = $593.00
4. Feed into Black-Scholes solver: `implied_volatility(price, stock_close, strike, T, r, option_type)`
5. Result: single IV value for that contract on that date

---

## Total API Calls per Ticker

| Stage | Description | Calls |
|-------|-------------|-------|
| 1 | Stock daily bars | 1 |
| 2 | Bracket expiry search | ~20 |
| 3 | Contract lookup per expiry | ~40 |
| 4 | Option daily bars (the bulk) | ~3,000 |
| **Total** | | **~3,060** |

For a **4-ticker batch** (SPY, QQQ, NVDA, TSLA): **~12,000 API calls**.
For the **old 15-ticker batch**: **~46,000 API calls**.

---

## IV Computation (No API Calls)

After fetching, all computation is local:

1. **Black-Scholes Newton-Raphson** → derive IV from option price
   - Inputs: market_price, stock_close, strike, T (DTE/365), risk_free_rate, option_type
   - Fallback: Brent's method bisection if Newton-Raphson diverges (common for OTM options)
   - Bounds: reject IV outside [5%, 300%]

2. **30-Day Interpolation** → constant-maturity IV
   - Two brackets: IV_low (DTE < 30) and IV_high (DTE > 30)
   - Linear interpolation: `IV_30 = w_low × IV_low + w_high × IV_high`
   - Fallback: DTE normalization if only one bracket: `IV × sqrt(30/DTE)`

3. **Forward-fill** → fill weekend/holiday gaps (max 2 days)

4. **Quality filters** → reject IV outside [5%, 300%], flag >50% day-over-day changes

---

## Polygon.io Plan Constraints (Starter)

| Constraint | Value |
|------------|-------|
| Historical depth | 2 years max |
| Data delay | 15-minute delayed |
| Rate limit | 5 calls/minute (free) or unlimited (paid) |
| Options contracts API | Supports `expired=true` for past contracts |
| Options snapshot API | **Live/unexpired only** — cannot query past expiration snapshots |
| Options daily bars | Available for expired contracts via aggregates endpoint |

---

## Key Files

| File | Role |
|------|------|
| `contract_finder.py` | Stage 2 + 3: finds bracket expiries and specific contracts |
| `iv_builder.py` | Stage 1 + 4: orchestrates the full IV build, calls BS solver |
| `bs_solver.py` | Newton-Raphson + Brent's method IV solver |
| `diagnostics.py` | Validates IV time series quality before research |
| `polygon_client.py` | Wrapper around Polygon Python SDK |

---

## Optimization Opportunities

1. **Batch option bar fetches**: Instead of 1 call per contract per day, fetch the full date range for each contract in a single call (1 call for all 500 days instead of 500 calls). This would reduce Stage 4 from ~3,000 to ~60 calls per ticker.

2. **Cache bracket contracts**: The same option contract ticker is used for many consecutive days. Currently each day fetches independently.

3. **Parallel API calls**: The current `_SEMAPHORE = asyncio.Semaphore(5)` exists but the IV builder runs synchronously. Making Stage 4 async with 5 concurrent calls would ~5× throughput.
