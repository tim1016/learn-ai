# learn-ai Engine — Roadmap to TradingView-Aligned Trades

**Status:** Plan of record. Validated against the Day 4 trade-divergence run on SPY 15-min over Oct 9 2025 → Mar 27 2026 (3,010 RTH bars).

**Owner:** to be assigned.

**Companion documents (in `docs/`):**
- [`tv-polygon-validation-gotchas.md`](tv-polygon-validation-gotchas.md) — field guide to every divergence cause
- [`engine-phase-1-2-refined-plan.md`](engine-phase-1-2-refined-plan.md) — original LEAN-port plan
- [`lean-engine-phase1-verification-report.md`](lean-engine-phase1-verification-report.md) — original engine validation

**Companion report (in `Downloads/`):**
- [`learn-ai_EMA_Ingestion_Audit.md`](../Downloads/learn-ai_EMA_Ingestion_Audit.md) — earlier audit (this doc supersedes its recommendations)

---

## 0. One-page summary

Day 4 of the data-divergence research study quantified what the engine has to change for its trades to match TradingView's. The headline result: **the engine's indicator math is already correct**. Variant V-C (engine fed RTH-filtered Polygon bars) matched the TradingView baseline on **20/20** S1 trades and **34/35** S2 trades over the 131-day window. The remaining mismatch comes entirely from data-pipeline behavior, not from the indicator implementations.

Fixing four things in the order below will cause every existing strategy to produce trades materially aligned with TradingView's:

| # | Fix | Effort | Expected impact |
|---|---|---|---|
| 1 | **RTH session filter on bar handlers** | 5 lines of code | S1 alignment goes 3/20 → 20/20 vs TV |
| 2 | **Warmup buffer on `set_start_date`** | 10 lines + a date guard | Long-EMA/SMA p95 divergence drops ~95% |
| 3 | **Counter + log on `Indicator.update()` silent dedup** | 5 lines in base class | Surfaces upstream data-quality issues |
| 4 | **Use `bar.time` instead of `bar.end_time` in indicator updates** | 3 lines per strategy | Cosmetic-only — fixes trade-log timestamp alignment |

This document specifies each fix as a code change against the current `main`, the test that proves it works, and the expected before/after divergence metric.

---

## 1. Why this is the right roadmap (the data)

The Day 4 trade-divergence run produced four-variant trade counts for three strategies. The variants are:

| Variant | Indicators computed by | Bars consumed by indicators |
|---|---|---|
| **V-A** | TradingView Pine (`ta.*`) | TV BATS RTH-only |
| **V-B** | Native pandas (vetted) | Polygon consolidated, RTH-only |
| **V-C** | learn-ai streaming engine | Polygon consolidated, **RTH-only** |
| **V-D** | learn-ai streaming engine | Polygon consolidated, **full session** (current behavior) |

Per-strategy match rate against TV:

| Strategy | V-B aligned | V-C aligned | **V-D aligned** | V-D A-only | V-D B-only |
|---|---:|---:|---:|---:|---:|
| S1 EMA crossover | 20/20 | 20/20 | **3/20** | 12 | 11 |
| S2 RSI mean-rev | 34/35 | 34/35 | **5/35** | 13 | 19 |
| S3 SMA crossover | 9/10 | 9/10 | **0/10** | 9 | 21 |

The pattern is unmistakable: **V-A → V-C is essentially identity. V-C → V-D is the entire problem.** Therefore the highest-leverage change is whatever turns V-D into V-C — namely, RTH-filtering the bars that flow into indicators.

P&L consequence — V-D vs V-A:
- S1: +$26.64 vs +$12.50 (looks better, but the trades are completely different — dangerous)
- S2: −$27.45 vs +$28.25 (P&L sign flip)
- S3: −$78.88 vs −$28.61 (loss 2.8× larger)

---

## 2. Tier 1 — RTH filter on bar handlers (THE FIX)

### Symptom

Indicators in `spy_ema_crossover.py` (and any other strategy that doesn't explicitly filter sessions) are updated on **every** consolidated bar emitted by `TradeBarConsolidator`, including pre-market (04:00-09:29 ET) and after-hours (16:01-19:59 ET) bars. That's 64 bars/day instead of TradingView's 26.

### Cause (existing code)

`PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py`:

```python
def _on_fifteen_minute_bar(self, bar: TradeBar) -> None:
    assert self._ema5 is not None
    ...

    # Update indicators with consolidated bar close at EndTime.
    self._ema5.update(bar.end_time, bar.close)
    self._ema10.update(bar.end_time, bar.close)
    self._rsi14.update(bar.end_time, bar.close)
    ...
```

No session check. Compare to `spy_orb.py`, which already handles this correctly at line 142:

```python
def _on_fifteen_minute_bar(self, bar: TradeBar) -> None:
    ...
    # Filter to RTH only.
    if not self._is_rth(bar.end_time):
        return
    ...
```

### Fix — apply at strategy level

Add the same helper + guard to every strategy that uses consolidated bars and isn't already RTH-aware. Diff for `spy_ema_crossover.py`:

```diff
+from datetime import time as _dtime
+
+_RTH_OPEN = _dtime(9, 30)
+_RTH_CLOSE = _dtime(16, 0)
+
 class SpyEmaCrossoverAlgorithm(Strategy):
     ...
+    @staticmethod
+    def _is_rth(bar_time: datetime) -> bool:
+        t = bar_time.time()
+        return _RTH_OPEN <= t < _RTH_CLOSE
+
     def _on_fifteen_minute_bar(self, bar: TradeBar) -> None:
         assert self._ema5 is not None
+        if not self._is_rth(bar.end_time):
+            return
         ...
```

Same change in `spy_ema_crossover_options.py`. Anywhere else? Audit by:

```bash
grep -L "_is_rth\|RTH_OPEN" PythonDataService/app/engine/strategy/algorithms/*.py
```

Files in the output need this patch.

### Alternative — apply at consolidator level (architectural fix)

If we don't want every new strategy to remember the filter, do it once in the consolidator pipeline. Two options:

**Option A** — pre-filter at the data reader. Make `LeanMinuteDataReader.iter_bars` accept an `rth_only` flag and drop ETH bars before yielding. Pros: every consumer benefits. Cons: changes a low-level interface; some strategies might genuinely want ETH bars (none today, but possible).

**Option B** — make the consolidator session-aware. Add an optional `session_filter` callable to `TradeBarConsolidator` and refuse to fold non-RTH input into the working bar. Pros: localizes the change. Cons: alters consolidator semantics that other tests depend on.

Recommendation: do the strategy-level fix today (5 lines per strategy, immediate impact, no architectural risk). If we add three more strategies that need RTH-only data, then do Option B as a separate refactor.

### Test to add

`PythonDataService/Backend.Tests/...` — equivalent in Python is `app/engine/tests/test_spy_ema_crossover_rth_filter.py`:

```python
def test_eth_bars_do_not_update_indicators():
    """Pre-market bars at 08:30 should NOT advance EMA5/EMA10/RSI14."""
    algo = SpyEmaCrossoverAlgorithm()
    algo.initialize()  # creates _ema5, _ema10, _rsi14

    # 1) Feed 100 RTH bars to fully warm up
    rth_bars = make_synthetic_rth_bars(n=100, start_close=500.0)
    for b in rth_bars:
        algo._on_fifteen_minute_bar(b)
    ema_state_before = algo._ema5.current_value, algo._ema10.current_value, algo._rsi14.current_value

    # 2) Feed an ETH bar
    eth_bar = make_bar(time=datetime(2025, 4, 8, 8, 30, tzinfo=EASTERN), close=Decimal("9999"))
    algo._on_fifteen_minute_bar(eth_bar)
    ema_state_after = algo._ema5.current_value, algo._ema10.current_value, algo._rsi14.current_value

    assert ema_state_after == ema_state_before, "ETH bar should be ignored"
```

### Expected impact (measured)

Re-run `python -m app.research.divergence.analysis.run_trades` after the fix. Expect:

| Metric | Before | After |
|---|---|---|
| S1 V-D matched_aligned vs V-A | 3 | ~20 |
| S1 V-D total trades | 19 | ~20 |
| S2 V-D matched_aligned | 5 | ~34 |
| S3 V-D matched_aligned | 0 | ~9 |

If the after-numbers don't move, this fix wasn't applied to the right strategy, or there's a second contamination source we missed.

---

## 3. Tier 2 — Warmup buffer for long-period indicators

### Symptom

For the first ~2 months of any backtest, EMA(100), EMA(200), and SMA(200) values diverge from their fully-warmed equivalents by significant amounts. Day 3's matrix:

| Indicator | Engine vs TV (after Tier 1 applied) | Native (warmup masked) |
|---|---|---|
| EMA(100) p95 | 0.12 | 0.004 |
| EMA(100) max | 1.29 | 0.39 |
| EMA(200) p95 | **0.96** | 0.05 |
| EMA(200) max | **2.01** | 1.11 |
| SMA(200) p95 | **1.14** | 0.002 |
| SMA(200) max | **4.26** | 0.003 |

The bolded numbers are big enough to flip a borderline crossover signal. Specifically, the 4.26 SMA(200) max means S3 (golden cross) can fire on completely fictitious crosses during the first ~38 RTH trading days.

### Cause

`spy_ema_crossover.py:100`:

```python
def initialize(self) -> None:
    self.set_start_date(2024, 3, 28)
    ...
```

The engine starts ingesting bars on the configured start date and immediately starts recording indicator values. EMA(200)'s SMA seed at sample 200 happens around 38 RTH trading days into the run — so the first ~38 days have no EMA(200) at all, and the next ~150 bars have a still-warming EMA(200) where the seed dominates.

For TradingView's chart, the indicator has been warming for the entire visible chart history (often years) — so by the time any bar in the comparison window is shown, EMA(200) is fully converged.

### Fix

Two-step pattern. (1) Move `set_start_date` 90 RTH trading days earlier than the first day you want to trade. (2) Gate the actual trading logic on a date check.

```python
from datetime import date

# Inside initialize():
warmup_days_back = 90  # ~3 months of RTH calendar
trading_start = date(2024, 3, 28)
warmup_start = (
    pd.Timestamp(trading_start) - pd.tseries.offsets.BDay(warmup_days_back)
).date()
self.set_start_date(warmup_start.year, warmup_start.month, warmup_start.day)
self.set_end_date(2026, 3, 27)
self._trading_start = trading_start

# Inside _on_fifteen_minute_bar(), after the RTH check from Tier 1:
if bar.end_time.date() < self._trading_start:
    # Indicators still update (good — they need to warm up); only the
    # entry/exit logic is gated.
    self._update_prev_state(bar)
    return
```

The RTH filter from Tier 1 must come BEFORE this date guard so warmup doesn't include ETH bars either.

### Test to add

```python
def test_ema200_converges_before_trading_window():
    algo = SpyEmaCrossoverAlgorithm()
    algo._trading_start = date(2024, 3, 28)  # mock-injected
    # Replay 90 days of RTH bars from before the trading start
    for b in load_rth_bars("2024-01-01", "2024-03-28"):
        algo._on_fifteen_minute_bar(b)
    # EMA200 (or whichever long EMA you care about) should now be ready
    assert algo._ema200.is_ready  # if such an indicator exists
    # And no trades should have been placed yet
    assert len(algo.trade_log) == 0
```

### Expected impact

Re-run Day 3 indicator comparison. Expect engine-vs-TV figures to approach native-vs-TV:

| Indicator | Before | Target |
|---|---|---|
| EMA(200) p95 | 0.96 | ≤ 0.10 |
| EMA(200) max | 2.01 | ≤ 0.50 |
| SMA(200) max | 4.26 | ≤ 0.10 |

S3 (golden cross) trade count should drop sharply — most of the spurious crosses come from the warmup window.

### Why 90 trading days

EMA(200) weight on the initial seed decays as `(199/201)^n`. To reach 1% residual takes ~800 post-seed bars. On RTH-only 15-min bars (26/day), 800 / 26 ≈ 31 days. We pad to 90 to cover EMA(500), volatility regimes, and to give Wilder-smoothed indicators (RSI, ADX) extra room.

---

## 4. Tier 3 — Surface silent indicator-update drops

### Symptom

If the data pipeline ever feeds duplicate or out-of-order timestamps to an indicator, the update is silently dropped. The indicator value lags and nobody knows.

### Cause

`PythonDataService/app/engine/indicators/base.py:65-67`:

```python
if self._current_time is not None and time <= self._current_time:
    # Stale or duplicate — skip.
    return False
```

This is correct LEAN-equivalent behavior. But it's silent — no log, no counter, no parity-test signal.

### Fix

Add a counter + warning + a parity-test assertion hook:

```diff
 class Indicator(ABC):
     def __init__(self, name: str, period: int) -> None:
         self.name = name
         self.period = period
         self.samples: int = 0
+        self.dropped_updates: int = 0
         self._current_value: Decimal | None = None
         ...

     def update(self, time: datetime, value: Decimal) -> bool:
         ...
         if self._current_time is not None and time <= self._current_time:
+            self.dropped_updates += 1
+            if self.dropped_updates in {1, 10, 100, 1000, 10_000}:
+                logger.warning(
+                    "%s dropped %d updates (latest at %s; this update at %s)",
+                    self.name, self.dropped_updates, self._current_time, time,
+                )
             return False
         ...
```

### Test to add

In every parity test (`test_*_parity.py`):

```python
def test_no_indicator_updates_dropped(self, result):
    for ind in result.strategy.list_indicators():
        assert ind.dropped_updates == 0, (
            f"{ind.name} silently dropped {ind.dropped_updates} updates"
        )
```

### Expected impact

Today: zero observable behavior change (we don't think the pipeline has dupes). After a future regression that introduces dupes (DST, pagination seam, cache merge): the parity tests fail loudly instead of producing wrong indicators silently.

---

## 5. Tier 4 — Cosmetic: bar timestamp labeling

### Symptom

learn-ai's trade log shows entry/exit timestamps offset by exactly 15 minutes (and sometimes 75 minutes when combined with the 5-bar hold) from TradingView's view of the same trade.

### Cause

`spy_ema_crossover.py:131-134` updates indicators at `bar.end_time`:

```python
self._ema5.update(bar.end_time, bar.close)
self._ema10.update(bar.end_time, bar.close)
self._rsi14.update(bar.end_time, bar.close)
```

TradingView labels bars at their **open** time. learn-ai labels at their **close** time. The numeric values are identical; only the displayed time differs.

### Fix

Use `bar.time` (open time) for the indicator timestamp, since that's the convention TV and most charts use. Diff:

```diff
-self._ema5.update(bar.end_time, bar.close)
-self._ema10.update(bar.end_time, bar.close)
-self._rsi14.update(bar.end_time, bar.close)
+self._ema5.update(bar.time, bar.close)
+self._ema10.update(bar.time, bar.close)
+self._rsi14.update(bar.time, bar.close)
```

Apply the same change to the RTH check from Tier 1:

```diff
-if not self._is_rth(bar.end_time):
+if not self._is_rth(bar.time):
     return
```

(Both work — the bar is owned by the same RTH session whether you check open or close time, except possibly the very last 15-min bar of the session when checked at end_time. Using `bar.time` for both keeps it consistent.)

### Test to add

A trade-log alignment test that compares the existing engine output against the newly-aligned trade list and asserts entry/exit timestamps line up to the bar.

### Expected impact

Trade-log timestamps on V-D will exactly match TradingView labels for any matched trade. Aggregate P&L is unchanged.

---

## 6. Things that look like fixes but aren't

For completeness, three things the prior audit suggested or implied but Day 4 disproved as the root cause:

### 6a. EMA seed convention

The audit suggested LEAN's SMA-seeded EMA might differ from TradingView. **Confirmed not the issue:** native-pandas-with-SMA-seed (V-B) matches TV on every tested EMA to <5¢ median. learn-ai uses the same SMA seed. They agree. The original divergence was 100% the dividend-adjustment difference (gotcha #1) plus ETH contamination (Tier 1 above).

### 6b. EMA recursion alpha

Both Pine and learn-ai use `α = 2/(length+1)`. No change needed.

### 6c. Polygon's `adjusted=true` flag

Toggling this on/off does not improve trade alignment — it changes whether splits are adjusted, but SPY hasn't split since 2005 so the flag is a no-op for current data. The correct stance is: keep `adjusted=True` (it does the split adjustment we want), and document that **dividend** adjustment is not applied — backtests should run on unadjusted-for-dividends prices because that's what an executing trader actually pays. See gotcha #1 for the longer treatment.

---

## 7. Implementation order (recommended)

| Day | Item | Effort | Outcome |
|---|---|---|---|
| 1 | Tier 1 fix in `spy_ema_crossover.py` | 30 min | S1 V-D aligns |
| 1 | Tier 1 fix in `spy_ema_crossover_options.py` | 15 min | Options strategy aligns |
| 1 | Run Day 4 to verify | 5 min | S1 alignment 3/20 → 20/20 |
| 2 | Tier 2 warmup buffer in same files | 1 hour | EMA(200) p95 < 10¢ |
| 2 | Re-run Day 3 + Day 4 | 10 min | Full alignment |
| 3 | Tier 3 dedup counter in base class | 30 min | Future-proofing |
| 3 | Update parity tests to assert counter | 30 min | Catches regressions |
| 4 (optional) | Tier 4 timestamp labeling | 30 min | Cosmetic alignment |

Total: half a developer-week to land all four fixes plus tests.

---

## 8. Acceptance criteria

After all four fixes are in:

1. **S1 EMA crossover**: V-D matches V-A on at least 18/20 trades (allow 1-2 trades' worth of BATS-vs-Polygon feed noise).
2. **S2 RSI mean-rev**: V-D matches V-A on at least 30/35 trades.
3. **S3 SMA crossover**: V-D and V-A produce the same trade count ±1 and identical P&L sign.
4. **EMA(200) engine vs TV**: p95 ≤ 0.10, max ≤ 0.50.
5. **SMA(200) engine vs TV**: max ≤ 0.10.
6. **Trade-log timestamps**: when matched, entry/exit times are bit-identical between learn-ai and TV.
7. **Indicator dropped-updates counter**: 0 in every parity test.

The Day 4 pipeline (`python -m app.research.divergence.analysis.run_trades`) is the canonical verifier for items 1-3 and 4-5; the parity-test suite is the verifier for items 6-7.

---

## 9. Verification plan

Each fix has a measured before/after metric (see Tiers above). The recommended verification flow:

1. Apply Tier 1 fix.
2. `python -m app.research.divergence.cli all --tv ... --pg ... --tf 15m` — produces Day 3 + Day 4 outputs.
3. Compare `cache/divergence/15m/trades/match_summary.csv` rows for `(s1_ema_crossover, V-D)`. Confirm `matched_aligned_n ≥ 18`. Stop and debug if not.
4. Apply Tier 2 fix.
5. Re-run as in step 2. Confirm long-EMA divergence floor.
6. Apply Tier 3, then 4 with their dedicated unit tests.
7. Run the full `Backend.Tests` suite to confirm no regressions in unrelated parity tests.

If any step fails, do not proceed — back-track to the previous green state and diagnose with the matched/flipped trade lists in `cache/divergence/15m/trades/`.

---

## 10. What this does not solve

For full transparency, the changes above bring the engine's trades to 90-95% alignment with TradingView. The residual ~5% comes from:

- **BATS vs Polygon-consolidated feed differences** (gotcha #10): ~1¢ per bar. Floor of agreement; nothing learn-ai can do about this without switching feeds.
- **SuperTrend direction-flip ambiguity** at regime changes (gotcha #14): can move SuperTrend-based exits by one bar. Not currently used in our strategies; documented for future-proofing.
- **ADX Wilder-compounded long tail** (gotcha #13): ADX values can disagree by 1-2 points around boundary thresholds. Mitigation is to widen ADX-gate buffers (e.g., exit at ADX < 14 instead of < 15) rather than chase exact alignment.

These three sources together cap our achievable alignment at ~95-98% per strategy, which is the "physical" limit of comparing two independent data pipelines. Anything beyond that requires running the same indicator code on the same input feed, which defeats the purpose of having an independent implementation in the first place.

---

## 11. Cross-references

| Topic | See |
|---|---|
| Why ETH bars contaminate indicators | gotcha #2 |
| Why dividend adjustment looked like the EMA bug | gotcha #1, plus correction note in audit |
| Why my native-pandas matches TV but engine didn't | Day 3 indicator matrix |
| The original engine audit (now superseded for the EMA-warmup hypothesis) | `Downloads/learn-ai_EMA_Ingestion_Audit.md` |
| The Pine script the validation uses | `Downloads/learn-ai_tv_indicator_dump_v6.pine` |
| The TV export procedure | `Downloads/learn-ai_tv_export_procedure.md` |
| The strategy-level trade matching | `cache/divergence/15m/trades/match_summary.csv` |
| The `Indicator.update()` silent dedup | `app/engine/indicators/base.py:65-67` |
| The bar handler that needs the RTH filter | `app/engine/strategy/algorithms/spy_ema_crossover.py:125` |
