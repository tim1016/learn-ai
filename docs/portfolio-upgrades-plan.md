# Portfolio System Upgrades — Implementation Plan

## Priority Matrix

```
                        HIGH IMPACT
                            │
         ┌──────────────────┼──────────────────┐
         │                  │                  │
         │  P2: Risk        │  P1: Price       │
         │  Decomposition   │  Versioning      │
         │                  │                  │
         │  P5: Benchmark   │  P3: Time-Series │
         │  Tracking        │  Greeks          │
         │                  │                  │
  LOW ───┤──────────────────┼──────────────────├─── HIGH
  EFFORT │                  │                  │  EFFORT
         │  P8: Trade Hash  │  P4: Slippage    │
         │  Integrity       │  Model           │
         │                  │                  │
         │  P9: CQRS Split  │  P6: Statistical │
         │  (architecture)  │  Confidence      │
         │                  │                  │
         │                  │  P7: Regime       │
         │                  │  Segmentation    │
         └──────────────────┼──────────────────┘
                            │
                        LOW IMPACT
```

### Recommended Build Order

| Phase | Items | Rationale |
|-------|-------|-----------|
| **Phase A** | P1 (Price Versioning) + P8 (Trade Hash) | Foundation — makes all subsequent analytics reproducible and auditable |
| **Phase B** | P3 (Time-Series Greeks) + P2 (Risk Decomposition) | Risk upgrade — unlocks real-time risk dashboards and Greeks research |
| **Phase C** | P5 (Benchmark Tracking) + P4 (Slippage Model) | Alpha validation — separates skill from luck, backtests match live |
| **Phase D** | P6 (Statistical Confidence) + P7 (Regime Segmentation) | Research quality — validates significance, segments by market regime |
| **Phase E** | P9 (CQRS Split) | Architecture — performance and scaling refactor after features stabilize |

---

## Phase A: Foundation (Price Versioning + Trade Integrity)

### P1: Historical Price Versioning

**Problem**: `TakeSnapshotAsync` calls `ComputeValuationAsync` which fetches live prices from Polygon. Re-running a snapshot later produces different equity values because prices have changed. This breaks historical reproducibility — the core requirement for quantitative research.

**Current flow**:
```
TakeSnapshot → ComputeValuation → FetchLivePrices (Polygon) → persist snapshot
                                                                 ↑
                                                          prices NOT saved
```

**Target flow**:
```
TakeSnapshot → ComputeValuation → FetchLivePrices → persist snapshot + SnapshotPrices
                                                                         ↑
ReplaySnapshot → load SnapshotPrices → recompute valuation          prices ARE saved
```

#### New Entity: `SnapshotPrice`

File: `Backend/Models/Portfolio/SnapshotPrice.cs`

```csharp
namespace Backend.Models.Portfolio;

public class SnapshotPrice
{
    public Guid Id { get; set; }
    public Guid SnapshotId { get; set; }
    public PortfolioSnapshot Snapshot { get; set; } = null!;
    public int TickerId { get; set; }
    public Ticker Ticker { get; set; } = null!;
    public decimal Price { get; set; }
    public string Source { get; set; } = "polygon";   // "polygon", "manual", "backtest"
}
```

#### Schema Changes

| Table | Change |
|-------|--------|
| `SnapshotPrices` | New table |
| `PortfolioSnapshot` | Add navigation: `List<SnapshotPrice> Prices` |

#### DbContext Configuration

```csharp
public DbSet<SnapshotPrice> SnapshotPrices => Set<SnapshotPrice>();

modelBuilder.Entity<SnapshotPrice>(entity =>
{
    entity.Property(e => e.Price).HasPrecision(18, 8);
    entity.Property(e => e.Source).HasMaxLength(50);
    entity.HasOne(e => e.Snapshot)
        .WithMany(s => s.Prices)
        .HasForeignKey(e => e.SnapshotId)
        .OnDelete(DeleteBehavior.Cascade);
    entity.HasIndex(e => new { e.SnapshotId, e.TickerId }).IsUnique();
});
```

#### Service Changes

**SnapshotService.PersistSnapshot** — save prices alongside snapshot:

```csharp
private async Task<PortfolioSnapshot> PersistSnapshot(Guid accountId,
    PortfolioValuation valuation, Dictionary<string, decimal> usedPrices,
    CancellationToken ct)
{
    var snapshot = new PortfolioSnapshot { /* ... existing fields ... */ };
    _context.PortfolioSnapshots.Add(snapshot);

    // Persist the exact prices used for this snapshot
    foreach (var pos in valuation.Positions)
    {
        var ticker = await _context.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == pos.Symbol, ct);
        if (ticker != null && usedPrices.TryGetValue(pos.Symbol, out var price))
        {
            _context.SnapshotPrices.Add(new SnapshotPrice
            {
                Id = Guid.NewGuid(),
                SnapshotId = snapshot.Id,
                TickerId = ticker.Id,
                Price = price,
                Source = "polygon",
            });
        }
    }

    await _context.SaveChangesAsync(ct);
    return snapshot;
}
```

**New method: ReplaySnapshotAsync** — recompute valuation from stored prices:

```csharp
// ISnapshotService
Task<PortfolioValuation> ReplaySnapshotAsync(Guid snapshotId, CancellationToken ct = default);
```

#### GraphQL Changes

| Type | Name | Signature |
|------|------|-----------|
| Query | `getSnapshotPrices` | `(snapshotId: UUID!) → [SnapshotPrice]` |
| Query | `replaySnapshot` | `(snapshotId: UUID!) → PortfolioValuation` |

#### Frontend Changes

- `EquityChartComponent`: Add tooltip showing price source on hover
- `PortfolioService`: Add `getSnapshotPrices(snapshotId)` method

#### Tests

| Test | Validates |
|------|-----------|
| `TakeSnapshot_PersistsPricesUsed` | SnapshotPrices rows match position count |
| `ReplaySnapshot_UsesStoredPrices` | Recomputed equity matches original |
| `ReplaySnapshot_IgnoresCurrentLivePrices` | Different live prices, same result |
| `SnapshotPrice_UniqueConstraint` | Duplicate (snapshotId, tickerId) throws |

#### Estimated Scope

- 1 new model file
- 3 files modified (PortfolioSnapshot, SnapshotService, AppDbContext)
- 1 new GraphQL query
- ~4 tests
- DB volume reset required (new table)

---

### P8: Trade Log Integrity (Hash Chain)

**Problem**: Since trades are the source of truth (event sourcing), silent mutation of trade records would corrupt all derived state (positions, PnL, equity curves). There is no tamper-detection mechanism.

**Solution**: Add a SHA-256 hash to each trade, computed from its immutable fields. Optionally chain hashes to form a linked integrity log (each trade hashes the previous hash too).

#### Schema Changes

| Table | Change |
|-------|--------|
| `PortfolioTrade` | Add `TradeHash: string` (max 64, indexed) |
| `PortfolioTrade` | Add `PreviousHash: string?` (max 64) |

#### Hash Formula

```csharp
TradeHash = SHA256(
    AccountId + TickerId + Side + Quantity + Price + Fees
    + Multiplier + ExecutionTimestamp.Ticks + PreviousHash
)
```

#### Service Changes

**PortfolioService.RecordTradeAsync / FillOrderAsync** — compute hash before saving:

```csharp
private static string ComputeTradeHash(PortfolioTrade trade, string? previousHash)
{
    var payload = $"{trade.AccountId}{trade.TickerId}{trade.Side}{trade.Quantity}" +
                  $"{trade.Price}{trade.Fees}{trade.Multiplier}" +
                  $"{trade.ExecutionTimestamp.Ticks}{previousHash ?? "genesis"}";
    var bytes = SHA256.HashData(Encoding.UTF8.GetBytes(payload));
    return Convert.ToHexStringLower(bytes);
}
```

**New method: VerifyTradeIntegrityAsync** — validate the hash chain:

```csharp
// IPortfolioService
Task<IntegrityReport> VerifyTradeIntegrityAsync(Guid accountId, CancellationToken ct = default);

public class IntegrityReport
{
    public bool Valid { get; set; }
    public int TradeCount { get; set; }
    public int InvalidCount { get; set; }
    public List<Guid> InvalidTradeIds { get; set; } = [];
}
```

#### GraphQL Changes

| Type | Name | Signature |
|------|------|-----------|
| Query | `verifyTradeIntegrity` | `(accountId: UUID!) → IntegrityReport` |

#### Frontend Changes

- `ReconciliationComponent`: Add "Verify Integrity" button and report display

#### Tests

| Test | Validates |
|------|-----------|
| `RecordTrade_ComputesHash` | Hash is non-null and 64 chars |
| `RecordTrade_ChainsHash` | Second trade references first trade's hash |
| `VerifyIntegrity_ValidChain` | 10 trades all pass |
| `VerifyIntegrity_TamperedTrade` | Manually changed price → detected |

#### Estimated Scope

- 2 columns added to PortfolioTrade
- PortfolioService modified (hash on record/fill)
- 1 new query + result type
- ~4 tests
- DB volume reset required

---

## Phase B: Risk Upgrade (Time-Series Greeks + Risk Decomposition)

### P3: Time-Series Greeks

**Problem**: `OptionLeg.EntryDelta` etc. store Greeks only at trade time. As the underlying moves, time passes, and IV changes, the actual Greeks diverge significantly from entry values. Risk calculations become increasingly inaccurate.

**Current**: `delta = 0.40` at entry → risk system uses `0.40` on day 20 when actual delta is `0.85`.

#### New Entity: `GreeksSnapshot`

File: `Backend/Models/Portfolio/GreeksSnapshot.cs`

```csharp
namespace Backend.Models.Portfolio;

public class GreeksSnapshot
{
    public Guid Id { get; set; }
    public Guid OptionContractId { get; set; }
    public OptionContract OptionContract { get; set; } = null!;
    public Guid AccountId { get; set; }
    public Account Account { get; set; } = null!;
    public DateTime Timestamp { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
    public decimal IV { get; set; }
    public decimal UnderlyingPrice { get; set; }
    public string Source { get; set; } = "polygon";  // "polygon", "computed", "manual"
}
```

Index: `(OptionContractId, Timestamp DESC)`

#### New Service: `IGreeksSnapshotService`

```csharp
public interface IGreeksSnapshotService
{
    // Capture current Greeks for all option positions (call Polygon or BS solver)
    Task<List<GreeksSnapshot>> CaptureGreeksAsync(Guid accountId, CancellationToken ct = default);

    // Get latest Greeks for a specific contract
    Task<GreeksSnapshot?> GetLatestGreeksAsync(Guid optionContractId, CancellationToken ct = default);

    // Get Greeks time series for research
    Task<List<GreeksSnapshot>> GetGreeksHistoryAsync(
        Guid optionContractId, DateTime? from = null, DateTime? to = null,
        CancellationToken ct = default);
}
```

#### Integration with Risk Service

**PortfolioRiskService.ComputeDollarDeltaAsync** — use latest Greeks snapshot instead of entry Greeks:

```csharp
// Before (current):
var latestLeg = await _context.OptionLegs ... ;
delta = latestLeg?.EntryDelta ?? 0;

// After:
var latestGreeks = await _greeksSnapshotService
    .GetLatestGreeksAsync(position.OptionContractId!.Value, ct);
delta = latestGreeks?.Delta ?? latestLeg?.EntryDelta ?? 0;  // fallback to entry
```

Same pattern for vega in `ComputePortfolioVegaAsync` and scenario analysis.

#### Greeks Computation Strategy

Two sources, configurable per account:

| Source | Method | Accuracy | Cost |
|--------|--------|----------|------|
| **Polygon** | `GET /v3/snapshot/options/{underlyingAsset}/{optionContract}` | High (market-implied) | 1 API call per contract |
| **BS Solver** | Existing `bs_solver.py` in Python service | Good (model-derived) | No API cost, needs underlying price + IV |

The Python data service already has a Black-Scholes solver. Add a new endpoint:

```python
# PythonDataService/app/routes/options.py
@router.post("/compute-greeks")
async def compute_greeks(request: GreeksRequest) -> GreeksResponse:
    """Compute Greeks for a list of option contracts given current underlying prices."""
```

The .NET backend calls this endpoint when Polygon data is unavailable or for backtesting.

#### GraphQL Changes

| Type | Name | Signature |
|------|------|-----------|
| Mutation | `captureGreeks` | `(accountId: UUID!) → CaptureResult` |
| Query | `getGreeksHistory` | `(optionContractId: UUID!, from?: DateTime, to?: DateTime) → [GreeksSnapshot]` |
| Query | `getLatestGreeks` | `(optionContractId: UUID!) → GreeksSnapshot?` |

#### Frontend Changes

- `RiskPanelComponent`: Show "last updated" timestamp for Greeks
- `RiskPanelComponent`: Add "Refresh Greeks" button → calls `captureGreeks`
- New `GreeksHistoryComponent` (or tab within positions): sparkline of delta/vega over time

#### Tests

| Test | Validates |
|------|-----------|
| `CaptureGreeks_PersistsForAllOptionPositions` | One row per option position |
| `GetLatestGreeks_ReturnsNewest` | Multiple snapshots → latest returned |
| `RiskService_UsesLatestGreeks_WhenAvailable` | Dollar delta uses snapshot, not entry |
| `RiskService_FallsBackToEntryGreeks` | No snapshot → uses OptionLeg entry values |
| `GreeksHistory_FiltersByDateRange` | From/to filtering works |

#### Estimated Scope

- 1 new model, 1 new service interface + implementation
- 3 existing services modified (risk, valuation, scenario)
- 1 new Python endpoint
- 3 GraphQL additions
- ~5 tests
- DB volume reset required

---

### P2: Position-Level Risk Decomposition

**Problem**: The system computes portfolio-level Greeks (NetDelta, NetVega) but cannot answer "which position contributes the most risk?" Institutional risk dashboards show risk attribution per position.

#### New DTO: `PositionRiskContribution`

```csharp
public class PositionRiskContribution
{
    public Guid PositionId { get; set; }
    public string Symbol { get; set; } = "";
    public AssetType AssetType { get; set; }

    // Absolute contributions
    public decimal DollarDelta { get; set; }
    public decimal Vega { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal MarketValue { get; set; }

    // Percentage of total (0–1)
    public decimal DeltaContributionPct { get; set; }
    public decimal VegaContributionPct { get; set; }
    public decimal GammaContributionPct { get; set; }
    public decimal ThetaContributionPct { get; set; }
    public decimal ValueContributionPct { get; set; }
}
```

#### Service Changes

Add to `IPortfolioRiskService`:

```csharp
Task<List<PositionRiskContribution>> ComputeRiskDecompositionAsync(
    Guid accountId, Dictionary<string, decimal> prices, CancellationToken ct = default);
```

**Algorithm**:

```
For each open position p:
    compute DollarDelta_p, Vega_p, Gamma_p, Theta_p, MarketValue_p

totalAbsDelta = sum(|DollarDelta_p|)
totalAbsVega  = sum(|Vega_p|)
... etc

DeltaContributionPct_p = |DollarDelta_p| / totalAbsDelta
VegaContributionPct_p  = |Vega_p| / totalAbsVega
... etc
```

#### GraphQL Changes

| Type | Name | Signature |
|------|------|-----------|
| Query | `getRiskDecomposition` | `(accountId: UUID!, prices: [PriceInput!]!) → [PositionRiskContribution]` |

#### Frontend Changes

- `RiskPanelComponent`: Add "Risk Heatmap" section
- Horizontal stacked bar or table showing each position's risk contribution %
- Color-coded by contribution magnitude (green < 15%, yellow 15-30%, red > 30%)

#### Tests

| Test | Validates |
|------|-----------|
| `RiskDecomposition_SinglePosition_100Percent` | One position = 100% of all risk |
| `RiskDecomposition_TwoStocks_ProportionalDelta` | Delta split matches market value ratio |
| `RiskDecomposition_MixedStockOption` | Options contribute vega, stocks don't |
| `RiskDecomposition_SumsTo100` | All contribution percentages sum to 1.0 |

#### Estimated Scope

- 1 new DTO (no new table — computed on the fly)
- PortfolioRiskService modified
- 1 new GraphQL query
- Frontend risk panel updated
- ~4 tests

---

## Phase C: Alpha Validation (Benchmark + Slippage)

### P5: Benchmark Tracking

**Problem**: The system mentions "Portfolio Alpha" but has no benchmark infrastructure. You cannot compute alpha, beta, information ratio, or tracking error without a reference index.

#### New Entities

**Benchmark** — `Backend/Models/Portfolio/Benchmark.cs`:

```csharp
public class Benchmark
{
    public Guid Id { get; set; }
    public Guid AccountId { get; set; }
    public Account Account { get; set; } = null!;
    public string Symbol { get; set; } = "SPY";    // Benchmark ticker symbol
    public string Name { get; set; } = "S&P 500";
    public bool Active { get; set; } = true;
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}
```

**BenchmarkSnapshot** — `Backend/Models/Portfolio/BenchmarkSnapshot.cs`:

```csharp
public class BenchmarkSnapshot
{
    public Guid Id { get; set; }
    public Guid BenchmarkId { get; set; }
    public Benchmark Benchmark { get; set; } = null!;
    public DateTime Timestamp { get; set; }
    public decimal Price { get; set; }
    public decimal CumulativeReturn { get; set; }  // from first snapshot
}
```

Index: `(BenchmarkId, Timestamp)`

#### New Service: `IBenchmarkService`

```csharp
public interface IBenchmarkService
{
    Task<Benchmark> SetBenchmarkAsync(Guid accountId, string symbol, string? name = null,
        CancellationToken ct = default);
    Task<BenchmarkSnapshot> CaptureBenchmarkPriceAsync(Guid benchmarkId,
        CancellationToken ct = default);
    Task<BenchmarkComparison> CompareAsync(Guid accountId, CancellationToken ct = default);
}

public class BenchmarkComparison
{
    public decimal PortfolioReturn { get; set; }
    public decimal BenchmarkReturn { get; set; }
    public decimal Alpha { get; set; }                // Portfolio - Benchmark
    public decimal Beta { get; set; }                  // Cov(Rp,Rb) / Var(Rb)
    public decimal InformationRatio { get; set; }      // Alpha / TrackingError
    public decimal TrackingError { get; set; }         // StdDev(Rp - Rb)
    public int OverlapSnapshots { get; set; }
}
```

#### Formulas

Given aligned return series `Rp[i]` (portfolio) and `Rb[i]` (benchmark):

```
Alpha = mean(Rp) - mean(Rb)                         (annualized: * 252)

Beta = Cov(Rp, Rb) / Var(Rb)

TrackingError = StdDev(Rp - Rb)                      (annualized: * sqrt(252))

InformationRatio = Alpha_annualized / TrackingError_annualized
```

> **Assumption**: Benchmark snapshots must be captured at the same frequency as portfolio snapshots. The comparison aligns by nearest timestamp (within 1-hour tolerance).

#### Integration with Snapshot Flow

When `TakeSnapshotAsync` runs, also capture benchmark price:

```csharp
// In SnapshotService.TakeSnapshotAsync:
var benchmark = await _context.Benchmarks
    .FirstOrDefaultAsync(b => b.AccountId == accountId && b.Active, ct);
if (benchmark != null)
    await _benchmarkService.CaptureBenchmarkPriceAsync(benchmark.Id, ct);
```

#### GraphQL Changes

| Type | Name | Signature |
|------|------|-----------|
| Mutation | `setBenchmark` | `(accountId: UUID!, symbol: String!, name?: String) → Benchmark` |
| Query | `getBenchmarkComparison` | `(accountId: UUID!) → BenchmarkComparison` |
| Query | `getBenchmarkSnapshots` | `(benchmarkId: UUID!) → [BenchmarkSnapshot]` |

#### Frontend Changes

- `EquityChartComponent`: Overlay benchmark return line on equity chart
- `DashboardComponent`: Add Alpha, Beta, IR, Tracking Error to metrics cards
- Account settings: Benchmark selector (default SPY)

#### Tests

| Test | Validates |
|------|-----------|
| `SetBenchmark_CreatesBenchmarkRow` | Benchmark persisted |
| `CaptureBenchmarkPrice_PersistsSnapshot` | Price saved with timestamp |
| `Compare_ComputesAlpha` | Alpha = portfolio return - benchmark return |
| `Compare_ComputesBeta` | Beta of 100% SPY portfolio ≈ 1.0 |
| `Compare_InsufficientData_ReturnsZeros` | < 2 snapshots → all metrics 0 |

#### Estimated Scope

- 2 new models, 1 new service interface + implementation
- SnapshotService modified (auto-capture benchmark)
- 3 GraphQL additions
- EquityChart + Dashboard components updated
- ~5 tests
- DB volume reset required

---

### P4: Slippage and Transaction Cost Model

**Problem**: All trades assume `fill_price = provided_price`. This is acceptable for manual paper trading but invalid for systematic backtesting. Backtest PnL will overstate actual performance.

#### New Entity: `ExecutionModel`

File: `Backend/Models/Portfolio/ExecutionModel.cs`

```csharp
public class ExecutionModel
{
    public Guid Id { get; set; }
    public Guid AccountId { get; set; }
    public Account Account { get; set; } = null!;
    public string Name { get; set; } = "Default";

    // Slippage
    public decimal SlippageBps { get; set; } = 0;        // basis points (1 bps = 0.01%)
    public decimal SpreadBps { get; set; } = 0;           // half-spread estimate

    // Fees
    public decimal FeePerShare { get; set; } = 0;
    public decimal FeePerContract { get; set; } = 0.65m;  // options
    public decimal MinFee { get; set; } = 0;

    public bool Active { get; set; } = true;
}
```

#### Execution Price Formula

```
effectiveSlippage = (slippageBps + spreadBps) / 10_000

Buy:  adjustedPrice = requestedPrice * (1 + effectiveSlippage)
Sell: adjustedPrice = requestedPrice * (1 - effectiveSlippage)

fees = max(minFee, quantity * feePerShare)                    // stocks
fees = max(minFee, quantity * feePerContract)                 // options
```

#### Service Changes

**PortfolioService.RecordTradeAsync / FillOrderAsync** — apply execution model:

```csharp
var model = await _context.ExecutionModels
    .FirstOrDefaultAsync(m => m.AccountId == input.AccountId && m.Active, ct);

if (model != null)
{
    var slippage = (model.SlippageBps + model.SpreadBps) / 10_000m;
    var adjustedPrice = input.Side == OrderSide.Buy
        ? input.Price * (1 + slippage)
        : input.Price * (1 - slippage);

    var modelFees = input.AssetType == AssetType.Option
        ? Math.Max(model.MinFee, input.Quantity * model.FeePerContract)
        : Math.Max(model.MinFee, input.Quantity * model.FeePerShare);

    trade.Price = adjustedPrice;
    trade.Fees = input.Fees + modelFees;  // caller fees + model fees
}
```

> **Important**: Original requested price should be stored for audit. Add `RequestedPrice` field to `PortfolioTrade`.

#### GraphQL Changes

| Type | Name | Signature |
|------|------|-----------|
| Mutation | `setExecutionModel` | `(accountId, slippageBps?, spreadBps?, feePerShare?, feePerContract?, minFee?) → ExecutionModel` |
| Query | `getExecutionModel` | `(accountId: UUID!) → ExecutionModel?` |

#### Frontend Changes

- Account settings panel: Execution model configuration form
- Trade table: Show both requested and fill prices when they differ

#### Tests

| Test | Validates |
|------|-----------|
| `RecordTrade_WithSlippage_AdjustsPrice` | Buy price increases, sell price decreases |
| `RecordTrade_WithFees_AddsModelFees` | Total fees = caller fees + model fees |
| `RecordTrade_NoModel_UsesRawPrice` | No active model → no adjustment |
| `SlippageBps_Calculation` | 5 bps on $100 = $0.05 slippage |

#### Estimated Scope

- 1 new model, 1 column added to PortfolioTrade
- PortfolioService modified
- 2 GraphQL additions
- Frontend account settings updated
- ~4 tests
- DB volume reset required

---

## Phase D: Research Quality (Statistical Confidence + Regime Segmentation)

### P6: Statistical Confidence Layer

**Problem**: Current metrics (Sharpe, Sortino, etc.) are point estimates with no confidence intervals. A Sharpe of 1.2 from 30 data points is meaningless without a t-test.

#### New DTO: `StatisticalMetrics`

```csharp
public class StatisticalMetrics
{
    // Existing metrics (extended)
    public decimal SharpeRatio { get; set; }
    public decimal SortinoRatio { get; set; }

    // Statistical significance
    public decimal ReturnTStat { get; set; }           // t-stat of mean return
    public decimal ReturnPValue { get; set; }          // p-value (two-tailed)
    public decimal SharpeStdError { get; set; }        // SE(Sharpe) ≈ sqrt((1 + 0.5*Sharpe²) / N)
    public decimal SharpeConfLow95 { get; set; }       // Sharpe - 1.96 * SE
    public decimal SharpeConfHigh95 { get; set; }      // Sharpe + 1.96 * SE

    // Rolling metrics
    public List<RollingMetric> RollingSharpe { get; set; } = [];

    public int SampleSize { get; set; }
    public bool IsSignificant { get; set; }            // p < 0.05
}

public class RollingMetric
{
    public DateTime Timestamp { get; set; }
    public decimal Value { get; set; }
    public int WindowSize { get; set; }
}
```

#### Formulas

```
t-stat = mean(R) / (stddev(R) / sqrt(N))

p-value = 2 * (1 - CDF_t(|t-stat|, df=N-1))     // two-tailed

SE(Sharpe) ≈ sqrt((1 + 0.5 * Sharpe²) / (N - 1))    // Lo (2002) approximation

Rolling Sharpe (window W):
    For each i from W to N:
        sharpe_i = mean(R[i-W..i]) / stddev(R[i-W..i]) * sqrt(252)
```

> **Reference**: Andrew W. Lo, "The Statistics of Sharpe Ratios," *Financial Analysts Journal*, 2002.

**Implementation note**: The t-distribution CDF is not available in .NET standard math. Options:
1. Use the Python service (`scipy.stats.t.cdf`) via a new endpoint
2. Use `MathNet.Numerics` NuGet package (provides `StudentT.CDF`)
3. Approximate with normal distribution for large N (N > 30)

**Recommendation**: Use the Python service for exact computation, with normal approximation as fallback.

#### Service Changes

Add to `ISnapshotService`:

```csharp
Task<StatisticalMetrics> ComputeStatisticalMetricsAsync(
    Guid accountId, int rollingWindow = 60, CancellationToken ct = default);
```

#### GraphQL Changes

| Type | Name | Signature |
|------|------|-----------|
| Query | `getStatisticalMetrics` | `(accountId: UUID!, rollingWindow?: Int) → StatisticalMetrics` |

#### Frontend Changes

- `EquityChartComponent`: Add rolling Sharpe line chart
- `DashboardComponent`: Show t-stat, p-value, confidence interval alongside Sharpe
- Color-code significance: green (p < 0.05), yellow (p < 0.10), red (p >= 0.10)

#### Estimated Scope

- 2 new DTOs (no new table — computed from snapshots)
- SnapshotService extended
- 1 new Python endpoint (t-distribution CDF)
- 1 GraphQL query
- Frontend equity chart + dashboard updated
- ~4 tests

---

### P7: Regime Segmentation

**Problem**: A Sharpe of 1.5 across both bull and bear markets is far more meaningful than 1.5 in a bull market only. Without regime tagging, performance metrics are blind to market context.

#### New Entity: `MarketRegime`

File: `Backend/Models/Portfolio/MarketRegime.cs`

```csharp
public class MarketRegime
{
    public Guid Id { get; set; }
    public DateTime Timestamp { get; set; }
    public RegimeType RegimeType { get; set; }
    public decimal VixLevel { get; set; }
    public decimal SpyReturn30d { get; set; }    // 30-day trailing SPY return
    public string Source { get; set; } = "computed";
}

public enum RegimeType
{
    LowVolBull,     // VIX < 20 and SPY 30d return > 0
    LowVolBear,     // VIX < 20 and SPY 30d return < 0
    HighVolBull,    // VIX >= 20 and SPY 30d return > 0
    HighVolBear,    // VIX >= 20 and SPY 30d return < 0
    Crisis,         // VIX > 30
}
```

#### Regime Classification Rules

```
if VIX > 30:
    regime = Crisis
elif VIX >= 20:
    regime = HighVolBull if SPY_30d > 0 else HighVolBear
else:
    regime = LowVolBull if SPY_30d > 0 else LowVolBear
```

> **Data source**: VIX from Polygon (`I:VIX`), SPY return computed from cached aggregates.

#### New Service: `IRegimeService`

```csharp
public interface IRegimeService
{
    Task<MarketRegime> ClassifyCurrentRegimeAsync(CancellationToken ct = default);
    Task<List<MarketRegime>> GetRegimeHistoryAsync(
        DateTime? from = null, DateTime? to = null, CancellationToken ct = default);
    Task<RegimePerformance> ComputeRegimePerformanceAsync(
        Guid accountId, CancellationToken ct = default);
}

public class RegimePerformance
{
    public List<RegimeMetrics> Regimes { get; set; } = [];
}

public class RegimeMetrics
{
    public RegimeType RegimeType { get; set; }
    public decimal SharpeRatio { get; set; }
    public decimal MeanReturn { get; set; }
    public decimal MaxDrawdownPercent { get; set; }
    public decimal WinRate { get; set; }
    public int SnapshotCount { get; set; }
}
```

#### Integration

Tag each `PortfolioSnapshot` with the current regime at capture time:

| Table | Change |
|-------|--------|
| `PortfolioSnapshot` | Add `RegimeType: RegimeType?` |

Then `ComputeRegimePerformanceAsync` groups snapshots by regime and computes metrics per group using the existing `ComputeMetrics` logic.

#### GraphQL Changes

| Type | Name | Signature |
|------|------|-----------|
| Query | `getCurrentRegime` | `→ MarketRegime` |
| Query | `getRegimePerformance` | `(accountId: UUID!) → RegimePerformance` |

#### Frontend Changes

- `DashboardComponent`: Show current regime badge (color-coded)
- `EquityChartComponent`: Shade background by regime on equity chart
- New regime performance table: Sharpe/DD/WinRate per regime

#### Estimated Scope

- 1 new model + enum, 1 new service
- PortfolioSnapshot modified (add RegimeType column)
- SnapshotService modified (tag regime on capture)
- 2 GraphQL queries
- Frontend dashboard + equity chart updated
- ~5 tests
- DB volume reset required

---

## Phase E: Architecture (CQRS Split)

### P9: Command/Query Responsibility Segregation

**Problem**: The system is already "almost CQRS" — trades/orders are commands while snapshots/metrics/risk are queries. Formalizing the split improves performance, caching, and testability.

**This is a refactoring phase, not a feature phase.** Defer until Phases A–D are stable.

#### Current Architecture

```
PortfolioService ──► does everything (CRUD + queries)
RiskService      ──► reads + computes
SnapshotService  ──► reads + computes + writes snapshots
```

#### Target Architecture

```
Command Side (write path):
├── ITradeCommandService      → RecordTrade, FillOrder, CancelOrder
├── IAccountCommandService    → CreateAccount
├── IPositionEngine           → ApplyTrade, Rebuild (already isolated)
└── ISnapshotCommandService   → TakeSnapshot

Query Side (read path):
├── IPortfolioQueryService    → GetPortfolioState, GetPositions
├── IValuationQueryService    → ComputeValuation (read-only, no side effects)
├── IMetricsQueryService      → ComputeMetrics, GetEquityCurve, GetDrawdown
├── IRiskQueryService         → DollarDelta, Vega, RiskDecomposition, Scenarios
└── IBenchmarkQueryService    → Compare, GetBenchmarkSnapshots
```

#### Benefits

| Benefit | Details |
|---------|---------|
| **Caching** | Query-side results are cacheable (metrics don't change between snapshots) |
| **Read scaling** | Query services can use `AsNoTracking()` DbContext, separate connection pool |
| **Testing** | Command services test state mutations; query services test pure computations |
| **Clarity** | Each service has a single responsibility |

#### Implementation Steps

1. Split `IPortfolioService` into `ITradeCommandService` + `IPortfolioQueryService`
2. Split `ISnapshotService` into `ISnapshotCommandService` + `IMetricsQueryService`
3. Update DI registrations in `Program.cs`
4. Update GraphQL resolvers to inject new service interfaces
5. Add `[UseDbContext]` / `AsNoTracking` to all query-side services

> **No schema changes, no new tables, no DB reset.** This is purely a service-layer refactor.

#### Estimated Scope

- ~8 interface files renamed/split
- ~8 implementation files renamed/split
- GraphQL resolvers updated (injection only)
- Program.cs DI updated
- All existing tests updated for new interfaces
- Zero new tests (behavior unchanged)

---

## Summary: Total Scope Across All Phases

| Phase | New Tables | New Services | GraphQL Additions | Frontend Changes | Tests |
|-------|-----------|-------------|-------------------|-----------------|-------|
| **A** (Price + Hash) | 1 | 0 | 3 | 2 components | ~8 |
| **B** (Greeks + Risk) | 1 | 1 | 4 | 2 components | ~9 |
| **C** (Benchmark + Slippage) | 3 | 1 | 5 | 3 components | ~9 |
| **D** (Stats + Regime) | 1 | 1 | 3 | 2 components | ~9 |
| **E** (CQRS) | 0 | 8 (split) | 0 | 0 | 0 (update) |
| **Total** | **6** | **11** | **15** | **9** | **~35** |

### DB Volume Resets

Phases A, B, C, and D each add new tables → require `podman volume rm learn-ai_pgdata`. Plan resets at phase boundaries, not per-item.

### Dependencies Between Items

```
P1 (Price Versioning) ────► P5 (Benchmark) uses same snapshot price infrastructure
P3 (Time-Series Greeks) ──► P2 (Risk Decomposition) uses latest Greeks from P3
P5 (Benchmark) ───────────► P7 (Regime) uses benchmark data for SPY returns
P8 (Trade Hash) ──────────► standalone, no dependencies
P4 (Slippage) ────────────► standalone, no dependencies
P6 (Statistical) ─────────► standalone (uses existing snapshots)
P9 (CQRS) ────────────────► depends on all features being stable
```

---

## References

| Topic | Source |
|-------|--------|
| Sharpe standard error | Lo, A.W. "The Statistics of Sharpe Ratios." *Financial Analysts Journal*, 58(4), 2002. |
| Information ratio | Grinold, R. & Kahn, R. *Active Portfolio Management*, 2nd ed., 1999. |
| FIFO accounting | SEC Rule 10b-10 (trade confirmation requirements) |
| Regime classification | Ang, A. & Bekaert, G. "International Asset Allocation with Regime Shifts." *RFS*, 2002. |
| Greeks approximation | Hull, J. *Options, Futures, and Other Derivatives*, 11th ed., Ch. 19. |
| Bootstrap Sharpe | Ledoit, O. & Wolf, M. "Robust Performance Hypothesis Testing with the Sharpe Ratio." *JEF*, 2008. |
