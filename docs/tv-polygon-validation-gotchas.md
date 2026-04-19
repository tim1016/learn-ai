# TradingView-vs-Polygon Validation — Field Guide to Gotchas

**Purpose.** Every gotcha surfaced while standing up the data-divergence research study, organized as a lookup table so the next person hitting the same symptom finds the root cause in one click.

**Scope.** Applies to any comparison between a TradingView-exported CSV and a Polygon-sourced DataFrame inside learn-ai's `PythonDataService`. Several items apply more broadly to any indicator validation work.

**Ordering.** By impact on bar-level price / indicator values, descending. The top few are the ones that create large visible discrepancies; the bottom few explain small residuals.

Format for each gotcha:
- **Symptom** — what you observe before knowing the cause
- **Cause** — what's actually happening
- **Impact** — magnitude on data / indicators / trades
- **Detection** — how to confirm it's this one, not something else
- **Fix** — what to change

---

## 1. TradingView adjusts for dividends; Polygon does not

- **Symptom.** Per-bar close-price gap between TV BATS and Polygon consolidated is in the $1-$4 range, decreasing monotonically toward "today" with visible step-down changes on what turn out to be SPY ex-dates.
- **Cause.** TradingView's default chart shows dividend-adjusted prices: historical prices are reduced by the sum of all dividends paid between that bar and "today." Polygon's `fetch_aggregates(..., adjusted=True)` only adjusts for **splits**, not dividends.
- **Impact.** Huge. Indicators computed on TV prices are offset by the cumulative future dividend from each bar. On SPY in a 7-month window, bars near the start of the window are depressed by ~$3.75. Downstream EMAs inherit the same offset. A previous team-internal validation report ascribed this to "EMA warmup convergence" — it wasn't; it was dividend adjustment end-to-end.
- **Detection.**
  1. Compute per-day TV-minus-Polygon close gap.
  2. Sort by date and look at the first-difference. If you see discrete ~$1.80-$2.00 step changes around the third Friday of March / June / September / December, it's SPY dividends.
  3. Use `app.research.divergence.ingest.detect_dividends_from_gap(merged, min_step=0.30)`; if it returns any events after you've "adjusted," your input is still wrong.
- **Fix.** Two good options:
  - **User-side:** In the TradingView chart, Settings → Symbol tab → uncheck "Adjustment for dividends," reload the tab, re-export.
  - **Code-side:** `reverse_dividend_adjustment(tv_df, SPY_DIVIDENDS)` in `app.research.divergence.ingest.dividend_adjuster`. Adds each dividend back to bars whose date is strictly before that dividend's ex-date.
- **Do not confuse with.** Gotcha #3 (warmup). Those look similar in monthly/quarterly charts but have different signatures — dividend adjustment produces step-function gaps, warmup produces exponentially-decaying gaps.

## 2. Polygon returns full-session bars; learn-ai's consolidator doesn't filter RTH by default

- **Symptom.** learn-ai's 15-min SPY bars number ~63 per day; TradingView's BATS_SPY at the same timeframe is 26 per day. EMA values computed by learn-ai on "the same" series come out 2.46× noisier than TV's.
- **Cause.** `polygon_export._polygon_bar_to_trade_bar` and `LeanMinuteDataReader.iter_bars` deliberately keep pre-market + after-hours minute bars (the file comment says: "Bars outside regular trading hours are kept — callers that want RTH-only data should filter upstream"). `TradeBarConsolidator` floor-rounds to wall-clock boundaries and has no session concept. Only `spy_orb.py` currently applies an RTH filter at the strategy layer; `spy_ema_crossover.py` does not.
- **Impact.** Very high for indicator correctness. A 5-min EMA can end up covering 25 minutes of thin pre-market prints instead of 25 minutes of RTH price action, contaminating the value well into the actual trading day. Causes ~18 MarketScope-only trades per 2-year SPY run that TV never fires.
- **Detection.**
  1. Count minute bars in any one day's LEAN zip: full-session days hold 951 bars; RTH-only is 391.
  2. Confirm the strategy doesn't filter: grep `_on_fifteen_minute_bar` for `_is_rth` or equivalent; if absent, you're eating ETH.
- **Fix.** Add an RTH filter at the top of the bar handler — the same pattern `spy_orb.py` uses:
  ```python
  @staticmethod
  def _is_rth(bar_time: datetime) -> bool:
      t = bar_time.time()
      return _RTH_OPEN <= t < _RTH_CLOSE

  def _on_fifteen_minute_bar(self, bar: TradeBar) -> None:
      if not self._is_rth(bar.end_time):
          return
      ...
  ```
  Alternative: filter at ingest time by passing `rth_only=True` to `resample_ohlcv` (the research module's default).

## 3. EMA warmup — seeding convention and convergence decay

- **Symptom.** Short EMAs (5, 10) agree between learn-ai and TV to 4+ decimal places, but long EMAs (100, 200) are ~$0.50 apart for the first ~2 months of any new run, converging to near-zero over time.
- **Cause.** Two compounding factors:
  1. *Seed.* LEAN / learn-ai uses SMA-of-first-`period`-samples as the EMA seed (matches Pine v4+). Some libraries (older pandas defaults) use first-value seeding. When both sides use SMA seeds, they agree exactly given the same input.
  2. *History.* TV's chart comes pre-warmed with years of off-chart history; learn-ai's engine starts EMA computation on the first bar of its configured `start_date`. EMA(200) weight on the initial seed decays as `(199/201)^n`; to reach 1% residual takes ~800 post-seed bars ≈ 38 RTH trading days.
- **Impact.** Medium during the first ~2 months of a new run; near-zero afterward. The effect is independent of dividend or session-filter issues (i.e., even after fixing those, expect this residual).
- **Detection.** Plot `|EMA_learn-ai − EMA_TV|` vs bars-since-start. If it fits `a · exp(-b·n) + c` with `c ≈ 0`, this is warmup. If `c` is non-zero and constant, look for another cause (usually dividend adjustment — see #1).
- **Fix.** Extend `set_start_date` 90 RTH trading days before the first date you want to trade, then gate the actual trading logic with a date check:
  ```python
  self.set_start_date(2024, 1, 1)             # warmup begins here
  ...
  if bar.end_time.date() < date(2024, 3, 28): # actual trading begins here
      return
  ```

## 4. TradingView plan tier limits bar-export count

- **Symptom.** "Export chart data" returns dramatically fewer rows than expected. A requested 2-year 15-min export yields 6.5 months (~3,700 bars); a 5-min export yields ~3,000 rows.
- **Cause.** TradingView caps max bars per chart by plan:
  - Free: ~5,000
  - Essential / Plus: ~10,000
  - Pro+: ~10,000
  - Premium: ~20,000
  Plus had you covered for 2-year 1-hour but not 2-year 15-min.
- **Impact.** Study window shrinks from requested to what fits. If 6 months is enough for the analysis, fine; if not, you must stitch multiple exports.
- **Detection.** Compare expected RTH bar count (26 per day × N days) to actual row count of the CSV.
- **Fix.** Three options:
  - Accept the smaller window. For divergence-study purposes 131 trading days × 26 bars is plenty.
  - **Stitch.** Use TV's date picker (bottom-left of chart) to jump back N months, scroll back to fill, export. Repeat for earlier windows. Merge in the ingestor — it deduplicates by timestamp.
  - Upgrade plan. $60 Premium × 1 month unblocks 2-year 15-min and 1-year 5-min.

## 5. Pine `time` is milliseconds; chart CSV `time` is seconds

- **Symptom.** Cross-check of Pine-emitted `bar_time_unix_s` against the chart's `time` column reports "mismatch on every row." Ratio is exactly 1000×.
- **Cause.** In Pine v5/v6, the built-in `time` variable is UTC **milliseconds** since epoch. TradingView's CSV export's `time` column is **seconds** since epoch. The Pine script name (`bar_time_unix_s`) is a misnomer — it's actually ms.
- **Impact.** Cosmetic if you use the chart's `time` column as the source of truth (recommended). Real bug if you ever blindly trust Pine's `time` as seconds.
- **Detection.** `df['bar_time_unix_s'].iloc[0] / df['time'].iloc[0]` returns ~1000.
- **Fix.** `tv_ingest.py` normalizes by detecting the 1000× ratio and dividing. If you rename the Pine column to `bar_time_unix_ms`, remove the normalizer.

## 6. TradingView requires scrolling back to load history before Pine computes

- **Symptom.** CSV export is tiny (~400 rows) regardless of the chart's configured date range.
- **Cause.** TradingView only computes Pine for bars actually loaded in the chart viewport. A freshly-opened chart loads only enough bars to fill what's on screen. "Export chart data" only sees those loaded bars.
- **Impact.** Massive under-export if you don't notice.
- **Detection.** Row count in the CSV is ~400 (or your current zoom level × 1-ish days' worth).
- **Fix.** Before exporting: click in the chart, press and hold the left arrow key (or click-drag leftward) until bars stop loading. Wait 5-10 seconds for Pine to compute on the newly-loaded bars. Then export.

## 7. Bar timestamp labeling — open vs end time

- **Symptom.** Trade-log entry / exit times are 15 minutes later in learn-ai than in TradingView for the exact same bar.
- **Cause.** `spy_ema_crossover._on_fifteen_minute_bar` updates indicators at `bar.end_time` (close of the 15-min window). TradingView labels bars at `bar.time` (open of the 15-min window). Both label the same bar; the difference is which side of the 15-min interval names it.
- **Impact.** Purely cosmetic — the fill price is identical, the P&L is identical. But anyone comparing trade logs side-by-side thinks there's a 15-min lag.
- **Detection.** Compare entry prices on a matched trade. If prices are identical (to the cent) and only the timestamps differ by exactly 15 min, this is it.
- **Fix.** If bit-exact trade-log parity is required: change `bar.end_time` → `bar.time` in the three `self._emaX.update()` calls. No value changes, only display.

## 8. Combined with a 5-bar hold: the "75-minute offset"

- **Symptom.** In cross-system trade logs, learn-ai's entry/exit pairs are consistently 75 minutes later than TV's equivalent trades.
- **Cause.** Two separate 15-min shifts compounding. (a) Labeling (#7): one 15-min interval. (b) The `holdBars = 5` exit rule exits 5 × 15 = 75 min after the entry signal bar — and because learn-ai labels at close and TV at open, the exit label is offset by another one-bar shift. Net effect: logs look shifted by 75 min even though fills happen on the same bars.
- **Impact.** Cosmetic. P&L unchanged. Confused analysts.
- **Detection.** For any matched trade pair in the log, subtract `learn_ai_entry_time - tv_entry_time`. If the answer is a multiple of 15 minutes and matches `holdBars × 15` for at least half of matches, it's this.
- **Fix.** Same as #7, plus report exit times by the signal bar's open time instead of the exit-fill bar's close time. Pure presentation choice.

## 9. `Indicator.update()` silently drops any `time <= current_time`

- **Symptom.** Indicators appear to lag; a specific bar's indicator update seems to have been skipped. No error, no log line.
- **Cause.** `app.engine.indicators.base.Indicator.update` guards:
  ```python
  if self._current_time is not None and time <= self._current_time:
      return False
  ```
  This is correct for LEAN's intended dedup-on-identical-timestamp behavior, but it's silent. Any input that's out-of-order (pagination seams, merged caches, DST fold-back) gets thrown away.
- **Impact.** Potentially high. Silent data loss is the worst kind of bug because nobody goes looking for it.
- **Detection.** Add a counter:
  ```python
  self._dropped_updates = 0
  ...
  if ...:
      self._dropped_updates += 1
      if self._dropped_updates % 100 == 1:
          logger.warning("%s dropped %d at %s", self.name, self._dropped_updates, time)
      return False
  ```
  Then assert `== 0` in parity tests.
- **Fix.** Add the logging. In addition, sort and dedup at the ingestion layer so this guard never fires in practice.

## 10. BATS is not "the SPY feed" — it's one venue

- **Symptom.** After fixing #1 (dividends) and all other adjustment issues, TV-vs-Polygon close prices still disagree by ~1¢ median, ~3¢ at p95, occasionally ~15¢ at extremes.
- **Cause.** TradingView's "BATS_SPY" ticker is the Cboe BZX exchange's print stream only. Polygon's "consolidated aggregates" are SIP-sourced across all venues (NYSE Arca, Cboe BZX, NASDAQ, IEX, etc.). Different venues can print at slightly different prices within the same minute.
- **Impact.** Small. 1-3¢ per bar. Never crosses a dollar unless something much bigger is wrong.
- **Detection.** This is the *floor* of agreement. If you've fixed everything else and still see single-cent noise, you're done.
- **Fix.** None for equality. Options to narrow further:
  - Use Polygon single-exchange BATS aggregates (`/v2/aggs/ticker/SPY/range/...?exchange=BATS`) — if supported — to match feeds.
  - Accept the noise floor.
- **Volume note.** BATS volume ≈ 3-4% of consolidated SPY volume. Don't expect them to match; they shouldn't.

## 11. Pine `ta.rsi` source — close by default, hlc3 in some chart presets

- **Symptom.** RSI from learn-ai and TV disagree by 2-5 points systematically, even after everything else is aligned.
- **Cause.** TradingView's default *chart-level* RSI indicator sometimes uses `hlc3` as source, especially on older saved charts. Pine's `ta.rsi(src, 14)` uses whatever `src` you pass. If the user re-exports with `src = hlc3` but learn-ai computes on `close`, they diverge.
- **Impact.** Small-medium. RSI-based strategies become unreliable.
- **Detection.** Compute RSI two ways locally — once on close, once on hlc3 — and check which matches TV.
- **Fix.** Our Pine script hard-codes `src = close` at the top; keep it that way. If a user ever says "I want the default TV indicator to match," ask what `source` the default is set to.

## 12. Half-day trading sessions

- **Symptom.** Some dates have 14 bars instead of 26, tripping a rigid "bars-per-day = 26" assertion.
- **Cause.** Day-after-Thanksgiving, day before July 4th, December 24th, etc. are half-days — exchange closes at 13:00 ET.
- **Impact.** False validation errors if you assert exact bar counts.
- **Detection.** Group by date, look at rows with `count < expected`.
- **Fix.** Use `expected` as a soft check (flag, not raise). Allow `count < 26` when `count in {14, others}` that match the US equities half-day calendar.

## 13. ADX + Wilder smoothing has the longest-tail convergence

- **Symptom.** After fixing all major discrepancies, ADX is the most-divergent indicator with mean absolute difference of ~0.58 on matched 15-min bars (even while short EMAs match to 4 decimals).
- **Cause.** ADX is Wilder-smoothed (length 14) over Wilder-smoothed DM and TR — compounding two levels of Wilder smoothing. Small input differences amplify through both layers. Warmup convergence is slower than any single-pass EMA.
- **Impact.** Medium for ADX-based filters. ADX thresholds at 15 or 25 can flip around the boundary even when both inputs are otherwise aligned.
- **Detection.** Expected: even on near-identical inputs, ADX differs slightly more than any EMA.
- **Fix.** Increase the ADX threshold buffer (e.g., exit at ADX < 14 instead of ADX < 15) to avoid boundary flicker, or widen all ADX-based filters by 1-2 points.

## 14. SuperTrend direction is bit-exact; line value can jump a lot at flip bars

- **Symptom.** `supertd_10_3` matches between systems 100% of the time; `supert_10_3` (the line value) occasionally differs by $5-$8 on a single bar, with zero error elsewhere.
- **Cause.** At a SuperTrend flip (uptrend → downtrend or vice versa), the line value swaps from the upper band to the lower band (or vice versa). Different implementations might flip one bar earlier or later depending on how the equality `close == upper_band` is resolved. The direction value is unambiguous, the line value at the flip bar is not.
- **Impact.** Only at regime changes. Doesn't affect trend-following strategies that gate on direction, only those that use the line value as a stop.
- **Detection.** Bars where `|supert_diff|` is large are also bars where direction changed. Restrict to trend-continuation bars for a fair comparison.
- **Fix.** When validating, compare direction (exact match expected) separately from line value (where flip bars are outliers). Don't compute RMSE on the line value across flips — it'll look worse than reality.

## 15. Polygon VWAP doesn't aggregate additively

- **Symptom.** Resampling 1-min Polygon bars to 15-min with a simple `mean` on VWAP produces VWAP values that drift slightly from what TV/Polygon returns at the 15-min aggregate level.
- **Cause.** VWAP is `Σ(price × volume) / Σ(volume)`. Its weighted average doesn't commute with a simple mean.
- **Impact.** 1-10¢ bar-level VWAP divergence; nobody computes indicators on VWAP directly so usually cosmetic.
- **Detection.** Expected: simple-mean VWAP ≠ volume-weighted VWAP when per-minute volume varies.
- **Fix.** When resampling, recombine as
  ```python
  vwap_new = (vwap_1min * volume_1min).sum() / volume_1min.sum()
  ```
  `app.research.divergence.ingest.polygon_ingest.resample_ohlcv` does this correctly.

## 16. Pine script plot count + display modes

- **Symptom.** Some Pine-emitted values come out as `NaN` in the exported CSV for no apparent reason.
- **Cause.** `plot(..., display=display.none)` is excluded from the data export in some TradingView versions. Conversely, `plot` called on a tuple's unused slot leaks a 0-valued phantom column.
- **Impact.** Low if you pay attention; confusing if you don't.
- **Detection.** Eyeball CSV columns; any all-NaN or all-zero column is suspicious.
- **Fix.** The dump script uses explicit named `plot(..., title="…")` calls, never anonymous tuple plots. For columns you want in data-window-only, use `display=display.data_window`, not `display.none`.

## 17. TradingView account-level preferences can silently pin settings

- **Symptom.** A chart-level toggle (e.g., "Adjustment for dividends") doesn't take effect even after multiple tries.
- **Cause.** Some TV settings are account-level defaults; others are chart-level. If a chart was saved with a pinned layout, reopening the chart restores the pinned defaults regardless of per-toggle changes.
- **Impact.** You think you've changed something and it reverts on the next export.
- **Detection.** Compare the newly-exported CSV's prices against a previous export for the same symbol / timeframe / date. If they're byte-identical, the toggle didn't take.
- **Fix.** Close the tab entirely. Open a new tab. Navigate to the symbol. Re-check the toggle. Reload once. Export.

---

## Quick-reference symptom → gotcha map

| Observed symptom | Most likely gotcha |
|---|---|
| Close-price gap > $1, step-changes on calendar quarter boundaries | #1 Dividends |
| learn-ai emits ~2.5× more bars per day than TV | #2 ETH contamination |
| Short EMA matches; long EMA off by < $1 | #3 Warmup |
| Requested 2 years, got 6 months | #4 TV plan limits |
| 1000× ratio in a cross-check column | #5 ms vs s |
| CSV has ~400 rows no matter what | #6 Didn't scroll back |
| Trade timestamps off by 15 min | #7 End-time vs open-time |
| Trade timestamps off by 75 min | #8 Hold period + labeling |
| Indicator seems to skip a bar | #9 Silent dedup |
| All differences < 3¢ after fixing everything | #10 Feed noise floor |
| RSI off by 2-5 points | #11 `ta.rsi` source |
| Day N has 14 bars instead of 26 | #12 Half-day |
| ADX is the worst-matching indicator | #13 Wilder compounding |
| `supert_10_3` diverges by $5+ on a single bar | #14 Flip ambiguity |
| Aggregated VWAP looks wrong | #15 Not volume-weighted |
| Pine column is all-NaN or all-zero | #16 Display mode |
| Toggle doesn't stick | #17 Chart pinning |

---

## Workflow sanity checklist before trusting any comparison

1. TV CSV has all 25 Pine columns + chart OHLCV (26 total indicator-related columns).
2. Bar count per day matches RTH expectation (26 for 15m full day, 14-26 otherwise).
3. **Dividend adjustment off** in TV, or reverse-applied in code. Verify by detector: `detect_dividends_from_gap(...)` returns zero steps > $0.30.
4. RTH-only applied to Polygon side before resample; median close gap should be < 5¢.
5. BATS feed noise floor: median 1¢, p95 3¢, max ≤ 15¢. If larger, something else is wrong.
6. Matched bar count ≥ 95% of TV bar count (inner join on time_utc).
7. Indicator NaN patterns look sane — EMA(200) warmup region is NOT all-NaN because TV pre-warms.

If any of these fail, walk the gotcha list above before writing new code.

---

## Related documents

- `Downloads/learn-ai_EMA_Ingestion_Audit.md` — earlier audit of learn-ai's engine that misdiagnosed gotcha #1 as warmup (#3). Correction note pending.
- `Downloads/Research_Plan_TV_vs_Polygon_Divergence.md` — the research plan this work executes.
- `learn-ai_tv_indicator_dump_v6.pine` — the Pine dump script this document assumes.
- `learn-ai_tv_export_procedure.md` — the step-by-step export procedure.
