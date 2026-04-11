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

**What V1 needs:** ~20 new parameters in logical groups, enum dropdowns, conditional visibility.

**What already works:** Pydantic v2 JSON Schema supports `enum` types (renders as dropdown), `title` and `description` (renders as labels/tooltips). The frontend form generator already handles numbers, booleans, and strings.

**Gaps:**
- Conditional visibility (show bull call delta targets only when `spread_type == BULL_CALL`) — not supported by the current form generator
- Logical grouping with section headers — would need JSON Schema `allOf` or a custom `x-group` extension

**V1 pragmatic approach:** Show all parameters flat (both bull call and bull put delta targets visible regardless of spread_type). It's not pretty but it works. Add grouping in a follow-up.

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

## Architectural Decision: Consolidating on QuantLib

**Current state:** The codebase has three parallel pricing implementations:

1. **QuantLib pricer** (`quantlib_pricer.py`) — full BSM process, 6 engines, all Greeks + rho, dividend yield, d1/d2 diagnostics. Used by `/api/quantlib/*` endpoints.
2. **Legacy BS solver** (`bs_solver.py`) — hand-rolled BS formula, IV solver. Used by the IV builder pipeline.
3. **Legacy strategy engine** (`strategy_engine.py`) — hand-rolled BS Greeks, POP, EV, payoff curves. Used by `/api/strategy/analyze`.

Additionally, `contract_finder.py` has its own `_bs_delta()` function (5 lines of manual BS delta).

**Recommendation: Lean into QuantLib as the single pricing authority for the engine.**

The backtest engine should use QuantLib's `price_option()` for all option pricing and Greeks. This means:

- **Strike selection** in the engine uses QuantLib delta, not the hand-rolled `_bs_delta()` from contract_finder
- **Fill prices** come from QuantLib's NPV, not from a separate BS calculation
- **Entry/exit Greeks** (for logging) come from the same QuantLib call that produces the price — no extra computation
- **Strategy-level Greeks** use QuantLib's `price_strategy()` for aggregate net delta/gamma/theta/vega

**What this doesn't replace (yet):**
- The IV builder pipeline still needs `bs_solver.py` for *implied volatility solving* (QuantLib has `ql.ImpliedVolatility` but it's not exposed through `quantlib_pricer.py` yet — worth adding)
- The strategy analyze endpoint (`/api/strategy/analyze`) uses `strategy_engine.py` for POP, EV, payoff curves — these are distribution-level computations that QuantLib doesn't directly provide, so the legacy engine stays for now
- Live option chain snapshots from Polygon still provide the real-market Greeks for the chain viewer UI

**Consolidation tasks for V1:**
1. Add an `implied_volatility()` function to `quantlib_pricer.py` using QuantLib's root-finding (replaces `bs_solver.py` over time)
2. Create an engine-facing `OptionPricer` wrapper that calls `quantlib_pricer.price_option()` and returns a clean dataclass with price + all Greeks
3. Use this wrapper in the new strategy for both strike selection (delta targeting) and fill pricing
4. Log QuantLib-computed Greeks in the trade's indicators bag for every entry

---

## Revised Summary: Prioritized Work Streams

| Priority | Challenge | Effort | What Already Exists |
|----------|-----------|--------|---------------------|
| **P0 — QuantLib Integration + Data Bridge** | | | |
| | QuantLib consolidation (new) | **Small** | `quantlib_pricer.py` exists. Need thin engine-facing wrapper + IV solver. |
| | #1 Option chain data for engine | **Medium** | `list_options_contracts(as_of_date)` + QuantLib synthetic pricing = no massive data download needed for V1. |
| | #5 Option fill prices | **Small** | QuantLib `price_option()` gives price + Greeks in one call. Add spread simulation. |
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

With QuantLib, the architecture simplifies significantly. The original "massive data problem" (Challenge #1) shrinks because QuantLib can synthesize option prices and Greeks from just the underlying's price, a volatility estimate, and the contract's strike/expiration. You don't need historical option bar data for V1.

**Phase 1 — QuantLib engine wrapper (the new critical path):**

1. Create `engine/options/pricer.py` — thin wrapper around `quantlib_pricer.price_option()` that accepts a contract (strike, expiration, type) + market state (spot, IV, rate, eval_date) and returns a dataclass with price, delta, gamma, theta, vega. This is the single pricing authority for the engine.
2. Add `implied_volatility()` to `quantlib_pricer.py` using QuantLib's root-finding, so the IV builder can migrate off `bs_solver.py` over time.
3. Create `engine/options/chain_builder.py` — at entry signal time, calls `PolygonClientService.list_options_contracts(as_of_date=signal_date)` to get available strikes/expirations, then uses the QuantLib pricer to compute theoretical prices and Greeks for each. Returns a filtered chain ready for strike selection.

**Phase 2 — Strategy class (can start in parallel):**

Build `SpyEmaCrossoverOptionsStrategy` inheriting from the base. It reuses the same signal engine (EMA crossover + RSI) and adds:
- `OpenSpread` state model (from the V1 spec)
- On entry signal: call chain_builder → filter by DTE/liquidity → select strikes by QuantLib delta → compute net debit/credit → log trade with full leg detail in indicators bag
- On exit: reprice legs via QuantLib with updated spot and DTE → compute exit credit/debit → log PnL
- Timed exit countdown (bars_remaining)

**Phase 3 — Frontend polish:**
Dynamic indicator rendering handles most of it. Add column formatting for option-specific fields, signal markers on charts, flat config form. The existing Options Strategy Lab patterns can inform the UI.

**Phase 4 — Validation:**
- Cross-check engine trades against `/api/quantlib/strategy` endpoint (independent QuantLib call path)
- Verify signal parity with equity strategy (same 63 entry/exit bars)
- Unit test each component: delta selection, liquidity filter, spread construction, PnL math
- Compare QuantLib Greeks against legacy `strategy_engine.py` Greeks for sanity

**What QuantLib unlocks for V2+:**
- Switch from flat IV to a proper volatility surface (QuantLib supports `BlackVarianceSurface`)
- Use binomial or finite-diff engines for American-style options
- Add dividend yield modeling for more accurate SPY pricing
- Monte Carlo simulations for exotic strategies
