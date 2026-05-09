> **Status:** Archived — stale research plan, work completed.
> **Do not use as implementation authority.**
> Current authority: `docs/spy-lean-output-report.md` (reconciliation report), `references/lean/` (vendored LEAN references).
> Archived because: LEAN output research completed; findings documented in the report and references/.

# Research Plan — LEAN SPY Study Output: What It Writes & How It's Calculated

**Owner:** Inkant
**Created:** 2026-04-09
**Study under investigation:** `SpyEmaCrossoverAlgorithm` (EMA 10 / EMA 20 crossover on SPY, 2024-03-28 → 2026-03-27, $100k start)
**Final deliverable location:** `learn-ai/docs/spy-lean-output-report.md` (+ supporting appendix files under `learn-ai/docs/spy-lean-output/`)

---

## 1. Goal

Produce a single, authoritative reference document that answers, for the SPY EMA crossover study:

1. **What** does a LEAN backtest write to disk when this algorithm finishes? (every file, every top-level section, every statistic, every chart series.)
2. **How** is each of those values computed? (formula, source-file pointer, inputs, edge cases, rounding, annualization convention.)
3. **Why** do the values look the way they do for this specific SPY study? (tie the abstract formula back to the concrete numbers in our run.)

The report must be concrete enough that a reader can open any metric in the summary JSON (e.g. `"Sharpe Ratio": "-0.679"`) and trace it all the way down to the lines of C# that produced it, plus reproduce it by hand from the equity curve and trade list.

---

## 2. Source Material (already on disk)

### 2.1 The algorithm
- `Lean/Algorithm.CSharp/SpyEmaCrossoverAlgorithm.cs` — the study itself.

### 2.2 The LEAN output files for this run
All under `Lean/Launcher/bin/Debug/`:

| File | Size | Purpose |
|---|---|---|
| `SpyEmaCrossoverAlgorithm.json` | ~13.5k lines | Full backtest result: charts (equity, drawdown, benchmark, daily returns, exposure, portfolio turnover, assets sales volume), order events, statistics, algorithm configuration, profit-loss per trade, rolling window of rolling-period statistics. |
| `SpyEmaCrossoverAlgorithm-summary.json` | ~150 lines | Compact summary: `charts` (Strategy Equity only), `statistics` (27 KPIs), `runtimeStatistics` (8 live-style KPIs), `state`, `algorithmConfiguration`. |
| `SpyEmaCrossoverAlgorithm-order-events.json` | — | Per-fill order events. |
| `SpyEmaCrossoverAlgorithm-log.txt` | 193 lines | `Debug` / `Log` output emitted by `OnData`, etc. |

### 2.3 The LEAN source that produces those files
- `Lean/Engine/Results/BacktestingResultHandler.cs` — the class that serializes the result files.
- `Lean/Engine/Results/BaseResultsHandler.cs` — shared sampling and chart-building logic.
- `Lean/Common/Statistics/StatisticsBuilder.cs` — assembles the `statistics` block from trades + equity + benchmark.
- `Lean/Common/Statistics/PortfolioStatistics.cs` — portfolio-level KPIs (Sharpe, Sortino, PSR, Alpha, Beta, Drawdown, etc.).
- `Lean/Common/Statistics/TradeStatistics.cs` — trade-level KPIs (Win Rate, Expectancy, Avg Win/Loss, Profit-Loss Ratio).
- `Lean/Common/Statistics/AlgorithmPerformance.cs` — container that combines both.
- `Lean/Common/Statistics/DrawdownMetrics.cs` — drawdown depth and recovery.
- `Lean/Common/Statistics/TradeBuilder.cs` + `Trade.cs` — how fills get aggregated into completed trades.
- `Lean/Common/Statistics/Statistics.cs` — lower-level math helpers (annualization, compounding, performance series).

### 2.4 Prior work we can lean on
- `learn-ai/PythonDataService/app/engine/` — our Python re-implementation, already bit-exact for trades, EMA, RSI against LEAN (see `tests/test_spy_validation.py` and `.auto-memory/lean_engine_reproducibility.md`). It validates the *execution* path but **not** the *statistics* path — the stats report is what this research plan fills in.

---

## 3. Scope — What the Report Must Cover

### 3.1 File-by-file catalog
For each of the four output files, document:
- Exact file name and when the result handler writes it (on `Sample`, `SaveResults`, `Exit`, etc.).
- Top-level JSON schema (keys, types, nesting).
- Which C# type is serialized (`BacktestResult`, `BacktestResultParameters`, etc.) and where `JsonSerializer` is invoked.

### 3.2 Chart series — every series, not just "Strategy Equity"
For each chart and each series in `SpyEmaCrossoverAlgorithm.json`:
- Chart name, series name, unit, series type, color.
- Point shape: `[timestamp, open, high, low, close]` vs `[timestamp, value]` — explain why.
- Sampling frequency (daily / minute / bar-resolution) and which code path samples it.
- For Strategy Equity: how the four-tuple (O/H/L/C) of an equity *candle* is computed from intra-day portfolio value samples.
- For Benchmark: how LEAN picks and normalizes the benchmark series (SPY itself in this study — note the self-comparison caveat).
- For Daily Returns / Drawdown / Exposure / Portfolio Turnover / Assets Sales Volume: the exact formula and the sampling cadence.

### 3.3 `statistics` block — all 27 KPIs
Enumerate every key currently in our run and for each one produce a subsection containing:

1. **Value from our run** (verbatim, e.g. `"Compounding Annual Return": "5.489%"`).
2. **Plain-English definition.**
3. **Formula** in math notation.
4. **Inputs** — which series drive it (equity curve, trade list, benchmark, risk-free rate).
5. **Annualization convention** — `tradingDaysPerYear = 252`, calendar days, or something else; where in source this constant is pulled.
6. **Source pointer** — file + method + approximate line where it's computed.
7. **Worked example** — recomputed by hand (or a tiny Python snippet) from our equity curve / trade list, showing we land on LEAN's reported value (or documenting the gap if we don't).
8. **Gotchas / known quirks** — e.g. how LEAN defines "Average Win" (per trade vs per day), Sharpe's use of arithmetic vs log returns, whether Sortino uses downside deviation or semi-variance, whether risk-free rate is non-zero, whether Alpha/Beta are zero because the benchmark *is* the traded asset.

Current KPI list to cover (from our `-summary.json`):

Total Orders, Average Win, Average Loss, Compounding Annual Return, Drawdown, Expectancy, Start Equity, End Equity, Net Profit, Sharpe Ratio, Sortino Ratio, Probabilistic Sharpe Ratio, Loss Rate, Win Rate, Profit-Loss Ratio, Alpha, Beta, Annual Standard Deviation, Annual Variance, Information Ratio, Tracking Error, Treynor Ratio, Total Fees, Estimated Strategy Capacity, Lowest Capacity Asset, Portfolio Turnover, Drawdown Recovery.

The full `.json` file may contain additional nested statistics (rolling-window stats per period, per-security statistics). Those get a sub-section too.

### 3.4 `runtimeStatistics` block
Eight keys (Equity, Fees, Holdings, Net Profit, PSR, Return, Unrealized, Volume). For each: definition, where it's populated (`RuntimeStatistics` updater in `BaseResultsHandler`), and how it differs from the matching entry in `statistics` (e.g. "Net Profit" string vs percent).

### 3.5 `state` + `algorithmConfiguration` + `rollingWindow`
- Field-by-field meaning.
- For `rollingWindow`: how the rolling-period statistics are bucketed (calendar months? rolling 12?) and which stats get included.

### 3.6 Order events file
- Schema of a single order event.
- Relationship to `Trade` objects built by `TradeBuilder` — i.e. how fills become the completed trades that feed `TradeStatistics`. Cover grouping method (`FillToFill` vs `FlatToFlat`) and where it's configured.

### 3.7 Log file
- Format, sources of lines (`Debug`, `Log`, `Error`), timestamp format, timezone.
- Which lines in *our* log come from `SpyEmaCrossoverAlgorithm.cs` vs the engine.

---

## 4. Method — How We'll Actually Produce the Report

1. **Catalog pass (mechanical).** Walk all four output files and generate a structured inventory (YAML or JSON) of every field that appears. This becomes the skeleton the report fills in — guarantees nothing is missed.
2. **Source-trace pass.** For each inventoried field, grep the LEAN C# tree for the key name (e.g. `"Sharpe Ratio"`, `SharpeRatio`, `ProbabilisticSharpeRatio`) to locate the originating property and its computation. Record file + line.
3. **Formula-extraction pass.** Read each originating method and transcribe the formula into math notation. Note dependencies (e.g. Sharpe depends on `AnnualStandardDeviation`, which depends on `DailyPerformance` series, which depends on the equity-curve sampling cadence).
4. **Worked-example pass.** Pull the equity curve and trade list out of our run into a small Python notebook or script under `learn-ai/docs/spy-lean-output/verify.py`. Recompute each KPI independently and compare to LEAN's reported number. Flag any that don't match within tolerance and investigate.
5. **Write the report** in `learn-ai/docs/spy-lean-output-report.md`, one section per file / block / KPI, following the structure in §3. Link to the verification script and the source-file pointers with `Lean/...` relative paths.
6. **Review pass.** Re-read against the four output files to confirm every field has a home in the report.

---

## 5. Open Questions to Resolve While Writing

These are things worth pinning down explicitly in the report rather than guessing:

- Is `tradingDaysPerYear = 252` used uniformly, or do some stats annualize by calendar days (365/365.25)?
- What risk-free rate does LEAN assume for this run? (Zero? A curve?) Where is it set?
- Since SPY *is* both the traded asset and the default benchmark, Alpha and Beta come out as 0 in our run — confirm this is a special-case short-circuit and document it.
- `Drawdown Recovery: 113` — is that in trading days or calendar days?
- `Expectancy: 0.525` — is it in R-multiples (win rate × avg win − loss rate × |avg loss| divided by |avg loss|) or some other normalization?
- `Probabilistic Sharpe Ratio` — which benchmark Sharpe is it comparing to (0 by default, or user-supplied)?
- Does `Portfolio Turnover: 17.16%` use average portfolio value or starting equity as the denominator, and is it annualized?
- What is `Estimated Strategy Capacity` and how is the "Lowest Capacity Asset" picked?

Each of these becomes a concrete subsection in the final report once answered from source.

---

## 6. Deliverables

1. `learn-ai/docs/spy-lean-output-report.md` — the main report.
2. `learn-ai/docs/spy-lean-output/inventory.json` — mechanical catalog of every field across the four output files (from step 4.1).
3. `learn-ai/docs/spy-lean-output/verify.py` — self-contained Python script that loads the run's JSON, recomputes every KPI, and prints a reconciliation table vs LEAN's values.
4. `learn-ai/docs/spy-lean-output/source-map.md` — flat index: "LEAN output field → C# file:line". Useful as a cheat-sheet independent of the long-form report.

---

## 7. Out of Scope

- Modifying the algorithm, the Python engine, or LEAN itself.
- Re-running LEAN — we use the already-generated output files in `Launcher/bin/Debug/` as our fixture.
- Live-trading result handler, optimizer reports, or any non-backtest output.
- Visualization / dashboards — the deliverable is a written report plus a verification script, not a UI.

---

## 8. Success Criteria

- Every single key that appears in any of the four output files for this run has a corresponding entry in the report explaining what it is and how it's computed.
- Every KPI in `statistics` and `runtimeStatistics` is reproduced by `verify.py` from the raw equity curve / trade list / config, matching LEAN's reported value to a stated tolerance (or the discrepancy is explicitly documented).
- Every formula in the report is pinned to an exact C# source location.
- A reader unfamiliar with LEAN can read the report top-to-bottom and come away knowing (a) what files LEAN produced, (b) what each number means, and (c) where in the C# codebase to look if they want to dig deeper.
