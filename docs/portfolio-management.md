# Portfolio Management System — Comprehensive Reference

## 1. Overview

The Portfolio Management System is a full-stack, event-sourced portfolio tracker integrated into MarketScope. It supports paper and backtest trading accounts with FIFO lot-based position tracking, real-time valuation, risk analytics, equity curve monitoring, and strategy attribution — all exposed via GraphQL and rendered in an Angular tabbed dashboard.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Angular 21 Frontend                             │
│  ┌───────────┬────────────┬──────────┬───────────┬──────────────────┐  │
│  │ Dashboard │ Positions  │ Equity   │ Risk      │ Strategy         │  │
│  │  - State  │  - FIFO    │ Chart    │ Panel     │ Attribution      │  │
│  │  - Trade  │    lots    │  - Area  │  - Rules  │  - Alpha bars    │  │
│  │    form   │  - Rebuild │  - DD    │  - Delta  │  - Import        │  │
│  │  - Snap   │            │  - KPIs  │ Scenario  │  - PnL split     │  │
│  │           │            │          │ Explorer  │                  │  │
│  │           │            │          │ Reconcil. │                  │  │
│  └─────┬─────┴──────┬─────┴────┬─────┴─────┬─────┴────────┬─────────┘ │
│        └─────────────┴──────────┴───────────┴──────────────┘           │
│                        PortfolioService (GraphQL client)               │
└──────────────────────────────┬─────────────────────────────────────────┘
                               │ GraphQL over HTTP
┌──────────────────────────────┴─────────────────────────────────────────┐
│                   .NET 10 Backend (Hot Chocolate v15)                   │
│  ┌──────────────────┐   ┌──────────────────────────────────────────┐  │
│  │ PortfolioQuery    │   │ PortfolioMutation                        │  │
│  │  (18 resolvers)   │   │  (12 mutations)                          │  │
│  └────────┬──────────┘   └────────┬────────────────────────────────┘  │
│           │                        │                                   │
│  ┌────────┴────────────────────────┴───────────────────────────────┐  │
│  │                        Service Layer                             │  │
│  │                                                                  │  │
│  │  IPortfolioService ────────► Account/Order/Trade CRUD            │  │
│  │  IPositionEngine ─────────► FIFO lot allocation + rebuild        │  │
│  │  IPortfolioValuationService ► live price lookup + MTM            │  │
│  │  ISnapshotService ────────► equity curve + performance metrics   │  │
│  │  IPortfolioRiskService ───► dollar delta, vega, scenarios        │  │
│  │  IPortfolioReconciliationService ► drift detection + auto-fix    │  │
│  │  IStrategyAttributionService ──► backtest import + PnL split     │  │
│  └──────────────────────────┬──────────────────────────────────────┘  │
│                              │ EF Core 10                              │
│  ┌───────────────────────────┴─────────────────────────────────────┐  │
│  │  PostgreSQL 16  (11 portfolio tables + indexes)                  │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **FIFO Lot Tracking** | Every buy creates a `PositionLot`. Sells close the oldest open lots first. Partial closes reduce `RemainingQuantity` without splitting rows. Realized PnL is deterministic and auditable. |
| 2 | **Trade as Source of Truth** | `PortfolioTrade` records are immutable facts. Positions and lots are derived state, fully rebuildable from the trade log via `RebuildPositionsAsync`. This is the event-sourcing guarantee. |
| 3 | **Multiplier-Aware** | All PnL, market value, and dollar delta calculations use the contract multiplier (1 for stocks, 100 for standard equity options). Stored on both `PortfolioTrade.Multiplier` and `OptionContract.Multiplier`. |
| 4 | **Cash Tracking** | `Account.Cash` updates atomically on every fill. Buys deduct `price * qty * mult + fees`, sells add `price * qty * mult - fees`. |
| 5 | **Options First-Class** | Option trades carry an `OptionLeg` with entry Greeks (IV, delta, gamma, theta, vega), powering risk calculations without a real-time pricing model. |
| 6 | **Symbol-Based Trade Entry** | `recordTrade` accepts a ticker symbol string. The service auto-resolves or creates a minimal `Ticker` row, removing the need to pre-fetch market data. |

---

## 2. Data Model

### 2.1 Entity Relationship Diagram

```
Account (1) ──────< (N) Order
   │                      │
   │ (1)                  │ (1)
   │                      │
   ├──< (N) PortfolioTrade ┘
   │            │
   │            ├──── (0..1) OptionLeg ──► OptionContract
   │            │
   │            └──< (N) PositionLot
   │                      │
   ├──< (N) Position ─────┘
   │            │
   │            └──── (0..1) OptionContract ──► Ticker
   │
   ├──< (N) PortfolioSnapshot
   │
   ├──< (N) RiskRule
   │
   ├──< (N) StrategyAllocation ──► StrategyExecution
   │
   └──< (N) StrategyTradeLink ──► StrategyExecution
```

### 2.2 Enums

```csharp
AccountType     : Paper | Backtest
OrderSide       : Buy | Sell
OrderType       : Market | Limit | Stop
OrderStatus     : Pending | Filled | PartiallyFilled | Cancelled
AssetType       : Stock | Option
PositionStatus  : Open | Closed
OptionType      : Call | Put
RiskRuleType    : MaxDrawdown | MaxPositionSize | MaxVegaExposure | MaxDelta
RiskAction      : Warn | Block
RiskSeverity    : Low | Medium | High | Critical
```

### 2.3 Entity Schemas

#### Account

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| Name | `string` | Max 200, required, indexed |
| Type | `AccountType` | Paper or Backtest |
| BaseCurrency | `string` | Max 10, default `"USD"` |
| InitialCash | `decimal` | Starting capital |
| Cash | `decimal` | Current available cash (mutated on fills) |
| CreatedAt | `DateTime` | UTC |

#### Order

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| AccountId | `Guid` | FK -> Account (cascade) |
| TickerId | `int` | FK -> Ticker |
| Side | `OrderSide` | Buy or Sell |
| OrderType | `OrderType` | Market, Limit, or Stop |
| Quantity | `decimal` | |
| LimitPrice | `decimal?` | Null for Market orders |
| Status | `OrderStatus` | Default: Pending |
| AssetType | `AssetType` | Default: Stock |
| OptionContractId | `Guid?` | FK -> OptionContract (set null on delete) |
| SubmittedAt | `DateTime` | Default: UTC now |
| FilledAt | `DateTime?` | Set on fill |

Index: `(AccountId, Status)`

#### PortfolioTrade

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| AccountId | `Guid` | FK -> Account (cascade) |
| OrderId | `Guid` | FK -> Order |
| TickerId | `int` | FK -> Ticker |
| Side | `OrderSide` | |
| Quantity | `decimal` | |
| Price | `decimal` | Execution price |
| Fees | `decimal` | |
| AssetType | `AssetType` | Default: Stock |
| OptionContractId | `Guid?` | FK -> OptionContract (set null on delete) |
| Multiplier | `int` | Default: 1 (stocks), 100 (options) |
| ExecutionTimestamp | `DateTime` | |

Index: `(AccountId, ExecutionTimestamp)`

Navigation: `Lots` (PositionLot[]), `OptionLeg` (0..1)

#### Position

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| AccountId | `Guid` | FK -> Account |
| TickerId | `int` | FK -> Ticker |
| AssetType | `AssetType` | Default: Stock |
| OptionContractId | `Guid?` | FK -> OptionContract |
| NetQuantity | `decimal` | Sum of open lot quantities |
| AvgCostBasis | `decimal` | Weighted average entry price |
| RealizedPnL | `decimal` | Accumulated from closed lots |
| Status | `PositionStatus` | Open or Closed |
| OpenedAt | `DateTime` | |
| ClosedAt | `DateTime?` | Set when fully closed |
| LastUpdated | `DateTime` | |

Index: `(AccountId, TickerId, Status)`

#### PositionLot

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| PositionId | `Guid` | FK -> Position |
| TradeId | `Guid` | FK -> PortfolioTrade (the entry trade) |
| Quantity | `decimal` | Original lot size |
| EntryPrice | `decimal` | |
| RemainingQuantity | `decimal` | Decremented on sells |
| RealizedPnL | `decimal` | Accumulated from partial/full closes |
| OpenedAt | `DateTime` | |
| ClosedAt | `DateTime?` | Set when RemainingQuantity = 0 |

Index: `(PositionId, OpenedAt)`

#### OptionContract

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| UnderlyingTickerId | `int` | FK -> Ticker |
| Symbol | `string` | Max 100, unique. E.g. `"O:AAPL250620C00150000"` |
| Strike | `decimal` | |
| Expiration | `DateOnly` | |
| OptionType | `OptionType` | Call or Put |
| Multiplier | `int` | Default: 100 |

Unique index: `(Symbol)`, Composite index: `(UnderlyingTickerId, Strike, Expiration, OptionType)`

#### OptionLeg

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| TradeId | `Guid` | FK -> PortfolioTrade (one-to-one, cascade) |
| OptionContractId | `Guid` | FK -> OptionContract (cascade) |
| Quantity | `decimal` | |
| EntryIV | `decimal?` | Implied volatility at trade time |
| EntryDelta | `decimal?` | |
| EntryGamma | `decimal?` | |
| EntryTheta | `decimal?` | |
| EntryVega | `decimal?` | |

#### PortfolioSnapshot

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| AccountId | `Guid` | FK -> Account |
| Timestamp | `DateTime` | |
| Equity | `decimal` | Cash + MarketValue |
| Cash | `decimal` | |
| MarketValue | `decimal` | |
| MarginUsed | `decimal` | |
| UnrealizedPnL | `decimal` | |
| RealizedPnL | `decimal` | |
| NetDelta | `decimal?` | |
| NetGamma | `decimal?` | |
| NetTheta | `decimal?` | |
| NetVega | `decimal?` | |

Index: `(AccountId, Timestamp)`

#### RiskRule

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| AccountId | `Guid` | FK -> Account |
| RuleType | `RiskRuleType` | MaxDrawdown, MaxPositionSize, MaxVegaExposure, MaxDelta |
| Threshold | `decimal` | Rule-specific threshold value |
| Action | `RiskAction` | Warn or Block |
| Severity | `RiskSeverity` | Low, Medium, High, Critical |
| Enabled | `bool` | Default: true |
| LastTriggered | `DateTime?` | |

Index: `(AccountId, Enabled)`

#### StrategyAllocation

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| AccountId | `Guid` | FK -> Account |
| StrategyExecutionId | `int` | FK -> StrategyExecution |
| CapitalAllocated | `decimal` | |
| StartDate | `DateTime` | |
| EndDate | `DateTime?` | |

Composite index: `(AccountId, StrategyExecutionId)`

#### StrategyTradeLink

| Field | Type | Notes |
|-------|------|-------|
| Id | `Guid` | PK |
| TradeId | `Guid` | FK -> PortfolioTrade |
| StrategyExecutionId | `int` | FK -> StrategyExecution |

Indexes on both `TradeId` and `StrategyExecutionId`

---

## 3. Service Layer

### 3.1 IPositionEngine — FIFO Lot Allocation

The position engine is the core accounting subsystem. It manages the mapping from trades to positions and lots.

```
IPositionEngine
├── ApplyTradeAsync(trade)        → applies a single trade to positions
├── RebuildPositionsAsync(acctId) → wipes and replays all trades
└── CalculateRealizedPnL(lots)    → pure function: sum of lot PnL
```

#### FIFO Algorithm

**Buy trade** — creates a new `PositionLot`:

```
lot.Quantity          = trade.Quantity
lot.EntryPrice        = trade.Price
lot.RemainingQuantity = trade.Quantity
lot.RealizedPnL       = 0
```

**Sell trade** — closes the oldest open lots first:

```
remaining = sellQuantity
for each lot in openLots (ordered by OpenedAt ASC):
    fill = min(remaining, lot.RemainingQuantity)
    pnl  = (sellPrice - lot.EntryPrice) * fill * multiplier
    lot.RemainingQuantity -= fill
    lot.RealizedPnL      += pnl
    if lot.RemainingQuantity == 0:
        lot.ClosedAt = now
    remaining -= fill
    if remaining == 0: break
```

**Position recalculation** (after every trade):

```
position.NetQuantity  = sum(lot.RemainingQuantity)  for all open lots
position.AvgCostBasis = sum(lot.EntryPrice * lot.RemainingQuantity) / NetQuantity
position.RealizedPnL  = sum(lot.RealizedPnL)        for all lots
position.Status       = NetQuantity > 0 ? Open : Closed
```

#### Worked Example

```
Trade 1: BUY  100 AAPL @ $150    → Lot A: 100 remaining @ $150
Trade 2: BUY   50 AAPL @ $155    → Lot B:  50 remaining @ $155
Trade 3: SELL 120 AAPL @ $160

FIFO closes Lot A first:
  Lot A: fill 100, PnL = (160 - 150) * 100 * 1 = +$1,000, remaining = 0 → closed
  Lot B: fill  20, PnL = (160 - 155) *  20 * 1 = +$100,   remaining = 30

Position after Trade 3:
  NetQuantity  = 30
  AvgCostBasis = $155 (only Lot B remains)
  RealizedPnL  = $1,100
  Status       = Open
```

#### Rebuild

`RebuildPositionsAsync` provides the event-sourcing guarantee:

1. Delete all `Position` and `PositionLot` rows for the account
2. Query all `PortfolioTrade` records ordered by `ExecutionTimestamp`
3. Replay each trade through the FIFO engine
4. Persist the rebuilt state

This ensures positions always match the trade log, regardless of any bugs or drift in incremental updates.

---

### 3.2 IPortfolioService — Account, Order, and Trade Management

```
IPortfolioService
├── CreateAccountAsync(name, type, initialCash)
├── SubmitOrderAsync(accountId, tickerId, side, orderType, qty, limitPrice?, assetType, optionContractId?)
├── CancelOrderAsync(orderId)
├── FillOrderAsync(orderId, fillPrice, fillQty, fees, multiplier, optionLeg?)
├── RecordTradeAsync(input)          → shortcut: creates order + trade atomically
└── GetPortfolioStateAsync(accountId) → returns account + positions + recent trades
```

#### Order Lifecycle

```
  SubmitOrder          FillOrder                          CancelOrder
      │                   │                                    │
      ▼                   ▼                                    ▼
  ┌────────┐        ┌──────────────┐                    ┌───────────┐
  │Pending │───────►│Filled /      │                    │ Cancelled │
  └────────┘        │PartiallyFilled│                   └───────────┘
                    └──────────────┘
                          │
                          ▼
              PortfolioTrade created
              PositionEngine.ApplyTrade()
              Account.Cash updated
```

#### Symbol Resolution (recordTrade)

When `recordTrade` is called with a symbol string:

1. Look up `Ticker` by `Symbol` (case-insensitive, stored uppercase)
2. If not found, create a minimal `Ticker` row: `{ Symbol, Name=Symbol, Market="stocks", Active=true }`
3. Use the resolved `ticker.Id` for the order and trade

This allows recording trades for any ticker without pre-fetching market data.

#### Cash Update Formula

```
Buy:  account.Cash -= price * quantity * multiplier + fees
Sell: account.Cash += price * quantity * multiplier - fees
```

---

### 3.3 IPortfolioValuationService — Mark-to-Market

```
IPortfolioValuationService
├── ComputeValuationAsync(accountId)                    → fetches live prices from Polygon
└── ComputeValuationWithPricesAsync(accountId, prices)  → uses caller-provided price dict
```

#### Valuation Formulas

For each open position `p`:

```
MarketValue   = currentPrice * p.NetQuantity * p.Multiplier
UnrealizedPnL = (currentPrice - p.AvgCostBasis) * p.NetQuantity * p.Multiplier
```

Portfolio-level aggregation:

```
PortfolioValuation.Cash          = account.Cash
PortfolioValuation.MarketValue   = sum(position.MarketValue)
PortfolioValuation.Equity        = Cash + MarketValue
PortfolioValuation.UnrealizedPnL = sum(position.UnrealizedPnL)
PortfolioValuation.RealizedPnL   = sum(position.RealizedPnL)
```

**Options Greeks** — aggregated from the latest `OptionLeg` for each option position:

```
NetDelta = sum(leg.EntryDelta * position.NetQuantity * multiplier)
NetGamma = sum(leg.EntryGamma * position.NetQuantity * multiplier)
NetTheta = sum(leg.EntryTheta * position.NetQuantity * multiplier)
NetVega  = sum(leg.EntryVega  * position.NetQuantity * multiplier)
```

---

### 3.4 ISnapshotService — Equity Curve and Performance Metrics

```
ISnapshotService
├── TakeSnapshotAsync(accountId)                    → captures current valuation
├── TakeSnapshotWithPricesAsync(accountId, prices)  → captures with provided prices
├── GetEquityCurveAsync(accountId, from?, to?)      → ordered snapshot series
├── GetDrawdownSeriesAsync(accountId)               → peak-relative drawdowns
└── ComputeMetrics(snapshots)                       → performance KPIs
```

#### Snapshot Fields

Each snapshot persists a point-in-time view: `Equity, Cash, MarketValue, MarginUsed, UnrealizedPnL, RealizedPnL, NetDelta, NetGamma, NetTheta, NetVega`.

#### Drawdown Series

```
peak = 0
for each snapshot s (chronological):
    peak           = max(peak, s.Equity)
    drawdown       = peak - s.Equity
    drawdownPercent = (peak > 0) ? drawdown / peak * 100 : 0
    emit DrawdownPoint { Timestamp, Equity, PeakEquity=peak, Drawdown, DrawdownPercent }
```

#### Performance Metrics

Given a time series of snapshots `E[0], E[1], ..., E[n]`:

| Metric | Formula |
|--------|---------|
| **Daily Return** | `R[i] = (E[i] - E[i-1]) / E[i-1]` |
| **Total Return** | `E[n] - E[0]` |
| **Total Return %** | `(E[n] - E[0]) / E[0] * 100` |
| **Sharpe Ratio** | `mean(R) / stddev(R) * sqrt(252)` |
| **Sortino Ratio** | `mean(R) / downside_stddev(R) * sqrt(252)` where downside = `stddev(R[i] where R[i] < 0)` |
| **Max Drawdown** | `max(peak[i] - E[i])` over all `i` |
| **Max Drawdown %** | `max((peak[i] - E[i]) / peak[i] * 100)` over all `i` |
| **Calmar Ratio** | `annualized_return / max_drawdown_pct` |
| **Win Rate** | `count(R[i] > 0) / count(R)` |
| **Profit Factor** | `sum(R[i] where R[i] > 0) / abs(sum(R[i] where R[i] < 0))` |

> **Assumption**: 252 trading days per year for annualization.

---

### 3.5 IPortfolioRiskService — Risk Analytics and Scenarios

```
IPortfolioRiskService
├── ComputeDollarDeltaAsync(accountId, prices)
├── ComputePortfolioVegaAsync(accountId)
├── EvaluateRiskRulesAsync(accountId, prices)
└── RunScenarioAsync(accountId, prices, scenario)
```

#### Dollar Delta

For each open position:

```
delta = 1.0                          (for stocks)
delta = latestOptionLeg.EntryDelta   (for options)

DollarDelta = delta * currentPrice * netQuantity * multiplier
```

> **Assumption**: Stock delta is always 1.0. Option delta uses the entry-time value from `OptionLeg` — it is NOT re-computed with current market conditions.

#### Portfolio Vega

```
TotalVega = sum(latestLeg.EntryVega * position.NetQuantity * multiplier)
            for all option positions
```

> **Assumption**: Vega uses entry-time snapshot, not live Greeks.

#### Risk Rule Evaluation

Each enabled `RiskRule` is checked against the current portfolio state:

| RuleType | Check | ActualValue |
|----------|-------|-------------|
| `MaxDrawdown` | `(peakEquity - currentEquity) / peakEquity` | Drawdown % |
| `MaxPositionSize` | `max(positionValue / equity)` for all positions | Largest position as % of equity |
| `MaxVegaExposure` | `abs(totalPortfolioVega)` | Total absolute vega |
| `MaxDelta` | `abs(totalDollarDelta)` | Total absolute dollar delta |

A violation is emitted when `ActualValue > rule.Threshold`. The rule's `LastTriggered` timestamp is updated.

#### Scenario Analysis (What-If)

Applies hypothetical shocks and returns the portfolio impact:

```
For each position p:
    scenarioPrice = currentPrice * (1 + priceChangePercent / 100)
    scenarioValue = scenarioPrice * p.NetQuantity * p.Multiplier

    // IV shock (options only, via vega approximation):
    scenarioValue += p.Vega * (ivChangePercent / 100) * p.NetQuantity * p.Multiplier

    // Theta decay (options only):
    scenarioValue += p.Theta * timeDaysForward * p.NetQuantity * p.Multiplier

ScenarioEquity  = account.Cash + sum(scenarioValue)
PnLImpact       = ScenarioEquity - CurrentEquity
PnLImpactPercent = PnLImpact / CurrentEquity * 100
```

> **Assumption**: Scenario uses linear approximations via Greeks (delta-1 for price, vega for IV, theta for time). This is a first-order approximation and does not account for gamma convexity, vanna, or volga effects.

---

### 3.6 IPortfolioReconciliationService — Drift Detection

```
IPortfolioReconciliationService
├── ReconcileAsync(accountId)  → detects drift between cached and rebuilt state
└── AutoFixAsync(accountId)    → rebuilds positions from trade log
```

#### Reconciliation Algorithm

1. Snapshot current cached positions (keyed by `TickerId + OptionContractId + Status`)
2. Run `PositionEngine.RebuildPositionsAsync` to generate ground-truth from trade log
3. Compare cached vs rebuilt:

| Drift Type | Condition |
|------------|-----------|
| `Mismatch` | Same key exists in both, but `NetQuantity` or `RealizedPnL` differ (tolerance: `0.01`) |
| `ExtraInCache` | Position key exists in cached but not in rebuilt |
| `MissingFromCache` | Position key exists in rebuilt but not in cached |

**AutoFix** simply calls `RebuildPositionsAsync`, which deletes all positions/lots and replays from trades.

---

### 3.7 IStrategyAttributionService — Backtest Import and PnL Split

```
IStrategyAttributionService
├── LinkTradeToStrategyAsync(tradeId, strategyExecutionId)
├── ImportBacktestTradesAsync(strategyExecutionId, accountId)
├── GetStrategyPnLAsync(strategyExecutionId)
└── GetAlphaAttributionAsync(accountId)
```

#### Backtest Import Flow

```
For each backtest trade in StrategyExecution:
    1. Create buy Order (Filled) + PortfolioTrade at entry price
    2. Create sell Order (Filled) + PortfolioTrade at exit price
    3. Create StrategyTradeLink for each trade
    4. Apply trades through PositionEngine
    5. Create StrategyAllocation record with capital + date range
```

#### Alpha Attribution

```
For each StrategyAllocation in account:
    PnL = sum(realizedPnL from linked position lots)
    ContributionPercent = PnL / totalAccountPnL * 100

Returns: [ { strategyName, PnL, contributionPercent, tradeCount } ]
```

---

## 4. GraphQL API

### 4.1 Queries (18 resolvers)

| Query | Arguments | Returns |
|-------|-----------|---------|
| `getAccounts` | — | `[Account]` (projectable) |
| `getAccount` | `id: UUID!` | `Account?` |
| `getPositions` | `accountId: UUID!` | `[Position]` (with lots, ticker) |
| `getPosition` | `id: UUID!` | `Position?` |
| `getPortfolioTrades` | `accountId: UUID!` | `[PortfolioTrade]` |
| `getPositionLots` | `positionId: UUID!` | `[PositionLot]` |
| `getPortfolioState` | `accountId: UUID!` | `PortfolioState` |
| `getPortfolioValuation` | `accountId: UUID!` | `PortfolioValuation` |
| `getPortfolioSnapshots` | `accountId: UUID!` | `[PortfolioSnapshot]` |
| `getEquityCurve` | `accountId: UUID!, from?: DateTime, to?: DateTime` | `[PortfolioSnapshot]` |
| `getDrawdownSeries` | `accountId: UUID!` | `[DrawdownPoint]` |
| `getPortfolioMetrics` | `accountId: UUID!` | `PortfolioMetrics` |
| `getRiskRules` | `accountId: UUID!` | `[RiskRule]` |
| `getDollarDelta` | `accountId: UUID!, prices: [PriceInput!]!` | `[DollarDeltaResult]` |
| `getPortfolioVega` | `accountId: UUID!` | `Decimal` |
| `evaluateRiskRules` | `accountId: UUID!, prices: [PriceInput!]!` | `[RiskViolation]` |
| `reconcilePortfolio` | `accountId: UUID!` | `ReconciliationReport` |
| `getStrategyPnL` | `strategyExecutionId: Int!` | `StrategyPnLResult` |
| `getAlphaAttribution` | `accountId: UUID!` | `[AlphaAttribution]` |
| `getStrategyAllocations` | `accountId: UUID!` | `[StrategyAllocation]` |

### 4.2 Mutations (12 mutations)

| Mutation | Key Arguments | Returns |
|----------|---------------|---------|
| `createAccount` | `name, type?, initialCash?` | `AccountResult` |
| `submitOrder` | `accountId, tickerId, side, orderType?, qty?, limitPrice?, assetType?` | `OrderResult` |
| `cancelOrder` | `orderId` | `OrderResult` |
| `fillOrder` | `orderId, fillPrice, fillQuantity, fees?, multiplier?` | `TradeResult` |
| `recordTrade` | `accountId, symbol, side, quantity, price, fees?, assetType?, multiplier?` | `TradeResult` |
| `rebuildPositions` | `accountId` | `RebuildResult` |
| `takePortfolioSnapshot` | `accountId` | `SnapshotResult` |
| `createRiskRule` | `accountId, ruleType, threshold, action?, severity?` | `RiskRuleResult` |
| `updateRiskRule` | `ruleId, threshold?, enabled?, action?, severity?` | `RiskRuleResult` |
| `runScenario` | `accountId, prices, priceChangePercent?, ivChangePercent?, timeDaysForward?` | `ScenarioResult` |
| `autoFixPortfolio` | `accountId` | `RebuildResult` |
| `linkTradeToStrategy` | `tradeId, strategyExecutionId` | `LinkResult` |
| `importBacktestTrades` | `strategyExecutionId, accountId` | `ImportResult` |

### 4.3 Common Input/Result Types

**PriceInput**: `{ symbol: String!, price: Decimal! }`

All mutations return result wrappers with `success: Boolean!, error: String?`, plus a domain object (e.g., `trade`, `account`, `rule`). This avoids throwing GraphQL errors for domain-level failures.

---

## 5. Frontend

### 5.1 Routing

```
/portfolio → PortfolioComponent (lazy-loaded via loadComponent)
```

### 5.2 Service — `PortfolioService`

Injectable singleton (`providedIn: 'root'`). All methods return `Observable<T>` using a lightweight `gql()` helper over `HttpClient`. Grouped by domain:

| Category | Methods |
|----------|---------|
| **Accounts** | `getAccounts()`, `createAccount(name, type, cash)` |
| **State** | `getPortfolioState(accountId)` |
| **Positions** | `getPositions(accountId)` |
| **Trades** | `recordTrade(accountId, symbol, side, qty, price, fees?, assetType?, multiplier?)` |
| **Valuation** | `getValuation(accountId)` |
| **Snapshots** | `takeSnapshot(accountId)`, `getEquityCurve(accountId, from?, to?)`, `getDrawdownSeries(accountId)`, `getMetrics(accountId)` |
| **Risk** | `getRiskRules(accountId)`, `createRiskRule(...)`, `updateRiskRule(...)`, `getDollarDelta(accountId, prices)`, `evaluateRiskRules(accountId, prices)`, `runScenario(...)` |
| **Reconciliation** | `reconcile(accountId)`, `autoFix(accountId)`, `rebuildPositions(accountId)` |
| **Strategy** | `getStrategyAllocations(accountId)`, `importBacktestTrades(strategyExecutionId, accountId)`, `getStrategyPnL(executionId)`, `getAlphaAttribution(accountId)` |

### 5.3 Component Architecture

The `PortfolioComponent` is the container with account selection and a 7-tab PrimeNG layout:

```
PortfolioComponent (account selector + create form)
├── Tab 1: DashboardComponent
│     - Summary cards (cash, initial capital, open positions, recent trades)
│     - Performance metrics (return %, Sharpe, Sortino, max DD, win rate, profit factor)
│     - Record Trade form (symbol, side, qty, price, fees)
│     - Recent Trades table
│     - Take Snapshot button
│
├── Tab 2: PositionsComponent
│     - Positions table with expandable FIFO lot detail
│     - Show Closed toggle
│     - Rebuild Positions action
│
├── Tab 3: EquityChartComponent
│     - Equity area chart (TradingView lightweight-charts v5)
│     - Drawdown histogram chart
│     - Metrics summary bar
│
├── Tab 4: RiskPanelComponent
│     - Risk rule CRUD (create, toggle enable, evaluate)
│     - Dollar Delta table
│     - Violation alerts (severity-colored)
│
├── Tab 5: ScenarioExplorerComponent
│     - 6 preset scenarios (crash, correction, rally, vol spike, theta 5d/30d)
│     - Custom scenario inputs (price %, IV %, theta days)
│     - Result summary + per-position breakdown
│
├── Tab 6: ReconciliationComponent
│     - Run reconciliation check
│     - Drift report table
│     - Auto-fix action
│
└── Tab 7: StrategyAttributionComponent
      - Alpha attribution bars (horizontal, normalized)
      - Import backtest trades
      - Strategy PnL detail cards
      - Allocation table
```

All components use Angular signals, `OnPush` change detection, and modern control flow (`@if`, `@for`, `@switch`).

---

## 6. Important Assumptions and Limitations

### Accounting

| # | Assumption | Impact |
|---|-----------|--------|
| 1 | **FIFO only** | No support for LIFO, specific lot identification, or average cost methods. Realized PnL will differ from brokers using other methods. |
| 2 | **No short selling lots** | The FIFO engine creates lots only on Buy. Selling more than owned is not explicitly blocked at the engine level — it simply won't find lots to close. |
| 3 | **Single-currency** | All values in USD. No FX conversion for international equities. `BaseCurrency` field exists but is not used in calculations. |
| 4 | **No margin accounting** | `MarginUsed` field exists on snapshots but is always 0. No margin requirements, maintenance calls, or buying power calculations. |
| 5 | **Fees are flat** | No per-share, tiered, or exchange-specific fee models. Fees are caller-provided on each trade. |

### Market Data and Pricing

| # | Assumption | Impact |
|---|-----------|--------|
| 6 | **Valuation requires live prices** | `ComputeValuationAsync` calls Polygon snapshot API. If the market is closed or the ticker has no data, valuation may use stale prices or fail. |
| 7 | **No intraday price history** | Snapshots capture a single point-in-time. Equity curves are as granular as the snapshot frequency. |
| 8 | **Symbol auto-creation is minimal** | When `recordTrade` creates a new Ticker, it only sets `Symbol`, `Name=Symbol`, `Market="stocks"`. No exchange, locale, or type metadata is populated. |

### Options and Greeks

| # | Assumption | Impact |
|---|-----------|--------|
| 9 | **Entry Greeks are static** | `OptionLeg` stores Greeks at trade time. Risk calculations (dollar delta, vega, scenario analysis) use these stale values, not live Greeks. Accuracy degrades as the underlying moves or time passes. |
| 10 | **Linear scenario approximation** | Scenario analysis uses first-order Greeks: delta-1 for price, vega for IV, theta for time. No gamma convexity, vanna (dVega/dSpot), or volga (dVega/dVol) adjustments. Large shocks will be inaccurate. |
| 11 | **No exercise or assignment** | Option expiration, exercise, and assignment are not modeled. Positions must be manually closed. |

### Performance Metrics

| # | Assumption | Impact |
|---|-----------|--------|
| 12 | **252 trading days/year** | Sharpe, Sortino, and Calmar ratios annualize using `sqrt(252)`. Actual trading calendar may differ. |
| 13 | **Risk-free rate = 0** | Sharpe and Sortino use excess returns = raw returns (no risk-free subtraction). |
| 14 | **Snapshot frequency = metric granularity** | Metrics assume each snapshot represents one period. If snapshots are taken irregularly, Sharpe/Sortino may be misleading. |
| 15 | **Minimum 2 snapshots** | Metrics require at least 2 snapshots to compute daily returns. With fewer, all metrics return 0. |

### Strategy Attribution

| # | Assumption | Impact |
|---|-----------|--------|
| 16 | **Backtest trades replay at stated prices** | Imported backtest trades use the strategy's recorded entry/exit prices. Slippage and market impact are not modeled. |
| 17 | **Attribution uses realized PnL only** | Unrealized PnL from open positions is not included in strategy attribution. |

---

## 7. Database Notes

- All `decimal` columns use precision `(18, 8)` via EF Core Fluent API
- `EnsureCreated()` is used for table creation — adding new entities requires **deleting the pgdata volume** and restarting (see workaround below)
- No EF migrations are configured; schema changes require a full reset in development

### Volume Reset Procedure

```bash
podman compose down
podman volume rm learn-ai_pgdata
podman compose up -d --build
```

> **Warning**: This deletes all data. For preserving data across schema changes, EF migrations should be implemented.

---

## 8. File Reference

### Backend

| Category | Path |
|----------|------|
| Models | `Backend/Models/Portfolio/*.cs` (Account, Order, PortfolioTrade, Position, PositionLot, OptionContract, OptionLeg, PortfolioSnapshot, RiskRule, StrategyAllocation, StrategyTradeLink, Enums) |
| Interfaces | `Backend/Services/Interfaces/IPortfolioService.cs`, `IPositionEngine.cs`, `ISnapshotService.cs`, `IPortfolioValuationService.cs`, `IPortfolioRiskService.cs`, `IPortfolioReconciliationService.cs`, `IStrategyAttributionService.cs` |
| Implementations | `Backend/Services/Implementation/PortfolioService.cs`, `PositionEngine.cs`, `SnapshotService.cs`, `PortfolioValuationService.cs`, `PortfolioRiskService.cs`, `PortfolioReconciliationService.cs`, `StrategyAttributionService.cs` |
| GraphQL | `Backend/GraphQL/PortfolioQuery.cs`, `Backend/GraphQL/PortfolioMutation.cs` |
| Database | `Backend/Data/AppDbContext.cs` |

### Frontend

| Category | Path |
|----------|------|
| Types | `Frontend/src/app/graphql/portfolio-types.ts` |
| Service | `Frontend/src/app/services/portfolio.service.ts` |
| Container | `Frontend/src/app/components/portfolio/portfolio.component.ts` |
| Dashboard | `Frontend/src/app/components/portfolio/dashboard/` |
| Positions | `Frontend/src/app/components/portfolio/positions/` |
| Equity Chart | `Frontend/src/app/components/portfolio/equity-chart/` |
| Risk Panel | `Frontend/src/app/components/portfolio/risk-panel/` |
| Scenario Explorer | `Frontend/src/app/components/portfolio/scenario-explorer/` |
| Reconciliation | `Frontend/src/app/components/portfolio/reconciliation/` |
| Strategy Attribution | `Frontend/src/app/components/portfolio/strategy-attribution/` |

### Tests

| Path | Coverage |
|------|----------|
| `Backend.Tests/Unit/Services/PortfolioServiceTests.cs` | Account/Order/Trade CRUD |
| `Backend.Tests/Unit/Services/PositionEngineTests.cs` | FIFO lot allocation, rebuild |
| `Backend.Tests/Unit/Services/SnapshotServiceTests.cs` | Snapshots, metrics |
| `Backend.Tests/Unit/Services/PortfolioValuationServiceTests.cs` | MTM, Greeks aggregation |
| `Backend.Tests/Unit/Services/PortfolioRiskServiceTests.cs` | Delta, vega, rules, scenarios |
| `Backend.Tests/Unit/Services/PortfolioReconciliationServiceTests.cs` | Drift detection |
| `Backend.Tests/Unit/Services/StrategyAttributionServiceTests.cs` | Import, PnL, attribution |
| `Frontend/src/app/components/portfolio/portfolio.component.spec.ts` | Account management (21 tests) |
| `Frontend/src/app/components/portfolio/dashboard/dashboard.component.spec.ts` | Dashboard behavior (26 tests) |
| `Frontend/src/app/services/portfolio.service.spec.ts` | Service methods |

---

## 9. Related Documentation

- [Portfolio System Plan](./portfolio-system-plan.md) — original 4-phase implementation plan
- [Portfolio System (detailed)](./portfolio-system.md) — exhaustive entity/service reference
- [Options Cross-Section Overview](./options-cross-section-overview.md) — IV pipeline and Greeks computation
- [Black-Scholes Implementation](./black-scholes-implementation.md) — pricing formulas and Greeks derivations
