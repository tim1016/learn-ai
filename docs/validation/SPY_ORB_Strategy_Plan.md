# SPY Opening Range Breakout (ORB) — Strategy & Validation Plan

**Date:** April 16, 2026
**Objective:** Replace indicator-dependent EMA crossover with a pure price-action strategy that produces identical signals across TradingView, Engine Lab, and Massive Market Data — eliminating warmup divergence entirely.

---

## 1. Strategy Design

### Why Opening Range Breakout?

The EMA crossover validation study revealed that **indicator warmup/seeding divergence** was the primary cause of trade mismatches between systems. The ORB strategy eliminates this problem by design:

| Property | EMA Crossover | ORB Strategy |
|----------|---------------|--------------|
| State across days | EMA carries forward forever | None — each day resets |
| Warmup needed | 10+ bars (varies by system) | Zero |
| Signal depends on | Floating-point EMA values | Exact price levels (ORB high) |
| Entry condition | Gap ≥ 0.20 (sensitive to 0.02 drift) | Close > ORB high (binary yes/no) |
| Cross-system agreement | ~30% match rate (8/26 trades) | Expected ~95%+ match rate |

### Strategy Rules

**Parameters (Strategy A — best profit factor):**

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| ORB Bars | 3 (45 min: 9:30–10:15 ET) | Captures the opening volatility and initial price discovery |
| Hold Period | 5 bars (75 min) | Consistent with current EMA strategy hold period |
| Min Range | 0.30% of price | Filters out tight/choppy opens with no momentum |
| Max Range | 1.50% of price | Filters out extreme gap days |
| Entry Type | Close above ORB high | Confirmation bar — reduces false breakouts vs wick-only |

**Logic (per day):**

1. At RTH open (9:30 ET), capture the high and low of the first 3 × 15-min bars
2. After bar 3, compute `range_pct = (ORB_high - ORB_low) / ORB_low × 100`
3. If `range_pct` is outside [0.30%, 1.50%] → skip the day
4. Scan subsequent bars: if any bar **closes** above `ORB_high` → enter long at that close
5. Hold for exactly 5 bars, then exit at the close
6. One trade per day maximum

### Backtest Results (Aug 2025 – Apr 2026, Massive API data)

| Metric | Value |
|--------|-------|
| Total trades | 70 |
| Win rate | 57% |
| Total return | +2.77% |
| Profit factor | 1.74 |
| Avg win | +0.163% |
| Avg loss | -0.125% |
| Max drawdown | 0.82% |
| Max win | +0.617% |
| Max loss | -0.590% |

**Alternative configurations tested:**

| Config | Trades | Win% | PnL | PF | Notes |
|--------|--------|------|-----|-----|-------|
| ORB=3 HOLD=5 MIN=0.30 (A) | 70 | 57% | +2.77% | 1.74 | Best PF, cleanest signals |
| ORB=1 HOLD=8 MIN=0.30 (B) | 52 | 62% | +3.69% | 1.67 | Longer hold, fewer trades |
| ORB=3 HOLD=10 MIN=0.20 (C) | 84 | 62% | +4.70% | 1.71 | Best total return |

---

## 2. Validation Plan — Three-Way Signal Agreement

The validation pipeline uses three independent systems that must agree on every signal:

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Massive Market   │     │   Engine Lab      │     │   TradingView     │
│  Data API         │     │   (Python)        │     │   (Pine Script)   │
│                   │     │                   │     │                   │
│  15-min OHLC bars │     │  LEAN minute data │     │  AMEX:SPY chart   │
│  → Python backtest│     │  → spy_orb.py     │     │  → ORB Pine script│
│  → trade list     │     │  → trade list     │     │  → trade list     │
└────────┬─────────┘     └────────┬─────────┘     └────────┬─────────┘
         │                        │                        │
         └────────────┬───────────┘────────────────────────┘
                      │
              ┌───────▼────────┐
              │  Signal Match  │
              │  Comparison    │
              │                │
              │  Per day check:│
              │  - Same ORB H/L│
              │  - Same entry  │
              │  - Same exit   │
              └────────────────┘
```

### Why this will work (unlike EMA crossover):

1. **ORB high/low are deterministic.** Given the same 3 bars, the max(high) and min(low) are identical in any system. No floating-point accumulation, no seeding.

2. **Entry condition is binary.** `close > ORB_high` is a simple comparison against an exact price level. There's no 0.02-wide sensitivity zone like the EMA gap threshold.

3. **Daily reset means no state drift.** Each trading day is a fresh calculation. Even if one system has a data gap on Monday, Tuesday's signals are unaffected.

4. **The only remaining divergence source is bar construction.** If two systems build slightly different 15-min OHLC (e.g., different tick aggregation at the boundary second), the ORB high could differ by a penny. The 0.30% minimum range filter (≈$2 on SPY) makes this insignificant.

### Step-by-step validation process:

**Step 1: Pull reference data from Massive API**
```
GET /v2/aggs/ticker/SPY/range/15/minute/{date}/{date}
```
For each day, extract the first 3 RTH bars → compute ORB high/low → determine if a breakout occurred.

**Step 2: Run Engine Lab backtest**
```bash
podman exec polygon-data-service python -m pytest tests/ -k "spy_orb" -v
```
Compare the trade log against the Massive API reference.

**Step 3: Load Pine script in TradingView**
Apply `SPY_ORB_Strategy.pine` to a 15-min SPY chart. Export the trade list.

**Step 4: Three-way comparison**
For each trading day, check:

| Check | Tolerance | Pass condition |
|-------|-----------|----------------|
| ORB high matches | ±$0.05 | All 3 systems agree on ORB high |
| ORB low matches | ±$0.05 | All 3 systems agree on ORB low |
| Entry signal (Y/N) | Exact | All 3 agree on whether a breakout occurred |
| Entry bar time | ±1 bar | Within 15 min of each other |
| Entry price | ±$0.10 | Account for close vs open-of-next-bar fills |

**Expected match rate: 95%+** (vs ~30% for EMA crossover). The remaining 5% would come from bar construction differences at the exact 15-min boundary.

---

## 3. Deliverables

| File | Description |
|------|-------------|
| `SPY_ORB_Strategy.pine` | TradingView Pine Script — ready to load |
| `PythonDataService/app/engine/strategy/algorithms/spy_orb.py` | Engine Lab implementation — follows same pattern as spy_ema_crossover.py |
| `SPY_ORB_Strategy_Plan.md` | This document |
| `SPY_EMA_Crossover_Validation_Report.md` | Root cause analysis of the original EMA divergences |

---

## 4. Massive API Integration for Ongoing Validation

To validate any single day's signals on-demand:

```python
# Pull one day's 15-min bars
GET /v2/aggs/ticker/SPY/range/15/minute/2026-04-14/2026-04-14

# Expected response for that day:
# Bar 1 (9:30): O=688.18 H=690.90 L=688.18 C=689.97
# Bar 2 (9:45): O=689.98 H=690.90 L=689.16 C=689.73
# Bar 3 (10:00): O=689.68 H=690.76 L=689.52 C=690.28
# → ORB_HIGH = 690.90, ORB_LOW = 688.18
# → Range = 0.395% ✓ (≥ 0.30%)
# Bar 4 (10:15): C=690.98 > 690.90 → ENTRY at 690.98
# Bar 9 (11:30): EXIT at close
```

This can be scripted as a daily validation check that compares Massive API signals against Engine Lab output, flagging any mismatches.

---

## 5. Next Steps

1. **Load the Pine script** in TradingView on a 15-min SPY chart and export trades
2. **Run the Engine Lab** strategy against the same date range
3. **Pull Massive API data** for the overlapping period and compute reference signals
4. **Compare all three** trade lists — expect 95%+ signal agreement
5. If validated, **deploy** the ORB strategy for live signal generation
