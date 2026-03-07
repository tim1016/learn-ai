# Portfolio Management System

## 1. Overview

The portfolio management system is an event-sourced portfolio tracker built into the learn-ai market data dashboard. It tracks paper and backtest trading accounts, manages positions using a FIFO (First-In, First-Out) lot-based engine, computes valuations and risk metrics, and attributes PnL to automated strategies.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Angular Frontend                             │
│  ┌───────────┬───────────┬──────────┬───────────┬────────────────┐  │
│  │ Dashboard │ Positions │ Equity   │ Risk      │ Strategy       │  │
│  │           │           │ Chart    │ Panel     │ Attribution    │  │
│  │           │           │          │ Scenario  │                │  │
│  │           │           │          │ Explorer  │                │  │
│  │           │           │          │ Reconcil. │                │  │
│  └─────┬─────┴─────┬─────┴────┬─────┴─────┬─────┴───────┬────────┘  │
│        └────────────┴──────────┴───────────┴─────────────┘           │
│                          PortfolioService (GraphQL client)           │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ GraphQL over HTTP
┌──────────────────────────────────┴──────────────────────────────────┐
│                     .NET Backend (Hot Chocolate v15)                │
│  ┌──────────────────┐  ┌──────────────────────────────────────┐    │
│  │ PortfolioQuery    │  │ PortfolioMutation                    │    │
│  └────────┬─────────┘  └────────┬─────────────────────────────┘    │
│           │                      │                                  │
│  ┌────────┴──────────────────────┴─────────────────────────────┐   │
│  │                      Service Layer                           │   │
│  │  PortfolioService ──► PositionEngine (FIFO lots)             │   │
│  │  ValuationService ──► live price lookup + multiplier math    │   │
│  │  SnapshotService  ──► equity curve, metrics (Sharpe, etc.)   │   │
│  │  RiskService      ──► dollar delta, vega, rule evaluation    │   │
│  │  ReconciliationService ──► drift detection + auto-fix        │   │
│  │  StrategyAttributionService ──► backtest import + PnL split  │   │
│  └──────────────────────────┬──────────────────────────────────┘   │
│                              │                                      │
│  ┌───────────────────────────┴─────────────────────────────────┐   │
│  │  EF Core 10 ──► PostgreSQL 16                               │   │
│  └─────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

1. **FIFO Lot Tracking** -- Every buy creates a `PositionLot`. Sells close the oldest open lots first. Partial closes reduce `RemainingQuantity` on the lot without splitting it into two rows. This makes realized PnL deterministic and auditable.

2. **Trade as Source of Truth** -- `PortfolioTrade` records are immutable facts. Positions and lots are derived state that can be completely rebuilt from the trade log at any time via `RebuildPositionsAsync`. This is the event-sourcing guarantee.

3. **Multiplier-Aware** -- All PnL, market value, and dollar delta calculations multiply by the contract multiplier (default 1 for stocks, 100 for standard equity options). The multiplier is stored on both `PortfolioTrade` and `OptionContract`.

4. **Cash Tracking** -- The `Account.Cash` field is updated on every fill: buys deduct `price * quantity * multiplier + fees`, sells add `price * quantity * multiplier - fees`.

5. **Options as First-Class Citizens** -- Option trades carry an `OptionLeg` with entry Greeks (IV, delta, gamma, theta, vega) that power risk calculations without needing a real-time pricing model.

---

## 2. Data Model

### Entity Relationship Diagram

```
Account (1) ──────< (N) Order
   │                      │
   │ (1)                  │ (1)
   │                      │
   ├──< (N) PortfolioTrade ┘
   │            │
   │            ├──── (0..1) OptionLeg
   │            │
   │            └──< (N) PositionLot
   │                      │
   ├──< (N) Position ─────┘
   │            │
   │            └──── (0..1) OptionContract
   │
   ├──< (N) PortfolioSnapshot
   │
   ├──< (N) RiskRule
   │
   └──< (N) StrategyAllocation ──── StrategyExecution

PortfolioTrade (N) ────── (0..1) StrategyTradeLink ──── StrategyExecution
```

### Entities

#### Account

| Field          | Type          | Description                                    |
|----------------|---------------|------------------------------------------------|
| `Id`           | `Guid`        | Primary key                                    |
| `Name`         | `string(200)` | Display name (required)                        |
| `Type`         | `AccountType` | `Paper` or `Backtest`                          |
| `BaseCurrency` | `string(10)`  | Default `"USD"`                                |
| `InitialCash`  | `decimal`     | Starting capital, never changes after creation |
| `Cash`         | `decimal`     | Current available cash, updated on every fill  |
| `CreatedAt`    | `DateTime`    | UTC timestamp of creation                      |

Navigation: `Orders`, `Trades`, `Positions`.

#### Order

| Field              | Type              | Description                                       |
|--------------------|-------------------|---------------------------------------------------|
| `Id`               | `Guid`            | Primary key                                       |
| `AccountId`        | `Guid`            | FK to Account                                     |
| `TickerId`         | `int`             | FK to Ticker (market data)                        |
| `Side`             | `OrderSide`       | `Buy` or `Sell`                                   |
| `OrderType`        | `OrderType`       | `Market`, `Limit`, or `Stop`                      |
| `Quantity`         | `decimal`         | Ordered quantity                                  |
| `LimitPrice`       | `decimal?`        | Limit price (null for market orders)              |
| `Status`           | `OrderStatus`     | `Pending`, `Filled`, `PartiallyFilled`, `Cancelled` |
| `AssetType`        | `AssetType`       | `Stock` or `Option`                               |
| `OptionContractId` | `Guid?`           | FK to OptionContract (null for stocks)            |
| `SubmittedAt`      | `DateTime`        | When the order was placed                         |
| `FilledAt`         | `DateTime?`       | When fully filled (null if not yet)               |

Navigation: `Account`, `Ticker`, `OptionContract`, `Trades`.

#### PortfolioTrade

| Field                | Type          | Description                                   |
|----------------------|---------------|-----------------------------------------------|
| `Id`                 | `Guid`        | Primary key                                   |
| `AccountId`          | `Guid`        | FK to Account                                 |
| `OrderId`            | `Guid`        | FK to originating Order                       |
| `TickerId`           | `int`         | FK to Ticker                                  |
| `Side`               | `OrderSide`   | `Buy` or `Sell`                               |
| `Quantity`           | `decimal`     | Filled quantity                               |
| `Price`              | `decimal`     | Execution price                               |
| `Fees`               | `decimal`     | Commission and fees                           |
| `AssetType`          | `AssetType`   | `Stock` or `Option`                           |
| `OptionContractId`   | `Guid?`       | FK to OptionContract                          |
| `Multiplier`         | `int`         | Contract multiplier (1 for stocks, 100 for standard options) |
| `ExecutionTimestamp`  | `DateTime`    | When the trade was executed                   |

Navigation: `Account`, `Order`, `Ticker`, `OptionContract`, `Lots`, `OptionLeg`.

#### Position

| Field              | Type             | Description                                         |
|--------------------|------------------|-----------------------------------------------------|
| `Id`               | `Guid`           | Primary key                                         |
| `AccountId`        | `Guid`           | FK to Account                                       |
| `TickerId`         | `int`            | FK to Ticker                                        |
| `AssetType`        | `AssetType`      | `Stock` or `Option`                                 |
| `OptionContractId` | `Guid?`          | FK to OptionContract                                |
| `NetQuantity`      | `decimal`        | Sum of remaining lot quantities                     |
| `AvgCostBasis`     | `decimal`        | Weighted average entry price across open lots        |
| `RealizedPnL`      | `decimal`        | Sum of realized PnL across all lots                 |
| `Status`           | `PositionStatus` | `Open` or `Closed`                                  |
| `OpenedAt`         | `DateTime`       | Timestamp of the first trade                        |
| `ClosedAt`         | `DateTime?`      | Timestamp when all lots fully closed                |
| `LastUpdated`      | `DateTime`       | Last time position was recalculated                 |

Navigation: `Account`, `Ticker`, `OptionContract`, `Lots`.

#### PositionLot

| Field               | Type        | Description                                              |
|---------------------|-------------|----------------------------------------------------------|
| `Id`                | `Guid`      | Primary key                                              |
| `PositionId`        | `Guid`      | FK to Position                                           |
| `TradeId`           | `Guid`      | FK to the buy trade that created this lot                |
| `Quantity`          | `decimal`   | Original quantity purchased                              |
| `EntryPrice`        | `decimal`   | Price at which the lot was opened                        |
| `RemainingQuantity` | `decimal`   | Shares/contracts not yet closed (decremented by sells)   |
| `RealizedPnL`       | `decimal`   | PnL realized from partial or full closes of this lot     |
| `OpenedAt`          | `DateTime`  | When the lot was opened                                  |
| `ClosedAt`          | `DateTime?` | When `RemainingQuantity` reached 0                       |

Navigation: `Position`, `Trade`.

#### OptionContract

| Field               | Type         | Description                           |
|---------------------|--------------|---------------------------------------|
| `Id`                | `Guid`       | Primary key                           |
| `UnderlyingTickerId`| `int`        | FK to the underlying Ticker           |
| `Symbol`            | `string(100)`| OCC-style symbol (e.g., `O:AAPL250620C00150000`) |
| `Strike`            | `decimal`    | Strike price                          |
| `Expiration`        | `DateOnly`   | Expiration date                       |
| `OptionType`        | `OptionType` | `Call` or `Put`                       |
| `Multiplier`        | `int`        | Shares per contract (default 100)     |

Navigation: `UnderlyingTicker`.

#### OptionLeg

| Field              | Type       | Description                                          |
|--------------------|------------|------------------------------------------------------|
| `Id`               | `Guid`     | Primary key                                          |
| `TradeId`          | `Guid`     | FK to PortfolioTrade (one-to-one)                    |
| `OptionContractId` | `Guid`     | FK to OptionContract                                 |
| `Quantity`         | `decimal`  | Number of contracts in this leg                      |
| `EntryIV`          | `decimal?` | Implied volatility at time of trade                  |
| `EntryDelta`       | `decimal?` | Delta at time of trade                               |
| `EntryGamma`       | `decimal?` | Gamma at time of trade                               |
| `EntryTheta`       | `decimal?` | Theta at time of trade                               |
| `EntryVega`        | `decimal?` | Vega at time of trade                                |

Navigation: `Trade`, `OptionContract`.

#### PortfolioSnapshot

| Field           | Type        | Description                               |
|-----------------|-------------|-------------------------------------------|
| `Id`            | `Guid`      | Primary key                               |
| `AccountId`     | `Guid`      | FK to Account                             |
| `Timestamp`     | `DateTime`  | When the snapshot was taken                |
| `Equity`        | `decimal`   | Cash + MarketValue at snapshot time       |
| `Cash`          | `decimal`   | Cash balance at snapshot time             |
| `MarketValue`   | `decimal`   | Total market value of open positions      |
| `MarginUsed`    | `decimal`   | Margin consumed (reserved for future use) |
| `UnrealizedPnL` | `decimal`   | Total unrealized PnL                      |
| `RealizedPnL`   | `decimal`   | Total realized PnL                        |
| `NetDelta`      | `decimal?`  | Portfolio-level net delta                  |
| `NetGamma`      | `decimal?`  | Portfolio-level net gamma                  |
| `NetTheta`      | `decimal?`  | Portfolio-level net theta                  |
| `NetVega`       | `decimal?`  | Portfolio-level net vega                   |

Navigation: `Account`.

#### RiskRule

| Field           | Type            | Description                                   |
|-----------------|-----------------|-----------------------------------------------|
| `Id`            | `Guid`          | Primary key                                   |
| `AccountId`     | `Guid`          | FK to Account                                 |
| `RuleType`      | `RiskRuleType`  | What the rule checks (see enum below)         |
| `Threshold`     | `decimal`       | Numeric limit (e.g., 0.10 for 10% drawdown)  |
| `Action`        | `RiskAction`    | `Warn` or `Block`                             |
| `Severity`      | `RiskSeverity`  | `Low`, `Medium`, `High`, or `Critical`        |
| `Enabled`       | `bool`          | Whether the rule is active                    |
| `LastTriggered`  | `DateTime?`     | Last time this rule was violated              |

Navigation: `Account`.

#### StrategyAllocation

| Field                 | Type        | Description                                      |
|-----------------------|-------------|--------------------------------------------------|
| `Id`                  | `Guid`      | Primary key                                      |
| `AccountId`           | `Guid`      | FK to Account                                    |
| `StrategyExecutionId` | `int`       | FK to StrategyExecution (market data model)       |
| `CapitalAllocated`    | `decimal`   | How much capital was earmarked for this strategy |
| `StartDate`           | `DateTime`  | When the allocation began                        |
| `EndDate`             | `DateTime?` | When the allocation ended (null if active)       |

Navigation: `Account`, `StrategyExecution`.

#### StrategyTradeLink

| Field                 | Type   | Description                                   |
|-----------------------|--------|-----------------------------------------------|
| `Id`                  | `Guid` | Primary key                                   |
| `TradeId`             | `Guid` | FK to PortfolioTrade                          |
| `StrategyExecutionId` | `int`  | FK to StrategyExecution                       |

Navigation: `Trade`, `StrategyExecution`.

### Enums

| Enum             | Values                                              |
|------------------|-----------------------------------------------------|
| `AccountType`    | `Paper`, `Backtest`                                 |
| `OrderSide`      | `Buy`, `Sell`                                       |
| `OrderType`      | `Market`, `Limit`, `Stop`                           |
| `OrderStatus`    | `Pending`, `Filled`, `PartiallyFilled`, `Cancelled` |
| `AssetType`      | `Stock`, `Option`                                   |
| `PositionStatus` | `Open`, `Closed`                                    |
| `OptionType`     | `Call`, `Put`                                       |
| `RiskRuleType`   | `MaxDrawdown`, `MaxPositionSize`, `MaxVegaExposure`, `MaxDelta` |
| `RiskAction`     | `Warn`, `Block`                                     |
| `RiskSeverity`   | `Low`, `Medium`, `High`, `Critical`                 |

---

## 3. Service Layer

### IPositionEngine

The core FIFO lot management engine. All position state is derived from replaying trades through this engine.

```csharp
public interface IPositionEngine
{
    Task<List<Position>> RebuildPositionsAsync(Guid accountId, CancellationToken ct = default);
    Task<Position> ApplyTradeAsync(PortfolioTrade trade, CancellationToken ct = default);
    decimal CalculateRealizedPnL(IEnumerable<PositionLot> lots);
}
```

| Method                  | Description |
|-------------------------|-------------|
| `RebuildPositionsAsync` | Deletes all existing positions and lots for the account, then replays every `PortfolioTrade` in chronological order through `ApplyTradeAsync`. Returns the freshly rebuilt position list. This is the event-sourcing "projection rebuild". |
| `ApplyTradeAsync`       | Applies a single trade to the position graph. For buys, creates a new `PositionLot`. For sells, closes lots in FIFO order. Recalculates `NetQuantity`, `AvgCostBasis`, `RealizedPnL`, and `Status` on the position. |
| `CalculateRealizedPnL`  | Pure function that sums `RealizedPnL` across a collection of lots. |

#### FIFO Algorithm

When a sell trade arrives, the engine processes open lots ordered by `OpenedAt` (earliest first):

```
Given: Sell 150 shares @ $170, Multiplier = 1

Lot A: 100 shares @ $150 (oldest)    Lot B: 100 shares @ $160

Step 1: Close 100 from Lot A
  closeQty  = min(100, 150) = 100
  PnL       = (170 - 150) * 100 * 1 = $2,000
  Lot A remaining = 0 → ClosedAt = now

Step 2: Close 50 from Lot B
  closeQty  = min(100, 50) = 50
  PnL       = (170 - 160) * 50 * 1 = $500
  Lot B remaining = 50 → still open

Position after:
  NetQuantity = 50
  RealizedPnL = $2,500
  AvgCostBasis = $160 (only Lot B remains)
  Status = Open
```

For options, the multiplier is applied to the PnL calculation:

```
PnL per lot = (sellPrice - entryPrice) * closeQuantity * multiplier
```

Example: Buy 1 contract @ $5.00 (multiplier 100), sell @ $8.00:

```
PnL = (8.00 - 5.00) * 1 * 100 = $300
```

After recalculation, if all lots have `RemainingQuantity == 0`, the position status is set to `Closed`.

### IPortfolioService

The primary service for account management, order lifecycle, and trade recording.

```csharp
public interface IPortfolioService
{
    Task<Account> CreateAccountAsync(string name, AccountType type, decimal initialCash,
        CancellationToken ct = default);
    Task<Order> SubmitOrderAsync(Guid accountId, int tickerId, OrderSide side,
        OrderType orderType, decimal quantity, decimal? limitPrice,
        AssetType assetType = AssetType.Stock, Guid? optionContractId = null,
        CancellationToken ct = default);
    Task<Order> CancelOrderAsync(Guid orderId, CancellationToken ct = default);
    Task<PortfolioTrade> FillOrderAsync(Guid orderId, decimal fillPrice, decimal fillQuantity,
        decimal fees = 0, int multiplier = 1, OptionLegInput? optionLeg = null,
        CancellationToken ct = default);
    Task<PortfolioTrade> RecordTradeAsync(RecordTradeInput input, CancellationToken ct = default);
    Task<PortfolioState> GetPortfolioStateAsync(Guid accountId, CancellationToken ct = default);
}
```

| Method                | Description |
|-----------------------|-------------|
| `CreateAccountAsync`  | Creates a new account with the given name, type, and initial cash. Sets both `InitialCash` and `Cash` to the provided amount. |
| `SubmitOrderAsync`    | Creates an order with `Pending` status. Does not execute -- the order must be filled separately. |
| `CancelOrderAsync`    | Sets order status to `Cancelled`. Throws `InvalidOperationException` if the order is already `Filled`. |
| `FillOrderAsync`      | Fills a pending order: creates a `PortfolioTrade`, updates `Account.Cash` (buy deducts, sell adds), and delegates to `PositionEngine.ApplyTradeAsync`. Cash formula: **Buy**: `cash -= price * quantity * multiplier + fees`. **Sell**: `cash += price * quantity * multiplier - fees`. |
| `RecordTradeAsync`    | Shortcut that creates both an order and trade in one call. Useful for importing historical trades or when the order/fill distinction is not needed. Also creates an `OptionLeg` if `input.OptionLeg` is provided. |
| `GetPortfolioStateAsync` | Returns a `PortfolioState` containing the account, all open positions (with ticker info), and total realized PnL. |

#### Order Lifecycle

```
SubmitOrder ──► [Pending] ──┬──► FillOrder ──► [Filled] + PortfolioTrade created
                            │
                            └──► CancelOrder ──► [Cancelled]
```

### IPortfolioValuationService

Computes the current market value of the portfolio given live (or provided) prices.

```csharp
public interface IPortfolioValuationService
{
    Task<PortfolioValuation> ComputeValuationAsync(Guid accountId, CancellationToken ct = default);
    Task<PortfolioValuation> ComputeValuationWithPricesAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default);
}
```

| Method                          | Description |
|---------------------------------|-------------|
| `ComputeValuationAsync`         | Fetches live prices from PolygonService for all open positions, then computes valuation. |
| `ComputeValuationWithPricesAsync` | Uses caller-provided prices (useful for scenarios and tests). |

#### Valuation Formulas

For each open position:

```
MarketValue   = currentPrice * quantity * multiplier
UnrealizedPnL = (currentPrice - avgCostBasis) * quantity * multiplier
CostBasis     = avgCostBasis * quantity * multiplier
```

Portfolio-level aggregation:

```
MarketValue   = SUM(position.MarketValue)
UnrealizedPnL = SUM(position.UnrealizedPnL)
Equity        = Cash + MarketValue
```

The valuation also aggregates net Greeks (delta, gamma, theta, vega) from option leg data when available.

### ISnapshotService

Captures point-in-time portfolio state and computes performance metrics from the snapshot history.

```csharp
public interface ISnapshotService
{
    Task<PortfolioSnapshot> TakeSnapshotAsync(Guid accountId, CancellationToken ct = default);
    Task<PortfolioSnapshot> TakeSnapshotWithPricesAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default);
    Task<List<PortfolioSnapshot>> GetEquityCurveAsync(Guid accountId,
        DateTime? from = null, DateTime? to = null, CancellationToken ct = default);
    Task<List<DrawdownPoint>> GetDrawdownSeriesAsync(Guid accountId,
        CancellationToken ct = default);
    PortfolioMetrics ComputeMetrics(List<PortfolioSnapshot> snapshots);
}
```

| Method                     | Description |
|----------------------------|-------------|
| `TakeSnapshotAsync`        | Computes current valuation and persists a `PortfolioSnapshot` row with equity, cash, market value, PnL, and Greeks. |
| `TakeSnapshotWithPricesAsync` | Same as above but with caller-provided prices. |
| `GetEquityCurveAsync`      | Returns snapshots ordered by timestamp, optionally filtered by date range. |
| `GetDrawdownSeriesAsync`   | Computes drawdown at each snapshot point relative to the running peak equity. |
| `ComputeMetrics`           | Pure function that computes performance metrics from a list of snapshots. |

#### Metrics Formulas

Given a series of equity snapshots `E[0], E[1], ..., E[n]`:

**Daily Returns**: `R[i] = (E[i] - E[i-1]) / E[i-1]`

**Total Return**: `(E[n] - E[0]) / E[0]`

**Sharpe Ratio**: `mean(R) / stdev(R) * sqrt(252)` (annualized, assuming daily snapshots)

**Sortino Ratio**: `mean(R) / downside_stdev(R) * sqrt(252)` where downside deviation only counts `R[i] < 0`

**Max Drawdown**: `max over all i of (peak[i] - E[i])` where `peak[i] = max(E[0..i])`

**Max Drawdown %**: `max over all i of (peak[i] - E[i]) / peak[i]`

**Calmar Ratio**: `annualized_return / MaxDrawdownPercent`

**Win Rate**: Fraction of positive returns among all return periods.

**Profit Factor**: `sum(positive_returns) / |sum(negative_returns)|`

The `PortfolioMetrics` result type:

| Field               | Type      | Description                                |
|---------------------|-----------|--------------------------------------------|
| `SharpeRatio`       | `decimal` | Annualized Sharpe ratio                    |
| `SortinoRatio`      | `decimal` | Annualized Sortino ratio                   |
| `MaxDrawdown`       | `decimal` | Maximum dollar drawdown                    |
| `MaxDrawdownPercent`| `decimal` | Maximum percentage drawdown                |
| `CalmarRatio`       | `decimal` | Return / max drawdown ratio                |
| `WinRate`           | `decimal` | Fraction of positive-return periods        |
| `ProfitFactor`      | `decimal` | Gross profit / gross loss                  |
| `TotalReturn`       | `decimal` | Dollar return from first to last snapshot  |
| `TotalReturnPercent`| `decimal` | Percentage return                          |
| `SnapshotCount`     | `int`     | Number of snapshots used in calculation    |

### IPortfolioRiskService

Risk analysis: dollar delta exposure, portfolio vega, rule-based alerts, and scenario simulation.

```csharp
public interface IPortfolioRiskService
{
    Task<List<DollarDeltaResult>> ComputeDollarDeltaAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default);
    Task<decimal> ComputePortfolioVegaAsync(Guid accountId, CancellationToken ct = default);
    Task<List<RiskViolation>> EvaluateRiskRulesAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default);
    Task<ScenarioResult> RunScenarioAsync(Guid accountId,
        Dictionary<string, decimal> prices, ScenarioInput scenario,
        CancellationToken ct = default);
}
```

| Method                  | Description |
|-------------------------|-------------|
| `ComputeDollarDeltaAsync` | Computes dollar delta for each open position. Stocks have delta = 1. Options use the `EntryDelta` from the most recent `OptionLeg`. |
| `ComputePortfolioVegaAsync` | Sums vega exposure across all option positions: `totalVega = SUM(entryVega * quantity * multiplier)`. |
| `EvaluateRiskRulesAsync` | Evaluates all enabled `RiskRule` records for the account. Returns a `RiskViolation` for each rule whose threshold is breached. Disabled rules are skipped. |
| `RunScenarioAsync`      | Simulates a what-if scenario by applying price shocks, IV changes, and/or theta decay to the current portfolio. |

#### Dollar Delta Formula

```
DollarDelta = delta * price * quantity * multiplier
```

- For stocks: `delta = 1`, `multiplier = 1`, so `DollarDelta = price * quantity`.
- For options: delta comes from the latest `OptionLeg.EntryDelta`, multiplier is typically 100.

Example: 10 call contracts with delta 0.65, underlying at $505, multiplier 100:

```
DollarDelta = 0.65 * 505 * 10 * 100 = $328,250
```

#### Portfolio Vega Formula

```
PortfolioVega = SUM(entryVega * quantity * multiplier)  for each option position
```

#### Risk Rule Evaluation

Each enabled rule is checked against a computed actual value:

| RuleType           | Actual Value Computation                                           |
|--------------------|--------------------------------------------------------------------|
| `MaxDrawdown`      | `(peakEquity - currentEquity) / peakEquity` from snapshot history  |
| `MaxPositionSize`  | Largest single position's market value as fraction of equity       |
| `MaxVegaExposure`  | Absolute portfolio vega                                            |
| `MaxDelta`         | Absolute net dollar delta                                          |

If `actualValue > threshold`, a `RiskViolation` is returned.

#### Scenario Analysis

The `ScenarioInput` allows combining up to three shocks:

| Parameter          | Effect                                                            |
|--------------------|-------------------------------------------------------------------|
| `PriceChangePercent` | Shifts all position prices by this fraction (e.g., -0.10 = -10%) |
| `IvChangePercent`  | Adjusts option values via vega: `vegaImpact = vega * ivChange * qty * multiplier` |
| `TimeDaysForward`  | Applies theta decay: `thetaImpact = theta * days * qty * multiplier` |

The scenario recalculates market values with shocked prices and returns the `PnLImpact` (dollar and percent).

### IPortfolioReconciliationService

Detects and fixes drift between cached position state and the authoritative trade log.

```csharp
public interface IPortfolioReconciliationService
{
    Task<ReconciliationReport> ReconcileAsync(Guid accountId, CancellationToken ct = default);
    Task AutoFixAsync(Guid accountId, CancellationToken ct = default);
}
```

| Method          | Description |
|-----------------|-------------|
| `ReconcileAsync` | Snapshots the current cached positions, rebuilds positions from the trade log, and compares. Returns a `ReconciliationReport` listing any drifts. |
| `AutoFixAsync`   | Calls `PositionEngine.RebuildPositionsAsync` to replace cached positions with freshly computed ones. |

#### How Drift Detection Works

1. Read all current positions for the account (the "cached" state).
2. Rebuild all positions from the trade log (the "rebuilt" state).
3. For each ticker, compare `NetQuantity` and `RealizedPnL` between cached and rebuilt.
4. Any difference generates a `PositionDrift` entry with `DriftType` describing the mismatch.

The `ReconciliationReport` includes:

| Field                  | Type               | Description                              |
|------------------------|--------------------|------------------------------------------|
| `AccountId`            | `Guid`             | The account that was reconciled          |
| `HasDrift`             | `bool`             | True if any drifts were found            |
| `Drifts`               | `List<PositionDrift>` | Detailed per-symbol drift information |
| `CachedPositionCount`  | `int`              | Number of positions before rebuild       |
| `RebuiltPositionCount` | `int`              | Number of positions after rebuild        |

### IStrategyAttributionService

Links portfolio trades to automated strategy executions for PnL attribution.

```csharp
public interface IStrategyAttributionService
{
    Task<StrategyTradeLink> LinkTradeToStrategyAsync(Guid tradeId,
        int strategyExecutionId, CancellationToken ct = default);
    Task<List<PortfolioTrade>> ImportBacktestTradesAsync(int strategyExecutionId,
        Guid accountId, CancellationToken ct = default);
    Task<StrategyPnLResult> GetStrategyPnLAsync(int strategyExecutionId,
        CancellationToken ct = default);
    Task<List<AlphaAttribution>> GetAlphaAttributionAsync(Guid accountId,
        CancellationToken ct = default);
}
```

| Method                      | Description |
|-----------------------------|-------------|
| `LinkTradeToStrategyAsync`  | Creates a `StrategyTradeLink` connecting a trade to a strategy execution. |
| `ImportBacktestTradesAsync` | Reads trades from a completed strategy execution (backtest) and records them as `PortfolioTrade` entries in the target account, linked via `StrategyTradeLink`. |
| `GetStrategyPnLAsync`       | Aggregates PnL for all trades linked to a specific strategy execution. Returns total PnL, trade count, and win rate. |
| `GetAlphaAttributionAsync`  | Breaks down account PnL by strategy, showing each strategy's contribution percentage. |

---

## 4. GraphQL API

### Queries

#### getAccounts

List all accounts. Supports projection, filtering, and sorting.

```graphql
query {
  getAccounts {
    id
    name
    type
    baseCurrency
    initialCash
    cash
    createdAt
  }
}
```

#### getAccount

Fetch a single account by ID.

```graphql
query GetAccount($id: UUID!) {
  getAccount(id: $id) {
    id name type cash initialCash createdAt
  }
}
```

#### getPortfolioState

Returns account, open positions, and recent trades in one call.

```graphql
query GetPortfolioState($accountId: UUID!) {
  getPortfolioState(accountId: $accountId) {
    account { id name type cash initialCash createdAt }
    positions {
      id tickerId assetType netQuantity avgCostBasis realizedPnL status openedAt closedAt
      ticker { symbol name }
    }
    totalRealizedPnL
  }
}
```

Response shape:

```json
{
  "data": {
    "getPortfolioState": {
      "account": { "id": "...", "name": "My Paper", "type": "Paper", "cash": 84990.00 },
      "positions": [
        {
          "id": "...",
          "tickerId": 1,
          "assetType": "Stock",
          "netQuantity": 100,
          "avgCostBasis": 150.00,
          "realizedPnL": 0,
          "status": "Open",
          "ticker": { "symbol": "AAPL", "name": "Apple Inc" }
        }
      ],
      "totalRealizedPnL": 0
    }
  }
}
```

#### getPositions

List positions for an account. Includes lots and ticker. Supports filtering/sorting.

```graphql
query GetPositions($accountId: UUID!) {
  getPositions(accountId: $accountId) {
    id tickerId assetType netQuantity avgCostBasis realizedPnL status openedAt closedAt
    ticker { symbol name }
    lots { id quantity entryPrice remainingQuantity realizedPnL openedAt closedAt }
  }
}
```

#### getPortfolioTrades

List trades for an account. Supports filtering/sorting.

```graphql
query GetTrades($accountId: UUID!) {
  getPortfolioTrades(accountId: $accountId) {
    id tickerId side quantity price fees multiplier executionTimestamp
    ticker { symbol name }
    optionLeg { entryIV entryDelta entryGamma entryTheta entryVega }
  }
}
```

#### getPositionLots

List lots for a specific position.

```graphql
query GetLots($positionId: UUID!) {
  getPositionLots(positionId: $positionId) {
    id quantity entryPrice remainingQuantity realizedPnL openedAt closedAt
  }
}
```

#### getPortfolioValuation

Compute current valuation with live prices.

```graphql
query GetValuation($accountId: UUID!) {
  getPortfolioValuation(accountId: $accountId) {
    cash marketValue equity unrealizedPnL realizedPnL
    netDelta netGamma netTheta netVega
    positions { symbol currentPrice quantity multiplier marketValue unrealizedPnL costBasis }
  }
}
```

#### getPortfolioSnapshots

List raw snapshot records. Supports filtering/sorting.

```graphql
query GetSnapshots($accountId: UUID!) {
  getPortfolioSnapshots(accountId: $accountId) {
    id timestamp equity cash marketValue unrealizedPnL realizedPnL
  }
}
```

#### getEquityCurve

Equity curve with optional date range filter.

```graphql
query GetEquityCurve($accountId: UUID!, $from: DateTime, $to: DateTime) {
  getEquityCurve(accountId: $accountId, from: $from, to: $to) {
    id timestamp equity cash marketValue
  }
}
```

#### getDrawdownSeries

Drawdown at each snapshot point.

```graphql
query GetDrawdown($accountId: UUID!) {
  getDrawdownSeries(accountId: $accountId) {
    timestamp equity peakEquity drawdown drawdownPercent
  }
}
```

#### getPortfolioMetrics

Performance statistics computed from the equity curve.

```graphql
query GetMetrics($accountId: UUID!) {
  getPortfolioMetrics(accountId: $accountId) {
    totalReturnPercent sharpeRatio sortinoRatio calmarRatio
    maxDrawdown maxDrawdownPercent winRate profitFactor snapshotCount
  }
}
```

#### getRiskRules

List all risk rules for an account.

```graphql
query GetRiskRules($accountId: UUID!) {
  getRiskRules(accountId: $accountId) {
    id ruleType threshold action severity enabled lastTriggered
  }
}
```

#### getDollarDelta

Compute dollar delta for each position given current prices.

```graphql
query GetDollarDelta($accountId: UUID!, $prices: [PriceInputInput!]!) {
  getDollarDelta(accountId: $accountId, prices: $prices) {
    positionId symbol delta price quantity multiplier dollarDelta
  }
}
```

Variables:

```json
{
  "accountId": "...",
  "prices": [{ "symbol": "AAPL", "price": 175.50 }]
}
```

#### getPortfolioVega

Total portfolio vega exposure.

```graphql
query GetPortfolioVega($accountId: UUID!) {
  getPortfolioVega(accountId: $accountId)
}
```

#### evaluateRiskRules

Check all enabled rules against current state.

```graphql
query EvaluateRules($accountId: UUID!, $prices: [PriceInputInput!]!) {
  evaluateRiskRules(accountId: $accountId, prices: $prices) {
    ruleId ruleType action severity threshold actualValue message
  }
}
```

#### reconcilePortfolio

Compare cached positions against rebuilt positions.

```graphql
query Reconcile($accountId: UUID!) {
  reconcilePortfolio(accountId: $accountId) {
    accountId hasDrift cachedPositionCount rebuiltPositionCount
    drifts { tickerId symbol cachedQuantity rebuiltQuantity cachedRealizedPnL rebuiltRealizedPnL driftType }
  }
}
```

#### getStrategyPnL

PnL breakdown for a specific strategy execution.

```graphql
query GetStrategyPnL($strategyExecutionId: Int!) {
  getStrategyPnL(strategyExecutionId: $strategyExecutionId) {
    strategyExecutionId strategyName totalPnL tradeCount winRate
  }
}
```

#### getAlphaAttribution

PnL attribution across all strategies for an account.

```graphql
query GetAttribution($accountId: UUID!) {
  getAlphaAttribution(accountId: $accountId) {
    strategyExecutionId strategyName pnL tradeCount contributionPercent
  }
}
```

#### getStrategyAllocations

Capital allocations to strategies.

```graphql
query GetAllocations($accountId: UUID!) {
  getStrategyAllocations(accountId: $accountId) {
    id strategyExecutionId capitalAllocated startDate endDate
    strategyExecution { strategyName }
  }
}
```

### Mutations

#### createAccount

```graphql
mutation CreateAccount($name: String!, $type: String!, $initialCash: Decimal!) {
  createAccount(name: $name, type: $type, initialCash: $initialCash) {
    success error account { id name type cash initialCash createdAt }
  }
}
```

Variables: `{ "name": "My Paper Account", "type": "Paper", "initialCash": 100000 }`

#### submitOrder

```graphql
mutation SubmitOrder($accountId: UUID!, $tickerId: Int!, $side: String!,
    $orderType: String!, $quantity: Decimal!, $limitPrice: Decimal,
    $assetType: String, $optionContractId: UUID) {
  submitOrder(accountId: $accountId, tickerId: $tickerId, side: $side,
      orderType: $orderType, quantity: $quantity, limitPrice: $limitPrice,
      assetType: $assetType, optionContractId: $optionContractId) {
    success error order { id status side quantity }
  }
}
```

#### cancelOrder

```graphql
mutation CancelOrder($orderId: UUID!) {
  cancelOrder(orderId: $orderId) {
    success error order { id status }
  }
}
```

#### fillOrder

```graphql
mutation FillOrder($orderId: UUID!, $fillPrice: Decimal!, $fillQuantity: Decimal!,
    $fees: Decimal, $multiplier: Int) {
  fillOrder(orderId: $orderId, fillPrice: $fillPrice, fillQuantity: $fillQuantity,
      fees: $fees, multiplier: $multiplier) {
    success error trade { id side quantity price executionTimestamp }
  }
}
```

#### recordTrade

Shortcut that creates order + trade + position in one call.

```graphql
mutation RecordTrade($accountId: UUID!, $tickerId: Int!, $side: String!,
    $quantity: Decimal!, $price: Decimal!, $fees: Decimal!,
    $assetType: String!, $multiplier: Int!) {
  recordTrade(accountId: $accountId, tickerId: $tickerId, side: $side,
      quantity: $quantity, price: $price, fees: $fees,
      assetType: $assetType, multiplier: $multiplier) {
    success error trade { id side quantity price executionTimestamp ticker { symbol } }
  }
}
```

#### rebuildPositions

Delete and rebuild all positions from the trade log.

```graphql
mutation RebuildPositions($accountId: UUID!) {
  rebuildPositions(accountId: $accountId) {
    success error positionCount message
  }
}
```

#### takePortfolioSnapshot

Capture current valuation as a snapshot.

```graphql
mutation TakeSnapshot($accountId: UUID!) {
  takePortfolioSnapshot(accountId: $accountId) {
    success error message
    snapshot { id timestamp equity cash marketValue unrealizedPnL realizedPnL }
  }
}
```

#### createRiskRule

```graphql
mutation CreateRiskRule($accountId: UUID!, $ruleType: String!, $threshold: Decimal!,
    $action: String!, $severity: String!) {
  createRiskRule(accountId: $accountId, ruleType: $ruleType, threshold: $threshold,
      action: $action, severity: $severity) {
    success error rule { id ruleType threshold action severity enabled }
  }
}
```

#### updateRiskRule

Partially update a risk rule (threshold, enabled, action, severity).

```graphql
mutation UpdateRiskRule($ruleId: UUID!, $threshold: Decimal, $enabled: Boolean,
    $action: String, $severity: String) {
  updateRiskRule(ruleId: $ruleId, threshold: $threshold, enabled: $enabled,
      action: $action, severity: $severity) {
    success error rule { id ruleType threshold action severity enabled }
  }
}
```

#### runScenario

Simulate a what-if scenario.

```graphql
mutation RunScenario($accountId: UUID!, $prices: [PriceInputInput!]!,
    $priceChangePercent: Decimal, $ivChangePercent: Decimal, $timeDaysForward: Int) {
  runScenario(accountId: $accountId, prices: $prices,
      priceChangePercent: $priceChangePercent, ivChangePercent: $ivChangePercent,
      timeDaysForward: $timeDaysForward) {
    currentEquity scenarioEquity pnLImpact pnLImpactPercent
    positions { symbol currentValue scenarioValue pnLImpact }
  }
}
```

#### autoFixPortfolio

Rebuild positions from trade log to fix any drift.

```graphql
mutation AutoFix($accountId: UUID!) {
  autoFixPortfolio(accountId: $accountId) {
    success error message
  }
}
```

#### linkTradeToStrategy

```graphql
mutation LinkTrade($tradeId: UUID!, $strategyExecutionId: Int!) {
  linkTradeToStrategy(tradeId: $tradeId, strategyExecutionId: $strategyExecutionId) {
    success error link { id tradeId strategyExecutionId }
  }
}
```

#### importBacktestTrades

Import trades from a strategy backtest into a portfolio account.

```graphql
mutation ImportTrades($strategyExecutionId: Int!, $accountId: UUID!) {
  importBacktestTrades(strategyExecutionId: $strategyExecutionId, accountId: $accountId) {
    success error tradeCount message
  }
}
```

### Complete Workflow Example

```graphql
# Step 1: Create an account
mutation { createAccount(name: "Paper Trading", type: "Paper", initialCash: 100000) {
  success account { id }
}}
# Returns: account.id = "abc-123"

# Step 2: Record some trades
mutation { recordTrade(accountId: "abc-123", tickerId: 1, side: "Buy",
    quantity: 100, price: 150, fees: 5, assetType: "Stock", multiplier: 1) {
  success trade { id }
}}

mutation { recordTrade(accountId: "abc-123", tickerId: 1, side: "Sell",
    quantity: 50, price: 170, fees: 5, assetType: "Stock", multiplier: 1) {
  success trade { id }
}}

# Step 3: Take a snapshot
mutation { takePortfolioSnapshot(accountId: "abc-123") {
  success snapshot { equity cash marketValue }
}}

# Step 4: Create a risk rule
mutation { createRiskRule(accountId: "abc-123", ruleType: "MaxDrawdown",
    threshold: 0.10, action: "Warn", severity: "High") {
  success rule { id }
}}

# Step 5: Evaluate risk rules
query { evaluateRiskRules(accountId: "abc-123", prices: [{ symbol: "AAPL", price: 165 }]) {
  ruleType severity threshold actualValue message
}}

# Step 6: Run a scenario
mutation { runScenario(accountId: "abc-123",
    prices: [{ symbol: "AAPL", price: 165 }],
    priceChangePercent: -0.20) {
  currentEquity scenarioEquity pnLImpact pnLImpactPercent
}}

# Step 7: Import backtest trades
mutation { importBacktestTrades(strategyExecutionId: 42, accountId: "abc-123") {
  success tradeCount message
}}

# Step 8: View strategy attribution
query { getAlphaAttribution(accountId: "abc-123") {
  strategyName pnL contributionPercent tradeCount
}}
```

---

## 5. Frontend

### Component Tree and Navigation

```
PortfolioComponent (root)
├── Account selector dropdown + "New Account" button
├── Create account form (toggle)
└── PrimeNG Tabs
    ├── Tab 0: DashboardComponent
    ├── Tab 1: PositionsComponent
    ├── Tab 2: EquityChartComponent
    ├── Tab 3: RiskPanelComponent
    ├── Tab 4: ScenarioExplorerComponent
    ├── Tab 5: ReconciliationComponent
    └── Tab 6: StrategyAttributionComponent
```

All child components receive `accountId` as a required input and reload their data whenever the account changes (via Angular `effect()`).

### Tab Descriptions

#### Tab 0: Dashboard

**Purpose**: Overview of the portfolio -- account info, open positions, key metrics, and a quick trade form.

**Features**:
- Displays account cash, open position count, and key metrics (total return %, Sharpe, max drawdown) if snapshots exist.
- Shows a summary table of open positions with ticker symbol, quantity, cost basis, and realized PnL.
- Lists recent trades.
- Provides a "Take Snapshot" button that captures current portfolio state.
- Includes a trade recording form with fields for ticker ID, side (Buy/Sell), quantity, price, and fees.

**How to use**:
1. Select an account from the dropdown at the top of the page.
2. Review the summary cards showing cash balance and metrics.
3. To record a trade: fill in the ticker ID, select Buy or Sell, enter quantity and price, then click "Record Trade".
4. Click "Take Snapshot" to capture a point-in-time record for the equity curve.

#### Tab 1: Positions

**Purpose**: Detailed view of all positions with lot-level drill-down.

**Features**:
- Table of positions showing symbol, asset type, quantity, average cost basis, realized PnL, and status.
- Toggle to show/hide closed positions.
- Expandable rows to view individual lots (entry price, remaining quantity, lot-level PnL, open/close dates).
- "Rebuild Positions" button to recompute all positions from the trade log.

**How to use**:
1. Review the positions table. Open positions are shown by default.
2. Toggle "Show Closed" to include fully closed positions.
3. Click a position row to expand it and see the underlying FIFO lots.
4. If positions look incorrect, click "Rebuild Positions" to recompute from the trade log.

#### Tab 2: Equity Chart

**Purpose**: Visual equity curve and drawdown chart with performance metrics.

**Features**:
- Area chart showing equity over time (built with TradingView lightweight-charts v5).
- Histogram chart showing drawdown percentage at each snapshot point. Drawdowns > 5% are colored red; smaller drawdowns are orange.
- Performance metrics display: total return %, Sharpe ratio, Sortino ratio, Calmar ratio, max drawdown, win rate, profit factor.
- Charts auto-resize with the container.

**How to use**:
1. Navigate to the Equity Curve tab. Charts load automatically if snapshots exist.
2. If the chart is empty, go back to Dashboard and take some snapshots first.
3. Review the metrics panel alongside the charts.

#### Tab 3: Risk Engine

**Purpose**: Manage risk rules and evaluate portfolio risk exposure.

**Features**:
- List of configured risk rules with type, threshold, action, severity, and enabled/disabled toggle.
- Form to create new rules (rule type, threshold, action, severity).
- "Evaluate Rules" button to check all enabled rules against current state and display violations.
- Dollar delta display showing per-position delta exposure.

**How to use**:
1. Create a risk rule: select a rule type (e.g., MaxDrawdown), set a threshold (e.g., 0.10 for 10%), choose an action (Warn or Block) and severity, then click Create.
2. Toggle rules on/off using the enable switch on each rule row.
3. Click "Evaluate Rules" to check for violations. Any breached rules appear in a violations list with severity and details.
4. Click "Load Delta" to see per-position dollar delta exposure.

#### Tab 4: Scenario Explorer

**Purpose**: What-if analysis for market shocks, volatility changes, and theta decay.

**Features**:
- Input fields for price change %, IV change %, and theta decay (days forward).
- Preset scenarios: Market Crash (-20%), Correction (-10%), Rally (+10%), Vol Spike (IV +20%), Theta Decay (5 days), Theta Decay (30 days).
- Results showing current equity, scenario equity, dollar PnL impact, and percentage impact.
- Per-position breakdown of scenario impact.

**How to use**:
1. Click a preset button to populate the scenario inputs, or enter custom values.
2. Click "Run Scenario" to simulate.
3. Review the results: the impact summary shows how much equity would change, and the position table shows each position's contribution to the PnL impact.

#### Tab 5: Reconciliation

**Purpose**: Verify that cached position data matches the trade log, and fix any discrepancies.

**Features**:
- "Run Reconciliation" button that compares cached positions against rebuilt positions.
- Report showing whether drift was detected, with counts of cached vs. rebuilt positions.
- Per-symbol drift details: cached quantity vs. rebuilt quantity, cached PnL vs. rebuilt PnL, drift type.
- "Auto Fix" button that rebuilds all positions from the trade log.

**How to use**:
1. Click "Run Reconciliation" to check for drift.
2. If the report shows `hasDrift: true`, review the drifts table to understand which positions diverged.
3. Click "Auto Fix" to rebuild positions from the trade log. The reconciliation re-runs automatically after fixing to confirm the drift is resolved.

#### Tab 6: Strategy Attribution

**Purpose**: Link portfolio trades to automated strategies and analyze per-strategy PnL.

**Features**:
- List of strategy allocations showing strategy name, capital allocated, and date range.
- Alpha attribution table showing each strategy's PnL, trade count, and contribution percentage (with bar visualization).
- Import form to bring backtest trades into the portfolio from a strategy execution ID.
- Click-to-view strategy PnL detail (total PnL, trade count, win rate).

**How to use**:
1. View existing strategy allocations in the allocations table.
2. Click "Load Attribution" to see PnL breakdown by strategy.
3. Click a strategy row to view its detailed PnL (total PnL, trade count, win rate).
4. To import backtest trades: enter the strategy execution ID and click "Import Trades". The trades are recorded in the portfolio and linked to the strategy.

---

## 6. Testing

### Backend Test Coverage

All backend tests use **xUnit** with **Moq** for mocking and an in-memory EF Core database (`TestDbContextFactory`). Tests are located in `Backend.Tests/Unit/Services/`.

#### PositionEngineTests (5 tests)

| Test | What It Verifies |
|------|------------------|
| `ApplyTrade_SingleBuy_CreatesPositionAndLot` | A buy creates a position with correct quantity, cost basis, and one lot |
| `ApplyTrade_TwoBuysOneSell_FifoClosesFirstLot` | FIFO order: selling closes the oldest lot first, PnL = (170-150)*100 = $2000 |
| `ApplyTrade_FullClose_PositionStatusClosed` | Selling all shares sets status to `Closed` with correct PnL |
| `CloseLotsFifo_PartialClose_SplitsLot` | Selling fewer shares than a lot reduces `RemainingQuantity` without closing the lot |
| `ApplyTrade_OptionTrade_MultiplierApplied` | Option PnL includes multiplier: (8-5)*1*100 = $300 |
| `CalculateRealizedPnL_MultipleLots_SumsCorrectly` | Sums PnL across multiple lots: 500 + (-200) + 1000 = $1300 |
| `RebuildPositions_ReplaysTrades_MatchesIncrementalState` | Rebuilding from scratch produces the same state as incremental application |

#### PortfolioServiceTests (7 tests)

| Test | What It Verifies |
|------|------------------|
| `CreateAccount_ReturnsAccountWithCorrectCash` | Account creation sets both Cash and InitialCash |
| `SubmitOrder_CreatesOrderWithPendingStatus` | New orders start as Pending |
| `CancelOrder_SetsCancelledStatus` | Cancellation sets status to Cancelled |
| `CancelOrder_AlreadyFilled_Throws` | Cannot cancel a filled order |
| `FillOrder_Buy_DeductsCashCorrectly` | Buy: cash = 100k - (150*100) - 10 = $84,990 |
| `FillOrder_Sell_AddsCashCorrectly` | Sell: cash = 100k - 15k + 17k - 10 = $101,990 |
| `RecordTrade_CreatesTradeAndPosition` | RecordTrade creates trade with correct price and quantity |
| `RecordTrade_OptionWithGreeks_CreatesOptionLeg` | OptionLeg is created with correct Greeks |
| `GetPortfolioState_ReturnsAccountAndPositions` | State includes account and open positions |

#### PortfolioValuationServiceTests (5 tests)

| Test | What It Verifies |
|------|------------------|
| `ComputeValuation_StockPosition_PriceTimesQuantity` | MarketValue = 175 * 100 = $17,500 |
| `ComputeValuation_OptionPosition_MultiplierApplied` | MarketValue = 5.50 * 10 * 100 = $5,500 |
| `ComputeValuation_UnrealizedPnL_CorrectDelta` | UnrealizedPnL = (175-150) * 100 = $2,500 |
| `ComputeValuation_EquityCashPlusMarketValue` | Equity = 50,000 + 17,500 = $67,500 |
| `ComputeValuation_NoPositions_EquityEqualsCash` | Empty portfolio: equity = cash |
| `ComputeValuation_MultiplePositions_AggregatesCorrectly` | Sums across AAPL + MSFT positions |

#### PortfolioRiskServiceTests (6 tests)

| Test | What It Verifies |
|------|------------------|
| `ComputeDollarDelta_Stock_DeltaIsOne` | Stock delta = 1, DollarDelta = 500 * 100 = $50,000 |
| `ComputeDollarDelta_Option_UsesLatestLegDelta` | Uses OptionLeg delta: 0.65 * 505 * 10 * 100 = $328,250 |
| `EvaluateRiskRules_MaxDrawdownExceeded_ReturnsViolation` | 15% drawdown exceeds 10% threshold, violation returned |
| `EvaluateRiskRules_DisabledRule_IsSkipped` | Disabled rules produce no violations |
| `RunScenario_PriceDown10Percent_EquityDrops` | -10% price shock: equity drops from $100k to $95k |
| `RunScenario_IVUp_OptionValueIncreasesViaVega` | IV increase adds value via vega, PnL impact > 0 |
| `ComputePortfolioVega_AggregatesAcrossPositions` | Vega = 0.15 * 10 * 100 = 150 |

#### PortfolioReconciliationServiceTests (2 tests)

| Test | What It Verifies |
|------|------------------|
| `Reconcile_ConsistentPositions_NoDrift` | Correctly built positions show no drift |
| `Reconcile_ManuallyAlteredPosition_DetectsDrift` | Tampered position (qty=999 vs actual 100) detected as drift |

### Frontend Test Coverage

#### portfolio.service.spec.ts (7 tests)

| Test | What It Verifies |
|------|------------------|
| `getAccounts` | Sends correct GraphQL query, maps response array |
| `createAccount` | Sends mutation with name/type/initialCash variables |
| `getPortfolioState` | Sends query with accountId, maps nested response |
| `recordTrade` | Sends mutation with all trade parameters |
| `takeSnapshot` | Sends snapshot mutation, returns result |
| `getRiskRules` | Sends risk rules query for account |
| GraphQL error handling | Throws on response.errors presence |

#### portfolio.component.spec.ts (12 tests)

| Test | What It Verifies |
|------|------------------|
| Component creation | Renders without errors |
| Load accounts | Auto-loads accounts on init |
| Auto-select first | First account selected by default |
| Account selector | Options rendered with correct labels |
| Toggle create form | Button toggles form visibility |
| Button text toggle | Shows "Cancel" when form is open |
| Create account | Mutation called, account added to list |
| Empty name guard | No mutation when name is blank |
| Error display | Error from mutation shown in component |
| Tab rendering | 7 tabs render when account selected |
| Empty state | Shows empty message when no accounts |
| Error banner dismiss | Error dismissed on click |

#### dashboard.component.spec.ts (4 tests)

| Test | What It Verifies |
|------|------------------|
| Component creation | Renders without errors |
| Summary cards | Displays cash, initial capital values |
| Take snapshot | Button triggers service call |
| Recent trades | Trade table renders trade data |

### How to Run Tests

**Backend tests** (from repository root):

```bash
cd Backend.Tests
dotnet test
```

Or to run only portfolio-related tests:

```bash
dotnet test --filter "FullyQualifiedName~Portfolio|FullyQualifiedName~PositionEngine"
```

**Frontend tests** (from `Frontend/` directory):

```bash
npm test
```

Or using the Angular CLI:

```bash
npx ng test
```
