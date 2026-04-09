# LEAN Engine — Phase 2+ Implementation Plan

**Date:** 2026-04-09
**Status:** Draft for review
**Prerequisite:** Phase 1 closed — SPY bit-exact parity holds, NEXT_BAR_OPEN statistics now reflect actual fills, Decimal performance confirmed as a non-issue (~2.3 s for the full 2-year SPY backtest).

This plan picks up where `lean-engine-phase1-verification-report.md` left off. Three Phase 1 gaps were closed in the same session: `_LoggedTrade` is now populated from `OrderEvent` fills so NEXT_BAR_OPEN statistics are internally consistent (Gap 1), a `test_spy_next_bar_open_validation` harness with a committed engine baseline and a tolerance framework for the LEAN reference comparison was added (Gap 2), and a profiled benchmark showed the SPY backtest runs in ~2.3 s median with ~2 s spent in zip/CSV parsing rather than Decimal math (Gap 3). No migration to float64 is planned; Decimal stays.

The user decisions captured before this plan was drafted: close out Phase 1 before Phase 2 (done), prefer Option A for the NEXT_BAR_OPEN fix (done), expose the LEAN Engine UI as a new top-level Angular route rather than embedding it in the existing backtest page, and accept the synthetic Polygon → LEAN round-trip check in place of a live Polygon run.

---

## 1. Guiding principles for Phase 2+

Three rules that everything below should obey.

**Keep the new engine separate from the legacy pipeline.** The existing `/api/backtest/*` flow, the `services/strategies/*` pandas-ta implementations, and the `StrategyExecution`/`BacktestTrade` Postgres tables continue to work untouched. The LEAN engine ships alongside them under `/api/engine/*` and a new Angular route. Users can compare both engines on the same strategy; nothing is silently rerouted.

**Never break bit-exact parity.** Every Phase 2+ change must keep `test_spy_validation` green in SIGNAL_BAR_CLOSE mode, and `test_spy_next_bar_open_validation` green against its committed baseline. Both tests run in CI (or at minimum pre-commit). Any refactor that touches `app/engine/` has these as its first gate.

**Port strategies before adding features.** The realism work (slippage, commissions) and framework work (Alpha/Portfolio/Risk split) will be much easier to validate once there are three or four strategies producing trades against the new engine. Widening the strategy coverage also exercises code paths the single-strategy SPY test cannot reach (short positions, multi-symbol universes, schedule-driven logic).

---

## 2. Phase 2 — Generalization

The goal of Phase 2 is to take the LEAN engine from "one strategy, one test" to "a registry of strategies, runnable from the UI, with results persisted to Postgres for comparison against the legacy pipeline."

### 2.1 Strategy porting

Four strategies live in `PythonDataService/app/services/strategies/`: `sma_crossover`, `rsi_mean_reversion`, `momentum_rsi_stochastic`, and `ema_crossover_rsi`. (`rsi_reversal` also exists but overlaps with `rsi_mean_reversion` — confirm before porting.) Each one is a pandas-ta pipeline that operates on a precomputed DataFrame and emits signals; the new engine expects a bar-by-bar `Strategy` subclass like `SpyEmaCrossoverAlgorithm`.

The port for each strategy is roughly the same shape: subclass `Strategy`, declare indicators in `initialize()`, register a consolidator at the appropriate resolution, port the entry/exit logic into a bar handler, and populate `_pending_entry` → `_open_trade` → `trade_log` the same way SPY does. The `_LoggedTrade` dataclass can be generalized into `strategy/base.py` so each ported strategy does not need its own copy.

Proposed order, easiest to hardest:

1. **`sma_crossover`** — the simplest. Two SMAs, one crossover rule, no RSI filter. Good for shaking out any rough edges in the base-class generalization.
2. **`ema_crossover_rsi`** — nearly identical to SPY but configurable. Porting this lets me retire `SpyEmaCrossoverAlgorithm` as a hand-written special case and replace it with a parameterized instance, which keeps the bit-exact test honest: if the generic version reproduces SPY, the generalization is correct.
3. **`rsi_mean_reversion`** — introduces a true long-on-dip / exit-on-recovery rule instead of a time-based exit. Exercises the fill model with entries and exits that can happen on the same bar type.
4. **`momentum_rsi_stochastic`** — adds a second filter indicator (stochastic), stresses the indicator plumbing.

Each ported strategy gets a parity test under `app/engine/tests/` that runs the same input data through both the legacy pandas-ta version and the new engine, and asserts that the **trade count and per-trade PnL sign match**. Exact price parity is not required here — the legacy pipeline uses daily bars and different entry/exit timing conventions — but the set of winning vs losing trades should be the same. This is the lightest contract that still catches "I ported the logic wrong."

### 2.2 Strategy registry and parameters

Right now the engine registry in `app/routers/engine.py` is a `dict[str, type[Strategy]]` with one entry. It needs two small additions for Phase 2:

First, a **parameter schema** per strategy. Each strategy already accepts a dict in the legacy pipeline; the engine should do the same via a typed `params` field on the request, validated at registration time. The simplest form is a Pydantic `BaseModel` per strategy (e.g., `SmaCrossoverParams`), registered alongside the strategy class. The `POST /api/engine/backtest` endpoint parses `params` against the schema and passes a validated object into the strategy constructor.

Second, **strategy metadata for the UI**: display name, short description, supported symbols, default parameter values, and required data resolution. This is returned from `GET /api/engine/strategies` so the frontend can render a strategy picker without hardcoding any of it.

### 2.3 Postgres writeback

The legacy `/api/backtest` flow writes each run to two tables:

- `StrategyExecution`: one row per backtest (ticker, strategy name, params JSON, date range, timespan, summary metrics).
- `BacktestTrade`: one row per fill pair (entry/exit timestamps, prices, PnL, cumulative PnL, signal reason).

The LEAN engine should write to the same tables so the existing ticker-explorer / strategy-lab pages can show new-engine results next to legacy-engine results with no schema change. To keep the two engines distinguishable in the UI, add a single nullable `EngineVersion` column to `StrategyExecution` with values `"legacy"` or `"lean_v1"`. This is a single-column migration and is backwards compatible: existing reads that ignore the column see both engines; new reads can filter.

The writeback path is Python → .NET GraphQL mutation. The PythonDataService does not have direct Postgres access in the existing pipeline — it calls back into the .NET Backend via GraphQL. This plan keeps that pattern: after a backtest completes, `app/routers/engine.py` posts a `createStrategyExecution` mutation to the Backend's GraphQL endpoint, with the trades attached as a nested list. The Backend adds a new resolver that persists the execution and trades in one transaction.

One subtlety: the legacy pipeline records trades with a `SignalReason` string. The LEAN engine already emits log lines (ENTRY SIGNAL / EXIT SIGNAL) with indicator values at decision time. Those log lines should be captured per trade and stored as the `SignalReason`, so the UI can show "EMA5=514.19 EMA10=513.93 RSI=57.33" next to each trade. This is a small addition to `_LoggedTrade` — one optional `signal_reason: str` field — and a small plumbing change in `on_order_event` to carry the log line from entry signal through to the trade record.

### 2.4 Angular UI — new top-level route

Per the decision: the LEAN engine gets its own top-level page, not a tab inside `strategy-lab`. The route is `/lean-engine`, lazy-loaded via `loadComponent`, under `Frontend/src/app/components/lean-engine/`.

Minimum viable layout:

- **Strategy picker** (dropdown) — driven by `GET /api/engine/strategies`. Shows display name + description.
- **Parameter form** — rendered dynamically from the strategy's parameter schema. Each strategy brings its own defaults.
- **Symbol + date range + fill mode + initial cash** — free-form inputs with sensible defaults (SPY, last 2 years, signal_bar_close, 100000).
- **Run button** — POSTs `/api/engine/backtest`, shows a spinner while waiting.
- **Results panel** — three tabs:
  - *Summary*: the statistics block (win rate, profit factor, Sharpe, Sortino, Calmar, max drawdown, final equity, net profit, total fees). Rendered as a KPI grid.
  - *Trades*: the trade table. Sortable, with entry/exit timestamps, prices, indicator snapshot, PnL, and result badge.
  - *Equity curve*: a simple line chart of cumulative PnL over time. Reuse the existing chart wrapper component if possible; otherwise a lightweight new one.

Everything is Angular signals + `OnPush`, standalone component, `inject()` for the HTTP service, per project conventions in `.claude/CLAUDE.md`. For the first cut, HTTP calls go directly to the Python service via a thin `lean-engine.service.ts` — no GraphQL wrapping. GraphQL can come later if it starts duplicating existing backend queries.

**What this page is not (yet):** a comparison view against the legacy engine, a multi-strategy / multi-symbol scheduler, or a persisted-run history browser. Those come in Phase 4. The first cut is "pick a strategy, run it, see the trades."

### 2.4a Documentation tab — "How the engine works"

Alongside the Run / Results tabs on `/lean-engine`, a **Docs** tab hosts a detailed explanation of how data flows through the engine and what mathematics each stage uses. It follows the existing in-app docs convention: a standalone Angular component under `Frontend/src/app/components/lean-engine/lean-engine-docs/`, using the shared `KatexDirective` (`appKatex`) for equations, the same SCSS look as `strategy-docs`, `portfolio-docs`, `indicator-docs`, and `options-math-docs`. HTML + CSS + KaTeX, no external markdown viewer.

The audience is the engineer (me, you, anyone who later touches this code). The depth is "formulas plus invariants": formal definitions with the edge cases that matter for LEAN parity, no derivations. Every section of code that does non-trivial arithmetic should be findable from this page, and every section of this page should link back to the file and function it describes.

**Top-level structure:**

1. **Overview and lifecycle** — one diagram of the full pipeline (data → reader → consolidator → indicators → strategy → orders → fill model → portfolio → trade log → statistics), plus a short description of each stage and its responsibility. Calls out the three invariants the engine depends on: immutable bars, monotonically increasing time, Decimal arithmetic end-to-end.

2. **Data stage** — LEAN minute zip format, the deci-cent integer encoding $p_{\text{int}} = \lfloor p \times 10000 \rfloor$, the per-day file convention, the Eastern Time trading-date grouping, early-close handling, and the Polygon → LEAN export path. Explains why we store and compare in ET-aware datetimes throughout.

3. **Consolidation stage** — wall-clock alignment rule with the exact expression: for an interval $\Delta$ and minute bar time $t$, the bar start is $t - (t \bmod \Delta)$ where $t$ is counted in ticks. Explains why the first bar of the regular session covers 09:30–09:44 inclusive and fires at 09:45, and why early-close days with no 13:00 minute bar correctly produce no partial bar. Links directly to `consolidator.py`.

4. **Indicators** — one subsection per indicator actually in use.

   - **Simple Moving Average:** $\mathrm{SMA}_n(t) = \frac{1}{n}\sum_{i=0}^{n-1} c_{t-i}$, warmup = $n$ samples.

   - **Exponential Moving Average (LEAN seeding):** for period $n$, smoothing factor $\alpha = \frac{2}{n+1}$, and warmup of the first $n$ samples seeded as an SMA rather than the usual "start from first price" shortcut. After warmup:
   $$\mathrm{EMA}_t = \alpha \cdot c_t + (1-\alpha) \cdot \mathrm{EMA}_{t-1}$$
   Explicitly flags the SMA-seed trap: without it, the first N-1 values drift by $10^{-3}$ to $10^{-4}$ vs LEAN, which silently breaks bit-exact parity.

   - **Wilders-smoothed RSI:** period $n = 14$. The invariant is that Wilders requires **$n+1 = 15$ close samples** before the first output. Gain and loss smoothing use the Wilders recursion:
   $$\bar{G}_t = \frac{(n-1)\bar{G}_{t-1} + G_t}{n}, \qquad \bar{L}_t = \frac{(n-1)\bar{L}_{t-1} + L_t}{n}$$
   then $\mathrm{RS}_t = \bar{G}_t / \bar{L}_t$ and $\mathrm{RSI}_t = 100 - \frac{100}{1 + \mathrm{RS}_t}$. Edge cases: all-losses bar ($\bar{G} = 0 \Rightarrow \mathrm{RSI} = 0$) and all-gains bar ($\bar{L} = 0 \Rightarrow \mathrm{RSI} = 100$).

5. **Strategy lifecycle** — `initialize()` → warmup (historic bars pushed through consolidator and indicators, no orders accepted) → `on_data()` per consolidated bar → order submission → `on_order_event()` on fill → `on_end_of_algorithm()`. Explains what "signal time" vs "fill time" means and why `_pending_entry` / `_open_trade` are split. Documents the invariant that `trade_log` is only appended to inside `on_order_event`, never inside `on_data`.

6. **Fill models** — two variants.

   - **`SignalBarCloseFillModel`:** fill price = signal bar's close; fill time = signal bar's end time. Designed for bit-exact reproduction of LEAN's trade-log convention. Explicitly noted as non-realistic (you cannot actually trade at a bar's close on the bar where you observed it).
   - **`NextBarOpenFillModel`:** fill price = open of the next bar after the signal; fill time = that bar's start time. Realistic but differs from LEAN's logged-trade convention by construction.

   The section makes it unambiguous which model to use for LEAN parity and which for live-style simulation, and why the trade-log statistics are now consistent with the portfolio net profit in either mode.

7. **Portfolio and order management** — `SetHoldings(symbol, weight)` math: target shares = $\lfloor (\text{equity} \times \text{weight}) / \text{price} \rfloor$, cash adjustment, and how fills update positions. The decimal-arithmetic invariant is repeated here because this is where float drift would accumulate over a 2-year backtest.

8. **Commission and slippage models** — populated once Phase 3 lands. For now, stubs for `FlatCommission` ($1/order) and the planned `InteractiveBrokersFeeModel` and three slippage models.

9. **Statistics** — definitions for every metric `statistics.summarize()` returns.

   - **Win rate:** $\text{wins} / \text{trades}$.
   - **Profit factor:** $\sum_{w \in \text{wins}} w / \left| \sum_{l \in \text{losses}} l \right|$.
   - **Expectancy:** $p_w \cdot \overline{w} + (1 - p_w) \cdot \overline{l}$ where $\overline{w}, \overline{l}$ are the average win and loss.
   - **Max drawdown:** $\max_{t} \left( \max_{s \le t} E_s - E_t \right)$ on the equity curve $E$.
   - **Sharpe ratio (annualized):** $\mathrm{SR} = \sqrt{N} \cdot \frac{\mu_r - r_f}{\sigma_r}$ where $N$ is the periods-per-year factor used by the engine (documented here), $\mu_r$ and $\sigma_r$ are per-trade return moments, and $r_f$ is the risk-free rate (currently 0).
   - **Sortino ratio:** same as Sharpe but with downside deviation $\sigma_d = \sqrt{\frac{1}{N}\sum_{r_i < 0} r_i^2}$ in the denominator.
   - **Calmar ratio:** annualized return $/$ max drawdown.

   Each formula is accompanied by one line of "where this comes from in the code" pointing at `statistics.py`.

10. **Worked example — SPY first trade bar-by-bar.** A full trace of the first trade in the committed SPY run (2024-04-11, entry near 515.33 in NEXT_BAR_OPEN mode). Shows the minute bars arriving, the consolidator producing the signal 15-minute bar, the EMA5 / EMA10 / RSI values at that moment, the exact crossover condition evaluating to true, the gap threshold check passing, the order submitting, the fill event arriving with price and time, `_pending_entry` being consumed into `_open_trade`, and eventually the exit bar producing the `_LoggedTrade` row. Real numbers pulled from the validation fixtures, not invented. This is the section a newcomer (or future-me) reads first to understand the whole flow concretely.

11. **Reproducibility traps** — condensed version of the reproducibility memory: ROUND_HALF_UP vs banker's rounding, SMA seeding for EMA, Wilders N+1 warmup, ET-aware datetimes, the deci-cent encoding, the `track` convention. This section exists so that when someone asks "why did my fork of this engine drift from LEAN," they have a checklist to work through.

**File layout:**

```
Frontend/src/app/components/lean-engine/lean-engine-docs/
  lean-engine-docs.component.ts
  lean-engine-docs.component.html
  lean-engine-docs.component.scss
```

Wired into `/lean-engine` as a tab alongside Run and Results, matching the tab pattern already used in `strategy-lab`. Uses `appKatex` from `Frontend/src/app/shared/katex.directive.ts`. No new dependencies.

**Done definition for the docs tab:** every engine file under `PythonDataService/app/engine/` that does arithmetic is referenced at least once from the docs page, the SPY worked example reproduces numbers taken from the actual validation fixture (not guessed), and all KaTeX blocks render without throwing. A lightweight test in `lean-engine-docs.component.spec.ts` asserts that the component renders and that at least one `appKatex` element produced a `.katex` child node.

### 2.5 Data export touch-ups

The Polygon → LEAN exporter is working end-to-end against synthetic data and is considered sufficient. Two small cleanups worth doing while Phase 2 is in flight:

- **Input validation on `/api/engine/export-lean`**: reject `from_date > to_date` before hitting Polygon, and cap the range at something reasonable (90 days?) to avoid a single bad call pulling megabytes of minute data.
- **Idempotency**: if a zip for a given day already exists, skip rewriting it unless the request passes `force: true`. This makes it safe to call the endpoint repeatedly while iterating on a strategy over the same date range.

Neither is a correctness issue; both are quality-of-life.

### 2.6 Phase 2 done definition

- All four existing strategies ported and green against their parity tests.
- `SpyEmaCrossoverAlgorithm` replaced by a parameterized `EmaCrossoverRsiAlgorithm` instance; `test_spy_validation` still passes bit-exactly against it.
- `GET /api/engine/strategies` returns the registry with schemas and metadata.
- `POST /api/engine/backtest` persists runs to `StrategyExecution` + `BacktestTrade` with `EngineVersion = "lean_v1"`.
- `/lean-engine` route runs a full strategy end-to-end from the UI and displays summary, trades, and equity curve.
- **Docs tab** under `/lean-engine` renders the full "How the engine works" document (HTML + CSS + KaTeX via the shared `appKatex` directive), including the SPY first-trade worked example with numbers pulled from the validation fixture.

---

## 3. Phase 3 — Realism

Phase 3 is about making fills and fees resemble what a real broker would actually charge and execute. The extended statistics (Sortino, Calmar, profit factor, max drawdown) were already delivered as part of Phase 1's gap-closing work, so this phase is narrower than the original plan.

### 3.1 Slippage models

LEAN's slippage model interface is a single method: `GetSlippageApproximation(security, order) → decimal`. The fill model consults it at execution time and shifts the fill price accordingly. Three models worth having:

- **`ConstantSlippageModel(amount)`** — fixed dollar amount per order, for smoke tests and deterministic comparisons.
- **`SpreadSlippageModel(spread_bps)`** — half-spread penalty in basis points. The cheapest realistic approximation for liquid equities.
- **`VolumeShareSlippageModel(price_impact)`** — market-impact model proportional to `(order_size / bar_volume)²`. LEAN's default for equities. This one depends on bar volume, which the current fill model already has access to via the `TradeBar`.

The slippage model is injected into `FillModel` at construction time, defaults to a zero model, and is applied in both `SIGNAL_BAR_CLOSE` and `NEXT_BAR_OPEN` code paths. The unit tests are straightforward — each model is a pure function of `(side, price, size, volume)`.

**Regression protection:** `test_spy_validation` continues to run with the zero slippage model so the bit-exact gate does not move. A new `test_spy_slippage_smoke` runs with a `ConstantSlippageModel(0.01)` and asserts that every fill is exactly one cent worse than the zero case. That is a strong signal the wiring is correct without adding another fragile reference file.

### 3.2 Commission model matching Interactive Brokers

Right now the commission is a flat `$1 per order` placeholder. LEAN's `InteractiveBrokersFeeModel` for US equities is tiered: $0.005 per share, minimum $1, maximum 1% of trade value, plus regulatory fees (SEC + FINRA TAF) on sells. The formula lives in LEAN's source — I'll port it directly rather than approximate, since the inputs (shares, price, side) are already available in the fill event.

The commission per order in the SPY backtest is currently $1 flat, which closely matches the IB minimum for SPY-sized orders, so moving to the tiered model will not materially change SPY results. That is a feature: the bit-exact test should still pass after the switch, giving me a ready-made regression gate for the new model.

Each fill model carries a commission model reference; the FastAPI request body accepts a `commission_model: "flat" | "ib_equities"` field with `"flat"` as the default. This keeps the old behavior for anyone who was already using it.

### 3.3 What moves to Phase 4

MAE/MFE (maximum adverse / favorable excursion) per trade is intentionally deferred to Phase 4. It requires the engine to walk every bar between entry and exit to track intra-trade extremes, which adds a new responsibility to the strategy base class. Rather than bolt it on mid-Phase 3, it belongs with the framework restructuring in Phase 4, where I can add it as an `IntraTradeMonitor` hook that any strategy can opt into.

### 3.4 Phase 3 done definition

- Three slippage models implemented, unit tested, and wired into `FillModel`.
- `InteractiveBrokersFeeModel` implemented, unit tested, and wired as an option.
- `test_spy_validation` still passes bit-exactly with `ZeroSlippage` + `FlatCommission` (the existing defaults).
- A new `test_spy_realistic_fills` runs the SPY strategy with `SpreadSlippageModel(2 bps)` + `InteractiveBrokersFeeModel` and commits a snapshot of the resulting statistics as a regression baseline. Future realism changes must reproduce the baseline or justify the drift.
- `POST /api/engine/backtest` accepts `slippage_model` and `commission_model` params with documented values and defaults.

---

## 4. Phase 4 — Framework

This is where the engine starts to look like LEAN structurally, and where multi-symbol / multi-strategy support lands. It is also the biggest phase in scope, so I expect it to split into sub-milestones once Phase 3 is closed.

### 4.1 Alpha / Portfolio / Risk / Execution split

LEAN's framework decomposes a strategy into four modules with clean interfaces:

- **Alpha model** — consumes data, emits `Insight` objects ("SPY will go up for 75 minutes").
- **Portfolio construction** — converts insights into target position weights.
- **Risk management** — adjusts targets to respect constraints (max position, max drawdown).
- **Execution** — turns target weights into orders via the fill model.

The current engine collapses all four into `Strategy.on_data`. Phase 4 introduces the interfaces and refactors `SpyEmaCrossoverAlgorithm` into an `Alpha` (the EMA+RSI signal), a default `EqualWeightingPortfolioConstruction`, a null risk model, and an `ImmediateExecutionModel`. The bit-exact test must survive this refactor, which is the hardest single thing in Phase 4 and why it leads.

### 4.2 Scheduled events

LEAN's `Schedule.On(DateRules, TimeRules, callback)` is used heavily for rebalancing, end-of-day cleanup, open/close logic. The scheduler is a separate component that runs alongside the data feed and fires callbacks at wall-clock times. Phase 4 introduces it with a minimal surface: `DateRules.EveryDay()`, `DateRules.WeekStart()`, `TimeRules.AfterMarketOpen(minutes)`, `TimeRules.BeforeMarketClose(minutes)`. More rules can be added as strategies need them.

### 4.3 Multi-symbol support

The data reader, consolidator, portfolio, and fill model all assume a single symbol today. Phase 4 generalizes each to a symbol-keyed dict. This is mostly mechanical but exposes a latent issue: the strategy's indicator updates are currently hand-rolled per symbol. The framework split makes this cleaner — the alpha model owns per-symbol indicators and iterates its own subscribed universe.

A multi-symbol smoke test runs a two-strategy portfolio (SPY EMA crossover + QQQ SMA crossover) through a combined engine and verifies the portfolio equity curve is the sum of the two standalone runs minus cross-strategy interaction. This is the simplest meaningful parity check.

### 4.4 MAE / MFE per trade

With the framework split in place, MAE/MFE can be added as a hook on the strategy base class that consumes every bar while a position is open and records the worst-case and best-case unrealized PnL for that trade. The fields land on `_LoggedTrade` and surface in the `statistics` block and the UI trade table.

### 4.5 Phase 4 done definition

- `AlphaModel`, `PortfolioConstructionModel`, `RiskManagementModel`, `ExecutionModel` interfaces defined.
- `SpyEmaCrossoverAlgorithm` decomposed into an alpha + default portfolio + null risk + immediate execution, and `test_spy_validation` still passes bit-exactly.
- `Schedule.On` implemented with a minimum of four rule combinators, unit tested.
- Multi-symbol backtest runs end-to-end and produces correct portfolio-level statistics.
- MAE/MFE populated on every trade in the trade log and exposed in the API response.

---

## 5. Phase 5 — Data infrastructure

The smallest phase in terms of code but the one most likely to surface tricky edge cases. Phase 5 makes the engine honest about corporate actions.

### 5.1 Map files

LEAN's map files record ticker changes over time (e.g., `FB → META` on 2022-06-09). Without them, a backtest of `META` before 2022-06-09 silently uses no data or the wrong data. The map file format is simple (two columns, CSV per symbol) and LEAN ships a default set; the engine should read the same files and apply the mapping when loading data.

### 5.2 Factor files

Factor files record split and dividend adjustments so a backtest can see price-adjusted bars. This is the hardest piece, not because the code is complex but because *deciding when to adjust* is subtle. The engine needs two modes: `"raw"` (what SPY currently uses — real prices) and `"adjusted"` (back-adjusted for corporate actions). Strategies declare which they want in `initialize()`.

The SPY bit-exact test uses raw mode and should continue to. A new `test_aapl_split_adjustment` validates that a strategy running across the 2020 AAPL 4:1 split produces continuous price bars with `"adjusted"` mode and discontinuous ones with `"raw"`.

### 5.3 Symbol change tracking

This is the runtime consequence of §5.1: the engine must seamlessly re-subscribe under the new ticker when a symbol change occurs mid-backtest. Most strategies will never hit this, but it needs to work when it does.

### 5.4 Phase 5 done definition

- Map file and factor file readers implemented and unit tested against LEAN's own fixtures.
- A multi-year backtest of a strategy that survives a symbol change (e.g., FB → META) runs cleanly.
- A multi-year backtest across a known split (e.g., AAPL 2020) produces adjusted and raw modes, each internally consistent.
- SPY bit-exact test still passes (it uses raw data with no corporate actions in range, so this should be free).

---

## 6. Open questions to confirm before starting Phase 2

A few things I would rather surface now than discover mid-implementation:

1. **Parity tests against the legacy pipeline — how strict?** My current proposal is "same set of winning vs losing trades." Would you rather the bar be stricter (exact trade count + same entry timestamps) or looser (summary statistics within tolerance)? This affects how much effort goes into debugging each port.

2. **EngineVersion column — OK to add as a schema migration to the Backend?** This is a single-column, backwards-compatible change but it does touch the shared database. Confirm I can add a new EF Core migration as part of Phase 2.

3. **GraphQL vs direct HTTP from the new Angular route.** I am proposing direct HTTP (Angular → Python service) for the first cut to avoid duplicating the entire strategy metadata pipeline through the .NET GraphQL schema. If you would rather this go through GraphQL from day one, it adds Backend resolver + DataLoader work to Phase 2.

4. **Second validation target beyond SPY.** Phase 1 used SPY because LEAN had a reference run on disk. For the parametrized EMA-crossover-RSI generalization, it would be useful to have a second LEAN reference run over a different symbol (e.g., QQQ or a single-name equity) to catch anything SPY-specific baked in by accident. Are you willing to run LEAN once more to capture that, or should Phase 2 rely on the cross-strategy parity tests alone?

---

## 7. Recommended starting point

If you approve this plan, the first commit of Phase 2 is the smallest thing that exercises the end-to-end shape: port `sma_crossover`, add a parameter schema to the registry, extend `POST /api/engine/backtest` to accept it, and stub the Angular route with just the strategy picker and run button (no results panel yet). That takes us from "one hardcoded strategy" to "two strategies picked from a dropdown" and unblocks everything else — each subsequent port and each UI panel becomes an independent, mergeable change.
