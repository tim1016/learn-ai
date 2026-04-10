# LEAN Output Field → Source-File Cheat Sheet

Flat index mapping every field in the SPY study output to the C# file and
approximate line where it is declared and computed. Use together with the
main report (`../spy-lean-output-report.md`) and `inventory.json`.

Abbreviations:
- `PS.cs` = `Lean/Common/Statistics/PortfolioStatistics.cs`
- `TS.cs` = `Lean/Common/Statistics/TradeStatistics.cs`
- `S.cs`  = `Lean/Common/Statistics/Statistics.cs`
- `SB.cs` = `Lean/Common/Statistics/StatisticsBuilder.cs`
- `PM.cs` = `Lean/Common/Statistics/PerformanceMetrics.cs`
- `TB.cs` = `Lean/Common/Statistics/TradeBuilder.cs`
- `T.cs`  = `Lean/Common/Statistics/Trade.cs`
- `BRH.cs` = `Lean/Engine/Results/BaseResultsHandler.cs`
- `BTR.cs` = `Lean/Engine/Results/BacktestingResultHandler.cs`
- `IRP.cs` = `Lean/Common/Data/InterestRateProvider.cs`

---

## Output files

| File | Writer | Line (approx) |
|---|---|---|
| `{alg}.json`             | `BTR.StoreResult()`             | 319 |
| `{alg}-summary.json`     | `BTR.CreateResultSummary()`     | 808–844 |
| `{alg}-order-events.json`| `BTR.StoreOrderEvents()`        | 346 |
| `{alg}-log.txt`          | `BRH.LogStore.Save()`           | ~201 |

---

## `statistics` dict (summary) — 27 keys

| Summary key | Property | Formatted in | Computed in |
|---|---|---|---|
| Total Orders                | (int param)                 | SB.cs ~230 | constructor arg |
| Average Win                 | AverageWinRate              | SB.cs ~230 | PS.cs 36 / 262 |
| Average Loss                | AverageLossRate             | SB.cs ~230 | PS.cs 42 / 263 |
| Compounding Annual Return   | CompoundingAnnualReturn     | SB.cs ~230 | PS.cs 88 / 285, S.cs 38–48 |
| Drawdown                    | Drawdown                    | SB.cs ~230 | PS.cs 94 / 318, S.cs 261–314 |
| Expectancy                  | Expectancy                  | SB.cs ~230 | PS.cs 69 / 277 |
| Start Equity                | StartEquity                 | SB.cs ~230 | PS.cs 75 / 223 |
| End Equity                  | EndEquity                   | SB.cs ~230 | PS.cs 81 / 224 |
| Net Profit                  | TotalNetProfit              | SB.cs ~230 | PS.cs 100 / 281 |
| Sharpe Ratio                | SharpeRatio                 | SB.cs ~230 | PS.cs 107 / 294, S.cs 148–164 |
| Sortino Ratio               | SortinoRatio                | SB.cs ~230 | PS.cs 122 / 297, S.cs 188–191 |
| Probabilistic Sharpe Ratio  | ProbabilisticSharpeRatio    | SB.cs ~230 | PS.cs 115 / 312, S.cs 199–234 |
| Loss Rate                   | LossRate                    | SB.cs ~230 | PS.cs 63 / 276 |
| Win Rate                    | WinRate                     | SB.cs ~230 | PS.cs 56 / 275 |
| Profit-Loss Ratio           | ProfitLossRatio             | SB.cs ~230 | PS.cs 49 / 264 |
| Alpha                       | Alpha                       | SB.cs ~230 | PS.cs 128 / 302 |
| Beta                        | Beta                        | SB.cs ~230 | PS.cs 134 / 300 |
| Annual Standard Deviation   | AnnualStandardDeviation     | SB.cs ~230 | PS.cs 140 / 288, S.cs 85–88 |
| Annual Variance             | AnnualVariance              | SB.cs ~230 | PS.cs 146 / 287, S.cs 69–73 |
| Information Ratio           | InformationRatio            | SB.cs ~230 | PS.cs 153 / 306 |
| Tracking Error              | TrackingError               | SB.cs ~230 | PS.cs 160 / 304, S.cs 123–138 |
| Treynor Ratio               | TreynorRatio                | SB.cs ~230 | PS.cs 166 / 308 |
| Total Fees                  | (decimal param)             | SB.cs ~230 | constructor arg, TS.cs 270/408 |
| Estimated Strategy Capacity | CapacityEstimate.Capacity   | SB.cs ~230 | CapacityEstimate.cs |
| Lowest Capacity Asset       | CapacityEstimate.LowestCap… | SB.cs ~230 | CapacityEstimate.cs |
| Portfolio Turnover          | PortfolioTurnover           | SB.cs ~230 | PS.cs 172 / 228, BRH.cs 795–801 |
| Drawdown Recovery           | DrawdownRecovery            | SB.cs ~230 | PS.cs 192 / 319, S.cs 261–314 |

## `runtimeStatistics` dict — 8 keys

All populated by `BRH.UpdateRuntimeStatistics()` / `BTR.SendFinalResult()`; values pulled directly from `Portfolio.TotalPortfolioValue`, `Portfolio.TotalFees`, etc.

| Key | Source |
|---|---|
| Equity, Fees, Holdings, Net Profit, Return, Unrealized, Volume | `Portfolio.*` snapshot |
| Probabilistic Sharpe Ratio | Duplicated from PS.cs 115 |

## `totalPerformance.portfolioStatistics` — 25 fields

All defined in `PS.cs`, properties at lines 36–197, populated in the `PortfolioStatistics` constructor at lines 210–326.

| Field | Declared | Computed |
|---|---|---|
| averageWinRate             | 36  | 262 |
| averageLossRate            | 42  | 263 |
| profitLossRatio            | 49  | 264 |
| winRate                    | 56  | 275 |
| lossRate                   | 63  | 276 |
| expectancy                 | 69  | 277 |
| startEquity                | 75  | 223 |
| endEquity                  | 81  | 224 |
| compoundingAnnualReturn    | 88  | 285 |
| drawdown                   | 94  | 318 |
| totalNetProfit             | 100 | 281 |
| sharpeRatio                | 107 | 294 |
| probabilisticSharpeRatio   | 115 | 312 |
| sortinoRatio               | 122 | 297 |
| alpha                      | 128 | 302 |
| beta                       | 134 | 300 |
| annualStandardDeviation    | 140 | 288 |
| annualVariance             | 146 | 287 |
| informationRatio           | 153 | 306 |
| trackingError              | 160 | 304 |
| treynorRatio               | 166 | 308 |
| portfolioTurnover          | 172 | 228 |
| valueAtRisk99              | 179 | 314 |
| valueAtRisk95              | 186 | 315 |
| drawdownRecovery           | 192 | 319 |

## `totalPerformance.tradeStatistics` — 41 fields

All defined in `TS.cs`, populated in the `TradeStatistics` constructor (~lines 290–440) via a single pass over closed trades using Welford running-mean/variance.

| Field | Declared | Computed |
|---|---|---|
| startDateTime                | 31  | 295 |
| endDateTime                  | 36  | 298 |
| totalNumberOfTrades          | 41  | 301 |
| numberOfWinningTrades        | 46  | 312 / 413 |
| numberOfLosingTrades         | 51  | 345 / 414 |
| totalProfitLoss              | 57  | 314 / 347 |
| totalProfit                  | 63  | 315 |
| totalLoss                    | 69  | 348 |
| largestProfit                | 75  | 323 |
| largestLoss                  | 81  | 361 |
| averageProfitLoss            | 87  | 388 |
| averageProfit                | 93  | 316 |
| averageLoss                  | 99  | 349–350 |
| averageTradeDuration         | 104 | 394 |
| averageWinningTradeDuration  | 109 | 318 |
| averageLosingTradeDuration   | 114 | 356 |
| medianTradeDuration          | 119 | 428 |
| medianWinningTradeDuration   | 124 | 430 |
| medianLosingTradeDuration    | 129 | 432 |
| maxConsecutiveWinningTrades  | 134 | 325–328 |
| maxConsecutiveLosingTrades   | 139 | 376–378 |
| profitLossRatio              | 146 | 416 |
| winLossRatio                 | 154 | 417 |
| winRate                      | 161 | 418 |
| lossRate                     | 168 | 419 |
| averageMAE                   | 174 | 396 |
| averageMFE                   | 180 | 397 |
| largestMAE                   | 186 | 400 |
| largestMFE                   | 192 | 403 |
| maximumClosedTradeDrawdown   | 199 | 382 |
| maximumIntraTradeDrawdown    | 206 | 303–307 |
| profitLossStandardDeviation  | 212 | 392 |
| profitLossDownsideDeviation  | 219 | 352–354 |
| profitFactor                 | 227 | 420 |
| sharpeRatio (trade-level)    | 233 | 421 |
| sortinoRatio (trade-level)   | 239 | 422 |
| profitToMaxDrawdownRatio     | 247 | 423 |
| maximumEndTradeDrawdown      | 253 | 405 |
| averageEndTradeDrawdown      | 259 | 425 |
| maximumDrawdownDuration      | 264 | 335–336 |
| totalFees                    | 270 | 408 |

## `charts`

| Chart | Sampler | Notes |
|---|---|---|
| Strategy Equity → Equity (candle) | `BRH.UpdateAlgorithmEquity()` ~158, flushed in `SampleEquity()` ~706, `ResamplePeriod` ~459 | Bar type from `Data/Market/Bar.cs` |
| Strategy Equity → Return (bar)    | `BRH.SampleEquity()` ~739 | Daily %Δ equity |
| Drawdown → Equity Drawdown        | `BRH.SampleEquity()` ~285 | Daily (current/peak)-1 |
| Portfolio Turnover                 | `BRH.SamplePortfolioTurnover()` ~795–801 | daily |
| Exposure → Long/Short Ratio       | `BRH.SampleExposure()` ~823 | daily |
| Capacity → Strategy Capacity      | `BRH.SampleCapacity()` ~684 | CapacityEstimate class |
| Assets Sales Volume               | `BRH.SampleSalesVolume()` ~809, top-30 ~813 | treemap |
| Portfolio Margin                  | `BRH.SamplePortfolioMargin()`   | state-change only |
| Benchmark                         | `BRH.SampleBenchmark()`         | raw benchmark expression |

## `orders` entries (20 fields each)

Defined in `Lean/Common/Orders/Order.cs`. No computation — fields are copied from the filled `Order` object at backtest completion.

## `closedTrades` entries (16 fields each)

Defined in `T.cs`. Populated by `TB.cs` (grouping lines 242–605; MAE/MFE lines 331–354; fee assignment lines 231–237).

## `profitLoss` dict

Built by `TB.cs` as trades close; keyed by exit time.

## `rollingWindow` dict

Generated in `SB.cs` ~lines 169–200. Key format `M{1|3|6|12}_{YYYYMMDD}`. Closed trades cleared at ~line 383.

## Risk-free rate

- `IRP.cs` — loads US Federal Primary Credit Rate CSV
- Default: `IRP.cs` ~30–40 → `DefaultRiskFreeRate = 0.01m`
- Consumed by: `PS.cs` line 293 via `riskFreeInterestRateModel.GetAverageRiskFreeRate(equity.Keys)`
