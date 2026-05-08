# External Trading Platform Inspiration Authority

**Status:** Research authority and product inspiration
**Created:** 2026-05-08
**Scope:** Backtesting, strategy research, options analysis, optimization, robustness testing, and trust UX for learn-ai
**Audience:** learn-ai contributors deciding what to build, port, document, or explicitly avoid

This document records what mature retail, prosumer, and open-source trading platforms do well, what assumptions they expose, and what learn-ai should borrow or exceed. It is not a numerical source-of-truth for learn-ai math. `docs/math-sources-of-truth.md` and `docs/architecture/engine-authority-map.md` remain authoritative for implementation ownership.

## Research Method

Claims in this document use this evidence ladder:

1. Official documentation from the platform vendor or project.
2. Official source-code-adjacent documentation for open-source engines.
3. Academic or practitioner research when it explains cross-engine risk.
4. Community commentary only for pain-point discovery, not for authority.
5. Internal learn-ai docs for current implementation status.

The research intentionally favors documented behavior over marketing pages. When a tool exposes an assumption, that is more useful to learn-ai than a broad claim of realism.

## Executive Position

learn-ai should not try to become a clone of TradingView, MetaTrader, or QuantConnect. The stronger lane is:

> Retail-grade usability plus institutional-style provenance.

Retail tools generally win on charting, iteration speed, templates, reports, no-code authoring, and user onboarding. learn-ai can win on transparent math authority, fixture-backed equivalence, run reproducibility, timestamp rigor, and assumption disclosure.

The best target is a research workbench where a user can:

- Build or import a strategy through a declarative `StrategySpec`.
- See exactly which engine, data snapshot, timestamp convention, fill model, fee model, and warmup policy ran.
- Jump from a metric to the trade list, from a trade to the chart bar, and from a chart event to the underlying math source.
- Stress the result with walk-forward, Monte Carlo, null baselines, sensitivity sweeps, and implementation-risk comparisons.
- Export a run record that is replayable and audit-ready.

The 2026 paper "Implementation Risk in Portfolio Backtesting" is useful framing here: even when strategy logic is nominally identical, different engines can diverge because of implementation details, especially transaction-cost handling. learn-ai's answer should be to measure and expose those differences rather than pretend they do not exist.

## Platform Capability Map

| Platform | Primary strength | Implementation pattern | Assumptions / limits worth noticing | learn-ai takeaway |
|---|---|---|---|---|
| TradingView | Chart-native strategy UX | Pine strategies + Strategy Tester + broker emulator | Non-standard chart types can use synthetic prices; broker emulator infers intrabar path unless Bar Magnifier is used | Borrow chart-linked trade review and loud assumption panels |
| QuantConnect / LEAN | Serious event-driven engine | Streaming time-slice engine, reality models, brokerage/data integrations | More complex than batch systems; realism depends on configured models and data | Keep LEAN as a semantic reference; preserve event-driven anti-lookahead posture |
| MetaTrader 5 | Mature desktop testing/optimization | Expert Advisors, multi-currency tester, tick modes, visual testing, distributed agents | Tick generation mode and delay assumptions change realism materially | Expose execution/tick-mode presets and realism level explicitly |
| TrendSpider | No-code strategy construction | Visual script/list-of-signals builder with constrained execution model | Single timeframe; no scale-in/out; timestamp-list equality has timezone pitfalls | Use visual builder ideas, but keep canonical timestamp rigor |
| NinjaTrader | Detailed backtest properties | Strategy Analyzer with fill resolution, slippage, bars required, commissions, order handling | Docs expect realtime/backtest discrepancies on exotic bars | Borrow a "backtest properties" drawer for every run |
| TradeStation | Optimization and walk-forward discipline | Strategy Optimization plus Walk-Forward Optimizer with rolling/anchored modes | Default net-profit optimization can be dangerous without OOS discipline | Build optimization only as train -> freeze -> test, not "pick best and celebrate" |
| AmiBroker | Portfolio-level research and robustness | AFL, portfolio backtester, optimization surfaces, walk-forward, Monte Carlo | User controls can create invalid WF settings; custom metric aggregation needs care | Borrow plateau/sensitivity visuals and mathematically correct OOS aggregation |
| Backtrader | Event lifecycle clarity | `Cerebro`, `Strategy`, `prenext`/`next`, broker notifications, order types | Flexible but user must understand lifecycle and order semantics | Make learn-ai engine lifecycle visible in docs and debug views |
| vectorbt | Fast parameter sweeps | Vectorized/Numba portfolio simulation from signals/orders | Fast research can still invite lookahead if signal timing is wrong | Use as inspiration for sensitivity sweeps, not canonical execution semantics |
| backtesting.py | Lightweight ergonomics | Small Python API with `Backtest.run()` and `Backtest.optimize()` | Single-asset focus; no intrabar decisions from candle data | Borrow simple API ergonomics for research helpers |
| OptionStrat | Options payoff UX | Visual builder, templates, payoff tables, Greeks, IV/time sliders | Calculator assumes IV constant; max profit/loss ignores assignment/dividend risks | Borrow options leg UX and assumption warnings |
| Option Omega | Options backtesting depth | Multi-leg option tests, DTE/strike rules, 0DTE intraminute stops, trade logs | Ticker universe and RTH/data assumptions are explicit | Borrow option-specific setup vocabulary and recent-run workflow |
| Option Alpha | Strategy-to-bot workflow | Options backtests, compare/combine runs, trade logs, bot automation | Strong product flow; methodology details are less public than engine docs | Borrow "backtest -> compare -> automate" shape, but require deeper methodology in learn-ai |
| Zipline Reloaded | Pythonic event-driven history | Algorithm lifecycle and metrics, Quantopian lineage | Less central than LEAN today, but useful as a design reference | Use for API/lifecycle inspiration, not as a primary semantic authority |

## Current learn-ai Baseline

learn-ai already has several practices that are stronger than common retail tooling:

- A concept-level math registry in `docs/math-sources-of-truth.md`.
- An engine ownership map in `docs/architecture/engine-authority-map.md`.
- Python as the primary numerical authority, with .NET and Angular constrained to transport/rendering except documented exceptions.
- Vendored LEAN references for selected ports.
- Strict tolerance rules and golden fixtures for indicators, options Greeks, engine statistics, portfolio scenario math, and selected strategies.
- Run-ledger infrastructure with canonical JSON hashing and data snapshot IDs.
- Walk-forward, Monte Carlo, and null-baseline research modules already present in Python.
- Timestamp policy: `int64 ms UTC` at wire/storage boundaries.

The honest gaps remain:

- Some math rows still have `NONE - pending` validation or `pending-fixture` status.
- Some migration rows still indicate old .NET or legacy Python paths.
- The research modules are stronger than their product surface; many are not yet first-class UI workflows.
- Execution realism is documented in pieces but not yet visible enough in every user-facing run.
- Options research has strong pricing authorities, but option strategy backtesting/product workflows are not yet as deep as specialized options tools.

## Platform Cards

### TradingView

**Best at:** chart-native strategy iteration, Pine scripting ergonomics, visual strategy reports, and user familiarity.

**Documented implementation:** TradingView strategies are Pine scripts declared with `strategy()`. The Strategy Tester shows overview, performance summary, list of trades, and properties. The properties view exposes date range, symbol information, strategy inputs, initial capital, order size, margin, commission, slippage, and other settings. TradingView uses a broker emulator that fills from chart data by default. Its docs explicitly describe intrabar path assumptions and the Bar Magnifier mode, which uses lower-timeframe data to improve fills.

**Important disclosed limitations:** TradingView warns that non-standard chart types such as Heikin Ashi, Renko, Kagi, Point & Figure, Range, and Line Break can produce unrealistic strategy results because the strategy may use synthetic prices. It also documents lookahead bias, selection bias, overfitting, and the need for forward testing.

**UX patterns to borrow:**

- Overview with equity, drawdown, and buy-and-hold baseline.
- Trade list with direct "scroll to bar" behavior.
- Properties tab that records all strategy and dataset assumptions.
- Clear warning when chart/data transformation changes realism.

**learn-ai recommendation:** Build an "Assumptions and Evidence" tab for every backtest. It should include engine, strategy spec hash, data snapshot ID, symbol/timeframe/session, fill model, slippage, commission, warmup, timestamp policy, source math rows, and known caveats. Learn from TradingView's accessibility, but exceed it with fixture links and run hashes.

Primary source: [TradingView Pine Script strategies](https://www.tradingview.com/pine-script-docs/concepts/strategies/)

### QuantConnect / LEAN

**Best at:** serious event-driven algorithm research, open-source engine semantics, multi-asset modeling, brokerage/data integrations, and explicit reality modeling.

**Documented implementation:** LEAN is an open-source algorithmic trading engine for research, backtesting, and live trading. Algorithms subclass `QCAlgorithm`; the engine synchronizes data into time slices, injects them into the algorithm, manages portfolio and transactions, and supports Python or C#. QuantConnect distinguishes batch processing from event streaming and argues that streaming helps avoid future data access. LEAN also exposes fill models, fee models, slippage models, brokerage models, warmup, and history APIs.

**Important disclosed limitations:** LEAN's realism depends on configured data and reality models. Built-in fill models can assume complete fills unless customized. Warmup can return fewer points than expected because of illiquidity, IPO dates, market hours, data issues, or data-provider limitations.

**Patterns to borrow:**

- Event-streaming as the canonical execution model.
- Security-level reality models.
- Warmup as explicit engine state where trading is disallowed.
- Fill model as a swappable model with per-order-type methods.
- Custom models for partial fills and stale fills.

**learn-ai recommendation:** Keep LEAN as the primary semantic reference for event replay, indicators, and fill semantics where applicable. Where learn-ai intentionally differs, require a module docstring and fixture/reconciliation note. The UI should expose "LEAN-compatible" versus "learn-ai simplified" execution modes.

Primary sources: [LEAN Algorithm Engine](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/algorithm-engine), [LEAN trade fills](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/trade-fills/key-concepts), [LEAN warm-up periods](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/warm-up-periods)

### MetaTrader 5

**Best at:** mature desktop strategy testing, multi-currency Expert Advisor testing, tick-mode controls, optimization, visual testing, and distributed compute.

**Documented implementation:** MetaTrader's Strategy Tester runs Expert Advisors over history and optimization runs them across parameter sets. It supports multi-currency EAs, multithreaded local agents, remote agents, and MQL5 Cloud Network compute. It exposes forward testing, execution delays, tick generation modes, visual testing, advanced account settings, margin settings, and commission models.

**Important disclosed limitations:** Realism varies sharply by tick generation mode. "Every tick based on real ticks" is closer to reality but heavier. "Open prices only" and "math calculations" are faster but less realistic. No-delay execution is explicitly an idealized condition. Margin, commission, delay, and data availability settings are part of the result.

**Patterns to borrow:**

- Realism tiers: fast/rough, standard, tick-accurate, and visual replay.
- Distributed optimization as a long-term architecture option.
- Saved parameter sets and repeatable test configurations.
- Visual mode for auditing trade operations.

**learn-ai recommendation:** Add a `RealismProfile` concept to engine configs: `fast_close_only`, `standard_ohlc`, `intrabar_resolution`, `market_microstructure_research`. The profile should change engine assumptions explicitly and be encoded in the run ledger.

Primary source: [MetaTrader 5 Strategy Testing](https://www.metatrader5.com/en/terminal/help/algotrading/testing)

### TrendSpider

**Best at:** visual, no-code strategy construction for non-programmers.

**Documented implementation:** TrendSpider's Strategy Tester lets users define strategy settings, timeframe, depth, chart type, extended-hours setting, direction, and trade cost. Entry and exit conditions can be built through a visual scripting editor, AI-assisted script boxes, or explicit lists of signal timestamps. It supports exits such as script conditions, take profit, stop loss, trailing stop, entry invalidated, candles passed, and lists of signals.

**Important disclosed limitations:** The tester allows a single timeframe, no scaling into a position, no multiple entries in a row, and no scaling out; exits close 100% of the position. Explicit signal lists rely on precise triggering-candle timestamps and strict JS timestamp equality. TrendSpider recommends keeping timestamps and chart time zones aligned.

**Patterns to borrow:**

- Strategy builder using the same condition vocabulary as scanners and alerts.
- Explicit list-of-signals import for external systems.
- Save/clone/re-run strategy iteration workflow.
- AI-assisted condition authoring, but with strict validation.

**learn-ai recommendation:** Build a visual StrategySpec editor rather than a separate no-code engine. Imported signal lists must be canonical `int64 ms UTC`, not ISO strings or local-time text. A visual builder should emit exactly the JSON that Python validates and runs.

Primary source: [TrendSpider Creating a New Strategy](https://help.trendspider.com/kb/strategy-tester/accessing-and-using-the-strategy-tester)

### NinjaTrader

**Best at:** exposing many backtest settings to advanced retail and futures users.

**Documented implementation:** NinjaTrader's Strategy Analyzer runs NinjaScript strategies over historical data. Its properties include backtest type, instrument/list, market data type, bar type/value, start/end dates, trading-hours template, break-at-EOD, commission inclusion, maximum bars lookback, bars required to trade, order fill resolution, fill-limit-on-touch, slippage in ticks, entries per direction, entry handling, exit on session close, order quantity policy, and time in force.

**Important disclosed limitations:** NinjaTrader docs explicitly note that realtime and backtest discrepancies should be expected, especially on exotic bar types. It also defaults trade-history storage off in some Strategy Analyzer contexts for memory reasons, which can affect code that references historical trade objects.

**Patterns to borrow:**

- Dense property model for every backtest.
- Fill-resolution and slippage controls in one place.
- Bars-required-to-trade as an explicit warmup/trading gate.
- Separate performance-memory tradeoffs for retaining trade history.

**learn-ai recommendation:** Backtest configs should have a complete "execution contract" object. Nothing meaningful should hide behind a default without appearing in the run details.

Primary source: [NinjaTrader Backtest a Strategy](https://ninjatrader.com/support/helpGuides/nt8/backtest_a_strategy.htm)

### TradeStation

**Best at:** optimization workflow and walk-forward analysis discipline.

**Documented implementation:** TradeStation optimizes numeric strategy inputs against a selected fitness function and produces reports of tested combinations. Its Walk-Forward Optimizer automates in-sample optimization and out-of-sample testing, supports rolling and anchored walk-forward modes, provides cluster analysis, sensitivity analysis, distribution analysis, P/L history, performance summaries, and pass/fail criteria.

**Important disclosed limitations:** TradeStation documentation says optimization should enhance a trading idea, not develop one. It also notes that default optimization can maximize net profit, which is a weak default if used without robustness controls. WFO pass/fail thresholds are configurable.

**Patterns to borrow:**

- Walk-forward cluster matrix.
- Sensitivity analysis by optimization parameter.
- Out-of-sample performance summary separate from in-sample performance.
- Explicit test criteria and pass/fail settings.

**learn-ai recommendation:** learn-ai should not ship "optimize and apply best params" as a primary flow. It should ship "optimize on train, freeze, test out-of-sample, then inspect sensitivity and baselines."

Primary sources: [TradeStation Strategy Optimization](https://help.tradestation.com/10_00/eng/tradestationhelp/optimize/strategy_optimization.htm), [TradeStation Walk-Forward Optimizer](https://help.tradestation.com/09_01/tswfo/topics/about_wfo.htm)

### AmiBroker

**Best at:** portfolio-level backtesting, fast optimization, walk-forward, Monte Carlo, custom metrics, and robustness visualization.

**Documented implementation:** AmiBroker's portfolio backtester uses a single portfolio equity value, supports portfolio-level position sizing, maximum simultaneous open positions, position scoring when signals exceed capacity, and dynamic money management. Its optimization supports many parameters, multi-symbol optimization, exhaustive and smart optimization engines, 3D optimization surfaces, and plateau-based robustness interpretation. Its walk-forward mode creates in-sample and out-of-sample segments and summary reports. Its Monte Carlo mode can sample trades or portfolio equity changes.

**Important disclosed limitations:** AmiBroker's walk-forward advanced mode can allow invalid settings if the user configures it poorly. Custom metrics across OOS segments require careful combine-method selection. Its Monte Carlo docs warn that trade-list bootstrap can understate drawdowns when original trades overlap, and recommend equity-change simulation for overlapping positions.

**Patterns to borrow:**

- Portfolio-level signal arbitration via `PositionScore`-like ranking.
- 3D parameter plateau visualization to avoid fragile spikes.
- OOS summary metrics computed from combined equity, not averaged naively.
- Monte Carlo method choice based on overlapping versus non-overlapping trades.

**learn-ai recommendation:** For sensitivity sweeps, emphasize broad stable parameter regions over best single point. For Monte Carlo, expose whether the simulation uses trade-list resampling, reshuffling, or equity-change sampling, and explain when each is valid.

Primary sources: [AmiBroker portfolio backtesting](https://amibroker.com/guide/h_portfolio.html), [AmiBroker optimization](https://amibroker.com/guide/h_optimization.html), [AmiBroker walk-forward](https://amibroker.com/guide/h_walkforward.html), [AmiBroker Monte Carlo](https://amibroker.com/guide/h_montecarlo.html)

### Backtrader

**Best at:** understandable event lifecycle and flexible order/trade notifications.

**Documented implementation:** Backtrader centers on `Cerebro` and `Strategy`. Strategy lifecycle includes `__init__`, `start`, `prenext`, `nextstart`, `next`, and `stop`. Strategies receive `notify_order`, `notify_trade`, `notify_cashvalue`, `notify_fund`, and store notifications. Orders support market, limit, stop, stop-limit, close, trailing stop, and bracket-style orders.

**Important disclosed limitations:** The lifecycle is flexible but can be subtle: `prenext`, `nextstart`, and `next` can be called multiple times for the same point in time when ticks update a larger timeframe bar. Market orders in backtesting execute at next bar open by default.

**Patterns to borrow:**

- Explicit lifecycle states.
- Separate order and trade notifications.
- Analyzer/observer pattern for metrics and diagnostics.
- Broker abstraction visible to strategy code.

**learn-ai recommendation:** Add a developer-facing engine trace view for one selected run: bar received, indicators updated, signal emitted, order created, fill resolved, portfolio updated, statistics updated.

Primary sources: [Backtrader Strategy](https://www.backtrader.com/docu/strategy/), [Backtrader Cerebro](https://www.backtrader.com/docu/cerebro/)

### vectorbt

**Best at:** high-throughput, vectorized research and parameter sweeps.

**Documented implementation:** vectorbt models portfolios with Numba-compiled simulation functions and record classes for orders, logs, trades, positions, and drawdowns. It supports `Portfolio.from_signals`, broadcasting across parameter grids, stop loss/take profit settings, custom signal functions, and broad performance metrics/plots.

**Important disclosed limitations:** Vectorized speed does not eliminate timing risk. The docs explicitly warn that if stop/signaling logic does not happen at the end of a bar, users may expose themselves to lookahead bias.

**Patterns to borrow:**

- Parameter broadcasting and grid-scale experimentation.
- Records-first analysis objects.
- Fast plots for orders, trades, drawdowns, exposure, cash, assets, and value.
- Simple API for sensitivity research.

**learn-ai recommendation:** Use vectorbt as inspiration for research sweeps, not as canonical event semantics. learn-ai's run ledger and event engine should remain the authority for user-comparable results.

Primary sources: [vectorbt getting started](https://vectorbt.dev/), [vectorbt Portfolio base](https://vectorbt.dev/api/portfolio/base/)

### backtesting.py

**Best at:** small, approachable Python API for single-asset strategy research.

**Documented implementation:** `Backtest` takes OHLC data, a `Strategy` subclass, cash, spread, commission, margin, trade-on-close, hedging, exclusive-orders, and finalize-trades settings. It supports `run()` and `optimize()`, including grid search and model-based optimization. Strategy authors implement `init()` and `next()`. Its docs explicitly state it is best suited for one tradable asset at a time and that intrabar decisions require finer-grained data.

**Important disclosed limitations:** It is intentionally not a stock-picking or multi-asset portfolio rebalancing engine. Warmup/lookback length affects when simulation begins. Candle-level data cannot support true intrabar decisions.

**Patterns to borrow:**

- Very low ceremony API.
- Strategy parameters as simple class variables.
- Readable result series with common metrics.
- Plot options for trades, drawdown, equity, and resampling.

**learn-ai recommendation:** Provide a thin research helper API over `StrategySpec` that feels this simple, while still writing a full run ledger under the hood.

Primary sources: [backtesting.py API](https://kernc.github.io/backtesting.py/doc/backtesting/backtesting.html), [backtesting.py quick start](https://kernc.github.io/backtesting.py/doc/examples/Quick%20Start%20User%20Guide.html)

### OptionStrat

**Best at:** fast visual comprehension of option structures.

**Documented implementation:** OptionStrat's builder lets users pick from many templates, select stock, expiration, strike, option direction, and quantities, then view net debit/credit, max loss/profit, breakevens, chance of profit, Greeks, P/L table, P/L chart, probability distribution overlays, IV adjustment, cost-basis editing, warnings for low liquidity/wide spreads, saved trades, rolling, and before/after comparison.

**Important disclosed limitations:** OptionStrat states max loss/profit does not account for assignment risk or dividend risk. It also states its calculator assumes IV is constant even though real IV changes.

**Patterns to borrow:**

- Options leg builder with direct manipulation.
- P/L table across underlying price and date.
- P/L chart with DTE/IV controls.
- Warnings attached to legs, not buried in docs.
- Compare-before/after mode for adjustments and rolls.

**learn-ai recommendation:** Options UX should make assumptions visually unavoidable: IV source, IV shift model, dividend/assignment caveats, pricing authority, and whether prices are market mids or theoretical. The best learn-ai version would pair OptionStrat-like clarity with Hull/QuantLib/fixture provenance.

Primary source: [OptionStrat Options Builder Tutorial](https://optionstrat.com/tutorials/options-builder)

### Option Omega

**Best at:** detailed options backtesting setup vocabulary.

**Documented implementation:** Option Omega supports multi-leg backtests, date ranges back to 2013 for listed symbols, RTH-only testing, specific ticker coverage, custom strategies up to 8 legs, multiple strike-selection modes, linked/dependent legs, exact DTE, rounded strikes, allocation controls, entry conditions, technical indicators, gap/intraday movement filters, ORB, premium filters, re-entry, leg groups, profit targets, stop losses, trailing stops, 0DTE intraminute stops, profit actions, exit conditions, commissions/fees, slippage, spread filters, close-open-trades behavior, CSV signal upload, saved/shareable tests, and recent run cards.

**Important disclosed limitations:** It explicitly scopes ticker coverage, RTH, New York time, data availability, and certain option-specific modes. 0DTE intraminute stops rely on one-minute intervals and contract high/low values from NBBO and trades depending on the selected mode.

**Patterns to borrow:**

- Option-specific controls that match actual trader vocabulary: DTE, delta, premium, width, linked legs, ORB, VIX, stop/profit actions.
- Recent test runs for rapid iteration.
- Trade log entries for partial profit actions.
- RTH and data-availability assumptions displayed near setup.

**learn-ai recommendation:** If learn-ai adds options backtesting, do not force users into generic stock-strategy primitives. Add option-native StrategySpec primitives while preserving Python as math authority.

Primary sources: [Option Omega Backtest Setup](https://docs.optionomega.com/backtesting/backtest-setup), [Option Omega Backtest Results](https://docs.optionomega.com/backtesting/backtest-results)

### Option Alpha

**Best at:** product workflow from options backtest to automation.

**Documented implementation:** Option Alpha describes tools for 0DTE and next-day options strategies, multiple entry/exit criteria, position settings, technical indicators, comparing and combining multiple backtests, detailed trade logs, and converting a backtest into an automated bot that trades the same strategy.

**Important disclosed limitations:** Public docs emphasize workflow more than full methodology. For learn-ai, that means Option Alpha is stronger as product-flow inspiration than as a numerical authority.

**Patterns to borrow:**

- Compare multiple backtest variations.
- Combine strategies into a portfolio curve.
- Detailed trade logs before automation.
- Explicit transition from research to automation.

**learn-ai recommendation:** Borrow compare/combine UX, but avoid automation until the execution engine, broker model, reconciliation, and risk controls are separately validated.

Primary source: [Option Alpha Backtesting](https://docs.optionalpha.com/tools/backtesting)

### Zipline Reloaded

**Best at:** Pythonic event-driven algorithm interface and Quantopian lineage.

**Documented implementation:** Zipline Reloaded describes an event-driven system for backtesting trading algorithms, with algorithm initialization, event handling, and metrics. The modern documentation notes Pythonic ease of use and common statistics.

**Important disclosed limitations:** Zipline is less central to learn-ai than LEAN because learn-ai already uses LEAN as a vendored semantic reference. Still, Zipline is useful for API and lifecycle comparisons.

**Patterns to borrow:**

- Pythonic algorithm entry points.
- Metrics-set configuration.
- Clear separation between initialization and per-event logic.

**learn-ai recommendation:** Keep Zipline as a secondary design reference for API ergonomics only.

Primary sources: [Zipline tutorial](https://zipline.ml4trading.io/beginner-tutorial.html), [Zipline API reference](https://zipline.ml4trading.io/api-reference.html)

## Cross-Platform Best Practices

### 1. Make Execution Assumptions First-Class

Every mature tool has execution assumptions. The good ones expose them. learn-ai should treat execution assumptions as part of the result, not as background configuration.

Required run metadata:

- Engine path and version.
- StrategySpec hash.
- Data source and data snapshot ID.
- Session template and timezone.
- Bar timestamp convention.
- Signal timing.
- Order timing.
- Fill model.
- Intrabar model.
- Commission model.
- Slippage/spread model.
- Warmup policy.
- Corporate-action adjustment policy.
- Known unsupported realism features.

### 2. Prefer Event-Driven Authority For User-Comparable Results

LEAN, Backtrader, Zipline, and learn-ai's canonical engine all point in the same direction: event-driven execution is easier to reason about for live-like semantics. Vectorized frameworks are valuable for research sweeps but should not be the sole authority for user-comparable P/L, fills, or trade timing unless their assumptions are explicitly accepted.

Recommendation:

- Canonical result: event-driven Python engine.
- Research screen: vectorized/sweep engines are allowed if labeled as exploratory.
- Promotion rule: exploratory results must pass event-driven replay before becoming a cited result.

### 3. Treat Intrabar Modeling As A Realism Toggle

TradingView has Bar Magnifier. NinjaTrader has order fill resolution. MetaTrader has tick generation modes. Option Omega has 0DTE intraminute stops.

learn-ai should expose:

- Close-only mode.
- OHLC path-assumption mode.
- Lower-timeframe replay mode.
- Tick/reconstructed microstructure mode when data exists.

Each mode should be incompatible with vague claims like "accurate". The run should say exactly what it assumed.

### 4. Optimization Must End In Out-Of-Sample Evidence

TradeStation and AmiBroker both center walk-forward as the discipline after optimization. TradingView explicitly warns about overfitting.

Recommendation:

- No one-click "best params" without OOS.
- Optimization result rows should show IS metric, OOS metric, degradation, drawdown, trade count, and parameter stability.
- Favor plateau visualizations over best-point rankings.
- Store all tested parameter combinations or a deterministic sample ledger.

### 5. Monte Carlo Needs Method Disclosure

AmiBroker's Monte Carlo docs are unusually honest: trade-list bootstrapping and equity-change bootstrapping have different validity conditions.

Recommendation:

- learn-ai Monte Carlo reports must name method: reshuffle, resample, block bootstrap, equity-change bootstrap, or synthetic OHLC.
- Reports must state whether overlapping trades exist and whether the method preserves that structure.
- Percentile bands should include exact simulation count, seed, and quantile method.

### 6. Trade Logs Are Not Optional

TradingView, Option Omega, Option Alpha, vectorbt, Backtrader, and NinjaTrader all surface some notion of trade/order records.

Recommendation:

- Every result should have a trade list.
- Every metric should be traceable to the trades/equity points that produced it.
- Every trade should link to chart context, signals, indicator values, order object, fill decision, and portfolio state before/after.

### 7. Options Tools Need Options-Native Language

OptionStrat and Option Omega show that options workflows need DTE, delta, premium, width, linked legs, IV shifts, assignment/dividend warnings, and multi-leg diagnostics. Generic "entry/exit" vocabulary is not enough.

Recommendation:

- Add option-native StrategySpec extensions only when Python authorities can price and validate them.
- Keep payoff calculators separate from historical options backtests.
- Clearly distinguish theoretical value, observed mid, modeled fill, and realized P/L.

### 8. Shareability Should Mean Replayability

Retail tools often let users save and share a strategy. learn-ai should go further: sharing should include enough metadata to replay the run.

Recommendation:

- A shared learn-ai run should include run ID, spec hash, result hash, data snapshot ID, engine version, and assumption profile.
- If data cannot be redistributed, the run should still explain how to reconstruct it.

## learn-ai Recommendations

### Adopt Now

1. Add a run-level "Assumptions and Evidence" panel modeled loosely after TradingView Properties and NinjaTrader Backtest Properties.
2. Add chart navigation from trade list rows to the exact bar, signal, and fill explanation.
3. Add a result badge that says `canonical`, `exploratory`, `legacy-ok`, or `pending-fixture`, sourced from `docs/math-sources-of-truth.md`.
4. Add strategy setup export/import around canonical `StrategySpec` JSON.
5. Add recent run cards for run-ledger, walk-forward, Monte Carlo, and baseline jobs.
6. Add OOS-first optimization language to docs before adding optimization UI.

### Adapt Carefully

1. TradingView Bar Magnifier -> learn-ai lower-timeframe replay with explicit data provenance.
2. TrendSpider visual builder -> StrategySpec editor, not a parallel engine.
3. TradeStation WFO -> walk-forward optimizer with stored parameter selection and OOS fold runs.
4. AmiBroker optimization surfaces -> sensitivity heatmaps and plateau detection.
5. OptionStrat payoff builder -> options lab with provenance badges and assignment/dividend warnings.
6. Option Omega 0DTE controls -> only after historical option data and pricing/fill fixtures are credible.

### Avoid

1. Hidden broker-emulator assumptions.
2. Synthetic chart/data backtests without loud warnings.
3. Client-side math authorities for user-comparable numbers.
4. "Best net profit" optimization as a default.
5. Naive averaging of fold metrics.
6. Monte Carlo reports that hide the sampling method.
7. ISO/local timestamp imports for signal lists.
8. Options P/L displays that blur theoretical prices, mids, fills, and realized trades.

## Proposed learn-ai Product Primitives

### Run Contract

A single object persisted with every run:

```json
{
  "engine": "PythonDataService/app/engine",
  "strategy_spec_hash": "...",
  "data_snapshot_id": "...",
  "timestamp_policy": "int64_ms_utc",
  "session": "NYSE_RTH",
  "bar_timestamp": "close",
  "signal_timing": "bar_close",
  "order_timing": "same_bar_close",
  "fill_model": "close_price",
  "intrabar_model": "none",
  "commission_model": "zero",
  "slippage_model": "zero",
  "warmup_policy": "...",
  "math_authority_rows": ["EMA", "RSI", "Sharpe ratio"],
  "known_caveats": []
}
```

### Realism Profile

Named presets for execution assumptions:

- `research_fast`: close fills, zero cost, no intrabar, fastest iteration.
- `retail_chart_equivalent`: match TradingView-style bar-close/broker-emulator assumptions for reconciliation.
- `lean_equivalent`: match vendored LEAN semantics where implemented.
- `cost_stress`: same strategy with conservative slippage/spread/commission.
- `intrabar_replay`: minute or tick replay where data exists.

### Evidence Badge

Every number-producing view should show a compact trust badge:

- `Fixture pinned`: linked to golden fixture or parity test.
- `Reference port`: linked to vendored reference or paper note.
- `Internal`: no external reference, but tested.
- `Pending fixture`: known validation debt.
- `Exploratory`: not a canonical result.

### Strategy Builder Levels

learn-ai should support three authoring layers:

1. **Template:** user chooses SPY EMA crossover, ORB, RSI mean reversion, option spread template, etc.
2. **Visual StrategySpec:** no-code builder emits validated StrategySpec JSON.
3. **Python strategy:** developer writes canonical engine algorithm when StrategySpec is insufficient.

All three should ultimately route to Python for numbers.

## Implementation Backlog

### Near Term

- Build a docs-only `Run Contract` spec and add it to `docs/architecture/`.
- Add `Assumptions and Evidence` UI requirements to Engine Lab planning docs.
- Add run-ledger source links from research outputs to math-source rows.
- Add `RealismProfile` enum proposal for engine configs.
- Add a trade-list-to-chart-bar UX design note.

### Medium Term

- Extend walk-forward from fixed-spec folds to train-selected parameters frozen on test.
- Add sensitivity/plateau heatmaps for StrategySpec parameters.
- Add Monte Carlo method disclosures and overlap warnings to the UI.
- Add StrategySpec import/export and signal-list import with `int64 ms UTC` validation.
- Add option payoff builder UI that consumes Python options authorities only.

### Long Term

- Add lower-timeframe intrabar replay.
- Add implementation-risk comparisons across learn-ai engine profiles.
- Add portfolio-level signal arbitration and multi-symbol StrategySpec execution.
- Add distributed or background optimization workers.
- Add options historical backtesting once data, pricing, fill, and assignment/dividend policies have fixtures.

## Source Ledger

### Official Platform Docs

- [TradingView Pine Script strategies](https://www.tradingview.com/pine-script-docs/concepts/strategies/)
- [QuantConnect LEAN Algorithm Engine](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/algorithm-engine)
- [QuantConnect trade fills](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/trade-fills/key-concepts)
- [QuantConnect warm-up periods](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/warm-up-periods)
- [MetaTrader 5 Strategy Testing](https://www.metatrader5.com/en/terminal/help/algotrading/testing)
- [TrendSpider Creating a New Strategy](https://help.trendspider.com/kb/strategy-tester/accessing-and-using-the-strategy-tester)
- [NinjaTrader Backtest a Strategy](https://ninjatrader.com/support/helpGuides/nt8/backtest_a_strategy.htm)
- [TradeStation Strategy Optimization](https://help.tradestation.com/10_00/eng/tradestationhelp/optimize/strategy_optimization.htm)
- [TradeStation Walk-Forward Optimizer](https://help.tradestation.com/09_01/tswfo/topics/about_wfo.htm)
- [AmiBroker portfolio backtesting](https://amibroker.com/guide/h_portfolio.html)
- [AmiBroker optimization](https://amibroker.com/guide/h_optimization.html)
- [AmiBroker walk-forward testing](https://amibroker.com/guide/h_walkforward.html)
- [AmiBroker Monte Carlo simulation](https://amibroker.com/guide/h_montecarlo.html)
- [Backtrader Strategy](https://www.backtrader.com/docu/strategy/)
- [Backtrader Cerebro](https://www.backtrader.com/docu/cerebro/)
- [vectorbt getting started](https://vectorbt.dev/)
- [vectorbt Portfolio base](https://vectorbt.dev/api/portfolio/base/)
- [backtesting.py API](https://kernc.github.io/backtesting.py/doc/backtesting/backtesting.html)
- [backtesting.py quick start](https://kernc.github.io/backtesting.py/doc/examples/Quick%20Start%20User%20Guide.html)
- [OptionStrat Options Builder Tutorial](https://optionstrat.com/tutorials/options-builder)
- [Option Omega Backtest Setup](https://docs.optionomega.com/backtesting/backtest-setup)
- [Option Omega Backtest Results](https://docs.optionomega.com/backtesting/backtest-results)
- [Option Alpha Backtesting](https://docs.optionalpha.com/tools/backtesting)
- [Zipline tutorial](https://zipline.ml4trading.io/beginner-tutorial.html)
- [Zipline API reference](https://zipline.ml4trading.io/api-reference.html)

### Research Context

- [Implementation Risk in Portfolio Backtesting: A Previously Unquantified Source of Error](https://arxiv.org/abs/2603.20319)

## Maintenance Rule

Refresh this document when:

- A learn-ai engine path is introduced, retired, or reclassified.
- A major external platform changes its documented testing assumptions.
- learn-ai adds optimization, intrabar replay, options backtesting, or automation.
- `docs/math-sources-of-truth.md` changes enough that the current-baseline section is stale.

Do not use this document to justify math changes directly. Use it to propose product and architecture improvements, then route implementation through the normal learn-ai authority hierarchy.
