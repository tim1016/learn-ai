using System.Diagnostics;
using Backend.Data;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.Services.Implementation;

public class PortfolioValidationService : IPortfolioValidationService
{
    private readonly IServiceProvider _serviceProvider;
    private readonly ILogger<PortfolioValidationService> _logger;

    public PortfolioValidationService(
        IServiceProvider serviceProvider,
        ILogger<PortfolioValidationService> logger)
    {
        _serviceProvider = serviceProvider;
        _logger = logger;
    }

    public async Task<ValidationSuiteResult> RunValidationSuiteAsync(CancellationToken ct = default)
    {
        var suiteStart = Stopwatch.StartNew();
        var suite = new ValidationSuiteResult { StartedAt = DateTime.UtcNow };

        // Each test gets its own scope so EF contexts are isolated
        using var scope = _serviceProvider.CreateScope();
        var sp = scope.ServiceProvider;
        var portfolioService = sp.GetRequiredService<IPortfolioService>();
        var positionEngine = sp.GetRequiredService<IPositionEngine>();
        var valuationService = sp.GetRequiredService<IPortfolioValuationService>();
        var snapshotService = sp.GetRequiredService<ISnapshotService>();
        var riskService = sp.GetRequiredService<IPortfolioRiskService>();
        var reconciliationService = sp.GetRequiredService<IPortfolioReconciliationService>();
        var db = sp.GetRequiredService<AppDbContext>();

        // Create a dedicated test account
        _logger.LogInformation("[Validation] Creating test account");
        var account = await portfolioService.CreateAccountAsync(
            $"__validation__{DateTime.UtcNow:yyyyMMddHHmmss}", AccountType.Paper, 100_000m, ct);
        suite.AccountId = account.Id.ToString();

        var tests = new List<ValidationTestResult>();

        tests.Add(await RunTestAsync("FIFO Accounting Correctness", "Accounting", 1,
            "Verify FIFO lot engine produces correct realized PnL and lot closures",
            () => Test1_FifoAccounting(portfolioService, db, account.Id, ct)));

        tests.Add(await RunTestAsync("Position Rebuild Determinism", "Event Sourcing", 2,
            "Verify positions rebuild identically from trade log",
            () => Test2_RebuildDeterminism(portfolioService, positionEngine, reconciliationService, db, account.Id, ct)));

        tests.Add(await RunTestAsync("Cash Accounting Integrity", "Accounting", 3,
            "Ensure cash flows are always correct including fees",
            () => Test3_CashAccounting(portfolioService, db, account.Id, ct)));

        tests.Add(await RunTestAsync("Unrealized PnL Valuation", "Valuation", 4,
            "Verify mark-to-market math with explicit prices",
            () => Test4_UnrealizedPnL(portfolioService, valuationService, db, account.Id, ct)));

        tests.Add(await RunTestAsync("Snapshot Time-Series Stability", "Snapshots", 6,
            "Ensure snapshot invariants (Equity = Cash + MV) hold at every point",
            () => Test6_SnapshotStability(snapshotService, db, account.Id, ct)));

        tests.Add(await RunTestAsync("Drawdown Calculation Correctness", "Metrics", 7,
            "Verify peak tracking and drawdown computation",
            () => Test7_DrawdownCalculation(snapshotService, db, account.Id, ct)));

        tests.Add(await RunTestAsync("Risk Rule Triggering", "Risk", 8,
            "Confirm risk limits trigger correctly when breached",
            () => Test8_RiskRules(portfolioService, riskService, db, account.Id, ct)));

        tests.Add(await RunTestAsync("Scenario Engine Accuracy", "Risk", 9,
            "Validate scenario simulation PnL under price shocks",
            () => Test9_ScenarioEngine(portfolioService, riskService, db, account.Id, ct)));

        tests.Add(await RunTestAsync("Equity = Cash + MarketValue Invariant", "Invariants", 10,
            "Verify the fundamental portfolio accounting identity",
            () => Test10_EquityInvariant(portfolioService, valuationService, db, account.Id, ct)));

        tests.Add(await RunTestAsync("Stress Test (Performance)", "Performance", 12,
            "Verify rebuild performance under high trade volume",
            () => Test12_StressTest(portfolioService, positionEngine, reconciliationService, db, account.Id, ct)));

        // Cleanup: delete test account and all related data
        await CleanupTestAccount(db, account.Id, ct);

        suiteStart.Stop();
        suite.Tests = tests;
        suite.TotalTests = tests.Count;
        suite.Passed = tests.Count(t => t.Passed);
        suite.Failed = tests.Count(t => !t.Passed);
        suite.CompletedAt = DateTime.UtcNow;
        suite.DurationMs = suiteStart.Elapsed.TotalMilliseconds;

        _logger.LogInformation("[Validation] Suite complete: {Passed}/{Total} passed in {Duration:F0}ms",
            suite.Passed, suite.TotalTests, suite.DurationMs);

        return suite;
    }

    private static async Task<ValidationTestResult> RunTestAsync(
        string name, string category, int testNumber, string objective,
        Func<Task<List<ValidationAssertion>>> testFunc)
    {
        var sw = Stopwatch.StartNew();
        var result = new ValidationTestResult
        {
            TestNumber = testNumber,
            Name = name,
            Category = category,
            Objective = objective,
        };

        try
        {
            result.Assertions = await testFunc();
            result.Passed = result.Assertions.All(a => a.Passed);
        }
        catch (Exception ex)
        {
            result.Passed = false;
            result.Error = ex.Message;
        }

        sw.Stop();
        result.DurationMs = sw.Elapsed.TotalMilliseconds;
        return result;
    }

    // ─── Test 1: FIFO Accounting ─────────────────────────────

    private static async Task<List<ValidationAssertion>> Test1_FifoAccounting(
        IPortfolioService portfolioService, AppDbContext db, Guid accountId, CancellationToken ct)
    {
        // BUY 100 AAPL @ 150, BUY 50 AAPL @ 155, SELL 120 AAPL @ 160
        await portfolioService.RecordTradeAsync(new RecordTradeInput
        {
            AccountId = accountId, Symbol = "AAPL", Side = OrderSide.Buy,
            Quantity = 100, Price = 150, Fees = 0,
        }, ct);
        await portfolioService.RecordTradeAsync(new RecordTradeInput
        {
            AccountId = accountId, Symbol = "AAPL", Side = OrderSide.Buy,
            Quantity = 50, Price = 155, Fees = 0,
        }, ct);
        await portfolioService.RecordTradeAsync(new RecordTradeInput
        {
            AccountId = accountId, Symbol = "AAPL", Side = OrderSide.Sell,
            Quantity = 120, Price = 160, Fees = 0,
        }, ct);

        var position = await db.Positions
            .Include(p => p.Lots)
            .Include(p => p.Ticker)
            .FirstAsync(p => p.AccountId == accountId && p.Ticker!.Symbol == "AAPL", ct);

        // Expected: Lot A closed 100 @ (160-150)*100 = 1000, Lot B closed 20 @ (160-155)*20 = 100
        // RealizedPnL = 1100, remaining = 30, avgCost = 155
        var assertions = new List<ValidationAssertion>
        {
            AssertDecimal("NetQuantity", 30, position.NetQuantity),
            AssertDecimal("AvgCostBasis", 155, position.AvgCostBasis),
            AssertDecimal("RealizedPnL", 1100, position.RealizedPnL),
            AssertString("Status", "Open", position.Status.ToString()),
        };

        // Check lot details
        var lots = position.Lots!.OrderBy(l => l.OpenedAt).ToList();
        if (lots.Count >= 2)
        {
            assertions.Add(AssertDecimal("Lot A RemainingQty", 0, lots[0].RemainingQuantity));
            assertions.Add(AssertDecimal("Lot A RealizedPnL", 1000, lots[0].RealizedPnL));
            assertions.Add(AssertDecimal("Lot B RemainingQty", 30, lots[1].RemainingQuantity));
            assertions.Add(AssertDecimal("Lot B RealizedPnL", 100, lots[1].RealizedPnL));
        }
        else
        {
            assertions.Add(new ValidationAssertion
            {
                Label = "Lot Count", Expected = "2", Actual = lots.Count.ToString(), Passed = false,
            });
        }

        return assertions;
    }

    // ─── Test 2: Rebuild Determinism ─────────────────────────

    private static async Task<List<ValidationAssertion>> Test2_RebuildDeterminism(
        IPortfolioService portfolioService, IPositionEngine positionEngine,
        IPortfolioReconciliationService reconciliationService,
        AppDbContext db, Guid accountId, CancellationToken ct)
    {
        // Record a diverse set of trades across multiple tickers
        var trades = new (string Symbol, OrderSide Side, decimal Qty, decimal Price)[]
        {
            ("MSFT", OrderSide.Buy, 200, 400),
            ("SPY", OrderSide.Buy, 100, 500),
            ("MSFT", OrderSide.Sell, 100, 410),
            ("SPY", OrderSide.Buy, 50, 505),
            ("NVDA", OrderSide.Buy, 30, 900),
            ("MSFT", OrderSide.Sell, 100, 420),
            ("SPY", OrderSide.Sell, 80, 510),
            ("NVDA", OrderSide.Sell, 15, 920),
            ("SPY", OrderSide.Sell, 70, 515),
        };

        foreach (var (symbol, side, qty, price) in trades)
        {
            await portfolioService.RecordTradeAsync(new RecordTradeInput
            {
                AccountId = accountId, Symbol = symbol, Side = side,
                Quantity = qty, Price = price, Fees = 0,
            }, ct);
        }

        // Capture pre-rebuild state
        var prePositions = await db.Positions
            .AsNoTracking()
            .Include(p => p.Ticker)
            .Where(p => p.AccountId == accountId)
            .OrderBy(p => p.Ticker!.Symbol)
            .ToListAsync(ct);

        // Rebuild
        await positionEngine.RebuildPositionsAsync(accountId, ct);

        // Re-fetch rebuilt state (clear tracker first)
        db.ChangeTracker.Clear();
        var postPositions = await db.Positions
            .AsNoTracking()
            .Include(p => p.Ticker)
            .Where(p => p.AccountId == accountId)
            .OrderBy(p => p.Ticker!.Symbol)
            .ToListAsync(ct);

        var assertions = new List<ValidationAssertion>
        {
            AssertDecimal("Position Count", prePositions.Count, postPositions.Count),
        };

        foreach (var pre in prePositions)
        {
            var post = postPositions.FirstOrDefault(p => p.TickerId == pre.TickerId);
            var symbol = pre.Ticker?.Symbol ?? pre.TickerId.ToString();
            if (post == null)
            {
                assertions.Add(new ValidationAssertion
                {
                    Label = $"{symbol} exists post-rebuild", Expected = "true", Actual = "false", Passed = false,
                });
                continue;
            }
            assertions.Add(AssertDecimal($"{symbol} NetQuantity", pre.NetQuantity, post.NetQuantity));
            assertions.Add(AssertDecimal($"{symbol} AvgCostBasis", pre.AvgCostBasis, post.AvgCostBasis));
            assertions.Add(AssertDecimal($"{symbol} RealizedPnL", pre.RealizedPnL, post.RealizedPnL));
            assertions.Add(AssertString($"{symbol} Status", pre.Status.ToString(), post.Status.ToString()));
        }

        // Also run reconciliation to confirm zero drift
        var report = await reconciliationService.ReconcileAsync(accountId, ct);
        assertions.Add(new ValidationAssertion
        {
            Label = "Reconciliation HasDrift",
            Expected = "false",
            Actual = report.HasDrift.ToString().ToLower(),
            Passed = !report.HasDrift,
        });

        return assertions;
    }

    // ─── Test 3: Cash Accounting ─────────────────────────────

    private static async Task<List<ValidationAssertion>> Test3_CashAccounting(
        IPortfolioService portfolioService, AppDbContext db, Guid accountId, CancellationToken ct)
    {
        // Get current cash (account may have trades from prior tests)
        db.ChangeTracker.Clear();
        var accountBefore = await db.Accounts.AsNoTracking().FirstAsync(a => a.Id == accountId, ct);
        var cashBefore = accountBefore.Cash;

        // BUY 100 TEST3 @ 200, fee = 5
        await portfolioService.RecordTradeAsync(new RecordTradeInput
        {
            AccountId = accountId, Symbol = "TEST3", Side = OrderSide.Buy,
            Quantity = 100, Price = 200, Fees = 5,
        }, ct);

        db.ChangeTracker.Clear();
        var afterBuy = await db.Accounts.AsNoTracking().FirstAsync(a => a.Id == accountId, ct);
        var expectedAfterBuy = cashBefore - (100 * 200) - 5;

        // SELL 100 TEST3 @ 210, fee = 5
        await portfolioService.RecordTradeAsync(new RecordTradeInput
        {
            AccountId = accountId, Symbol = "TEST3", Side = OrderSide.Sell,
            Quantity = 100, Price = 210, Fees = 5,
        }, ct);

        db.ChangeTracker.Clear();
        var afterSell = await db.Accounts.AsNoTracking().FirstAsync(a => a.Id == accountId, ct);
        var expectedAfterSell = expectedAfterBuy + (100 * 210) - 5;

        return
        [
            AssertDecimal("Cash after BUY", expectedAfterBuy, afterBuy.Cash),
            AssertDecimal("Cash after SELL", expectedAfterSell, afterSell.Cash),
        ];
    }

    // ─── Test 4: Unrealized PnL ──────────────────────────────

    private static async Task<List<ValidationAssertion>> Test4_UnrealizedPnL(
        IPortfolioService portfolioService, IPortfolioValuationService valuationService,
        AppDbContext db, Guid accountId, CancellationToken ct)
    {
        await portfolioService.RecordTradeAsync(new RecordTradeInput
        {
            AccountId = accountId, Symbol = "TEST4", Side = OrderSide.Buy,
            Quantity = 100, Price = 900, Fees = 0,
        }, ct);

        var prices = new Dictionary<string, decimal> { ["TEST4"] = 950 };
        var valuation = await valuationService.ComputeValuationWithPricesAsync(accountId, prices, ct);

        // Find the TEST4 position valuation
        var test4Pos = valuation.Positions.FirstOrDefault(p => p.Symbol == "TEST4");
        var assertions = new List<ValidationAssertion>();

        if (test4Pos != null)
        {
            assertions.Add(AssertDecimal("MarketValue", 95_000, test4Pos.MarketValue));
            assertions.Add(AssertDecimal("UnrealizedPnL", 5_000, test4Pos.UnrealizedPnL));
        }
        else
        {
            assertions.Add(new ValidationAssertion
            {
                Label = "TEST4 position found", Expected = "true", Actual = "false", Passed = false,
            });
        }

        // Check equity = cash + market value (across all positions)
        assertions.Add(AssertDecimal("Equity = Cash + MV", valuation.Cash + valuation.MarketValue, valuation.Equity));

        return assertions;
    }

    // ─── Test 6: Snapshot Stability ──────────────────────────

    private static async Task<List<ValidationAssertion>> Test6_SnapshotStability(
        ISnapshotService snapshotService, AppDbContext db, Guid accountId, CancellationToken ct)
    {
        // Take multiple snapshots
        var snap1 = await snapshotService.TakeSnapshotAsync(accountId, ct);
        await Task.Delay(50, ct); // ensure distinct timestamps
        var snap2 = await snapshotService.TakeSnapshotAsync(accountId, ct);

        var assertions = new List<ValidationAssertion>
        {
            // Invariant: Equity = Cash + MarketValue
            AssertDecimal("Snap1: Equity = Cash + MV",
                snap1.Cash + snap1.MarketValue, snap1.Equity),
            AssertDecimal("Snap2: Equity = Cash + MV",
                snap2.Cash + snap2.MarketValue, snap2.Equity),
            // Chronological order
            new()
            {
                Label = "Snapshots chronological",
                Expected = "true",
                Actual = (snap2.Timestamp > snap1.Timestamp).ToString().ToLower(),
                Passed = snap2.Timestamp > snap1.Timestamp,
            },
            // RealizedPnL monotonic
            new()
            {
                Label = "RealizedPnL monotonic",
                Expected = $">= {snap1.RealizedPnL}",
                Actual = snap2.RealizedPnL.ToString("F2"),
                Passed = snap2.RealizedPnL >= snap1.RealizedPnL,
            },
        };

        return assertions;
    }

    // ─── Test 7: Drawdown Calculation ────────────────────────

    private static async Task<List<ValidationAssertion>> Test7_DrawdownCalculation(
        ISnapshotService snapshotService, AppDbContext db, Guid accountId, CancellationToken ct)
    {
        var drawdown = await snapshotService.GetDrawdownSeriesAsync(accountId, ct);

        var assertions = new List<ValidationAssertion>();

        if (drawdown.Count < 2)
        {
            assertions.Add(new ValidationAssertion
            {
                Label = "Drawdown series has data",
                Expected = ">= 2 points",
                Actual = drawdown.Count.ToString(),
                Passed = false,
            });
            return assertions;
        }

        // Verify peak tracking logic: peak should never decrease
        bool peakMonotonic = true;
        for (int i = 1; i < drawdown.Count; i++)
        {
            if (drawdown[i].PeakEquity < drawdown[i - 1].PeakEquity)
            {
                peakMonotonic = false;
                break;
            }
        }
        assertions.Add(new ValidationAssertion
        {
            Label = "Peak equity monotonically non-decreasing",
            Expected = "true",
            Actual = peakMonotonic.ToString().ToLower(),
            Passed = peakMonotonic,
        });

        // Verify drawdown = peak - equity
        foreach (var point in drawdown)
        {
            var expectedDD = point.PeakEquity - point.Equity;
            assertions.Add(AssertDecimal(
                $"Drawdown @ {point.Timestamp:HH:mm:ss}",
                expectedDD, point.Drawdown));
        }

        // Verify drawdownPercent = drawdown / peak * 100
        var lastPoint = drawdown.Last();
        if (lastPoint.PeakEquity > 0)
        {
            var expectedPct = (lastPoint.PeakEquity - lastPoint.Equity) / lastPoint.PeakEquity * 100;
            assertions.Add(AssertDecimal("Last DrawdownPercent", expectedPct, lastPoint.DrawdownPercent));
        }

        return assertions;
    }

    // ─── Test 8: Risk Rules ──────────────────────────────────

    private static async Task<List<ValidationAssertion>> Test8_RiskRules(
        IPortfolioService portfolioService, IPortfolioRiskService riskService,
        AppDbContext db, Guid accountId, CancellationToken ct)
    {
        // Create a rule: MaxPositionSize = 30%
        var rule = new RiskRule
        {
            Id = Guid.NewGuid(),
            AccountId = accountId,
            RuleType = RiskRuleType.MaxPositionSize,
            Threshold = 0.30m,
            Action = RiskAction.Warn,
            Severity = RiskSeverity.Medium,
            Enabled = true,
        };
        db.RiskRules.Add(rule);
        await db.SaveChangesAsync(ct);

        // Buy a large position that exceeds 30% of equity
        await portfolioService.RecordTradeAsync(new RecordTradeInput
        {
            AccountId = accountId, Symbol = "TEST8", Side = OrderSide.Buy,
            Quantity = 500, Price = 200, Fees = 0,
        }, ct);

        // Evaluate rules with a price that makes position large
        var prices = new Dictionary<string, decimal> { ["TEST8"] = 200 };
        // Also need prices for all other open positions
        var openPositions = await db.Positions
            .Include(p => p.Ticker)
            .Where(p => p.AccountId == accountId && p.Status == PositionStatus.Open)
            .ToListAsync(ct);
        foreach (var pos in openPositions)
        {
            if (pos.Ticker?.Symbol != null && !prices.ContainsKey(pos.Ticker.Symbol))
                prices[pos.Ticker.Symbol] = pos.AvgCostBasis; // use cost as proxy
        }

        var violations = await riskService.EvaluateRiskRulesAsync(accountId, prices, ct);

        var maxPosViolation = violations.FirstOrDefault(v => v.RuleType == RiskRuleType.MaxPositionSize);

        return
        [
            new ValidationAssertion
            {
                Label = "MaxPositionSize rule evaluated",
                Expected = "violation triggered",
                Actual = maxPosViolation != null ? "triggered" : "not triggered",
                Passed = maxPosViolation != null,
            },
            new ValidationAssertion
            {
                Label = "Violation threshold",
                Expected = "0.30",
                Actual = maxPosViolation?.Threshold.ToString("F2") ?? "N/A",
                Passed = maxPosViolation?.Threshold == 0.30m,
            },
        ];
    }

    // ─── Test 9: Scenario Engine ─────────────────────────────

    private static async Task<List<ValidationAssertion>> Test9_ScenarioEngine(
        IPortfolioService portfolioService, IPortfolioRiskService riskService,
        AppDbContext db, Guid accountId, CancellationToken ct)
    {
        // Use existing positions — apply a -10% price shock
        var openPositions = await db.Positions
            .Include(p => p.Ticker)
            .Where(p => p.AccountId == accountId && p.Status == PositionStatus.Open)
            .AsNoTracking()
            .ToListAsync(ct);

        if (openPositions.Count == 0)
        {
            return
            [
                new ValidationAssertion
                {
                    Label = "Open positions exist",
                    Expected = "> 0",
                    Actual = "0",
                    Passed = false,
                },
            ];
        }

        var prices = new Dictionary<string, decimal>();
        foreach (var pos in openPositions)
        {
            if (pos.Ticker?.Symbol != null)
                prices[pos.Ticker.Symbol] = pos.AvgCostBasis; // use cost basis as current price
        }

        var scenario = new ScenarioInput { PriceChangePercent = -10m };
        var result = await riskService.RunScenarioAsync(accountId, prices, scenario, ct);

        var assertions = new List<ValidationAssertion>
        {
            new()
            {
                Label = "Scenario equity < current equity (for -10% shock)",
                Expected = "true",
                Actual = (result.ScenarioEquity < result.CurrentEquity).ToString().ToLower(),
                Passed = result.ScenarioEquity < result.CurrentEquity,
            },
            new()
            {
                Label = "PnL impact is negative",
                Expected = "< 0",
                Actual = result.PnLImpact.ToString("F2"),
                Passed = result.PnLImpact < 0,
            },
        };

        // Verify PnL impact = scenario equity - current equity
        assertions.Add(AssertDecimal("PnL = ScenarioEquity - CurrentEquity",
            result.ScenarioEquity - result.CurrentEquity, result.PnLImpact));

        return assertions;
    }

    // ─── Test 10: Equity Invariant ───────────────────────────

    private static async Task<List<ValidationAssertion>> Test10_EquityInvariant(
        IPortfolioService portfolioService, IPortfolioValuationService valuationService,
        AppDbContext db, Guid accountId, CancellationToken ct)
    {
        // Get all open position symbols and compute valuation
        var openPositions = await db.Positions
            .Include(p => p.Ticker)
            .Where(p => p.AccountId == accountId && p.Status == PositionStatus.Open)
            .AsNoTracking()
            .ToListAsync(ct);

        var prices = new Dictionary<string, decimal>();
        foreach (var pos in openPositions)
        {
            if (pos.Ticker?.Symbol != null)
                prices[pos.Ticker.Symbol] = pos.AvgCostBasis;
        }

        var valuation = await valuationService.ComputeValuationWithPricesAsync(accountId, prices, ct);

        var assertions = new List<ValidationAssertion>
        {
            AssertDecimal("Equity = Cash + MarketValue",
                valuation.Cash + valuation.MarketValue, valuation.Equity),
        };

        // NetQuantity = sum of open lot remaining quantities
        foreach (var pos in openPositions)
        {
            var lots = await db.PositionLots
                .Where(l => l.PositionId == pos.Id && l.RemainingQuantity > 0)
                .AsNoTracking()
                .ToListAsync(ct);
            var lotSum = lots.Sum(l => l.RemainingQuantity);
            assertions.Add(AssertDecimal(
                $"{pos.Ticker?.Symbol} NetQty = Sum(lot remaining)",
                lotSum, pos.NetQuantity));
        }

        return assertions;
    }

    // ─── Test 12: Stress Test ────────────────────────────────

    private static async Task<List<ValidationAssertion>> Test12_StressTest(
        IPortfolioService portfolioService, IPositionEngine positionEngine,
        IPortfolioReconciliationService reconciliationService,
        AppDbContext db, Guid accountId, CancellationToken ct)
    {
        // Create a fresh account for stress testing
        var stressAccount = await portfolioService.CreateAccountAsync(
            "__stress_test__", AccountType.Paper, 10_000_000m, ct);
        var stressId = stressAccount.Id;

        var symbols = Enumerable.Range(0, 50).Select(i => $"STR{i:D3}").ToArray();
        var rng = new Random(42); // deterministic seed

        // Insert 200 trades (reduced from 1000 for reasonable test speed)
        var tradeCount = 200;
        for (int i = 0; i < tradeCount; i++)
        {
            var symbol = symbols[rng.Next(symbols.Length)];
            var side = rng.NextDouble() < 0.6 ? OrderSide.Buy : OrderSide.Sell;

            // Check if we have a position to sell
            if (side == OrderSide.Sell)
            {
                var pos = await db.Positions
                    .Include(p => p.Ticker)
                    .FirstOrDefaultAsync(p => p.AccountId == stressId
                        && p.Ticker!.Symbol == symbol
                        && p.Status == PositionStatus.Open, ct);
                if (pos == null || pos.NetQuantity <= 0)
                    side = OrderSide.Buy;
            }

            var qty = rng.Next(10, 100);
            if (side == OrderSide.Sell)
            {
                var pos = await db.Positions
                    .Include(p => p.Ticker)
                    .FirstOrDefaultAsync(p => p.AccountId == stressId
                        && p.Ticker!.Symbol == symbol
                        && p.Status == PositionStatus.Open, ct);
                if (pos != null)
                    qty = Math.Min(qty, (int)pos.NetQuantity);
                if (qty <= 0) continue;
            }

            var price = 50 + rng.Next(0, 200);

            await portfolioService.RecordTradeAsync(new RecordTradeInput
            {
                AccountId = stressId, Symbol = symbol, Side = side,
                Quantity = qty, Price = price, Fees = 0,
            }, ct);
        }

        // Rebuild and time it
        var sw = Stopwatch.StartNew();
        await positionEngine.RebuildPositionsAsync(stressId, ct);
        sw.Stop();

        // Reconcile
        var report = await reconciliationService.ReconcileAsync(stressId, ct);

        var posCount = await db.Positions
            .Where(p => p.AccountId == stressId)
            .CountAsync(ct);

        var assertions = new List<ValidationAssertion>
        {
            new()
            {
                Label = "Rebuild time < 5 seconds",
                Expected = "< 5000ms",
                Actual = $"{sw.Elapsed.TotalMilliseconds:F0}ms",
                Passed = sw.Elapsed.TotalSeconds < 5,
            },
            new()
            {
                Label = "Reconciliation zero drift",
                Expected = "false",
                Actual = report.HasDrift.ToString().ToLower(),
                Passed = !report.HasDrift,
            },
            new()
            {
                Label = "Position count > 0",
                Expected = "> 0",
                Actual = posCount.ToString(),
                Passed = posCount > 0,
            },
        };

        // Cleanup stress account
        await CleanupTestAccount(db, stressId, ct);

        return assertions;
    }

    // ─── Helpers ─────────────────────────────────────────────

    private static ValidationAssertion AssertDecimal(string label, decimal expected, decimal actual, decimal tolerance = 0.01m)
    {
        return new ValidationAssertion
        {
            Label = label,
            Expected = expected.ToString("F2"),
            Actual = actual.ToString("F2"),
            Passed = Math.Abs(expected - actual) < tolerance,
            Tolerance = tolerance,
        };
    }

    private static ValidationAssertion AssertString(string label, string expected, string actual)
    {
        return new ValidationAssertion
        {
            Label = label,
            Expected = expected,
            Actual = actual,
            Passed = string.Equals(expected, actual, StringComparison.OrdinalIgnoreCase),
        };
    }

    private static async Task CleanupTestAccount(AppDbContext db, Guid accountId, CancellationToken ct)
    {
        // Delete in dependency order
        var tradeIds = await db.PortfolioTrades
            .Where(t => t.AccountId == accountId)
            .Select(t => t.Id)
            .ToListAsync(ct);

        if (tradeIds.Count > 0)
        {
            await db.StrategyTradeLinks
                .Where(l => tradeIds.Contains(l.TradeId))
                .ExecuteDeleteAsync(ct);
        }

        await db.PortfolioSnapshots.Where(s => s.AccountId == accountId).ExecuteDeleteAsync(ct);
        await db.RiskRules.Where(r => r.AccountId == accountId).ExecuteDeleteAsync(ct);
        await db.StrategyAllocations.Where(a => a.AccountId == accountId).ExecuteDeleteAsync(ct);

        var positionIds = await db.Positions
            .Where(p => p.AccountId == accountId)
            .Select(p => p.Id)
            .ToListAsync(ct);

        if (positionIds.Count > 0)
            await db.PositionLots.Where(l => positionIds.Contains(l.PositionId)).ExecuteDeleteAsync(ct);

        await db.Positions.Where(p => p.AccountId == accountId).ExecuteDeleteAsync(ct);

        // Delete option legs linked to trades
        var optionLegIds = await db.PortfolioTrades
            .Where(t => t.AccountId == accountId && t.OptionLeg != null)
            .Select(t => t.OptionLeg!.Id)
            .ToListAsync(ct);
        // Option legs are owned — they'll cascade with trades

        await db.PortfolioTrades.Where(t => t.AccountId == accountId).ExecuteDeleteAsync(ct);

        var orderIds = await db.Orders
            .Where(o => o.AccountId == accountId)
            .Select(o => o.Id)
            .ToListAsync(ct);
        if (orderIds.Count > 0)
            await db.Orders.Where(o => o.AccountId == accountId).ExecuteDeleteAsync(ct);

        await db.Accounts.Where(a => a.Id == accountId).ExecuteDeleteAsync(ct);
    }
}
