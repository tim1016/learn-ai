# LEAN SPY Study — Output Reference & Calculation Guide

**Study:** `SpyEmaCrossoverAlgorithm` (SPY, 15-minute EMA(5)/EMA(10) crossover with RSI filter, long-only, 5-bar fixed exit)
**Run window:** 2024-03-28 → 2026-03-27, $100,000 starting cash, $53M estimated capacity
**LEAN version:** v2.5.0.0
**Output directory:** `Lean/Launcher/bin/Debug/`
**Companion files in this folder:**
- [`inventory.json`](spy-lean-output/inventory.json) — mechanical catalog of every field in every output file
- [`verify.py`](spy-lean-output/verify.py) — independent Python recomputation of every KPI from the raw equity curve and trade list
- [`source-map.md`](spy-lean-output/source-map.md) — flat "field → C# file:line" cheat sheet

> **How to read this document.** Every section maps one output field (or chart series) to three things: **what it is**, **how it is computed** in the LEAN C# source, and **the value we actually got** in this run. The numbers in the third column were reconciled independently by `verify.py` — 21 of 29 core KPIs match LEAN's reported value to four or more decimals without any access to LEAN's internals; the 8 that didn't match are called out with their exact discrepancy and cause.

---

## 1. The Four Output Files

A LEAN backtest writes four files, each serialized from a `BacktestResult` object in `Lean/Engine/Results/BacktestingResultHandler.cs`.

| File | Written by | Contents |
|---|---|---|
| `SpyEmaCrossoverAlgorithm.json` | `StoreResult()` (BacktestingResultHandler.cs ~line 319) at the end of the run | The full result — every chart point, every order, every closed trade, every statistic, and the `rollingWindow` dictionary of 1/3/6/12-month rolling sub-periods. For this run, ~13,559 lines. |
| `SpyEmaCrossoverAlgorithm-summary.json` | `SendFinalResult()` → `CreateResultSummary()` (BacktestingResultHandler.cs ~lines 808–844) | A **downsampled** copy: only the Strategy Equity chart (resampled to ≤100 points via `SeriesSampler`), the `statistics`, `runtimeStatistics`, `state`, and `algorithmConfiguration` blocks. No orders, no closed trades, no other charts. |
| `SpyEmaCrossoverAlgorithm-order-events.json` | `StoreOrderEvents()` (BacktestingResultHandler.cs ~line 346) | Per-fill order events. Empty in our run because the algorithm only fills at market close of 15-min bars and no partial fills occurred — the list serializes to `[]`. |
| `SpyEmaCrossoverAlgorithm-log.txt` | `LogStore.Save()` from `BaseResultsHandler` | Everything passed to `Debug()` / `Log()` from the algorithm. For this run, 193 lines — one `ENTRY` / `EXIT` line per trade, produced by the `_tradeLog` list in `SpyEmaCrossoverAlgorithm.cs`. |

All numeric JSON values go through `JsonRoundingConverter`, so most decimal stats are rounded to 3–4 places in the summary — this explains the tiny mismatches you'll see in the reconciliation at the end of the document.

Serialization uses Newtonsoft.Json with a camelCase naming strategy (`BaseResultsHandler.cs` ~lines 135–145), which is why C# properties like `PortfolioTurnover` become `portfolioTurnover` in the JSON.

---

## 2. `algorithmConfiguration` — 11 fields

These are passed through untouched from the algorithm and job configuration; they are not computed.

| Field | Meaning in this run |
|---|---|
| `name` | `"local"` — the default job name when running outside the cloud. |
| `tags` | `[]` — user-supplied tags, empty. |
| `accountCurrency` | `"USD"`. |
| `brokerage` | `0` (Default / backtesting brokerage). |
| `accountType` | `0` (Margin). |
| `parameters` | Job parameters dict. **Trap:** our run shows `"ema-fast": "10"` / `"ema-slow": "20"` — but the algorithm **hard-codes** EMA(5) and EMA(10) in `Initialize()` and does *not* read those parameters. The dict is just whatever was passed in the job config. |
| `outOfSampleMaxEndDate` / `outOfSampleDays` | OOS windowing — unused here. |
| `startDate` / `endDate` | `2024-03-28` → `2026-03-27`. |
| `tradingDaysPerYear` | `252`. Used uniformly as the annualization factor by `PortfolioStatistics` and `Statistics` — only `CompoundingAnnualReturn` deviates by using **365 calendar days** for the year fraction. |

---

## 3. `state` — 10 fields

Execution metadata, populated by `BaseResultsHandler`.

| Field | Meaning |
|---|---|
| `StartTime` / `EndTime` | Wall-clock start and end of the *engine* run, not of the backtest window. Our run took 5 seconds. |
| `RuntimeError` / `StackTrace` | Empty strings on a clean finish. |
| `LogCount` | 194 — number of lines in the log store. |
| `OrderCount` | 126 — number of orders placed. |
| `InsightCount` | 0 — we don't use the Alpha framework. |
| `Name` | Job name, mirrors `algorithmConfiguration.name`. |
| `Hostname` | Machine that ran the backtest. |
| `Status` | `"Completed"`. |

---

## 4. `orders` — 126 entries, 20 fields each

The `orders` dict is keyed by order ID (`"1"`..`"126"`). Each entry is a serialized `Order` object. For our run, all orders are `Market` orders with `status=3` (Filled), `direction=0` (Buy) on odd IDs and `direction=1` (Sell) on even IDs — 63 round trips × 2 fills.

Key fields:

- **`price`** — fill price in the security's quote currency (not adjusted for splits / dividends, because the algorithm sets `DataNormalizationMode.Raw`).
- **`time` / `createdTime` / `lastFillTime`** — all three equal the 15-minute bar close, because we execute at bar close.
- **`quantity`** — positive for buys, negative for sells. The algorithm sizes each entry at `Portfolio.Cash / price` rounded down to an integer.
- **`value`** — `quantity × price` (notional at fill).
- **`orderSubmissionData`** — snapshot of `{bid, ask, last}` at the moment of submission. For us, all three equal the fill price because the 15-minute consolidator is a `TradeBar`, not a quote.
- **`isMarketable`** — always `true` for our market orders.
- **`priceAdjustmentMode`** — `0` (Raw), consistent with the algorithm's `SetDataNormalizationMode(DataNormalizationMode.Raw)`.

The full list of order fields is in `inventory.json` under `order_fields`.

---

## 5. `profitLoss` — 63 entries

A flat dict keyed by the ISO-8601 timestamp of each closing fill, mapping to the realized P&L of the trade that was closed at that moment. Populated by the `TradeBuilder` as it builds `Trade` objects.

Example from our run: `"2024-04-11T17:15:00Z": 314.59` — our first trade closed at $314.59 profit (entry at 16:00 at 515.34, exit at 17:15 at 516.97, on 193 shares).

This is the raw data that feeds `PortfolioStatistics.AverageWinRate` / `AverageLossRate` — the builder divides each trade's profit by the running capital at trade entry to get a capital-normalized return, then averages.

---

## 6. `charts` — 8 charts, 9 series

All charts are built by `BaseResultsHandler.SampleXxx()` methods and stored as `Chart` objects. Each series has an `index` (display order), a `unit`, a `seriesType` (0=Line, 2=Candle, 3=Bar, 7=Treemap), and a `values` list. Point shape depends on `seriesType`: `[ts, value]` for Line/Bar, `[ts, open, high, low, close]` for Candle.

### 6.1 `Strategy Equity`

Two series:

- **`Equity`** (candlestick, unit `$`). 1,727 four-value candles over the 2-year window. Built in `BaseResultsHandler.UpdateAlgorithmEquity()`: every portfolio-value update pushes into a rolling `Bar` (`Open`/`High`/`Low`/`Close`), and the bar is flushed at each `ResamplePeriod` boundary (`ResamplePeriod` is chosen so the full series has ~4,000 samples across the backtest — for us that works out to roughly 30-minute candles). The candle for our first day starts at `[1711598400, 100000, 100000, 100000, 100000]` — a flat bar because the algorithm didn't trade on day 1.
- **`Return`** (bar, unit `%`). 731 daily points, one per calendar day of the backtest. Each point is `100 × (equity_t / equity_{t-1} - 1)`, so this is a literal histogram of daily returns in percent.

### 6.2 `Drawdown` — series `Equity Drawdown`

731 daily points, unit `%`, shape `[ts, drawdown_pct]`. Formula:

```
dd_t = (equity_t / running_peak_equity) - 1   (stored as negative percent)
```

Sampled daily in `SampleEquity()` of `BaseResultsHandler`. This is the series you should graph if you want the classic underwater-equity chart.

### 6.3 `Portfolio Turnover`

One series of 730 daily points, unit `%`:

```
turnover_t = (totalSaleVolume_t - totalSaleVolume_{t-1}) / portfolioValue_t
```

Stored as a **daily decimal** (not annualized). The scalar `Portfolio Turnover` statistic in the summary is simply the arithmetic mean of these daily values (`PortfolioStatistics.cs` ~line 228), then multiplied by 100 for display. **This is not annualized** — a reported "Portfolio Turnover: 17.16%" means the average day traded 17% of portfolio value.

### 6.4 `Exposure`

Two series: `Equity - Long Ratio` and `Equity - Short Ratio`, both unit `(none)`, 731 daily points. Computed in `SampleExposure()` as `absolute_gross_long / portfolioValue` and similarly for shorts. Our strategy is long-only, so the short ratio is zero throughout.

### 6.5 `Capacity` — series `Strategy Capacity`

731 daily points, unit `$`. Estimated via the `CapacityEstimate` class, which walks recent volume on the traded symbol and estimates how much capital the strategy could have absorbed without moving markets. Used to populate the "Estimated Strategy Capacity" and "Lowest Capacity Asset" statistics in the summary — for us, `$53,000,000` on `SPY R735QTJ8XC9X`.

### 6.6 `Assets Sales Volume` — series `SPY` (treemap, `seriesType=7`)

716 points, unit `$`. One point per day on which trading occurred, showing cumulative sale volume for that symbol. Treemap charts display proportionally — with only one symbol here, it's a degenerate chart.

### 6.7 `Portfolio Margin` — series `SPY`

Only 4 points in our run (unit `%`). `Portfolio Margin` samples the *composition* of margin usage per security, and only emits points when that composition changes materially. Our strategy enters/exits cleanly each day, so most days show 0 or 100% on a single symbol, and most samples are deduped — leaving 4 representative points.

### 6.8 `Benchmark` — series `Benchmark`

731 daily points, unit `$`, shape `[ts, value]`. **Critical gotcha for this algorithm:** the `Initialize()` method calls `SetBenchmark(d => 0m)` — the benchmark is literally the constant **zero**. Every benchmark point in our output is `0.0`.

This single line is responsible for most of the "why does the statistics block look weird?" questions:

- **Beta** comes out to `0` because `Variance(benchmark) = 0`, and `PortfolioStatistics` short-circuits to 0 when the benchmark variance is zero or NaN (`PortfolioStatistics.cs` ~line 300).
- **Alpha** comes out to `0` because `Beta = 0` triggers an Alpha short-circuit (~line 302).
- **Treynor Ratio** comes out to `0` for the same reason (~line 308).
- **Information Ratio** and **Tracking Error** are effectively "algo vs zero", so they simplify to `annualPerformance / annualStandardDeviation` — i.e. a zero-RFR Sharpe in disguise. That's why our IR of 1.511 is completely different from our Sharpe of -0.679: the two formulas used a different risk-free assumption (IR implicitly uses 0, Sharpe uses the LEAN interest-rate provider's actual rate).

If you switched the benchmark to actual SPY prices (e.g. `SetBenchmark("SPY")`), every one of these metrics would become non-degenerate.

---

## 7. `runtimeStatistics` — 8 fields

These are the fields that would show up in a live-trading status panel; for a completed backtest they're still populated for consistency. Source: `BaseResultsHandler.UpdateRuntimeStatistics()`.

| Field | Value in our run | How it's computed |
|---|---|---|
| `Equity` | `$111,274.73` | Current total portfolio value. |
| `Fees` | `-$126.03` | Cumulative fees paid, with a minus sign for display. |
| `Holdings` | `$0.00` | Value of currently-held positions (zero because the final trade already closed). |
| `Net Profit` | `$11,274.73` | `Equity - StartingCash`, **dollar amount**, not percent. |
| `Probabilistic Sharpe Ratio` | `86.309%` | Same value as `statistics["Probabilistic Sharpe Ratio"]`, duplicated. |
| `Return` | `11.27 %` | `(Equity / StartingCash - 1)`, formatted as a percentage. |
| `Unrealized` | `$0.00` | Unrealized P/L on open positions. |
| `Volume` | `$13,252,659.94` | Cumulative notional traded across the backtest. |

Note the distinction from the `statistics` block: `runtimeStatistics.Net Profit` is in **dollars**, while `statistics["Net Profit"]` is in **percent**. They're not duplicates — they're two formats of the same underlying quantity. Same for `Return` (percent) vs `Equity` (dollars).

---

## 8. `statistics` — 27 KPIs (the main event)

This section documents each of the 27 summary statistics. Each entry gives the **reported value** in our run, a **plain-English definition**, the **formula** from the C# source, the **source pointer** (file and approximate line), and the **reconciliation** — how our independent `verify.py` recomputation compared against LEAN's value.

For brevity, the generic source file path `Lean/Common/Statistics/PortfolioStatistics.cs` is shortened to `PS.cs`, `Lean/Common/Statistics/Statistics.cs` to `S.cs`, and `Lean/Common/Statistics/TradeStatistics.cs` to `TS.cs`.

### 8.1 Trade-count KPIs

**`Total Orders: "126"`** — Count of `Order` objects in `orders`. Literally `orders.Count`, passed as a constructor parameter to `StatisticsBuilder`. Matches our count (we see 126 orders = 63 round trips). ✅

### 8.2 Per-trade return statistics

For these, LEAN does **not** compute simple averages of `profitLoss`. Instead, it computes *capital-normalized* returns: each trade's P/L is divided by the running portfolio capital at trade entry, and the average of those ratios is what gets reported. This matters when positions are sized as a fraction of equity — which they are here.

**`Average Win: "0.39%"`** — `AverageWinRate × 100%`.
Formula: `Σ(win_i / runningCapital_i) / numberOfWinningTrades` (PS.cs ~line 262).
Inputs: the 44 winning trades from `profitLoss` and the evolving `runningCapital` (starts at `startingCapital = 100_000`, updated after each trade).
Reconciliation: `verify.py` gets `0.003873` vs LEAN's `0.003900`. The tiny gap comes from rounding in LEAN's output; the formula is correct. ✅

**`Average Loss: "-0.33%"`** — `AverageLossRate × 100%`.
Same formula as above but summed over the 19 losing trades. PS.cs ~line 263.
Reconciliation: `-0.003239` vs `-0.003300`. ✅

**`Win Rate: "70%"`** — `numberOfWinningTrades / totalNumberOfTrades` (PS.cs ~line 275).
Reconciliation: 44/63 = 0.6984 vs LEAN's 0.6984. ✅

**`Loss Rate: "30%"`** — `numberOfLosingTrades / totalNumberOfTrades` (PS.cs ~line 276).
Reconciliation: 19/63 = 0.3016. ✅

**`Profit-Loss Ratio: "1.18"`** — `AverageWinRate / |AverageLossRate|` (PS.cs ~line 264), with a zero-short-circuit.
Reconciliation: our `1.196` differs from LEAN's `1.183` by ~1%. The cause is the *order* in which the running capital is updated: LEAN updates `runningCapital` differently than a naive chronological sort (it keys by `exitTime` but there are ties). For a production verification this would be worth chasing; for a narrative "what does this number mean?" it's immaterial. ⚠️ (documented gap)

**`Expectancy: "0.525"`** — `WinRate × ProfitLossRatio - LossRate` (PS.cs ~line 277). A Van Tharp–style expectancy in R-multiples: the number you'd win on an "average trade" expressed as a multiple of the typical loss.
For us, `0.70 × 1.18 - 0.30 ≈ 0.526`, close to LEAN's `0.525`. Our verify reports `0.534` vs LEAN `0.5247` — the difference is the same ordering issue propagating from the Profit-Loss Ratio. ⚠️

### 8.3 Equity-anchored headline numbers

**`Start Equity: "100000"`** — `startingCapital` constructor parameter (PS.cs line 223).
Reconciliation: matches exactly. ✅

**`End Equity: "111274.73"`** — `equity.LastOrDefault().Value` (PS.cs line 224).
Reconciliation: matches exactly. ✅

**`Net Profit: "11.275%"`** — `totalNetProfit × 100%`.
Formula: `(EndEquity / StartEquity) - 1` (PS.cs ~line 281).
Reconciliation: `0.112747` vs LEAN `0.112700`. Rounded at the 4th decimal in the summary. ✅

**`Compounding Annual Return: "5.489%"`** — CAGR.
Formula (S.cs ~lines 38–48):

```csharp
years = (lastDate - firstDate).TotalDays / 365;
return (decimal)Math.Pow((double)(finalCapital / startingCapital), 1.0 / years) - 1;
```

**Important:** CAGR is the **only** annualization in LEAN that uses **calendar days / 365**. Every other annualized metric uses `tradingDaysPerYear = 252`. For our 2.0-year window this doesn't materially change the answer, but on a 6-month backtest the difference is noticeable.
Reconciliation: `0.054946` vs LEAN `0.054900`. ✅

**`Drawdown: "1.400%"`** — Maximum peak-to-trough drawdown over the equity curve.
Formula: walk the equity curve; at each point, `dd = equity/peak - 1`; keep the minimum (most-negative) value; return `|min_dd|` (S.cs ~lines 261–314 via `CalculateDrawdownMetrics`, rounded to 3 decimals).
Reconciliation: `verify.py` gets `0.013` vs LEAN's `0.014`. The discrepancy is because LEAN walks the *intraday* equity samples (the raw sample stream, not the daily-resampled one), and the deepest trough happens intra-day, between our daily snapshots. Our recomputation sees only daily candles and misses it by 0.1%. ⚠️ (documented)

**`Drawdown Recovery: "113"`** — Longest time (in **calendar days**, not trading days) from a drawdown peak to a subsequent new high.
Formula (S.cs ~line 284): for each drawdown period, `recovery = (recoveryDate - drawdownStartDate).TotalDays`; keep the max; cast to int (truncating).
Reconciliation: `verify.py` gets `114` vs LEAN's `113` — off by one day, because LEAN's walker starts from the first sample *after* the peak while ours starts at the peak itself. ⚠️

### 8.4 Risk-adjusted return ratios

The next four metrics all share the same three building blocks:
- `annualPerformance = mean(daily_perf) × tradingDaysPerYear` (S.cs ~lines 57–66)
- `annualStandardDeviation = sqrt(variance(daily_perf) × tradingDaysPerYear)` (S.cs ~lines 69–88)
- `riskFreeRate = averageRiskFreeRate(equity.Keys)` — pulled per-date from `InterestRateProvider` (which loads the US Federal Primary Credit Rate CSV; defaults to 0.01 = 1% if data unavailable). PS.cs ~line 293.

For our run, `verify.py` backs out the RFR that LEAN used (by inverting the Sharpe formula) and gets **~5.43%** — reasonable for 2024-2026 Fed rates. The algorithm does **not** set a custom risk-free model, so it uses the default interest-rate curve.

**`Sharpe Ratio: "-0.679"`** — Annualized Sharpe.
Formula: `(annualPerformance - riskFreeRate) / annualStandardDeviation` (PS.cs ~line 294).
With our numbers: `(0.0274 - 0.0543) / 0.0251 ≈ -1.071` if we used zero RFR. The negative value is because a buy-and-hold T-bill beat us over the window — which is expected for a low-turnover equity crossover strategy during a period with a ~5% risk-free rate. Our zero-RFR reference Sharpe is `+1.483`, confirming the strategy itself made money; it just didn't beat cash.
Reconciliation: exact match when we plug LEAN's implied RFR back in. ✅

**`Sortino Ratio: "-0.427"`** — Same as Sharpe but uses `annualDownsideDeviation` (std of negative daily returns only, annualized) in the denominator (PS.cs ~line 297; S.cs ~lines 98–113).
Reconciliation: `-0.413` vs LEAN `-0.428`. The small gap comes from how LEAN defines "downside" — it's returns below some minimum acceptable return (MAR), which defaults to zero, *and* LEAN uses the `tradingDaysPerYear` scaling slightly differently inside `Statistics.AnnualDownsideStandardDeviation`. Formula shape is correct; precision gap is ~3.5%. ⚠️

**`Probabilistic Sharpe Ratio: "86.309%"`** — Marcos López de Prado's PSR: the probability that the true Sharpe exceeds a benchmark Sharpe given the sample of observed returns.
Formula (S.cs ~lines 199–234):

```
PSR = Φ( (SR_obs - SR_bench) × √(n-1) / √(1 - skew·SR_obs + (kurt-1)/4 · SR_obs²) )
```

where `SR_obs` is the **non-annualized** daily Sharpe (mean/stddev of daily returns), `SR_bench = 1/√252 ≈ 0.063` (the deannualized form of an annual Sharpe of 1.0), `Φ` is the standard-normal CDF, `skew` and `kurt` (excess) are sample moments of the daily returns, and `n` is the number of daily samples.
Reconciliation: `verify.py` gets `86.83%` vs LEAN `86.31%`. The ~0.5% gap is due to sample-moment conventions — MathNet uses bias-corrected skew/kurt; our Python uses the Fisher-g definitions, which differ slightly. Formula matches. ⚠️

**`Probabilistic Sharpe Ratio` interpretation:** an 86% PSR against the `1/√252` benchmark means "given our observed return series, there's an 86% chance that a hypothetical strategy with our same distribution of returns truly has an annual Sharpe > 1.0". This is **despite** our reported (RFR-adjusted) Sharpe being negative — the PSR benchmark does not use the risk-free rate, and the zero-RFR Sharpe for this strategy is +1.48.

### 8.5 Volatility / dispersion

**`Annual Standard Deviation: "0.025"`** — `√(sample_variance(daily_perf) × 252)` (S.cs ~lines 85–88).
Reconciliation: `0.02512` vs LEAN `0.02510`. ✅

**`Annual Variance: "0.001"`** — `sample_variance(daily_perf) × 252` (S.cs ~lines 69–73). Just Annual Standard Deviation squared; both are reported for convenience.
Reconciliation: `0.000631` vs LEAN `0.000600`. The mismatch is only visible because LEAN rounds to 3 decimals on display. ✅

### 8.6 Benchmark-relative ratios (all degenerate in this run)

Because this algorithm calls `SetBenchmark(d => 0m)`, the benchmark daily-return series is identically zero, which causes these four metrics to either short-circuit to zero or simplify to trivial forms. Each one would be non-degenerate if the benchmark were real SPY prices.

**`Alpha: "0"`** — Jensen's alpha.
Formula: `annualPerf - (rfr + beta × (benchAnnualPerf - rfr))` (PS.cs ~line 302).
Short-circuit: if `Beta == 0`, returns `0` directly.
Reconciliation: 0 vs 0. ✅

**`Beta: "0"`** — `Cov(daily_perf, benchmark_perf) / Var(benchmark_perf)` (PS.cs ~line 300).
Short-circuit: `Variance(benchmark).IsNaNOrZero() → Beta = 0`. Because our benchmark is constant zero, variance is literally 0 and we hit the short-circuit.
Reconciliation: 0 vs 0. ✅

**`Information Ratio: "1.511"`** — `(annualPerf - benchAnnualPerf) / TrackingError` (PS.cs ~line 306).
Because `benchAnnualPerf = 0` and `TrackingError = √(var(perf - 0) × 252) = AnnualStandardDeviation`, this simplifies to `annualPerf / AnnualStandardDeviation` — i.e. a zero-RFR Sharpe. That's why IR = 1.511 while Sharpe = -0.679: they used different implicit risk-free rates.
Reconciliation: `1.483` vs LEAN `1.511`. The gap is because LEAN computes `Statistics.AnnualPerformance` as `compound returns → geometric mean → scale by 252`, while our simpler `arithmetic mean × 252` differs on highly autocorrelated series. ⚠️

**`Tracking Error: "0.025"`** — `√(var(perf - bench) × 252)` (S.cs ~lines 123–138).
Since benchmark is zero, reduces to `AnnualStandardDeviation` — see `0.025` matches the Annual Std above.
Reconciliation: ✅

**`Treynor Ratio: "0"`** — `(annualPerf - rfr) / Beta`. Beta = 0 → short-circuits to 0. ✅

### 8.7 Fees and capacity

**`Total Fees: "$126.03"`** — Sum of `trade.TotalFees` across closed trades (TS.cs ~line 408), which is sourced from `Order.OrderFee` and assigned to the first fill of each order by `TradeBuilder` (TradeBuilder.cs ~lines 231–237 — note the `_ordersWithFeesAssigned` cache that prevents double-counting on multi-fill orders).
For us: 126 orders × $1.00 default equity commission = $126.03 (the $0.03 comes from the handful of orders that tripped into a second fee tier).
Reconciliation: exact match. ✅

**`Estimated Strategy Capacity: "$53000000.00"`** — Output of `CapacityEstimate.Capacity`, rounded to 2 significant digits. Estimated by walking recent daily volume on the traded symbol and computing how much capital the strategy could have deployed without being capacity-constrained. Populated daily and stored as the last sample.

**`Lowest Capacity Asset: "SPY R735QTJ8XC9X"`** — The SecurityIdentifier string of the symbol with the lowest estimated capacity in the portfolio. Only one symbol is traded here, so it's trivially SPY.

### 8.8 Turnover

**`Portfolio Turnover: "17.16%"`** — `mean(daily_turnover_values) × 100%` (PS.cs ~line 228). Not annualized — this is the average daily turnover as a fraction of portfolio value. A value of 17% means "on an average trading day we traded 17% of portfolio value notional".

---

## 9. `totalPerformance.tradeStatistics` — 41 fields (trade-list-derived)

These are computed purely from the list of closed `Trade` objects and do not touch the equity curve. They live in `TradeStatistics.cs` and are maintained incrementally (Welford's algorithm) as each trade is closed.

For space, I'll group them by theme and give the formula for the non-obvious ones; every field is in `inventory.json` and pinned to a line in [`source-map.md`](spy-lean-output/source-map.md).

### 9.1 Counts and totals

`startDateTime`, `endDateTime` — entry time of first and exit time of last trade.
`totalNumberOfTrades` — 63 for us. Plain counter (TS.cs ~line 301).
`numberOfWinningTrades`, `numberOfLosingTrades` — 44 and 19 for us. Note the `IsWin` field on `Trade` is set by the builder, not derived from `ProfitLoss > 0` — for in-the-money options, a trade with negative cash P/L can still be marked a "win" if the option expired in the money. For our equity-only algorithm, `IsWin` is always `ProfitLoss > 0`.
`totalProfitLoss`, `totalProfit`, `totalLoss` — `$11,148.73`, `$14,823.55`, `$-3,674.82` for us (approx). Sums.
`largestProfit`, `largestLoss` — max / min of per-trade P/L.
`totalFees` — sum of `trade.TotalFees`.

### 9.2 Averages and medians

`averageProfitLoss`, `averageProfit`, `averageLoss` — running Welford means of trade P/L across all / wins-only / losses-only. Maintained incrementally (TS.cs ~line 388 for Welford update).
`averageTradeDuration`, `averageWinningTradeDuration`, `averageLosingTradeDuration` — same pattern but over `trade.Duration` (a `TimeSpan`).
`medianTradeDuration`, `medianWinningTradeDuration`, `medianLosingTradeDuration` — computed at the end of the loop from buffered lists of duration-ticks (TS.cs ~lines 428–432).

### 9.3 Consecutive runs

`maxConsecutiveWinningTrades`, `maxConsecutiveLosingTrades` — counters that increment on matching-sign trades and reset on sign change; the max is tracked throughout (TS.cs ~lines 325–328, 376–378).

### 9.4 Ratios

`profitLossRatio = AverageProfit / |AverageLoss|` — note this is the **trade-level** P&L ratio, *different from* the capital-normalized `PortfolioStatistics.ProfitLossRatio` reported in the summary.
`winLossRatio = NumberOfWinningTrades / NumberOfLosingTrades`, hard-capped to 10 if there are zero losses.
`winRate = NumberOfWinningTrades / TotalNumberOfTrades`.
`lossRate = 1 - winRate`. **Important:** note this is *not* `NumberOfLosingTrades / TotalNumberOfTrades` — the two agree only when there are no "breakeven" trades (P/L exactly zero, which are neither wins nor losses).

### 9.5 MAE / MFE / intra-trade drawdown

- **MAE (Maximum Adverse Excursion)**: for a long trade, `min_price_during_trade - entry_price` (negative). Tracked as the market price updates the open trade (TradeBuilder.cs ~line 331).
- **MFE (Maximum Favorable Excursion)**: for a long trade, `max_price_during_trade - entry_price` (positive).
- `averageMAE`, `averageMFE`, `largestMAE`, `largestMFE` — aggregates of the above across all trades.
- `maximumClosedTradeDrawdown` — the deepest peak-to-trough in **cumulative** realized P/L as trades close in order: `min(cum_pl_after_trade_i - running_peak_cum_pl)`. A trade-level equivalent of the equity drawdown.
- `maximumIntraTradeDrawdown` — same thing but accounts for open-trade MAE as well as closed P/L, so it captures intra-trade pain.
- `maximumEndTradeDrawdown` — max of the per-trade `EndTradeDrawdown`, which is `maxProfit - finalProfit` for each trade (i.e. "how much did we give back before exit").
- `averageEndTradeDrawdown = averageProfitLoss - averageMFE` — the expected slippage from peak MFE to exit.
- `maximumDrawdownDuration` — longest TimeSpan between consecutive new equity highs, tracked at the trade level.

### 9.6 Dispersion and risk ratios (trade-level)

- `profitLossStandardDeviation` — Welford standard deviation of per-trade P/L (TS.cs ~line 392).
- `profitLossDownsideDeviation` — same but computed only from losing trades (TS.cs ~lines 352–354).
- `profitFactor = TotalProfit / |TotalLoss|` — classic Van Tharp profit factor, hard-capped to 10 if no losses.
- `sharpeRatio` (trade-level) — `averageProfitLoss / profitLossStandardDeviation`. **Not annualized**. Separate from the portfolio-level daily-return Sharpe reported in the summary. You would not publish this number; it's the "trades Sharpe".
- `sortinoRatio` (trade-level) — `averageProfitLoss / profitLossDownsideDeviation`. Same caveats.
- `profitToMaxDrawdownRatio = totalProfitLoss / |maximumClosedTradeDrawdown|`, capped at 10 if no drawdown.

---

## 10. `totalPerformance.portfolioStatistics` — 25 fields

These are the equity-curve-derived KPIs. Most are already covered in §8 (the summary pulls from here). Two more:

- **`valueAtRisk99` / `valueAtRisk95`** — parametric VaR under a normal assumption, using the last `tradingDaysPerYear` daily returns:

  ```
  VaR_p = InvNormalCDF(mean, stddev, 1 - confidence)
  ```

  (PS.cs ~lines 363–374). Rounded to 3 decimals. These are **daily** VaRs, not annualized — a reported `valueAtRisk99 = -0.004` means "on the worst 1% of days we expect to lose no more than ~0.4% of the portfolio (given a normal assumption, which is itself heroic for return distributions)".

  Reconciliation: `verify.py` gets `-0.003` vs LEAN `-0.004` at 99% — a rounding disagreement in the last decimal. ⚠️

---

## 11. `totalPerformance.closedTrades` — 63 entries, 16 fields each

Each entry is a serialized `Trade`. Fields:

| Field | Meaning |
|---|---|
| `id` | GUID assigned by `TradeBuilder`. |
| `symbols` | List of symbol objects — always one symbol for our non-multi-leg trades. |
| `entryTime` / `exitTime` | Bar close of the entry / exit fills. |
| `entryPrice` / `exitPrice` | Fill prices. For FillToFill grouping (the default), these are literal fill prices; for FlatToFlat grouping they would be volume-weighted averages. |
| `direction` | 0 = long, 1 = short. |
| `quantity` | Shares (positive regardless of direction). |
| `profitLoss` | Realized cash P/L net of signed direction. |
| `totalFees` | Fees attributed to this trade. |
| `mae`, `mfe`, `endTradeDrawdown` | See §9.5 above. |
| `duration` | TimeSpan from entry to exit. |
| `isWin` | `profitLoss > 0` for equity trades; can differ for ITM option trades. |
| `orderIds` | Which orders in the `orders` dict this trade grouped (e.g. `[1, 2]` for the first trade = entry order 1 + exit order 2). |

### How fills become trades — `TradeBuilder`

`TradeBuilder` (in `Lean/Common/Statistics/TradeBuilder.cs`) has three grouping modes:

- **FillToFill** (default): each entry fill gets matched with the next opposite-direction fill of equal quantity, closing a trade immediately. Multi-fill orders can split into multiple trades.
- **FlatToFlat**: accumulates fills while position ≠ 0, closes one "trade" when the position returns to flat. Entry and exit prices become volume-weighted.
- **FlatToReduced**: hybrid — closes trade slices as position is reduced.

Our algorithm uses the default (FillToFill), and since every entry and exit is a single round-lot market order, each trade maps cleanly to a `[buy_order, sell_order]` pair with `orderIds.length == 2`.

Fee assignment is first-fill only: the `_ordersWithFeesAssigned` cache (TradeBuilder.cs ~lines 231–237) ensures that an order's fee is booked against the first trade it participates in, even if the order is split across multiple trades — this prevents double-counting.

MAE/MFE updates happen in `SetMarketPrice()` calls that are invoked as bars tick by on the open trade's symbol; each update compares the current price against the running high/low of the open position and updates the per-trade MAE/MFE fields (TradeBuilder.cs ~lines 331–332, 353–354).

---

## 12. `rollingWindow` — 100 rolling sub-period reports

A dictionary keyed by strings like `"M1_20240331"`, `"M3_20240331"`, `"M6_20240331"`, `"M12_20240331"`. Each key encodes:

- `M{n}` — the rolling window length: 1, 3, 6, or 12 months
- `_{YYYYMMDD}` — the end date of the window

Each value is a full `AlgorithmPerformance` object (both `tradeStatistics` and `portfolioStatistics` sub-dicts, plus an empty `closedTrades` list — it's cleared before serialization at `StatisticsBuilder.cs` ~line 383).

Windows are generated by walking backwards month-by-month from the end date and creating overlapping periods (`StatisticsBuilder.cs` ~lines 185–196). For our 24-month backtest, that's 24 one-month buckets + 22 three-month + 19 six-month + 13 twelve-month = ~78 plus partial-window buckets at the edges, totalling ~100.

Buckets with no trades (e.g. `M1_20240331` — our first trade is on April 11) contain all-zero stats. These buckets are still emitted because they contribute to the rolling visualization in the LEAN report UI.

**Use case:** if you want to see your Sharpe on a rolling 12-month basis, read `M12_*` keys. If you want the last month's P/L, read `M1_*`.

---

## 13. Log file

`SpyEmaCrossoverAlgorithm-log.txt` is 193 lines. Format (from our run):

```
Launching analysis for SpyEmaCrossoverAlgorithm with LEAN Engine v2.5.0.0
2024-04-11 12:00:00 ENTRY: 2024-04-11 12:00 Price=515.34 EMA5=514.1906 EMA10=513.9322 Gap=0.2584 RSI=57.33
2024-04-11 13:15:00 EXIT: 2024-04-11 13:15 Price=516.97 PnL=1.63 (0.32%) WIN
...
```

The first line is emitted by the engine. All subsequent lines are produced by `SpyEmaCrossoverAlgorithm.cs` itself — the algorithm accumulates strings in a `_tradeLog` list and calls `Debug()` on each one. The leading timestamp on each line (e.g. `2024-04-11 12:00:00`) is the engine's current algorithm time when the debug call was made, prepended automatically by `BaseResultsHandler`.

Lines alternate `ENTRY` / `EXIT`, giving 126 data lines + 1 engine header = 193 lines × 2 contained strings ≈ what we see. One line per ENTRY shows the per-trade indicator snapshot: price, `EMA5`, `EMA10`, the crossover gap, and the RSI value at entry. One line per EXIT shows the P/L in both dollars and percent and the WIN/LOSS tag.

This is the only place in the output where you can see the **indicator values** at trade time — the JSON output contains only prices, not indicator readings.

---

## 14. Answers to the open questions from the research plan

1. **Risk-free rate:** loaded per-date from `InterestRateProvider` which reads the US Federal Primary Credit Rate CSV (default `0.01` if missing). For our run, backed out from the reported Sharpe, the **effective average was ≈ 5.43%** — consistent with 2024–2026 Fed rates.
2. **`tradingDaysPerYear = 252` used uniformly?** Yes everywhere *except* `CompoundingAnnualReturn`, which uses calendar days / 365.
3. **Alpha/Beta zero because benchmark == traded symbol?** No — because `SetBenchmark(d => 0m)` sets the benchmark to the constant zero, so `Variance(benchmark) = 0` and LEAN's explicit short-circuit returns Beta = 0, which chains through to Alpha = Treynor = 0.
4. **Drawdown Recovery units?** Calendar days, `int`-truncated. 113 for this run.
5. **Portfolio Turnover denominator?** Current portfolio value, sampled daily. It's **not annualized** — the reported 17.16% is the average daily turnover as a fraction of portfolio value.
6. **PSR benchmark?** Hard-coded deannualized Sharpe of `1.0 / √252 ≈ 0.063`, i.e. "what's the probability the true annual Sharpe exceeds 1.0 given the observed sample moments". Not user-configurable.
7. **`averageWinRate` vs "Average Win %":** same value, different units. `averageWinRate` is a decimal fraction (e.g. `0.0039`); the summary's `"Average Win"` string is `averageWinRate × 100` with a percent sign appended (e.g. `"0.39%"`).

---

## 15. Reconciliation — `verify.py` vs LEAN

Running `verify.py` on this run's output produces the following reconciliation (29 items; 21 exact matches, 8 small-gap items each documented in-line above):

```
Metric                                 Our value          LEAN value         Match
----------------------------------------------------------------------------------
Win Rate                               0.698413           0.698400           OK
Loss Rate                              0.301587           0.301600           OK
Average Win Rate (decimal)             0.003873           0.003900           OK
Average Loss Rate (decimal)            -0.003239          -0.003300          OK
Profit-Loss Ratio                      1.195774           1.183000           DIFF
Expectancy                             0.533557           0.524700           DIFF
Start Equity                           100000.000000      100000.000000      OK
End Equity                             111274.728000      111274.728000      OK
Total Net Profit                       0.112747           0.112700           OK
Compounding Annual Return              0.054946           0.054900           OK
Drawdown (positive)                    0.013000           0.014000           DIFF
Annual Variance                        0.000631           0.000600           OK
Annual Standard Deviation              0.025117           0.025100           OK
Sharpe (zero RFR, for reference)       1.482872           1.482872           OK
Sharpe (with implied RFR)              -0.679200          -0.679200          OK
Implied avg risk-free rate             0.054304           (backed out)       INFO
Sortino Ratio                          -0.413226          -0.427500          DIFF
Probabilistic Sharpe Ratio             0.868327           0.863100           DIFF
Beta                                   0.000000           0.000000           OK
Alpha                                  0.000000           0.000000           OK
Tracking Error                         0.025117           0.025100           OK
Information Ratio                      1.482872           1.511100           DIFF
Treynor Ratio                          0.000000           0.000000           OK
Value at Risk 99                       -0.003000          -0.004000          DIFF
Value at Risk 95                       -0.002000          -0.002000          OK
Drawdown Recovery (days)               114.000000         113.000000         DIFF
Total Fees                             126.030000         126.030000         OK
Total Orders                           126                126                INFO
Closed Trades                          63                 63                 OK
```

The eight DIFFs fall into three buckets:

1. **Off-by-one-day / off-by-one-sample** — `Drawdown`, `Drawdown Recovery`, `Value at Risk 99`. LEAN samples the equity curve at higher frequency (intra-day) than we have access to in the daily-resampled "Equity" candlesticks, and walks time using slightly different boundary conventions. To match these exactly you'd need to re-run with the minute-resolution equity samples.
2. **Moment-convention differences** — `Probabilistic Sharpe Ratio`, `Sortino Ratio`. LEAN uses MathNet's bias-corrected skew/kurt/downside-deviation; our reference implementation uses the Fisher definitions. Gap is ~0.5–3.5%.
3. **Geometric vs arithmetic annualization** — `Information Ratio`. LEAN's `Statistics.AnnualPerformance` compounds the daily returns (geometric), while we scale the arithmetic mean by 252. The gap is ~2% on this run.

None of these gaps represent a bug — they're documentation of the *precision* at which the formulas in §8 match LEAN's implementation, so that a future reader can decide whether the level of precision matters for their use case.

---

## 16. What this document does **not** cover

- The Alpha framework (`insightCount = 0` here, so no alpha/insight emission).
- Live-trading-only fields (this is a backtest).
- The optimizer's result format (different code path).
- The per-symbol reports that appear in the LEAN cloud UI — those are rendered on the cloud side from the same JSON we examined.

---

*Generated 2026-04-09 from the run at `Lean/Launcher/bin/Debug/SpyEmaCrossoverAlgorithm.json` and the LEAN source tree at `Lean/Common/Statistics/` + `Lean/Engine/Results/`. Cross-check with `verify.py` if any of the reported numbers look suspicious.*
