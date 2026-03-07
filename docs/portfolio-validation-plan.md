# Portfolio System Validation Plan

## Purpose

This document defines **10 core tests** (plus 2 bonus tests) that validate the correctness, determinism, and reliability of the Portfolio Management System. Each test follows a consistent structure to ensure results are **repeatable and auditable**.

### Test Structure

Every test uses the same format:

| Section             | Description                                      |
|---------------------|--------------------------------------------------|
| **Objective**       | What the test proves                             |
| **Setup**           | Initial state and input data                     |
| **Execution**       | API calls or methods invoked                     |
| **Expected Result** | Precise values the system must produce           |
| **Validation**      | Assertion strategy and tolerance                 |

---

## Test 1 — FIFO Accounting Correctness

### Objective

Verify that the FIFO lot engine produces correct realized PnL and lot closures when selling across multiple lots.

### Setup

| Parameter     | Value    |
|---------------|----------|
| Initial Cash  | $100,000 |

**Trade sequence:**

| # | Side | Qty | Ticker | Price |
|---|------|-----|--------|-------|
| 1 | BUY  | 100 | AAPL   | $150  |
| 2 | BUY  |  50 | AAPL   | $155  |
| 3 | SELL | 120 | AAPL   | $160  |

### Execution

```graphql
mutation { recordTrade(input: { ... }) { tradeId } }
query  { getPositions(accountId: "...") { ... } }
query  { getPositionLots(positionId: "...") { ... } }
```

### Expected Result

**Lot closures (FIFO order):**

| Lot | Opened Qty | Closed Qty | Remaining | Cost Basis | Sale Price | Lot PnL |
|-----|-----------|------------|-----------|------------|------------|---------|
| A   | 100       | 100        | 0         | $150       | $160       | $1,000  |
| B   | 50        | 20         | 30        | $155       | $160       | $100    |

**Aggregated position state:**

| Field          | Expected Value |
|----------------|----------------|
| NetQuantity    | 30             |
| AvgCostBasis   | $155.00        |
| RealizedPnL    | $1,100.00      |
| Status         | Open           |

**PnL breakdown:**

```
Lot A: (160 - 150) * 100 = $1,000
Lot B: (160 - 155) *  20 =   $100
                           -------
Total RealizedPnL         = $1,100
```

### Validation

```csharp
Assert.Equal(30, position.NetQuantity);
Assert.Equal(155.00m, position.AvgCostBasis);
Assert.True(Math.Abs(1100.00m - position.RealizedPnL) < 0.01m);
Assert.Equal(PositionStatus.Open, position.Status);
```

---

## Test 2 — Position Rebuild Determinism

### Objective

Verify the event-sourcing guarantee: positions must rebuild **identically** from the trade log at any point in time.

> **This is the single most important test.** If the system can always rebuild correctly from trades, every other bug becomes recoverable. That is the power of the event-sourced design.

### Setup

Execute a sequence of **~20 mixed trades** across multiple tickers and sides:

| # | Side | Qty | Ticker | Price |
|---|------|-----|--------|-------|
| 1  | BUY  | 100 | AAPL   | $150  |
| 2  | BUY  | 200 | SPY    | $500  |
| 3  | SELL |  50 | AAPL   | $155  |
| 4  | BUY  |  75 | MSFT   | $400  |
| 5  | SELL | 100 | SPY    | $505  |
| 6  | BUY  |  30 | AAPL   | $148  |
| 7  | SELL |  80 | AAPL   | $160  |
| 8  | BUY  |  50 | NVDA   | $900  |
| 9  | SELL |  75 | MSFT   | $410  |
| 10 | SELL | 100 | SPY    | $510  |
| 11 | BUY  | 120 | TSLA   | $250  |
| 12 | SELL |  50 | NVDA   | $920  |
| 13 | BUY  |  60 | AAPL   | $145  |
| 14 | SELL |  60 | TSLA   | $260  |
| 15 | BUY  |  40 | SPY    | $515  |
| 16 | SELL |  60 | TSLA   | $255  |
| 17 | BUY  |  25 | NVDA   | $910  |
| 18 | SELL |  60 | AAPL   | $158  |
| 19 | BUY  | 100 | AMZN   | $180  |
| 20 | SELL |  40 | SPY    | $520  |

### Execution

```graphql
# Step 1 — Capture current state
query { getPositions(accountId: "test-acct") { ticker netQuantity avgCostBasis realizedPnL status } }

# Step 2 — Rebuild from trade log
mutation { rebuildPositions(accountId: "test-acct") { success } }

# Step 3 — Capture rebuilt state
query { getPositions(accountId: "test-acct") { ticker netQuantity avgCostBasis realizedPnL status } }
```

### Expected Result

All values must match **exactly** between pre-rebuild and post-rebuild snapshots:

| Field        | Must Match |
|--------------|------------|
| NetQuantity  | Exact      |
| AvgCostBasis | Exact      |
| RealizedPnL  | Exact      |
| Status       | Exact      |

### Validation

```csharp
var report = await reconciliationService.ReconcileAsync(accountId);

Assert.Equal(0, report.DriftCount);
Assert.True(report.IsConsistent);

// Per-position deep comparison
foreach (var pair in report.PositionPairs)
{
    Assert.Equal(pair.Before.NetQuantity,  pair.After.NetQuantity);
    Assert.Equal(pair.Before.AvgCostBasis, pair.After.AvgCostBasis);
    Assert.Equal(pair.Before.RealizedPnL,  pair.After.RealizedPnL);
    Assert.Equal(pair.Before.Status,       pair.After.Status);
}
```

---

## Test 3 — Cash Accounting Integrity

### Objective

Ensure cash balances are always correct after every trade, including fee deductions.

### Setup

| Parameter     | Value    |
|---------------|----------|
| Initial Cash  | $100,000 |
| Fee per trade | $5       |

**Trade sequence:**

| # | Side | Qty | Ticker | Price |
|---|------|-----|--------|-------|
| 1 | BUY  | 100 | SPY    | $500  |
| 2 | SELL | 100 | SPY    | $510  |

### Execution

```graphql
mutation { recordTrade(input: { side: BUY,  ticker: "SPY", quantity: 100, price: 500, fee: 5 }) { tradeId } }
mutation { recordTrade(input: { side: SELL, ticker: "SPY", quantity: 100, price: 510, fee: 5 }) { tradeId } }
query   { getAccount(accountId: "...") { cash } }
```

### Expected Result

```
Buy cost   = 100 * 500       = $50,000
Buy fee    =                     $5
Cash after buy  = 100,000 - 50,000 - 5 = $49,995

Sell value = 100 * 510       = $51,000
Sell fee   =                     $5
Cash after sell = 49,995 + 51,000 - 5  = $100,990

Realized PnL = (510 - 500) * 100 = $1,000
Total fees   = $10
Net PnL      = $1,000 - $10 = $990
```

| Checkpoint       | Expected Cash |
|------------------|---------------|
| After BUY        | $49,995       |
| After SELL       | $100,990      |

### Validation

```csharp
Assert.Equal(49_995.00m, accountAfterBuy.Cash);
Assert.Equal(100_990.00m, accountAfterSell.Cash);
```

---

## Test 4 — Unrealized PnL Valuation

### Objective

Verify mark-to-market calculation for open positions.

### Setup

| Parameter     | Value    |
|---------------|----------|
| Initial Cash  | $100,000 |

**Trade:**

| Side | Qty | Ticker | Entry Price |
|------|-----|--------|-------------|
| BUY  | 100 | NVDA   | $900        |

**Current market price:** $950

### Execution

```graphql
mutation { recordTrade(input: { side: BUY, ticker: "NVDA", quantity: 100, price: 900 }) { tradeId } }

# Trigger valuation with current price = 950
query { getValuation(accountId: "...") { marketValue unrealizedPnL equity } }
```

### Expected Result

```
CostBasis      = 100 * 900 = $90,000
MarketValue    = 100 * 950 = $95,000
UnrealizedPnL  = 95,000 - 90,000 = $5,000
Cash           = 100,000 - 90,000 = $10,000
Equity         = Cash + MarketValue = $105,000
```

| Field          | Expected Value |
|----------------|----------------|
| MarketValue    | $95,000        |
| UnrealizedPnL  | $5,000         |
| Equity         | $105,000       |

### Validation

```csharp
var valuation = await portfolioService.ComputeValuationWithPricesAsync(accountId, prices);

Assert.True(Math.Abs(95_000m - valuation.MarketValue)   < 0.01m);
Assert.True(Math.Abs(5_000m  - valuation.UnrealizedPnL) < 0.01m);
Assert.True(Math.Abs(105_000m - valuation.Equity)       < 0.01m);
```

---

## Test 5 — Portfolio Greeks Aggregation

### Objective

Verify that option Greeks are correctly aggregated across all positions, accounting for direction and contract multiplier.

### Setup

| Position | Side  | Qty | Type | Delta per Contract |
|----------|-------|-----|------|--------------------|
| AAPL     | Long  | 10  | Call | +0.60              |
| SPY      | Short |  5  | Put  | -0.40              |

**Contract multiplier:** 100

### Execution

```graphql
query { getDollarDelta(accountId: "...") { ticker delta netDelta } }
```

### Expected Result

```
Delta AAPL = 0.60 * 10 * 100       =  600
Delta SPY  = (-0.40) * (-5) * 100  =  200   (short put → positive delta)

Net Portfolio Delta = 600 + 200     =  800
```

| Field              | Expected Value |
|--------------------|----------------|
| AAPL Position Delta| 600            |
| SPY Position Delta | 200            |
| Net Portfolio Delta| 800            |

### Validation

```csharp
Assert.Equal(600m, greeks.First(g => g.Ticker == "AAPL").Delta);
Assert.Equal(200m, greeks.First(g => g.Ticker == "SPY").Delta);
Assert.Equal(800m, greeks.NetDelta);
```

---

## Test 6 — Snapshot Time-Series Stability

### Objective

Ensure snapshot generation produces consistent equity curves where system invariants hold at every point.

### Setup

Simulate a trading session with snapshots taken at regular intervals:

| Time  | Action                   | Cash     | MarketValue | Equity   |
|-------|--------------------------|----------|-------------|----------|
| T+0   | Initial state            | $100,000 | $0          | $100,000 |
| T+1   | BUY 100 SPY @ 500       | $50,000  | $50,000     | $100,000 |
| T+2   | SPY price moves to $510  | $50,000  | $51,000     | $101,000 |
| T+3   | SELL 50 SPY @ 510        | $75,500  | $25,500     | $101,000 |
| T+4   | SPY price moves to $520  | $75,500  | $26,000     | $101,500 |

### Execution

```graphql
mutation { takeSnapshot(accountId: "...") { snapshotId timestamp equity } }
query   { getEquityCurve(accountId: "...") { snapshots { timestamp equity cash marketValue realizedPnL } } }
```

### Expected Result

**Invariants that must hold at every snapshot:**

1. `Equity = Cash + MarketValue`
2. `RealizedPnL` is monotonically non-decreasing (no reversals)
3. Snapshots are in strict chronological order
4. No duplicate timestamps

### Validation

```csharp
var snapshots = await snapshotService.GetSnapshotsAsync(accountId);

for (int i = 0; i < snapshots.Count; i++)
{
    var s = snapshots[i];

    // Invariant 1: Equity = Cash + MarketValue
    Assert.True(Math.Abs(s.Equity - (s.Cash + s.MarketValue)) < 0.01m);

    if (i > 0)
    {
        var prev = snapshots[i - 1];

        // Invariant 2: RealizedPnL monotonically non-decreasing
        Assert.True(s.RealizedPnL >= prev.RealizedPnL);

        // Invariant 3: Chronological order
        Assert.True(s.Timestamp > prev.Timestamp);
    }
}
```

---

## Test 7 — Drawdown Calculation Correctness

### Objective

Verify that peak tracking, drawdown amount, and drawdown percentage are computed correctly.

### Setup

**Equity curve (from snapshots):**

| Snapshot | Equity   |
|----------|----------|
| S1       | $100,000 |
| S2       | $110,000 |
| S3       | $105,000 |
| S4       | $120,000 |
| S5       | $115,000 |

### Execution

```graphql
query { getDrawdownSeries(accountId: "...") { timestamp equity peak drawdown drawdownPct } }
```

### Expected Result

| Snapshot | Equity   | Peak     | Drawdown | Drawdown % |
|----------|----------|----------|----------|------------|
| S1       | $100,000 | $100,000 | $0       | 0.00%      |
| S2       | $110,000 | $110,000 | $0       | 0.00%      |
| S3       | $105,000 | $110,000 | $5,000   | 4.55%      |
| S4       | $120,000 | $120,000 | $0       | 0.00%      |
| S5       | $115,000 | $120,000 | $5,000   | 4.17%      |

**Summary:**

| Metric           | Expected Value |
|------------------|----------------|
| Max Drawdown     | $5,000         |
| Max Drawdown %   | 4.55%          |
| Current Drawdown | $5,000         |

### Validation

```csharp
var dd = await drawdownService.GetDrawdownSeriesAsync(accountId);

Assert.Equal(5_000m, dd.MaxDrawdown);
Assert.True(Math.Abs(4.55m - dd.MaxDrawdownPct) < 0.01m);
Assert.Equal(5_000m, dd.CurrentDrawdown);

// Verify per-point calculation
Assert.Equal(110_000m, dd.Series[2].Peak);
Assert.Equal(5_000m,   dd.Series[2].Drawdown);
```

---

## Test 8 — Risk Rule Triggering

### Objective

Confirm that risk limit violations are detected and reported correctly.

### Setup

**Risk rule configuration:**

| Rule             | Threshold |
|------------------|-----------|
| MaxPositionSize  | 30%       |
| MaxDrawdown      | 10%       |

**Portfolio state:**

| Position | MarketValue | Weight |
|----------|-------------|--------|
| SPY      | $60,000     | 60%    |
| AAPL     | $25,000     | 25%    |
| MSFT     | $15,000     | 15%    |

Total equity: $100,000

### Execution

```graphql
query { evaluateRiskRules(accountId: "...") { violations { ruleType triggered ticker currentValue threshold } } }
```

### Expected Result

| Rule            | Triggered | Ticker | Current | Threshold | Breach |
|-----------------|-----------|--------|---------|-----------|--------|
| MaxPositionSize | Yes       | SPY    | 60%     | 30%       | +30%   |
| MaxPositionSize | No        | AAPL   | 25%     | 30%       | —      |
| MaxPositionSize | No        | MSFT   | 15%     | 30%       | —      |

**Expected violations count:** 1

### Validation

```csharp
var result = await riskService.EvaluateRiskRulesAsync(accountId);

Assert.Single(result.Violations);

var violation = result.Violations.First();
Assert.Equal(RuleType.MaxPositionSize, violation.RuleType);
Assert.True(violation.Triggered);
Assert.Equal("SPY", violation.Ticker);
Assert.Equal(0.60m, violation.CurrentValue, precision: 2);
Assert.Equal(0.30m, violation.Threshold, precision: 2);
```

---

## Test 9 — Scenario Engine Accuracy

### Objective

Validate that the scenario simulation engine correctly computes hypothetical PnL under user-defined market shocks.

### Setup

**Position:**

| Ticker | Qty | Entry Price |
|--------|-----|-------------|
| SPY    | 100 | $500        |

**Scenario parameters:**

| Parameter    | Value |
|--------------|-------|
| Price Change | -10%  |

### Execution

```graphql
query {
  runScenario(input: {
    accountId: "...",
    shocks: [{ ticker: "SPY", priceChangePct: -10 }]
  }) {
    positions { ticker currentPrice shockedPrice pnlImpact }
    totalImpact
  }
}
```

### Expected Result

```
Current price  = $500
Shocked price  = $500 * (1 - 0.10) = $450
PnL impact     = (450 - 500) * 100  = -$5,000
```

| Field         | Expected Value |
|---------------|----------------|
| Shocked Price | $450           |
| PnL Impact    | -$5,000        |
| Total Impact  | -$5,000        |

### Validation

```csharp
var scenario = await scenarioService.RunScenarioAsync(accountId, shocks);

Assert.Equal(450m, scenario.Positions.First().ShockedPrice);
Assert.Equal(-5_000m, scenario.Positions.First().PnlImpact);
Assert.Equal(-5_000m, scenario.TotalImpact);
```

---

## Test 10 — Strategy Attribution Correctness

### Objective

Ensure PnL is correctly attributed to individual strategies and that contributions sum to 100%.

### Setup

**Trades tagged by strategy:**

| Strategy   | Trades                          | Net PnL |
|------------|---------------------------------|---------|
| Strategy A | BUY/SELL AAPL (+$2,000)         | $2,000  |
| Strategy B | BUY/SELL MSFT (+$1,000)         | $1,000  |

**Total PnL:** $3,000

### Execution

```graphql
query {
  getAlphaAttribution(accountId: "...") {
    strategies { name pnl contributionPct }
    totalPnL
  }
}
```

### Expected Result

| Strategy   | PnL    | Contribution |
|------------|--------|--------------|
| Strategy A | $2,000 | 66.67%       |
| Strategy B | $1,000 | 33.33%       |

**Invariants:**

- `Sum(contributionPct) = 100%`
- `Sum(strategyPnL) = totalPnL`

### Validation

```csharp
var attr = await attributionService.GetAlphaAttributionAsync(accountId);

Assert.Equal(3_000m, attr.TotalPnL);
Assert.Equal(2_000m, attr.Strategies.First(s => s.Name == "Strategy A").PnL);
Assert.Equal(1_000m, attr.Strategies.First(s => s.Name == "Strategy B").PnL);

// Contribution percentages
Assert.True(Math.Abs(66.67m - attr.Strategies.First(s => s.Name == "Strategy A").ContributionPct) < 0.01m);
Assert.True(Math.Abs(33.33m - attr.Strategies.First(s => s.Name == "Strategy B").ContributionPct) < 0.01m);

// Invariant: contributions sum to 100%
Assert.True(Math.Abs(100m - attr.Strategies.Sum(s => s.ContributionPct)) < 0.01m);
```

---

## Bonus Tests

### Test 11 — Option Expiration Handling

#### Objective

Verify that expired options are correctly handled: value drops to zero, position closes, and cash/PnL reflect the loss.

#### Setup

| Position | Qty | Type | Strike | Entry Premium | Expiration |
|----------|-----|------|--------|---------------|------------|
| AAPL     | 10  | Call | $160   | $5.00         | Yesterday  |

**Underlying price at expiration:** $155 (out-of-the-money)

#### Expected Result

| Field          | Expected Value          |
|----------------|------------------------|
| MarketValue    | $0                     |
| Status         | Closed (Expired)       |
| RealizedPnL    | -$5,000 (10 * 100 * $5)|
| Remaining Qty  | 0                      |

#### Validation

```csharp
var position = await positionService.GetPositionAsync(accountId, "AAPL-CALL-160");

Assert.Equal(0, position.NetQuantity);
Assert.Equal(PositionStatus.Closed, position.Status);
Assert.Equal(-5_000m, position.RealizedPnL);
```

---

### Test 12 — Stress Test (Performance)

#### Objective

Verify system performance and correctness under high volume.

#### Setup

| Parameter         | Value        |
|-------------------|------------- |
| Total Trades      | 1,000        |
| Unique Positions  | 100          |
| Account           | Paper        |

Generate trades programmatically with randomized but deterministic data (seeded RNG).

#### Execution

```csharp
var stopwatch = Stopwatch.StartNew();

// Insert 1000 trades
for (int i = 0; i < 1000; i++)
    await portfolioService.RecordTradeAsync(GenerateTrade(i, seed: 42));

var insertTime = stopwatch.Elapsed;

// Rebuild all positions from scratch
stopwatch.Restart();
await portfolioService.RebuildPositionsAsync(accountId);
var rebuildTime = stopwatch.Elapsed;
```

#### Expected Result

| Metric                  | Threshold   |
|-------------------------|-------------|
| Rebuild time            | < 2 seconds |
| Position count          | 100         |
| Reconciliation drift    | 0           |
| All invariants hold     | Yes         |

#### Validation

```csharp
Assert.True(rebuildTime.TotalSeconds < 2.0, $"Rebuild took {rebuildTime.TotalSeconds}s");

var report = await reconciliationService.ReconcileAsync(accountId);
Assert.Equal(0, report.DriftCount);
Assert.Equal(100, report.PositionCount);
```

---

## Test Automation Structure

### Project Layout

```
Backend.Tests/
  Portfolio/
    ├── FifoAccountingTests.cs        # Test 1
    ├── RebuildDeterminismTests.cs     # Test 2
    ├── CashAccountingTests.cs         # Test 3
    ├── ValuationTests.cs              # Test 4
    ├── GreeksAggregationTests.cs      # Test 5
    ├── SnapshotStabilityTests.cs      # Test 6
    ├── DrawdownTests.cs               # Test 7
    ├── RiskRuleTests.cs               # Test 8
    ├── ScenarioEngineTests.cs         # Test 9
    ├── StrategyAttributionTests.cs    # Test 10
    ├── OptionExpirationTests.cs       # Test 11 (bonus)
    ├── StressTests.cs                 # Test 12 (bonus)
    └── Fixtures/
        ├── PortfolioTestFixture.cs    # Shared setup (TestContainers)
        └── TestDataGenerator.cs       # Deterministic trade generation
```

### Recommended Frameworks

| Tool              | Purpose                                |
|-------------------|----------------------------------------|
| xUnit             | Test runner                            |
| FluentAssertions  | Readable assertion syntax              |
| NSubstitute       | Service mocking                        |
| TestContainers    | Ephemeral PostgreSQL for integration   |
| Bogus             | Deterministic fake data (seeded)       |

### Running the Tests

```bash
# All portfolio tests
dotnet test --filter "Namespace~Portfolio"

# Single test category
dotnet test --filter "FullyQualifiedName~FifoAccountingTests"

# With detailed output
dotnet test --filter "Namespace~Portfolio" --logger "console;verbosity=detailed"
```

---

## Key Validation Invariants

These invariants must hold true **at all times** across the entire system:

| #  | Invariant                                          | Verified In    |
|----|----------------------------------------------------|----------------|
| I1 | `Equity = Cash + MarketValue`                      | Tests 4, 6     |
| I2 | `NetQuantity = Sum(open lot quantities)`           | Tests 1, 2     |
| I3 | `PositionPnL = Sum(lot PnL)`                       | Tests 1, 3     |
| I4 | Snapshots are in strict chronological order         | Test 6         |
| I5 | `RebuildPositions` is deterministic                 | Test 2         |
| I6 | `RealizedPnL` is monotonically non-decreasing       | Test 6         |
| I7 | `Sum(strategy contributions) = 100%`               | Test 10        |
| I8 | `Sum(strategy PnL) = Total PnL`                    | Test 10        |
| I9 | Cash never goes negative (paper trading)           | Test 3         |
| I10| Expired options close with correct terminal value  | Test 11        |

---

## Priority Order

If resources are limited, execute tests in this order:

| Priority | Test | Rationale                                       |
|----------|------|-------------------------------------------------|
| 1        | T2   | Rebuild determinism — foundation of event sourcing |
| 2        | T1   | FIFO correctness — core accounting engine        |
| 3        | T3   | Cash integrity — money must always balance       |
| 4        | T4   | Valuation — drives all downstream analytics      |
| 5        | T6   | Snapshot stability — equity curve reliability    |
| 6        | T7   | Drawdown — key risk metric                       |
| 7        | T8   | Risk rules — protective guardrails               |
| 8        | T9   | Scenario engine — what-if analysis               |
| 9        | T5   | Greeks aggregation — options-specific            |
| 10       | T10  | Attribution — strategy-level analytics           |
