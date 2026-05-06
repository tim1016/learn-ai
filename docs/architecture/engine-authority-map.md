# Engine authority map

**Status:** Active
**Last reviewed:** 2026-05-04
**Pairs with:** `docs/math-sources-of-truth.md` (concept-level registry), `docs/architecture/numerical-authority-migration-plan.md` (migration sequencing)

This document answers exactly one question: **which engine owns which job?**

`math-sources-of-truth.md` is the *concept-level* registry — one row per math concept, with the canonical file. This document is the *engine-level* map — one row per execution path, with its declared role. The two are paired: every engine that produces user-comparable numbers must also have its outputs accounted for in the concept-level registry.

If two docs disagree, `math-sources-of-truth.md` wins for math and this doc wins for engine ownership; both should be updated in the same PR.

## The map

| Job | Owning engine (canonical) | Path / entry point | Role | Status |
|---|---|---|---|---|
| Interactive backtest (stocks, indicator strategies) | **Engine Lab** | `PythonDataService/app/engine/` via router `app/routers/engine.py` | Canonical event-driven engine; LEAN-ported semantics | **canonical** — vendored LEAN reference at `references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/` |
| Interactive backtest (options strategies) | **Engine Lab options layer** | `PythonDataService/app/engine/options/` + `app/engine/strategy/algorithms/spy_ema_crossover_options.py` | Same engine as stocks; options pricing routes through `bs_greeks.py` / `quantlib_pricer.py` | canonical |
| Configurable strategy spec (declarative entry/exit/manage rules over indicators) | **Strategy Spec layer** | `PythonDataService/app/engine/strategy/spec/` (Pydantic schema, `SpecAlgorithm` evaluator, primitives, indicator registry); canonical fixtures at `app/engine/strategy/spec/fixtures/*.spec.json`; HTTP entry `app/routers/spec_strategy.py` (`POST /api/spec-strategy/backtest`, `GET /api/spec-strategy/schema`, `GET /api/spec-strategy/fixtures`); GraphQL passthrough `Backend/GraphQL/SpecStrategyMutation.cs::runSpecStrategyBacktest` via `Backend/Services/Implementation/SpecStrategyService.cs`. | Phase 1 (shipped 2026-05-04): equity-only, single-symbol, indicator-driven entry/exit. Three parity-pinned secondary implementations (SPY EMA / SMA / RSI mean reversion) reproduce hand-coded twins trade-by-trade. **Phase 2.1:** ADX / MACD / Supertrend indicators wired; `PnLPercent`/`PnLPoints` primitives; `survival.CLOSE_ALL` manage action. **Phase 2.2:** `DrawdownFromPeak`/`BarProperty` primitives. **Phase 3a:** FastAPI router. **Phase 3b (shipped 2026-05-04):** .NET passthrough — `runSpecStrategyBacktest` GraphQL mutation calling `/api/spec-strategy/backtest`, with the spec passed through as a JSON object so Python's Pydantic schema remains the single source of truth for spec validation. Schema still admits options-template shapes; the evaluator refuses to run them. | parity-pinned secondary — hand-coded `algorithms/*.py` remain math-authority per `docs/math-sources-of-truth.md`; spec layer drift = test failure. Replaces `app/services/rule_based_backtest.py` per migration **Phase 4** |
| Research signal scoring (IC, walk-forward, diagnostics) | **Research signal backtester** | `PythonDataService/app/research/signal/backtest.py` (+ `engine.py`, `walk_forward.py`, `diagnostics.py`) | Research-metric infrastructure; produces IC, hit rates, regime splits, etc. | canonical — separate-purpose engine, not a duplicate of Engine Lab |
| Volatility / edge research | **Edge research** | `PythonDataService/app/engine/edge/` (`edge_score.py`, `regime_clustering.py`, `vrp.py`, `cross_asset_runner.py`) | Edge / regime / VRP analysis; consumes Engine Lab outputs and Polygon snapshots | canonical |
| Options strategy analysis (payoff, POP, Greeks for a hypothetical strategy) | **Options strategy analysis service** | `PythonDataService/app/services/strategy_engine.py::AnalyzeOptionsStrategy` (+ `app/routers/strategy.py`) | Server-side options-strategy analytics; consumed by GraphQL passthrough → Strategy Lab UI | canonical — **scheduled for payload extension in Phase 1.1** so Angular stops computing the same fields client-side |
| Portfolio scenario / what-if (theoretical option value across spot/time/IV grid) | **(Python `/portfolio/scenario` endpoint — not yet implemented)** | Planned: `PythonDataService/app/routers/portfolio.py::scenario` | Recomputes Greeks per scenario point against current spot/time/IV using `bs_greeks.py` / `quantlib_pricer.py` | **planned in Phase 2.1** |
| Portfolio live Greeks (current-time delta/gamma/theta/vega per position) | **(Python — not yet implemented)** | Planned: same router as above | Replaces the .NET stale-entry-Greek shock-propagation path | **planned in Phase 2.1** |
| Indicator computation (one-shot, for charts and `/api/indicators/calculate`) | **TA service (pandas-ta passthrough)** | `PythonDataService/app/services/ta_service.py` | Vectorized indicator computation for non-engine callers (charts, divergence pipeline, indicator reliability) | canonical — note: not parity-tested against Engine Lab indicators because the call shape differs (one-shot vs streaming bar updates); divergences would mean a real bug |
| Indicator computation (streaming, inside an event-driven backtest) | **Engine Lab indicator framework** | `PythonDataService/app/engine/indicators/` (`sma.py`, `ema.py`, `rsi.py`, `macd.py`, `adx.py`, `supertrend.py`) | LEAN-ported streaming indicators; consumed by Engine Lab strategies | canonical — bit-exact parity with vendored LEAN reference (`references/lean/<sha>/Indicators/`) per `tests/test_indicator_parity.py` |
| BS / Greeks / IV math | **Python options authorities** | `app/services/quantlib_pricer.py` (price + Greeks), `app/services/bs_greeks.py` (closed-form), `app/volatility/solver.py` (IV solver), `app/volatility/fitting.py` (surface fits) | Per `docs/architecture/options-math-authorities.md`; the dispatch rules between QuantLib and closed-form live there | canonical |

## Deprecated engines (scheduled for removal)

| Path | Role today | Replacement | Migration plan reference |
|---|---|---|---|
| `Backend/Services/Implementation/BacktestService.cs` | In-process .NET strategy execution (`RunSmaCrossover`, `RunRsiMeanReversion`, `RunMomentumRsiStochastic`, `RunRsiReversal`) + local Sharpe / drawdown helpers | `app/engine/` via GraphQL passthrough to `/api/engine/backtest` | **Phase 3** |
| `Frontend/src/app/components/strategy-lab/strategy-lab.component.*` | Strategy Lab UI; deprecation banner present in template | Engine Lab UI (`Frontend/src/app/components/lean-engine/lean-engine.component.ts`) | Already declared deprecated — see `docs/engine-phase-1-2-refined-plan.md` |
| `Frontend/src/app/utils/black-scholes.ts` | Client-side Black-Scholes price + Greeks for Strategy Lab UI | Server-extended `AnalyzeOptionsStrategy` payload | **Phase 1.1 → 1.3** |
| `PythonDataService/app/services/rule_based_backtest.py` (as a separate engine) | Standalone rule-based backtest path | `app/engine/strategy/spec/` (`StrategySpec` schema + `SpecAlgorithm` evaluator). Phase 1 of the spec layer shipped 2026-05-04 with parity-pinned ports of three reference strategies; Phase 4 will translate legacy `rule_based_backtest` params to a `StrategySpec` and delete this file. | **Phase 4** |
| `Backend/Services/Implementation/PortfolioRiskService.cs` (Greek *computation*; aggregation is fine) | Shock-propagates from stored `EntryDelta` / `EntryVega` / `EntryTheta` | Python `/portfolio/scenario` and `/portfolio/live-greeks` endpoints | **Phase 2** |
| `Backend/Services/Implementation/PortfolioValuationService.cs:80` (theoretical option-value path) | Computes option theoretical from entry Greeks | Same as above | **Phase 2** |

## Validation-only paths (not deprecated; have a clean separate purpose)

| Path | Why it stays | What it must NOT do |
|---|---|---|
| `Backend/Services/Implementation/PortfolioValuationService.cs::ComputeValuationInternal` (the aggregation arithmetic, not the option-theoretical path) | Pure aggregation over persistence data; rule-5 compliant | Compute FX, theoretical option value with cost-basis lots, or any non-trivial math. If it ever does, the canonical moves to Python and this path becomes a passthrough. |
| `Backend/Services/Implementation/PortfolioReconciliationService.cs` | Persistence-layer reconciliation only | Same as above |
| `Backend/Services/Implementation/StrategyAttributionService.cs` | Persistence-layer trade ↔ strategy linking | Same as above |
| `Backend/Services/Implementation/TechnicalAnalysisService.cs` | HTTP passthrough to `/api/indicators/calculate` | Compute indicators locally |
| `Backend/Services/Implementation/MarketDataService.cs` | Polygon API client for bars / aggregates | Compute indicators or strategy results |
| `docs/validation/*.pine` | TradingView Pine reference for strategy validation | Run in production; one-time validation only |

## Reading guide for future contributors

A new feature lands in this repo in roughly this order:

1. **Check `math-sources-of-truth.md`** for the concept being touched. If it has a canonical row, use it.
2. **Check this map** for which engine owns the job. If the job is owned by a specific engine, extend or call that engine — do not introduce a parallel path.
3. **If there is no concept row and no engine row,** decide *first* which engine owns the new work, then add a row to both docs in the same PR as the implementation.
4. **If you find yourself wanting to compute math in `.NET` or Angular,** stop and re-read `AGENTS.md` § Python owns all math. The exception is rare and must be documented in `math-sources-of-truth.md` with `legacy-ok` status and a parity test.

## Why this doc exists, and why it's short

`math-sources-of-truth.md` answers "where is the canonical implementation of *X*?" One row per math concept, granular, dozens of rows.

This doc answers "which engine should I extend / which engine owns this job?" One row per engine path, coarse, ~20 rows.

The two documents are deliberately at different levels of granularity. A coarse engine map plus a granular concept registry is enough metadata for one developer to make consistent decisions over time. Anything more would be ceremony.
