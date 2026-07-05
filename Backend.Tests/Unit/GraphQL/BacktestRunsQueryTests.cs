using Backend.GraphQL;
using Backend.GraphQL.Types;
using Backend.Models.MarketData;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;

namespace Backend.Tests.Unit.GraphQL;

/// <summary>
/// Tests for BacktestRunsQuery.GetBacktestRuns filtering and wire-contract logic.
/// The [UsePaging] middleware is a HC concern; these tests verify the
/// resolver behaviour against an InMemory DbContext.
/// </summary>
public class BacktestRunsQueryTests
{
    private static async Task<(BacktestRunsQuery query, Backend.Data.AppDbContext db)> BuildAsync()
    {
        var db = TestDbContextFactory.Create();

        var ticker = new Ticker { Symbol = "SPY", Name = "SPDR S&P 500", Market = "stocks" };
        db.Tickers.Add(ticker);
        await db.SaveChangesAsync();

        db.StrategyExecutions.AddRange(
            new StrategyExecution
            {
                TickerId = ticker.Id,
                StrategyName = "EMA Crossover",
                Source = "lean-sidecar",
                LeanRunId = "run-lean-001",
                Parameters = "{\"symbol\":\"SPY\",\"short_window\":10}",
                StartDate = "2025-01-01",
                EndDate = "2025-06-01",
                ExecutedAt = new DateTime(2025, 6, 1, 0, 0, 0, DateTimeKind.Utc),
                Trades =
                [
                    new BacktestTrade
                    {
                        TradeType = "Buy",
                        IsSyntheticExit = true,
                        EntryTimestamp = new DateTime(2025, 1, 2, 0, 0, 0, DateTimeKind.Utc),
                        ExitTimestamp = new DateTime(2025, 1, 10, 0, 0, 0, DateTimeKind.Utc),
                    },
                ],
            },
            new StrategyExecution
            {
                TickerId = ticker.Id,
                StrategyName = "EMA Crossover",
                Source = "engine",
                Parameters = "{\"symbol\":\"SPY\",\"short_window\":10}",
                StartDate = "2025-01-01",
                EndDate = "2025-06-01",
                ExecutedAt = new DateTime(2025, 5, 1, 0, 0, 0, DateTimeKind.Utc),
                Trades =
                [
                    new BacktestTrade
                    {
                        TradeType = "Buy",
                        IsSyntheticExit = false,
                        EntryTimestamp = new DateTime(2025, 1, 2, 0, 0, 0, DateTimeKind.Utc),
                        ExitTimestamp = new DateTime(2025, 1, 10, 0, 0, 0, DateTimeKind.Utc),
                    },
                ],
            },
            new StrategyExecution
            {
                TickerId = ticker.Id,
                StrategyName = "Momentum",
                Source = "strategy-lab",
                Parameters = "{\"symbol\":\"AAPL\",\"period\":20}",
                StartDate = "2025-01-01",
                EndDate = "2025-06-01",
                ExecutedAt = new DateTime(2025, 4, 1, 0, 0, 0, DateTimeKind.Utc),
            }
        );
        await db.SaveChangesAsync();

        return (new BacktestRunsQuery(), db);
    }

    [Fact]
    public async Task GetBacktestRuns_FilterByLean_ReturnsOnlyLeanRows()
    {
        var (query, db) = await BuildAsync();

        var result = query.GetBacktestRuns(db, engine: Engine.LEAN).ToList();

        Assert.Single(result);
        Assert.Equal("lean-sidecar", result[0].Source);
        Assert.Equal("run-lean-001", result[0].LeanRunId);
    }

    [Fact]
    public async Task GetBacktestRuns_FilterByPython_ReturnsEngineAndStrategyLabRows()
    {
        // PR B (2026-05-19): the unified Engine enum collapses the legacy
        // "engine" and "strategy-lab" sources into a single PYTHON engine
        // identity. Both rows must surface under the PYTHON filter so the
        // unified history table doesn't lose pre-PR-B runs.
        var (query, db) = await BuildAsync();

        var result = query.GetBacktestRuns(db, engine: Engine.PYTHON).ToList();

        Assert.Equal(2, result.Count);
        Assert.Contains(result, r => r.Source == "engine");
        Assert.Contains(result, r => r.Source == "strategy-lab");
    }

    [Fact]
    public async Task GetBacktestRuns_NoFilter_ReturnsAllRows()
    {
        var (query, db) = await BuildAsync();

        var result = query.GetBacktestRuns(db).ToList();

        Assert.Equal(3, result.Count);
    }

    [Fact]
    public async Task GetBacktestRuns_ReturnsQueryableForDatabasePaging()
    {
        var (query, db) = await BuildAsync();

        var result = query.GetBacktestRuns(db);

        Assert.IsAssignableFrom<IQueryable<BacktestRunNodeType>>(result);
        Assert.Single(result.Take(1));
    }

    [Fact]
    public async Task GetBacktestRuns_FilterBySymbol_ReturnsMatchingRows()
    {
        var (query, db) = await BuildAsync();

        // "SPY" appears in two rows, "AAPL" in one
        var spyRows = query.GetBacktestRuns(db, symbol: "SPY").ToList();
        var aaplRows = query.GetBacktestRuns(db, symbol: "AAPL").ToList();

        Assert.Equal(2, spyRows.Count);
        Assert.Single(aaplRows);
        Assert.Equal("Momentum", aaplRows[0].StrategyName);
    }

    [Fact]
    public async Task GetBacktestRuns_NewFields_LeanRunIdAndIsSyntheticExitExposed()
    {
        var (query, db) = await BuildAsync();

        var result = query.GetBacktestRuns(db, engine: Engine.LEAN).ToList();

        Assert.Single(result);
        var execution = result[0];
        Assert.Equal("run-lean-001", execution.LeanRunId);
        Assert.NotEmpty(execution.Trades);
        Assert.True(execution.Trades[0].IsSyntheticExit);
    }

    [Fact]
    public async Task GetBacktestRuns_OrderedNewestFirst()
    {
        var (query, db) = await BuildAsync();

        var result = query.GetBacktestRuns(db).ToList();

        // Seed order: lean (2025-06-01), engine (2025-05-01), strategy-lab (2025-04-01)
        Assert.Equal("lean-sidecar", result[0].Source);
        Assert.Equal("engine", result[1].Source);
        Assert.Equal("strategy-lab", result[2].Source);
    }

    [Fact]
    public async Task GetBacktestRuns_FilterBySymbol_NoPrefixFalsePositive()
    {
        // Regression: "SP" must NOT match a run whose symbol is "SPY".
        // Prior implementation used s.Parameters.Contains(symbol) which matched
        // any substring; the fix uses an exact JSON key/value fragment.
        var (query, db) = await BuildAsync();

        var result = query.GetBacktestRuns(db, symbol: "SP").ToList();

        Assert.Empty(result);
    }

    [Fact]
    public async Task GetBacktestRuns_ExecutedAt_ReturnsUnixMilliseconds()
    {
        var (query, db) = await BuildAsync();

        var result = query.GetBacktestRuns(db, engine: Engine.LEAN).ToList();

        var execution = Assert.Single(result);
        Assert.Equal(1_748_736_000_000, execution.ExecutedAt);
    }
}
