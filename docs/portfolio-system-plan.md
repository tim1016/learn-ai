# Portfolio System — Implementation Plan

## Architecture Flow

```
Trades (event log)
      |
Position Engine (FIFO lot tracking)
      |
Portfolio Valuation Engine (prices -> market value)
      |
Snapshot Engine (point-in-time capture)
      |
Risk Engine (Greeks aggregation, rules)
      |
Analytics + UI
```

---

## Phase 1 — Core Schema + Position Engine + FIFO Lots

### Entities (7 total)

**Account**

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| Name | string | e.g. "Paper Trading 1" |
| Type | enum | Paper, Backtest |
| BaseCurrency | string | "USD" |
| InitialCash | decimal | Starting capital |
| Cash | decimal | Current available cash |
| CreatedAt | DateTime | |

**Order**

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| AccountId | Guid | FK -> Account |
| TickerId | int | FK -> Ticker |
| Side | enum | Buy, Sell |
| OrderType | enum | Market, Limit, Stop |
| Quantity | decimal | |
| LimitPrice | decimal? | null for Market |
| Status | enum | Pending, Filled, PartiallyFilled, Cancelled |
| AssetType | enum | Stock, Option |
| OptionContractId | Guid? | FK -> OptionContract (if option) |
| SubmittedAt | DateTime | |
| FilledAt | DateTime? | |

**PortfolioTrade** (separate from market data Trade)

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| AccountId | Guid | FK -> Account |
| OrderId | Guid | FK -> Order |
| TickerId | int | FK -> Ticker |
| Side | enum | Buy, Sell |
| Quantity | decimal | |
| Price | decimal(18,8) | |
| Fees | decimal | |
| AssetType | enum | Stock, Option |
| OptionContractId | Guid? | FK -> OptionContract |
| Multiplier | int | 1 for stocks, 100 for options |
| ExecutionTimestamp | DateTime | |

**Position**

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| AccountId | Guid | FK -> Account |
| TickerId | int | FK -> Ticker |
| AssetType | enum | Stock, Option |
| OptionContractId | Guid? | FK -> OptionContract |
| NetQuantity | decimal | Derived from lots |
| AvgCostBasis | decimal(18,8) | Weighted from open lots |
| RealizedPnL | decimal(18,8) | Sum of closed lot PnL |
| Status | enum | Open, Closed |
| OpenedAt | DateTime | First trade timestamp |
| ClosedAt | DateTime? | When NetQuantity hits 0 |
| LastUpdated | DateTime | |

**PositionLot** (FIFO tracking)

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| PositionId | Guid | FK -> Position |
| TradeId | Guid | FK -> PortfolioTrade (entry trade) |
| Quantity | decimal | Original lot size |
| EntryPrice | decimal(18,8) | |
| RemainingQuantity | decimal | Decremented on closing |
| RealizedPnL | decimal(18,8) | PnL when lot closed |
| OpenedAt | DateTime | |
| ClosedAt | DateTime? | null while open |

**OptionContract**

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| UnderlyingTickerId | int | FK -> Ticker |
| Symbol | string | e.g. "O:AAPL250620C00150000" |
| Strike | decimal(18,8) | |
| Expiration | DateOnly | |
| OptionType | enum | Call, Put |
| Multiplier | int | Usually 100 |

**OptionLeg** (entry Greeks snapshot)

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| TradeId | Guid | FK -> PortfolioTrade |
| OptionContractId | Guid | FK -> OptionContract |
| Quantity | decimal | |
| EntryIV | decimal? | |
| EntryDelta | decimal? | |
| EntryGamma | decimal? | |
| EntryTheta | decimal? | |
| EntryVega | decimal? | |

### Services (2)

**IPositionEngine**

- RebuildPositionsAsync(accountId) — replay all trades -> rebuild lots + positions
- ApplyTradeAsync(trade) — process single trade, create/update lots (FIFO)
- CloseLotsFifo(position, trade) — close oldest lots first
- CalculateRealizedPnL(lots) — sum closed lot PnL

**IPortfolioService**

- CreateAccountAsync(name, type, initialCash)
- SubmitOrderAsync(accountId, orderDto)
- CancelOrderAsync(orderId)
- FillOrderAsync(orderId, fillPrice, fillQuantity, fees) — creates Trade, calls PositionEngine
- RecordTradeAsync(tradeDto) — direct trade entry (for backtests)
- GetPortfolioStateAsync(accountId) — account + positions + cash

### GraphQL

**Queries:** getAccounts, getAccount(id), getPositions(accountId), getPosition(id), getPortfolioTrades(accountId), getPositionLots(positionId)

**Mutations:** createAccount, submitOrder, cancelOrder, fillOrder, recordTrade, rebuildPositions(accountId)

### Phase 1 Tests

| Test | Scenario | Assertion |
|------|----------|-----------|
| ApplyTrade_SingleBuy_CreatesPositionAndLot | Buy 100 AAPL @ 150 | Position.NetQuantity = 100, 1 open lot |
| ApplyTrade_TwoBuysOneSell_FifoClosesFirstLot | Buy 100@150, Buy 100@160, Sell 100@170 | Lot1 closed, RealizedPnL = +2000, Lot2 open |
| ApplyTrade_FullClose_PositionStatusClosed | Buy 100@150, Sell 100@170 | Position.Status = Closed, ClosedAt set |
| ApplyTrade_OptionTrade_MultiplierApplied | Buy 1 AAPL Call @ 5.00, Multiplier=100 | Position cost basis reflects 500 total |
| RebuildPositions_ReplaysTrades_MatchesIncrementalState | Apply 10 trades, then rebuild | Both produce identical state |
| CloseLotsFifo_PartialClose_SplitsLot | Buy 100@150, Sell 50@170 | Lot1.RemainingQuantity = 50, PnL = +1000 |
| CalculateRealizedPnL_MultipleLots_SumsCorrectly | 3 lots, various close prices | Sum matches expected |
| FillOrder_UpdatesCash_DeductsCostAndFees | Account 100k, buy 100@150, fees=10 | Cash = 84990 |
| RecordTrade_OptionWithGreeks_CreatesOptionLeg | Option trade with entry Greeks | OptionLeg populated |
| Reconcile_RebuildVsCached_DetectsDrift | Manually alter Position, rebuild | Detects mismatch |

---

## Phase 2 — Portfolio Valuation + Snapshots + Equity Curve

### Entities (1)

**PortfolioSnapshot**

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| AccountId | Guid | FK -> Account |
| Timestamp | DateTime | |
| Equity | decimal(18,8) | Cash + MarketValue |
| Cash | decimal(18,8) | |
| MarketValue | decimal(18,8) | Sum of position market values |
| MarginUsed | decimal(18,8) | |
| UnrealizedPnL | decimal(18,8) | |
| RealizedPnL | decimal(18,8) | Cumulative |
| NetDelta | decimal? | |
| NetGamma | decimal? | |
| NetTheta | decimal? | |
| NetVega | decimal? | |

### Services (2)

**IPortfolioValuationService**

- ComputeMarketValueAsync(accountId) — positions x current prices (via PolygonService)
- ComputeUnrealizedPnLAsync(accountId) — market value - cost basis (multiplier-aware)
- ComputeEquityAsync(accountId) — cash + market value
- ComputePortfolioGreeksAsync(accountId) — sum Greeks across option positions

**ISnapshotService**

- TakeSnapshotAsync(accountId) — calls valuation, persists snapshot
- GetEquityCurveAsync(accountId, from, to) — returns snapshot time series
- GetDrawdownSeriesAsync(accountId) — peak-to-trough from snapshots
- ComputeMetricsAsync(accountId) — Sharpe, Sortino, Calmar, MaxDrawdown, WinRate

Snapshot triggers: on trade execution, on manual refresh, on price update

### Phase 2 Tests

| Test | Scenario | Assertion |
|------|----------|-----------|
| ComputeMarketValue_StockPositions_PriceTimesQuantity | 100 AAPL, price=175 | MarketValue = 17500 |
| ComputeMarketValue_OptionPosition_MultiplierApplied | 10 calls, price=5.50, mult=100 | MarketValue = 5500 |
| ComputeUnrealizedPnL_OpenLots_CorrectDelta | Bought 100@150, current=175 | UnrealizedPnL = +2500 |
| ComputeEquity_CashPlusMarketValue | Cash=50k, MarketValue=30k | Equity = 80000 |
| TakeSnapshot_PersistsAllFields | Known values | All fields match |
| GetEquityCurve_ReturnsOrderedSeries | 10 snapshots | Ordered by timestamp |
| ComputeMetrics_SharpeRatio_KnownSeries | Known daily returns | Sharpe matches hand-calc |
| ComputeMetrics_MaxDrawdown_KnownSeries | Equity: 100, 110, 90, 105 | MaxDrawdown = 18.18% |
| ComputePortfolioGreeks_AggregatesAcrossPositions | 3 option positions | Sum matches expected |

---

## Phase 3 — Risk Engine + Stress Testing

### Entities (1)

**RiskRule**

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| AccountId | Guid | FK -> Account |
| RuleType | enum | MaxDrawdown, MaxPositionSize, MaxVegaExposure, MaxDelta |
| Threshold | decimal | e.g. 0.10 for 10% |
| Action | enum | Warn, Block |
| Severity | enum | Low, Medium, High, Critical |
| Enabled | bool | |
| LastTriggered | DateTime? | |

### Services (2)

**IPortfolioRiskService**

- ComputeDollarDeltaAsync(accountId) — Delta x price x multiplier per position
- ComputePortfolioVegaAsync(accountId) — total vega exposure
- EvaluateRiskRulesAsync(accountId) — check all rules, return violations
- RunScenarioAsync(accountId, scenarios) — stress test: SPY +/-5/10%, IV +/-20%, time +5d

**IPortfolioReconciliationService**

- ReconcileAsync(accountId) — rebuild from trades, compare to cached positions
- GetDriftReportAsync(accountId) — list mismatches
- AutoFixAsync(accountId) — overwrite cached state with rebuilt state

### Phase 3 Tests

| Test | Scenario | Assertion |
|------|----------|-----------|
| DollarDelta_Stock_PriceTimesQuantity | 100 AAPL, delta=1, price=175 | DollarDelta = 17500 |
| DollarDelta_Option_IncludesMultiplier | 10 calls, delta=0.5, price=175, mult=100 | DollarDelta = 87500 |
| EvaluateRules_MaxDrawdown_Triggers | Drawdown=12%, threshold=10% | Violation returned |
| EvaluateRules_DisabledRule_Skipped | Enabled=false | No violation |
| RunScenario_SpyDown10_ComputesImpact | SPY positions | PnL impact calculated |
| RunScenario_IvUp20_VegaImpact | Option positions with known vega | Impact = totalVega x 0.20 |
| Reconcile_DriftDetected_ReportsCorrectly | Cached qty=100, rebuilt qty=90 | Drift report shows mismatch |
| Reconcile_NoDrift_CleanReport | Cached matches rebuilt | Empty drift report |

---

## Phase 4 — Strategy Attribution

### Entities (2)

**StrategyAllocation**

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| AccountId | Guid | FK -> Account |
| StrategyExecutionId | int | FK -> StrategyExecution |
| CapitalAllocated | decimal | |
| StartDate | DateTime | |
| EndDate | DateTime? | |

**StrategyTradeLink**

| Field | Type | Notes |
|-------|------|-------|
| Id | Guid | PK |
| TradeId | Guid | FK -> PortfolioTrade |
| StrategyExecutionId | int | FK -> StrategyExecution |

### Services (1)

**IStrategyAttributionService**

- LinkTradeToStrategyAsync(tradeId, strategyId)
- ImportBacktestTradesAsync(strategyExecutionId, accountId) — converts BacktestTrades -> portfolio Trades
- GetStrategyPnLAsync(strategyId) — PnL from linked trades
- GetAlphaAttributionAsync(accountId) — per-strategy contribution

### Phase 4 Tests

| Test | Scenario | Assertion |
|------|----------|-----------|
| ImportBacktestTrades_CreatesPortfolioTrades | 5 BacktestTrades | 5 portfolio Trades created, linked |
| GetStrategyPnL_SumsLinkedTrades | 3 linked trades | PnL = sum |
| GetAlphaAttribution_MultipleStrategies_SplitsCorrectly | 2 strategies, known PnL | Attribution correct |

---

## File Structure

```
Backend/
  Models/
    Portfolio/
      Account.cs
      Order.cs
      PortfolioTrade.cs
      Position.cs
      PositionLot.cs
      OptionContract.cs
      OptionLeg.cs
      PortfolioSnapshot.cs
      RiskRule.cs
      StrategyAllocation.cs
      StrategyTradeLink.cs
      Enums.cs
  Services/
    Interfaces/
      IPositionEngine.cs
      IPortfolioService.cs
      IPortfolioValuationService.cs
      ISnapshotService.cs
      IPortfolioRiskService.cs
      IPortfolioReconciliationService.cs
      IStrategyAttributionService.cs
    Implementation/
      PositionEngine.cs
      PortfolioService.cs
      PortfolioValuationService.cs
      SnapshotService.cs
      PortfolioRiskService.cs
      PortfolioReconciliationService.cs
      StrategyAttributionService.cs
  GraphQL/
    PortfolioQuery.cs
    PortfolioMutation.cs
```

## Build Order

1. Phase 1 -> entities + DbContext + PositionEngine + PortfolioService + tests -> validates foundation
2. Phase 2 -> valuation + snapshots -> enables equity curves and metrics
3. Phase 3 -> risk + reconciliation -> makes it robust
4. Phase 4 -> strategy links -> connects to existing backtest system
