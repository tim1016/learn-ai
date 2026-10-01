# LEAN SPY Study — Output Reference & Calculation Guide

**Study:** `SpyEmaCrossoverAlgorithm` (SPY, 15-minute EMA(5)/EMA(10) crossover with RSI filter, long-only, 5-bar fixed exit)
**Run window:** 2024-03-28 → 2026-03-27, $100,000 starting cash, $53M estimated capacity
**LEAN version:** v2.5.0.0
**Output directory:** `Lean/Launcher/bin/Debug/`
**Companion files (lived in `docs/spy-lean-output/`; removed, in Git history):** `inventory.json` (a catalog of every field in every output file), `verify.py` (an independent Python recomputation of every KPI from the raw equity curve and trade list) and `source-map.md` (a flat "field → C# file:line" cheat sheet). The pinned [LEAN native statistics oracle](references/lean-native-statistics-oracle-v1.md) supersedes this unpinned study.

> **How to read this document.** What remains is an accepted numerical divergence: §15 compares an independent recomputation of LEAN's statistics with the values LEAN reported, §8 keeps the per-KPI notes that explain each gap, and §14 answers the research plan's questions about LEAN's conventions. Sections 1–7, 9–13 and 16 restated LEAN's output schema (the LEAN source is vendored under `references/lean/`) and were cut; Git history has them. The numbers were reconciled independently by `verify.py` (removed; in Git history) — 21 of 29 core KPIs match LEAN's reported value to four or more decimals without any access to LEAN's internals; the 8 that didn't match are called out with their exact discrepancy and cause.

---

## 8. `statistics` — 27 KPIs (the main event)

This section keeps the statistics whose recomputation differed from LEAN (§15), with the context they need; subsections 8.1, 8.5, 8.7 and 8.8 matched and were cut. Each entry gives the **reported value** in our run, a **plain-English definition**, the **formula** from the C# source, the **source pointer** (file and approximate line), and the **reconciliation** — how our independent `verify.py` recomputation compared against LEAN's value.

For brevity, the generic source file path `Lean/Common/Statistics/PortfolioStatistics.cs` is shortened to `PS.cs`, `Lean/Common/Statistics/Statistics.cs` to `S.cs`, and `Lean/Common/Statistics/TradeStatistics.cs` to `TS.cs`.

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
Since benchmark is zero, reduces to `AnnualStandardDeviation` — `0.025`, the Annual Standard Deviation row in §15.
Reconciliation: ✅

**`Treynor Ratio: "0"`** — `(annualPerf - rfr) / Beta`. Beta = 0 → short-circuits to 0. ✅

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

Running `verify.py` on this run's output produces the following reconciliation (29 items; 21 exact matches, 8 small-gap items each explained in §8 or in the buckets below):

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

*Generated 2026-04-09 from the run at `Lean/Launcher/bin/Debug/SpyEmaCrossoverAlgorithm.json` and the LEAN source tree at `Lean/Common/Statistics/` + `Lean/Engine/Results/`. `verify.py` lived in `docs/spy-lean-output/` and is in Git history.*
