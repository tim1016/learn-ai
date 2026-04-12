# Options Strategy V1 — Implementation Challenges (Revised)

**Strategy:** SpyEmaCrossoverOptionsAlgorithm V1 (Bull Call / Bull Put Spread overlay)  
**Date:** 2026-04-11  
**Purpose:** Identify every gap between the current LearnAI engine/UI architecture and what V1 requires, accounting for existing infrastructure.

---

## What Already Exists (Asset Inventory)

Before listing gaps, here's what's already built and can be leveraged:

**Python Data Service:**
- `POST /api/snapshot/options-chain` — live option chain snapshot with Greeks (delta, gamma, theta, vega), IV, OI, bid/ask, day OHLCV per contract
- `POST /api/options/contracts` — contract listing with strike/expiration/type filtering
- `POST /api/options/expirations` — unique expiration dates for an underlying
- `POST /api/strategy/analyze` — multi-leg strategy analysis with Black-Scholes payoff curve, POP, EV, max profit/loss, breakevens, aggregate Greeks
- `POST /api/quantlib/price` — QuantLib-backed single option pricing with full Greeks (delta, gamma, theta, vega, rho, d1, d2)
- `POST /api/quantlib/strategy` — QuantLib multi-leg strategy pricing with per-leg and aggregate Greeks
- `GET /api/quantlib/status` — QuantLib availability check and engine listing
- `PolygonClientService` wrapping Polygon SDK: `list_options_contracts()`, `list_snapshot_options_chain()`, `list_options_expirations()`
- **QuantLib pricer** (`quantlib_pricer.py`): `price_option()` and `price_strategy()` with 6 engines (Analytic BS, Binomial CRR/JR/LR, Finite Diff, Monte Carlo), BSM process builder, numeric Greek fallbacks, dividend yield support
- Legacy Black-Scholes solver (`bs_solver.py`): pricing, vega, Newton-Raphson IV solver with Brent's fallback
- Legacy strategy engine (`strategy_engine.py`): pure-Python BS payoff curves, POP (lognormal N(d2)), EV, breakevens, Greeks — independent of QuantLib
- Contract finder (`contract_finder.py`): ATM strike selection, 25Δ OTM put/call selection by BS delta, liquidity filtering (min volume, min OI, max bid-ask spread), bracket expiry finder for IV interpolation
- IV builder (`iv_builder.py`): 30-day constant-maturity IV time series pipeline

**Backend (.NET):**
- GraphQL queries: `getOptionsChainSnapshot`, `getOptionsContracts`, `getOptionsExpirations`, `analyzeOptionsStrategy`
- Database models: `OptionContract` (symbol, strike, expiration, type, multiplier), `OptionLeg` (quantity, entry Greeks: IV, delta, gamma, theta, vega), `OptionsIvSnapshot`
- `PortfolioTrade` already has: `AssetType` (Stock/Option), `OptionContractId`, `OptionContract` nav, `Multiplier` (default 1), `OptionLeg` nav
- `PortfolioValuationService` already computes `NetDelta`, `NetGamma`, `NetTheta`, `NetVega` across option positions
- `FillOrderAsync` already accepts `multiplier` and optional `OptionLegInput` with Greeks snapshots
- Position tracking with multiplier-aware cost basis and PnL (`AvgCostBasis × NetQuantity × Multiplier`)

**Massive MCP (Starter Plan):**
- Built-in analytical Black-Scholes functions: `bs_price`, `bs_delta`, `bs_gamma`, `bs_vega`, `bs_theta`, `bs_rho` — applied as column-level post-processing on any data query
- Technical functions: `ema`, `sma`, `simple_return`, `log_return`, `cumulative_return`, `sharpe_ratio`
- All functions take standard inputs (S, K, T, r, sigma for Greeks; column + window for technicals)
- **No options chain endpoints, no IV data, no snapshots** on the starter plan — you supply `sigma` (IV) from another source (Polygon or estimated)
- Best suited for: bulk scenario analysis on tabular data, enriching Massive query results with Greeks, ad-hoc research

**Frontend (Angular):**
- `OptionsChainComponent` — full chain viewer with Greeks, IV, OI, bid/ask, ATM highlighting, expiration ribbon
- `OptionsStrategyLabComponent` — multi-leg builder with templates (Bull Call Spread, Bear Put Spread, etc.), payoff chart, What-If scenarios, time decay analysis
- `OptionsHistoryComponent` — historical chain analysis on specific dates
- TypeScript interfaces: `SnapshotContractResult`, `GreeksSnapshot`, `StrategyLegInput`, `StrategyAnalyzeResult`, `PayoffPoint`, etc.
- `MarketDataService`: `getOptionsChainSnapshot()`, `getOptionsContracts()`, `getOptionsExpirations()`, `analyzeOptionsStrategy()`

---

## Challenge 1: Bridging Option Chain Data into the Backtest Engine

**What exists:** The app fetches **live** option chain snapshots from Polygon via `POST /api/snapshot/options-chain`. The contract finder already does delta-based strike selection, liquidity filtering, and bracket expiry finding. QuantLib can price any option and compute Greeks given spot, strike, IV, DTE, and rate.

**What the engine lacks:** The backtest engine (`PythonDataService/app/engine/`) is a self-contained loop over historical minute bars. It has no mechanism to resolve option chains at historical timestamps.

**The real gap is historical option data for backtesting. Three approaches, now ranked with QuantLib in mind:**

1. **QuantLib synthetic chain (recommended for V1):** At each entry signal, use the underlying's current bar price as spot. Use Polygon's `list_options_contracts()` (which supports `as_of_date`) to find which contracts existed at that date with their strikes and expirations. Then use QuantLib's `price_option()` to compute theoretical prices and Greeks for each contract using a volatility assumption (flat IV from `OptionsIvSnapshot`, or interpolated from the IV builder). This avoids needing historical option *bar* data entirely — you only need the contract listing and a volatility estimate.
   - **Pros:** Fast, no massive data download, leverages QuantLib for Greeks (more accurate than hand-rolled BS), deterministic.
   - **Cons:** Theoretical prices may differ from actual market prices. Bid/ask spread is synthetic (e.g., ±2% of theoretical). No real OI/volume data for historical liquidity filtering.
   - **Mitigation:** For V1's goal of validating the *mechanics* (spread construction, PnL accounting, lifecycle), theoretical prices are sufficient. Real market prices matter more for V2+ performance analysis.

2. **Polygon historical option aggs:** Fetch actual OHLCV bars per option contract via `list_aggs()`. The contract finder already knows which tickers to look up. Greeks computed by QuantLib from the bar's close price and solved IV. Gives real market prices but requires many API calls per backtest and no OI/volume at bar level.

3. **Pre-cached daily snapshots:** Extend the `iv_builder.py` pattern to download and cache full option chain data per trading day. Richest data but largest upfront cost (~3,000+ API calls per ticker per year).

**Key insight with QuantLib:** The contract finder's `_bs_delta()` is a hand-rolled 5-line BS delta. QuantLib's `price_option()` gives you delta, gamma, theta, vega, rho in one call with full BSM process support (including dividend yield). Replacing `_bs_delta()` with QuantLib in the engine consolidates all pricing to one source of truth.

**Effort:** Medium. The synthetic chain approach (option 1) can be built quickly since `list_options_contracts(as_of_date=...)` + QuantLib `price_option()` are both already available. The contract finder's filtering logic ports directly.

---

## Challenge 2: The Engine Trade Model Is Single-Instrument, Single-Price

**What exists:** The `LoggedTrade` in `strategy/base.py` has single `entry_price`, `exit_price`, `pnl_pts`, `pnl_pct` fields. The frontend `engine-results` component and backend `BacktestTrade` model mirror this shape.

**What V1 needs:** A spread trade has two legs with separate symbols, strikes, prices, and deltas. There is no single "entry price."

**Mitigation path:** The `LoggedTrade` already has a flexible `indicators: dict[str, Decimal]` bag for strategy-specific data. V1 can pack leg details into this bag (e.g., `long_strike`, `short_strike`, `long_entry_price`, `short_entry_price`, `net_debit`, `expiration`, `spread_type`, `long_delta`, `short_delta`). The summary fields can be repurposed:

- `entry_price` → net debit/credit at entry
- `exit_price` → net debit/credit at exit
- `pnl_pts` → realized spread PnL (per contract)
- `pnl_pct` → PnL as % of capital at risk

This avoids a schema break while still carrying full leg detail in the indicators bag. The frontend already renders strategy-specific indicators dynamically.

**Effort:** Small-Medium. Mostly a convention decision, not a structural rewrite.

---

## Challenge 3: PnL Calculation Is Equity-Based

**What exists:** Engine PnL is `exit_price - entry_price`. The backend `PortfolioValuationService` already handles multiplier-aware PnL for the portfolio module, but the engine's internal PnL is simpler.

**What V1 needs:**

- **Bull Call Spread:** `PnL = exit_credit - entry_debit`
- **Bull Put Spread:** `PnL = entry_credit - exit_debit`
- **Dollar PnL:** spread PnL × multiplier (100) × contracts_per_trade
- **PnL %:** relative to max loss (entry debit for bull call, spread width - entry credit for bull put)

**Key insight:** The strategy class owns its own PnL calculation in the `_log_trade()` method. This is an override in the new strategy subclass, not a change to the base class. The base class `LoggedTrade` shape can stay the same — the *values* put into `pnl_pts` and `pnl_pct` just mean something different.

**Downstream impact:** Statistics (Sharpe, Sortino, drawdown) are computed from the PnL values in the trade log. As long as dollar PnL values are consistent, the stats engine works unchanged. However, `pnl_pts` historically meant "price points on the underlying" — documenting that it now means "net spread PnL per contract" is important for interpretation.

**Effort:** Small. Contained in the new strategy class.

---

## Challenge 4: The Order System Does Not Support Multi-Leg Orders

**What exists:** The engine's `Portfolio.submit_market_order()` creates a single `Order` → single `OrderEvent`. The backend portfolio module's `FillOrderAsync` already supports `OptionLegInput` for recording option trades, but the backtest engine has no equivalent.

**What V1 needs:** Two simultaneous fills per entry/exit.

**Recommended approach for V1:** Don't build a full combo order system. Instead:

1. The strategy submits two sequential market orders (long leg, short leg) in the same handler call
2. The fill model fills both immediately at their respective option prices (in backtesting, there's no partial fill risk)
3. The strategy tracks the spread state internally (via `OpenSpread` dataclass from the spec) rather than relying on the portfolio's position tracking
4. Log a single `LoggedTrade` per spread round-trip with leg details in the indicators bag

This sidesteps the multi-leg order complexity entirely for V1. The engine's portfolio can optionally track the two positions for equity curve purposes, or the strategy can self-report PnL.

**Effort:** Small. The strategy manages its own spread lifecycle.

---

## Challenge 5: The Fill Model Only Knows Equity Prices

**What exists:** `FillModel.fill_market_order()` fills at signal bar close or next bar open — equity prices only. QuantLib's `price_option()` can compute a theoretical option price at any moment given spot, strike, IV, DTE, and rate.

**What V1 needs:** Option fills at option prices, not the underlying's price.

**Recommended approach with QuantLib:** At entry signal time, the strategy:
1. Selects strikes via delta targeting (QuantLib `price_option()` returns delta alongside price)
2. Gets the theoretical price for each leg from the same QuantLib call
3. Fills the long leg at `theoretical_price × (1 + half_spread)` (simulated ask)
4. Fills the short leg at `theoretical_price × (1 - half_spread)` (simulated bid)
5. The `half_spread` is configurable (e.g., 1-2% of theoretical, or derived from `max_bid_ask_spread_pct`)

At exit, the same QuantLib call with updated spot price (from the exit bar) and reduced DTE gives the exit prices.

This is clean because one QuantLib `price_option()` call gives you **both** the fill price and the Greeks snapshot for logging — no separate lookups.

**Effort:** Small. QuantLib already returns everything needed in a single call.

---

## Challenge 6: Portfolio/Equity Curve Mark-to-Market for Spreads

**What exists:** The engine's portfolio tracks `holdings_value = position_quantity × current_price` from equity bars. The backend's `PortfolioValuationService` already supports multiplier-aware option positions.

**What V1 needs:** Mark-to-market for open spreads requires option prices at every bar, not just at entry/exit. This circles back to Challenge #1 — if historical option price data isn't available at bar-level resolution, the equity curve can't show intra-trade value changes.

**V1 pragmatic approach:**

- Track cash impact at entry (debit reduces cash) and exit (credit increases cash)
- During the 75-minute hold, show equity as `cash + entry_cost` (flat line) — no mark-to-market
- Accept that the equity curve is "step function" style for options trades rather than continuous
- This is a common approach in options backtesting and is defensible for V1

**Effort:** Small for the step-function approach. Large if you want true bar-by-bar mark-to-market (requires solving #1 fully).

---

## Challenge 7: The Frontend Trade Table Needs Options Columns

**What exists:** `engine-results` component renders dynamic indicator columns from the `indicators` bag. The component already iterates over strategy-specific indicator keys and renders them as columns.

**What V1 can leverage:** If leg details are packed into the `indicators` bag (per Challenge #2), the frontend will automatically render them as additional columns. However, the raw keys (`long_strike`, `short_strike`, `long_entry_price`, etc.) may produce a very wide table.

**Recommended UI approach:**

- Keep the summary columns (trade #, entry/exit time, net debit/credit, PnL, result)
- Pack option-specific data into the indicators bag but group them with a naming convention (e.g., `opt_` prefix)
- Add an expandable row or tooltip that shows full leg details
- The Options Strategy Lab component already has patterns for displaying multi-leg data that can be reused

**Effort:** Small-Medium. The dynamic indicator rendering does most of the work; the refinement is UX polish.

---

## Challenge 8: Chart Entry/Exit Markers Don't Represent Fill Prices

**What exists:** Trade markers on the SPY price chart show entry/exit at the equity price.

**What V1 needs:** The signal fires on the SPY chart, but the trade is in options at a different price scale.

**Recommended approach:** Keep markers on the SPY chart as **signal markers** (vertical lines or arrows with a different style from equity trade markers). Add a tooltip showing spread details (strikes, net debit, deltas). Don't try to overlay option prices on the equity chart — they're incomparable scales.

**Effort:** Small. Mostly CSS/tooltip changes in the chart component.

---

## Challenge 9: Strategy Config Form Needs Grouped Parameters

**What exists:** Dynamic form generation from Pydantic `params_schema` with JSON Schema support.

**What V1 needs:** ~20 new parameters in logical groups, enum dropdowns, conditional visibility. Notably this includes `pricing_mode` (MARKET_PREFERRED / QUANTLIB_ONLY / MARKET_REQUIRED) and `pricing_engine` (analytic_bs / binomial_crr / etc.) as top-level strategy settings.

**What already works:** Pydantic v2 JSON Schema supports `enum` types (renders as dropdown), `title` and `description` (renders as labels/tooltips). The frontend form generator already handles numbers, booleans, and strings. The QuantLib pricer already defines a `PricingEngine` enum with 6 engines.

**Gaps:**
- Conditional visibility (show bull call delta targets only when `spread_type == BULL_CALL`) — not supported by the current form generator
- Logical grouping with section headers — would need JSON Schema `allOf` or a custom `x-group` extension

**V1 pragmatic approach:** Show all parameters flat (both bull call and bull put delta targets visible regardless of spread_type). The `pricing_mode` and `pricing_engine` dropdowns work out of the box as Pydantic enums. Add grouping in a follow-up.

**Effort:** Small for the flat approach. Medium if you want proper grouping/conditional fields.

---

## Challenge 10: No LEAN Reference for Options Validation

**What exists:** Bit-exact parity with LEAN for the equity strategy (63 trades). The signal engine is trusted.

**What V1 needs:** Validation that the options overlay produces correct results.

**Validation strategy:**

1. **Signal parity** — verify that entry/exit bars match the equity strategy exactly (same 63 signal events). This is testable today.
2. **Component unit tests** — delta selection, liquidity filter, DTE filter, spread construction, PnL math. The contract finder in `research/options/` already has some of this logic; write tests against it.
3. **Hand-verified trades** — pick 3-5 historical dates, manually look up the option chain, verify the engine's strike selection and fill prices match what you'd expect.
4. **Strategy analysis cross-check** — use the existing `POST /api/strategy/analyze` endpoint to independently verify PnL and Greeks for trades the engine produces.

**Effort:** Medium. Ongoing as you build, not a separate phase.

---

## Challenge 11: Backend BacktestTrade Schema for Persistence

**What exists:** `BacktestTrade.cs` with scalar entry/exit price fields. The backend already has `OptionContract` and `OptionLeg` entities for the portfolio module.

**What V1 needs:** If persisting engine option trades to the study history, the schema needs leg detail.

**Recommended approach:** Add a nullable `OptionsMetadataJson` column (JSONB in Postgres) to `BacktestTrade` that stores the full leg detail as a JSON blob. The scalar `EntryPrice`/`ExitPrice` fields store the net debit/credit for quick sorting and display. This is additive — no breaking changes to existing equity backtest trades.

**Effort:** Small. One migration, one new column, serialize the indicators bag.

---

## Challenge 12: Skipped Signals Need Logging and Display

**What exists:** The engine logs strategy messages via `log_lines`. The frontend shows these in a log panel.

**What V1 needs:** When option chain filtering fails (no liquidity, no valid expiration, no delta match), the signal fires but no trade opens. This needs to be:

- Logged with a specific reason (`"NO TRADE: no contracts with OI >= 100"`)
- Counted separately from trades (e.g., "63 signals, 58 trades, 5 skipped")
- Optionally shown in the UI as a separate table or as dimmed rows in the trade table

**Effort:** Small. Use existing `log_lines` for V1; add structured skip tracking later.

---

## Challenge 13: Time Decay and Intra-Trade Dynamics

This is not a code challenge — it's an **interpretation challenge**. A 75-minute spread trade will show different PnL characteristics than the same-direction equity trade due to theta decay, gamma exposure, and vega sensitivity. The V1 spec deliberately uses timed exits (no dynamic adjustments), so the code is simple, but backtest results need to be interpreted with this in mind.

**Action:** Add a note to the strategy's description/documentation that PnL behavior differs from equity due to options Greeks, and that V1 does not attempt to optimize around these dynamics.

**Effort:** None (documentation only).

---

## Architectural Decision: Configurable Pricing Mode — Market Data, QuantLib, or Hybrid

**Core principle: The pricing source should be a strategy-level configuration, not a hardcoded hierarchy.**

There are legitimate reasons to prefer each approach:

- **Real market data** gives you actual supply/demand dynamics, real skew, real bid/ask — the most realistic backtest. But it's only available for recent dates (live snapshot) or requires significant data caching (historical aggs), and market Greeks from Polygon can occasionally be stale or inconsistent.
- **QuantLib-only** gives you deterministic, reproducible results with a consistent model. Every run produces identical Greeks and prices. It supports dividend yield, multiple pricing engines (analytic BS, binomial, finite difference, Monte Carlo), and is fully self-contained — no API calls per bar. This is ideal for validating mechanics, debugging, and comparing engine behaviors across runs.
- **Hybrid** (prefer market, fall back to QuantLib) gives you the best available data at each point in time, but results may not be perfectly reproducible if market data availability changes between runs.

**The pricing mode should be a configurable enum on the strategy:**

```
pricing_mode:
  MARKET_PREFERRED  — Use real market data when available, QuantLib when not
  QUANTLIB_ONLY     — Use QuantLib for all pricing and Greeks (deterministic, no API dependency)
  MARKET_REQUIRED   — Use real market data only; skip trade if unavailable (strictest)
```

**The data sources available to the engine:**

| Source | When Available | What It Provides |
|--------|---------------|-----------------|
| Polygon live snapshot | Current/recent dates | Real Greeks, IV, bid/ask, OI, volume — full liquidity picture |
| Polygon historical option aggs | Any past date, per-contract | Real OHLCV prices, but no Greeks/IV/OI |
| Polygon contract listing + QuantLib | Any past date (contract metadata only) | Theoretical prices and Greeks from QuantLib given spot + IV estimate |
| QuantLib standalone | Always (no external data needed) | Full theoretical pricing and Greeks from inputs alone |

**How each mode resolves data:**

**`MARKET_PREFERRED` (recommended default for production backtests):**
1. Try Polygon live snapshot → real Greeks, bid/ask, OI, volume. Delta targeting uses market delta. Fills use market bid/ask.
2. Fall back to Polygon historical option aggs → real prices, QuantLib Greeks (from solved IV).
3. Fall back to contract listing + QuantLib synthetic → theoretical everything.
4. Log the source tier used per trade for transparency.

**`QUANTLIB_ONLY` (recommended default for V1 development and validation):**
1. Use `list_options_contracts(as_of_date=...)` to know which contracts existed at the signal date.
2. Price every contract with QuantLib's `price_option()` using spot from the equity bar + IV estimate from `OptionsIvSnapshot` or a flat assumption.
3. All Greeks come from QuantLib. Fill prices are QuantLib NPV ± configurable half-spread.
4. Fully deterministic. No per-bar API calls. Same result every run.

**`MARKET_REQUIRED` (for high-fidelity backtests):**
1. Only use real market data. If the snapshot or historical aggs aren't available for this date, skip the trade.
2. Logs "NO TRADE: market data unavailable" — counts toward skipped signals.
3. Most conservative, but trade count may be lower.

**How QuantLib is used in each mode:**

| Mode | Pricing | Greeks | Fill Prices | IV |
|------|---------|--------|-------------|-----|
| `MARKET_PREFERRED` | Market when available, QuantLib fallback | Market when available, QuantLib fallback | Market bid/ask when available, QuantLib NPV ± spread fallback | Market IV when available, solved or estimated fallback |
| `QUANTLIB_ONLY` | QuantLib always | QuantLib always | QuantLib NPV ± configurable spread | Estimated from `OptionsIvSnapshot` or flat assumption |
| `MARKET_REQUIRED` | Market only | Market only | Market bid/ask only | Market IV only |

The strategy doesn't need to know which source was used internally. The `ChainResolver` returns the same `OptionChainSnapshot` dataclass regardless of mode, with a `source` field indicating provenance ("live", "historical_aggs", "quantlib_synthetic") for logging and trust assessment.

**Current state of the codebase — four pricing sources:**

1. **Polygon market data** — real-market Greeks, IV, bid/ask, OI, volume from live snapshots. The ground truth when available.
2. **QuantLib pricer** (`quantlib_pricer.py`) — full BSM process, 6 engines (Analytic BS, Binomial CRR/JR/LR, Finite Diff, Monte Carlo), all Greeks + rho, dividend yield, d1/d2 diagnostics. Used by `/api/quantlib/*` endpoints.
3. **Massive MCP BS functions** — `bs_price`, `bs_delta`, `bs_gamma`, `bs_vega`, `bs_theta`, `bs_rho`. Column-level analytical BS functions that run as post-processing on Massive data queries. Same closed-form math as QuantLib's `AnalyticEuropeanEngine`. No options chain endpoints or IV data — you supply `sigma`.
4. **Legacy hand-rolled BS** — `bs_solver.py` (pricing + IV solver), `strategy_engine.py` (Greeks, POP, EV, payoff curves), `contract_finder.py`'s `_bs_delta()`.

**Recommended roles for each source:**

| Source | Role | When to Use |
|--------|------|-------------|
| **Polygon market data** | Observed pricing authority | Live/recent backtests, paper trading, production. Real Greeks, IV, bid/ask. |
| **QuantLib** | Model pricing authority (engine) | Backtest engine pricing and Greeks. Supports exotic engines (binomial, FD, MC), dividend yield, full BSM process. The engine's `QUANTLIB_ONLY` mode uses this exclusively. Also used for IV solving (new `implied_volatility()` function). |
| **Massive BS functions** | Scenario analysis & data pipeline enrichment | What-if analysis on Massive query results (e.g., "what's the delta of every SPY call at these strikes given 20% vol?"). Ad-hoc research queries where you want Greeks computed inline on tabular data without calling the Python service. Not for the engine's hot path. |
| **Legacy BS** | Existing consumers only (deprecation path) | `bs_solver.py` stays for IV builder until QuantLib IV solver is ready. `strategy_engine.py` stays for POP/EV/payoff curves. `_bs_delta()` in contract_finder replaced by QuantLib in engine context. |

**Why QuantLib for the engine, not Massive BS functions:**

Massive's `bs_*` functions and QuantLib's `AnalyticEuropeanEngine` produce identical results — they're the same closed-form Black-Scholes math. The difference is operational:

- **QuantLib** runs locally in the Python process, supports 6 pricing engines (not just analytical), handles dividend yield, and will gain IV solving. It's the right tool for the engine's inner loop where you need sub-millisecond pricing, engine switching, and full control.
- **Massive BS functions** run as column transforms on Massive data queries. They're ideal for bulk enrichment of tabular data (e.g., computing Greeks across an entire option chain in a single query). They're the right tool for research/analysis workflows where you're already in the Massive data pipeline.

Both should coexist. They serve different use cases and neither replaces the other.

**Consolidation plan:**

- QuantLib becomes the engine's model pricing authority — all theoretical pricing and Greeks in the backtest engine go through QuantLib
- Massive BS functions become the research/analysis pricing tool — use them for ad-hoc queries, scenario analysis, and data pipeline enrichment
- Polygon market data remains the observed pricing authority — real market data takes precedence when available and the pricing mode allows it
- Legacy BS implementations are on a deprecation path: `bs_solver.py` stays until QuantLib IV solver ships; `strategy_engine.py` stays for POP/EV/payoff curves; `_bs_delta()` replaced by QuantLib in engine context

**Consolidation tasks for V1:**
1. Add `implied_volatility()` to `quantlib_pricer.py` using QuantLib's root-finding (enables solving IV from real option prices — needed for `MARKET_PREFERRED` tier 2)
2. Create `engine/options/chain_resolver.py` — the mode-aware data resolver. In `QUANTLIB_ONLY` mode it skips all market data calls. In `MARKET_PREFERRED` it tries live → historical → synthetic. In `MARKET_REQUIRED` it only uses market data or skips.
3. Create `engine/options/pricer.py` — thin wrapper that accepts a contract + market state, returns a unified dataclass with price + all Greeks. In `QUANTLIB_ONLY` mode, Greeks always come from QuantLib. In `MARKET_PREFERRED`, market Greeks are used when present, QuantLib fills gaps.
4. Add `pricing_mode` (enum) and `pricing_engine` (enum, defaults to `analytic_bs`) to the strategy's Pydantic params schema
5. Log the pricing mode, engine, and per-trade data source in every trade's indicators bag

---

## Revised Summary: Prioritized Work Streams

| Priority | Challenge | Effort | What Already Exists |
|----------|-----------|--------|---------------------|
| **P0 — Data Resolution + Pricing** | | | |
| | ChainResolver (mode-aware) | **Medium** | Live snapshot, `list_options_contracts(as_of_date)`, QuantLib pricer all exist. Need resolver that respects `pricing_mode` config: `QUANTLIB_ONLY`, `MARKET_PREFERRED`, or `MARKET_REQUIRED`. |
| | QuantLib IV solver | **Small** | `quantlib_pricer.py` exists. Add `implied_volatility()` — needed for `MARKET_PREFERRED` mode (solve IV from real prices → QuantLib Greeks). |
| | #1 Option chain data for engine | **Medium** | Three pricing modes available. V1 defaults to `QUANTLIB_ONLY` for deterministic development; switch to `MARKET_PREFERRED` for production backtests. |
| | #5 Option fill prices | **Small** | `QUANTLIB_ONLY`: NPV ± configurable spread. `MARKET_PREFERRED`/`MARKET_REQUIRED`: real bid/ask when available. |
| **P1 — Engine Core** | | | |
| | #2 Multi-leg trade model | **Small** | `indicators` bag already supports dynamic fields. Convention decision. |
| | #3 Spread PnL calculation | **Small** | Strategy subclass override. Stats engine works unchanged. |
| | #4 Multi-leg orders | **Small** | V1 can use strategy-internal spread tracking, bypassing the order system. |
| | #6 Equity curve for spreads | **Small** | Step-function approach (no intra-trade MTM). Acceptable for V1. |
| **P2 — Frontend & Persistence** | | | |
| | #7 Trade table with option columns | **Small-Medium** | Dynamic indicator rendering exists. Needs UX polish. |
| | #8 Chart signal markers | **Small** | Tooltip/style change only. |
| | #9 Strategy config form | **Small** | Flat params work today. Grouping is a refinement. |
| | #11 Backend persistence schema | **Small** | One JSONB column addition. |
| **P3 — Quality & Documentation** | | | |
| | #10 Validation strategy | **Medium** | QuantLib + `/api/quantlib/strategy` can cross-check. Strategy analyze endpoint for POP/EV. |
| | #12 Skipped signal logging | **Small** | `log_lines` exists. |
| | #13 Time decay interpretation | **None** | Documentation only. |

---

## Revised Recommended Approach

The architecture now follows a clear principle: **real market data when available, QuantLib when not.** The `ChainResolver` abstraction means the strategy doesn't care where the data comes from — it gets the same dataclass either way, and the source is logged for transparency.

**Phase 1 — ChainResolver + Pricer (the critical path):**

1. Create `engine/options/chain_resolver.py` — the mode-aware resolver:
   - Accepts `pricing_mode` (QUANTLIB_ONLY / MARKET_PREFERRED / MARKET_REQUIRED) and `pricing_engine` (analytic_bs, binomial_crr, etc.)
   - `QUANTLIB_ONLY`: Fetches contract metadata from Polygon (`list_options_contracts`), prices everything with QuantLib. No market Greeks used even if available. Deterministic, fast, zero per-bar API calls.
   - `MARKET_PREFERRED`: Tries live snapshot → historical aggs + QuantLib IV solver → QuantLib synthetic. Uses real Greeks/prices when available, QuantLib fills gaps.
   - `MARKET_REQUIRED`: Only uses real market data. Skips trade if unavailable.
   - All modes return the same `OptionChainSnapshot` dataclass with a `source` field per contract.
2. Create `engine/options/pricer.py` — thin wrapper over QuantLib and/or market data. In `QUANTLIB_ONLY` mode, always calls `quantlib_pricer.price_option()`. In `MARKET_PREFERRED`, passes through market Greeks when present, calls QuantLib otherwise.
3. Add `implied_volatility()` to `quantlib_pricer.py` using QuantLib's root-finding — needed for `MARKET_PREFERRED` tier 2 (solve IV from historical option prices, then compute Greeks via QuantLib).
4. Port `contract_finder.py`'s filtering logic (liquidity, DTE, delta targeting) into the engine namespace, using the pricer for delta rather than the hand-rolled `_bs_delta()`.

**Phase 2 — Strategy class (can start in parallel with Phase 1):**

Build `SpyEmaCrossoverOptionsStrategy` inheriting from the base. It reuses the same signal engine (EMA crossover + RSI) and adds:
- `OpenSpread` state model (from the V1 spec)
- On entry signal: call `ChainResolver` → filter by DTE/liquidity → select strikes by delta (real or QuantLib) → fill at bid/ask (real) or theoretical ± spread (synthetic) → log trade with full leg detail + data source in indicators bag
- On exit: resolve prices again with updated spot and DTE → compute exit credit/debit → log PnL
- Timed exit countdown (bars_remaining)

**Phase 3 — Frontend polish:**
Dynamic indicator rendering handles most of it. Add column formatting for option-specific fields (including a "data source" indicator so you can see which trades used real vs. synthetic data), signal markers on charts, flat config form. The existing Options Strategy Lab patterns can inform the UI.

**Phase 4 — Validation:**
- Cross-check engine trades against `/api/quantlib/strategy` endpoint (independent QuantLib call path)
- Run the same backtest in `QUANTLIB_ONLY` and `MARKET_PREFERRED` modes — compare trade-by-trade Greeks and PnL to quantify how much the model diverges from market reality
- For trades with real market data: compare QuantLib theoretical Greeks against market Greeks to gauge model accuracy per strike/DTE bucket
- Verify signal parity with equity strategy (same 63 entry/exit bars)
- Unit test each component: delta selection, liquidity filter, spread construction, PnL math

**What this architecture unlocks for V2+:**
- **`MARKET_PREFERRED` becomes the default** as you build up cached option data — progressively more trades use real data
- `QUANTLIB_ONLY` mode stays valuable for reproducible research, model comparison, and development
- The `pricing_engine` parameter lets you compare analytic BS vs. binomial vs. finite-diff results on the same backtest — the QuantLib engine enum is already defined with 6 options
- QuantLib upgrades to volatility surfaces (`BlackVarianceSurface`) improve `QUANTLIB_ONLY` accuracy without changing the strategy
- Binomial/finite-diff engines for American-style options
- The `source` field in trade logs becomes a data quality metric — you can measure how much of your backtest relied on real vs. modeled data
- Paper trading / live trading uses `MARKET_REQUIRED`, and the same strategy class works without changes
- A/B testing model accuracy: run `QUANTLIB_ONLY` vs. `MARKET_PREFERRED` on the same date range, diff per-trade Greeks and PnL
